"""새 장소·새 인물 시트 시안을 vast(Qwen Image 2.1)로 만든다(크레딧 0). 결과는 assets/_candidates/ 에, 사용자가 보고 고른 뒤 --accept 로 assets 에 넣는다.
(2026-10-16 사용자 "D+18 3화 만들자 하면 알잘딱으로 돌아가게": 새 화에 처음 나오는 장소·인물이 있으면 tools/make_episode.py 가 이걸 불러 시안을 만들고 멈춘다)

    .venv/Scripts/python vast/new_assets.py <series_hf_dir> location <장소 이름> [--accept]
    .venv/Scripts/python vast/new_assets.py <series_hf_dir> sheet <인물 id> [--accept]
프롬프트는 그 작품의 prompts/loc_<이름>.txt · prompts/sheet_<id>.txt(make_location_prompts.py · make_sheet_prompts.py 가 만든다, 없으면 그 파일에 한 줄 넣고 돌린다).
이미 있는 assets 그림은 덮지 않는다.
"""
import argparse
import json
import random
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import graph, upload  # noqa: E402

SHEET_HEAD = ("<image1> is ONLY a reference for the sheet layout and the art style: it shows a DIFFERENT person. Draw a new character design "
              "reference sheet with exactly the same layout (four full-body views on the left, a grid of head-and-shoulders expressions on the right), "
              "the same plain background and the same art style, for the new character described below. Do NOT copy the face, hairstyle, hair color, "
              "body or outfit of <image1>.\n\n")


def paths(series: Path, kind: str, name: str):
    if kind == "location":
        return series / "prompts" / f"loc_{name}.txt", series / "assets" / "_candidates" / "locations" / f"{name}.png", series / "assets" / "locations" / f"{name}.png"
    return series / "prompts" / f"sheet_{name}.txt", series / "assets" / "_candidates" / f"{name}_sheet.png", series / "assets" / f"{name}_sheet.png"


def style_ref(series: Path, name: str) -> Path:
    """시트 모양 본보기: 그 작품 cast.json 순서에서 시트가 있는 첫 인물(주인공)."""
    cast = json.loads((series / "cast.json").read_text(encoding="utf-8"))
    for cid in cast:
        f = series / "assets" / f"{cid}_sheet.png"
        if not cid.startswith("_") and cid != name and f.exists():
            return f
    raise SystemExit("본보기로 쓸 인물 시트가 하나도 없다")


def candidate_prompt(series: Path, kind: str, name: str):
    """시안 프롬프트와 참고 그림(로컬 경로) 목록. vast/make_workflow.py 의 저장 워크플로우(시안)도 이걸 쓴다."""
    prompt_f = paths(series, kind, name)[0]
    if not prompt_f.exists():
        raise SystemExit(f"프롬프트가 없다: {prompt_f}({'make_location_prompts.py 의 LOC' if kind == 'location' else 'make_sheet_prompts.py'} 에 한 줄 넣고 돌린다)")
    prompt = prompt_f.read_text(encoding="utf-8").strip()
    refs = []
    if kind == "sheet":
        refs = [style_ref(series, name)]
        # 2026-10-16 시험: 중년 오 팀장이 젊은 미남으로 나왔다 → 나이를 못 박는다
        prompt = SHEET_HEAD + prompt + "\n\nKeep the character's age exactly as described: do not make them look younger or more glamorous than described."
    else:
        # 그림체 글의 눈·머리·얼굴 문장이 사람을 불러냈다(호프집 시안 앞에 여자, comfy_panel 의 사람 없는 컷과 같은 문제) → 빼고 빈 장소를 못 박는다
        prompt = " ".join(s for s in re.split(r"(?<=\.)\s+", prompt) if not re.search(r"^(Eyes|Hair)\b|face|blush|chest|adult|lash|chibi", s, re.I))
        # 장소 글 자체에 "뒤쪽 손님들"이 있는 곳(호프집)은 "아무도 없음"과 부딪혀 앞 테이블에 사람을 앉혔다 → 앞·가운데만 비우게
        prompt += ("\n\nNobody in the foreground or middle ground: every table, seat and doorway near the camera is empty. "
                   "At most tiny, blurred background figures, and only if the location text asks for them.")
    return prompt, refs


def make(series: Path, kind: str, name: str, seed=None) -> Path:
    cand = paths(series, kind, name)[1]
    prompt, ref_paths = candidate_prompt(series, kind, name)
    c, cache = Comfy(), {}
    refs = [upload(c, r, cache) for r in ref_paths]
    # 오래 쓰는 그림이라 터보 없이 40스텝, 16:9
    g = graph(prompt, refs, 1920, 1088, seed if seed is not None else random.randrange(2**48), 40, 1024, turbo=False)
    g["9"]["inputs"]["filename_prefix"] = f"webtoon_studio/new_{kind}"
    hist = c.wait(c.queue(g), 1200)
    cand.parent.mkdir(parents=True, exist_ok=True)
    c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), cand)
    if kind == "location":  # 장소는 사람 0명이 정답: 글로 막아도 앞 테이블에 사람을 앉혀서(호프집) 2단 지우기를 건다
        from refine import fix_extras
        fix_extras(c, cache, cand, series, {"id": name, "chars": [], "action": "", "outfit": {}}, seed=7, log=print)
    return cand


def accept(series: Path, kind: str, name: str) -> Path:
    _, cand, dst = paths(series, kind, name)
    if not cand.exists():
        raise SystemExit(f"시안이 없다: {cand}")
    if dst.exists():
        raise SystemExit(f"이미 있다(덮지 않음): {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(cand), str(dst))
    return dst


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("kind", choices=["location", "sheet"])
    ap.add_argument("name")
    ap.add_argument("--accept", action="store_true", help="이미 만든 시안을 assets 로 옮긴다")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    series = Path(args.series_dir)
    if args.accept:
        print("넣음", accept(series, args.kind, args.name))
    else:
        print("시안", make(series, args.kind, args.name, args.seed))


if __name__ == "__main__":
    main()
