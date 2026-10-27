"""말풍선이 얼굴을 가리는 컷, 읽는 순서가 거꾸로인 컷 찾기(합성 전 점검). compose.py 와 같은 크기(폭 800)로 컷을 줄여 lettering.layout_panel 을 돌리고,
풍선 상자와 얼굴 상자(face_detect)가 겹치는 비율을 찍는다(2026-10-13 사용자 "어느 웹툰이나 말풍선이 사람 얼굴 가리면 안 된다").

    .venv/Scripts/python tools/check_bubbles.py <episode_dir> [--min 0.02]
"""
import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lettering as lt  # noqa: E402
from common import load_json, load_style  # noqa: E402

W = 800


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_dir")
    ap.add_argument("--min", type=float, default=0.02)
    args = ap.parse_args()
    ep = Path(args.episode_dir)
    style = load_style()
    lt.set_style((style.get("lettering") or {}).get("bubble_style", "inside"))
    bad = 0
    for p in load_json(ep / "storyboard.json")["panels"]:
        src = ep / "panels" / f"{p['id']}.png"
        dlg = [d for d in p.get("dialogue", []) if d.get("pos") not in ("above", "below") and d.get("type") not in ("sfx", "narration", "monologue")]
        if not src.exists() or not dlg or not (p.get("characters") or p.get("layout")):
            continue
        im = Image.open(src).convert("RGB")
        if im.height > im.width:
            continue  # 세로 컷은 compose 에서 폭을 줄여 가운데에 둔다(여기선 건너뜀)
        im = im.resize((W, round(im.height * W / im.width)))
        faces = lt.face_boxes(im, W, im.height)
        bubbles, _, _ = lt.layout_panel(p, dlg, W, im.height, im)
        worst = 0.0
        for b in bubbles:
            box = (b.cx - b.rx, b.cy - b.ry, b.cx + b.rx, b.cy + b.ry)
            worst = max(worst, sum(lt.overlap(box, f) for f in faces) / (b.w * b.h))
        flag = "가림" if worst > args.min else ""
        if any(a_.neck_to and lt.neck_hits(a_, b_, faces) for a_, b_ in zip(bubbles, bubbles[1:])):
            flag += " 목"  # 이어 말하는 풍선을 잇는 띠가 얼굴을 지나감
        for a_, b_ in zip(bubbles, bubbles[1:]):  # 읽는 순서: 위→아래, 같은 줄이면 왼→오
            same_row = abs(a_.cy - b_.cy) < min(a_.ry, b_.ry)
            if (same_row and b_.cx < a_.cx) or (not same_row and b_.cy < a_.cy - a_.ry * 0.5):
                flag += " 순서"
                break
        if flag:
            bad += 1
        print(f"{p['id']:7s} 얼굴 {len(faces)} 풍선 {len(bubbles)} 최대겹침 {worst:.3f} {flag}")
    print(f"얼굴을 가리거나(풍선·잇는 띠) 순서가 어긋난 컷: {bad}")


if __name__ == "__main__":
    main()
