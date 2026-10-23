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
from comfy_panel import graph, strip_people, upload  # noqa: E402

SHEET_HEAD = ("<image1> is ONLY a reference for the sheet layout and the art style: it shows a DIFFERENT person. Draw a new character design "
              "reference sheet with exactly the same layout (four full-body views on the left, a grid of head-and-shoulders expressions on the right), "
              "the same plain background and the same art style, for the new character described below. Do NOT copy the face, hairstyle, hair color, "
              "body or outfit of <image1>. Every figure and every expression head on the sheet is the new character; the person in <image1> appears "
              "nowhere on it.\n\n")  # 2026-10-18 새 작품 시험: 유나 시트의 표정 칸 하나에 본보기(민호) 얼굴이 그대로 들어갔다


def paths(series: Path, kind: str, name: str):
    if kind == "location":
        return series / "prompts" / f"loc_{name}.txt", series / "assets" / "_candidates" / "locations" / f"{name}.png", series / "assets" / "locations" / f"{name}.png"
    return series / "prompts" / f"sheet_{name}.txt", series / "assets" / "_candidates" / f"{name}_sheet.png", series / "assets" / f"{name}_sheet.png"


def style_ref(series: Path, name: str):
    """시트 모양 본보기: 그 작품 cast.json 순서에서 시트가 있는 첫 인물(주인공). 새 작품의 첫 인물이라 없으면 None(글로만 그린다)."""
    cast = json.loads((series / "cast.json").read_text(encoding="utf-8"))
    for where in ("", "_candidates/"):  # 새 작품은 첫 회차에 시안이 한꺼번에 생긴다: 먼저 그린 시안을 본보기로(그림체를 맞춘다)
        for cid in cast:
            f = series / "assets" / f"{where}{cid}_sheet.png"
            if not cid.startswith("_") and cid != name and f.exists():
                return f
    return None


REVERSE_HEAD = ("<image1> is a location. Draw the SAME room seen from the opposite end: the camera now stands at the far wall of <image1> "
                "and looks back toward the wall that was behind the camera in <image1>. Keep the same furniture, materials, colors and light, "
                "but mirror the sides (what was on the right in <image1> is now on the left). The far wall of <image1> and anything on it "
                "is behind the camera now and does not appear.\n\n")


def candidate_prompt(series: Path, kind: str, name: str):
    """시안 프롬프트와 참고 그림(로컬 경로) 목록. vast/make_workflow.py 의 저장 워크플로우(시안)도 이걸 쓴다."""
    prompt_f = paths(series, kind, name)[0]
    base = name[: -len("_reverse")] if kind == "location" and name.endswith("_reverse") else None
    if base and paths(series, kind, base)[2].exists():
        # <장소>_reverse: 그 장소를 반대편에서 본 그림. 마주 보는 장면의 듣는 쪽 컷(컷 설명 "view": "reverse")이 쓴다(2026-10-19 외출 회의실).
        # 장소 글은 loc_<장소>_reverse.txt 가 있으면 그것(앞 벽의 스크린처럼 반대편에선 안 보이는 것을 뺀 글), 없으면 원래 장소 글
        src = prompt_f if prompt_f.exists() else paths(series, kind, base)[0]
        prompt = REVERSE_HEAD + strip_people(src.read_text(encoding="utf-8").strip())
        return prompt + ("\n\nThe foreground and middle ground are free of people. Only the objects named in the location text are there; "
                         "add no extra furniture."), [paths(series, kind, base)[2]]
    if not prompt_f.exists():
        raise SystemExit(f"프롬프트가 없다: {prompt_f}({'make_location_prompts.py 의 LOC' if kind == 'location' else 'make_sheet_prompts.py'} 에 한 줄 넣고 돌린다)")
    prompt = prompt_f.read_text(encoding="utf-8").strip()
    refs = []
    if kind == "sheet":
        # 새 웹툰 생성기로 연 작품(series.json)은 그림체 글이 정본이라 인물 시트를 모두 글로만 그린다. 다른 인물 시트를 본보기로 넣으면
        # 그 얼굴을 베꼈다(2026-10-18 agent app 시험: 여자 조교 유나가 남학생 민오 얼굴에 머리만 묶은 꼴, 옥상 카페에선 표정 칸 하나가 민호).
        # 옛 작품(D+18·무대 뒤)은 하던 대로 주인공 시트를 그림체·배치 본보기로 쓴다.
        ref = None if (series / "series.json").exists() else style_ref(series, name)
        refs = [ref] if ref else []
        # 2026-10-16 시험: 중년 오 팀장이 젊은 미남으로 나왔다 → 나이를 못 박는다
        prompt = (SHEET_HEAD if ref else "") + prompt + "\n\nKeep the character's age exactly as described: do not make them look younger or more glamorous than described."
    else:
        # 그림체 글의 눈·머리·얼굴 문장이 사람을 불러냈다(호프집 시안 앞에 여자, comfy_panel 의 사람 없는 컷과 같은 문제) → 빼고 빈 장소를 못 박는다
        prompt = strip_people(prompt)
        # 장소 글 자체에 "뒤쪽 손님들"이 있는 곳(호프집)은 "아무도 없음"과 부딪혀 앞 테이블에 사람을 앉혔다 → 앞·가운데만 비우게.
        # 가구 이름은 쓰지 않는다: "every table, seat … is empty" 라고 썼더니 길·골목·빈 방마다 빈 탁자와 의자를 그려 넣었다(2026-10-18 추몽 시안 7곳)
        prompt += ("\n\nThe foreground and middle ground are free of people. Only the objects named in the location text are there; "
                   "add no extra furniture. At most tiny, blurred background figures, and only if the location text asks for them.")
    return prompt, refs


