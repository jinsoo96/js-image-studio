"""앞에 나온 컷을 세피아로 바래게 처리해 회상 컷을 만든다.

    python tools/sepia_flashback.py <episode_dir> <원본 컷 id> <회상 컷 id>

예: 아침 TV 컷(p013)을 끝부분 회상(p118)으로. 같은 그림이 되살아나야 회상으로 읽혀서 새로 그리지 않는다.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    panels = Path(sys.argv[1]) / "panels"
    im = Image.open(panels / f"{sys.argv[2]}.png").convert("RGB")
    sep = ImageOps.colorize(ImageOps.grayscale(im), black=(48, 34, 22), white=(240, 222, 190))
    sep = ImageEnhance.Contrast(sep).enhance(0.9).filter(ImageFilter.GaussianBlur(1.2))
    # 가장자리를 어둡게 눌러 회상 느낌을 낸다
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).ellipse([-im.width * 0.1, -im.height * 0.15, im.width * 1.1, im.height * 1.15], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(80))
    out = Image.composite(sep, Image.new("RGB", im.size, (30, 22, 14)), mask)
    out.save(panels / f"{sys.argv[3]}.png")
    print(f"{sys.argv[3]}.png <- {sys.argv[2]}.png (세피아 회상)")


if __name__ == "__main__":
    main()
