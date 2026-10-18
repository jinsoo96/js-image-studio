"""장면 이어짐 점검: 콘티 순서대로 같은 장소의 앞 컷과 이 컷을 나란히 보여 주고, 자세·소품·옷·얼굴·그림체·좌우가 깨진 곳을 묻는다.
(2026-10-18 외출 1화 사용자 지적: 앉아 있던 은영이 다음 컷에 서 있고, 맥주 두 잔이 세 잔·커피잔으로 바뀌고, 다음 컷에서 그림체와 얼굴이 바뀌었다.
 컷 하나씩 보는 검수로는 못 잡는다.) 근거: 영화 연출의 연속성 원칙(소품 수량·상태, 동작·위치 연속성), skills/panel-spec '연출 문법'.

    .venv/Scripts/python tools/continuity_check.py <회차 폴더> [--only p010,p012] [--json out.json]
--only 를 주면 그 컷이 들어간 짝만 본다. 걸린 짝을 "이어짐 p010→p012: ..." 로 찍고, 하나라도 있으면 종료 코드 4.
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from refine import continuity_pair  # noqa: E402


def pairs(ep: Path):
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))["panels"]
    spec = {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))}
    prev = None
    for b in board:
        p = spec.get(b["id"])
        img = ep / "panels_hf" / f"{b['id']}.png"
        if b.get("composite") or not p or not img.exists():
            continue
        if "insert" in (p.get("shot") or "") or not p.get("chars"):  # 손·소품 인서트와 빈 장소는 건너뛰고 그 앞 인물 컷과 잇는다
            continue
        if prev and prev[0].get("location") == p.get("location"):
            yield prev[0], prev[1], p, img
        prev = (p, img)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--only")
    ap.add_argument("--json")
    a = ap.parse_args()
    ep = Path(a.episode_dir)
    only = set(a.only.split(",")) if a.only else None
    todo = [x for x in pairs(ep) if not only or x[0]["id"] in only or x[2]["id"] in only]

    def one(x):
        pp, pimg, p, img = x
        return pp["id"], p["id"], continuity_pair(pimg, img, p)
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, todo))
    bad = [(a_, b_, pr) for a_, b_, pr in res if pr]
    for a_, b_, pr in bad:
        print(f"이어짐 {a_}→{b_}: " + "; ".join(pr), flush=True)
    print(f"이어짐 점검 {len(todo)}짝, 걸린 짝 {len(bad)}")
    if a.json:
        Path(a.json).write_text(json.dumps([{"prev": x, "panel": y, "problems": z} for x, y, z in bad], ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(4 if bad else 0)


if __name__ == "__main__":
    main()