PLACE_EXPECT = """This describes the background location of a webtoon: {text}
Is the camera meant to be inside a room or building (an interior), outdoors (a street, an alley, a field, the outside of a building), or
both (a half-open place such as a station platform under a roof, a porch, a covered market)?
Answer JSON only: {{"where": "inside" | "outside" | "both" | "unclear"}}"""
PLACE_SEE = """Look at this background picture. Is the camera inside a room or building (walls, ceiling or furniture around the viewer), or outdoors
(sky, a street, the outside of a building seen from the street), or both (a half-open place under a roof, such as a station platform or a porch)?
Answer JSON only: {{"where": "inside" | "outside" | "both" | "unclear"}}"""
PLACE_FIX = {"inside": "The camera stands INSIDE this place, looking at its interior: walls, shelves and furniture around the viewer. Do not show the outside of the building or the street.",
             "outside": "The camera stands OUTDOORS, looking at this place from outside."}


def place_mismatch(series: Path, name: str, img: Path) -> str:
    """장소 글이 실내인데 그림이 바깥(또는 반대)이면 글이 말하는 쪽('inside'|'outside'), 맞거나 모르면 ''. 글과 그림을 따로 묻고 코드가 비교한다.
    '골목 끝의 실내'를 가게 바깥(골목에서 본 가게 앞)으로 그렸고 그 그림이 모든 컷의 배경이 됐다(2026-10-24 등불 상점).
    지붕 덮인 승강장처럼 반쯤 열린 곳은 'both': 둘로만 고르게 하니 경성역 승강장을 글은 바깥, 그림은 안으로 갈랐다.
    글이 실내면 그림도 실내여야 한다(그림 'both' 도 어긋남): 길에서 본 가게 앞을 그림은 'both' 라 해서 넘겼다(등불 상점 3판).
    기존 장소 80곳에서 이 규칙에 걸린 것은 등불 상점과 회전문 바깥 처마로 그린 '마포 사옥 로비' 둘, 둘 다 실제로 글과 다름."""
    from refine import _text_json, _vlm
    txt = paths(series, "location", name)[0].read_text(encoding="utf-8")
    m = re.search(r"LOCATION:\s*(.+)", txt, flags=re.S)
    want = str(_text_json(PLACE_EXPECT.format(text=(m.group(1) if m else txt).strip()[:800])).get("where", "")).lower()
    got = str(_vlm([img], PLACE_SEE, 60).get("where", "")).lower()
    return want if (want == "inside" and got in ("outside", "both")) or (want == "outside" and got == "inside") else ""


def make(series: Path, kind: str, name: str, seed=None) -> Path:
    cand = paths(series, kind, name)[1]
    prompt, ref_paths = candidate_prompt(series, kind, name)
    c, cache = Comfy(), {}
    refs = [upload(c, r, cache) for r in ref_paths]
    seed = seed if seed is not None else random.randrange(2**48)
    for k in range(2):
        # 오래 쓰는 그림이라 터보 없이 40스텝, 16:9
        g = graph(prompt, refs, 1920, 1088, seed + k, 40, 1024, turbo=False, series=series,
                  role="candidate_location" if kind == "location" else "candidate_sheet")
        g["9"]["inputs"]["filename_prefix"] = f"webtoon_studio/new_{kind}"
        hist = c.wait(c.queue(g), 1200)
        cand.parent.mkdir(parents=True, exist_ok=True)
        c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), cand)
        if kind != "location" or name.endswith("_reverse"):
            break
        want = place_mismatch(series, name, cand)
        if not want:
            break
        if k:  # 두 번째도 어긋나면 말없이 넘기지 않는다(등불 상점 4판: 다시 그린 것도 반쯤 바깥이었는데 아무 말이 없었다)
            print(f"  {name}: 확인 필요 - 다시 그려도 장소 글은 {want} 인데 그림은 아님. 장소 글을 더 분명히(add_to_series) 하고 redo_candidate", flush=True)
            break
        print(f"  {name}: 장소 글은 {want} 인데 그림은 반대 → 한 번 더 그림", flush=True)
        prompt += "\n\n" + PLACE_FIX[want]
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
