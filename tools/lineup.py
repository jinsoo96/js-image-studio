"""캐릭터 소개컷을 한 장에 모은 라인업 이미지를 만든다.

    python tools/lineup.py <series_dir> [--view intro] [--cols 5]

<series_dir>/characters/<id>/<view>_v1.png 를 characters.json 순서대로 붙이고,
아래에 id · 나이와 포지션 · 얼굴상을 적어 <series_dir>/characters/_lineup_<view>.png 로 저장한다.
"""
import argparse
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_json, load_style  # noqa: E402
from compose import Fonts  # noqa: E402

CELL_H = 520
PAD = 16
LABEL_H = 74


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("--view", default="intro")
    ap.add_argument("--cols", type=int, default=5)
    args = ap.parse_args()

    series = Path(args.series_dir)
    chars = load_json(series / "characters.json")
    fonts = Fonts(load_style()["lettering"]["fonts"], 20)
    cells = []
    for cid, c in chars.items():
        src = series / "characters" / cid / f"{args.view}_v1.png"
        if not src.exists():
            continue
        im = Image.open(src).convert("RGB")
        im = im.resize((round(im.width * CELL_H / im.height), CELL_H), Image.LANCZOS)
        role = re.split(r"[.,]", c.get("role", ""))[0]
        name = c.get("name", "")
        label = f"{name} ({cid})" if name and not name.startswith("(") else cid
        cells.append((im, label, role, c.get("face_type", "")))
    if not cells:
        sys.exit("소개컷이 없다")
    cw = max(im.width for im, *_ in cells)
    rows = (len(cells) + args.cols - 1) // args.cols
    sheet = Image.new("RGB", (PAD + args.cols * (cw + PAD), PAD + rows * (CELL_H + LABEL_H + PAD)), "#ffffff")
    d = ImageDraw.Draw(sheet)
    for i, (im, cid, role, face) in enumerate(cells):
        x = PAD + (i % args.cols) * (cw + PAD)
        y = PAD + (i // args.cols) * (CELL_H + LABEL_H + PAD)
        sheet.paste(im, (x + (cw - im.width) // 2, y))
        # NanumSquareAc 는 공백이 네모로 찍혀서 이름 줄은 Pretendard 로 쓴다
        d.text((x, y + CELL_H + 6), cid, fill="#1a1a2a", font=fonts.get("narration", 1.1))
        d.text((x, y + CELL_H + 32), role[:24], fill="#444455", font=fonts.get("narration", 0.8))
        d.text((x, y + CELL_H + 52), face, fill="#c2386b", font=fonts.get("narration", 0.8))
    out = series / "characters" / f"_lineup_{args.view}.png"
    sheet.save(out)
    print(f"{out} {sheet.size[0]}x{sheet.size[1]} ({len(cells)}명)")


if __name__ == "__main__":
    main()
