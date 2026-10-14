"""여러 컷을 세로 띠로 잘라 나란히 붙인 분할 컷을 만든다(단체 장면용).

    python tools/strip_panel.py <out.png> <panel1.png> <panel2.png> ... [--width 1216] [--height 832] [--gap 6]

SDXL 은 한 장에 정확히 8명을 못 그리므로, 멤버 개인 컷의 얼굴·상반신 쪽을 띠로 잘라 이어 붙인다.
각 띠는 원본 가운데에서 얼굴이 들어가게 위쪽 위주로 자른다.
"""
import argparse
from pathlib import Path

from PIL import Image


def strip(path, w, h):
    im = Image.open(path).convert("RGB")
    scale = max(w / im.width, h / im.height) * 1.15  # 조금 확대해서 상반신이 띠를 채우게
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    left = (im.width - w) // 2
    top = int((im.height - h) * 0.15)  # 얼굴이 위쪽에 있으므로 위를 많이 남긴다
    return im.crop((left, top, left + w, top + h))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("panels", nargs="+")
    ap.add_argument("--width", type=int, default=1216)
    ap.add_argument("--height", type=int, default=832)
    ap.add_argument("--gap", type=int, default=6)
    args = ap.parse_args()

    n = len(args.panels)
    sw = (args.width - args.gap * (n - 1)) // n
    canvas = Image.new("RGB", (args.width, args.height), "#ffffff")
    for i, p in enumerate(args.panels):
        canvas.paste(strip(p, sw, args.height), (i * (sw + args.gap), 0))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.out)
    print(f"{args.out} {args.width}x{args.height} ({n}분할)")


if __name__ == "__main__":
    main()
