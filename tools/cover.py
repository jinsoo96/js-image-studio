"""그린 표지 이미지에 제목 글자를 얹는다(글자는 모델이 아니라 여기서 쓴다).

    python tools/cover.py <in.png> <out.png> --title "무대 뒤" [--sub "부제"] [--pos top|bottom] [--width 800]
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_style  # noqa: E402
from compose import Fonts, draw_lines  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--title", required=True)
    ap.add_argument("--sub", default="")
    ap.add_argument("--pos", choices=("top", "bottom"), default="top")
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--glow", default="#ff3d8b", help="제목 뒤 번짐 색")
    args = ap.parse_args()

    im = Image.open(args.src).convert("RGB")
    W = args.width
    im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
    H = im.height
    lt = load_style()["lettering"]
    fonts = Fonts(lt["fonts"], W // 8)
    title_font = fonts.get("shout")
    sub_font = fonts.get("narration", 0.28)
    cy = H * (0.12 if args.pos == "top" else 0.86)

    # 제목 뒤를 살짝 어둡게 깔아 글자가 배경에 묻히지 않게 한다
    shade = Image.new("L", (W, H), 0)
    band = H * 0.2
    ImageDraw.Draw(shade).rectangle([0, cy - band / 2, W, cy + band / 2], fill=140)
    shade = shade.filter(ImageFilter.GaussianBlur(H * 0.04))
    im = Image.composite(Image.new("RGB", (W, H), (8, 8, 20)), im, shade)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_lines(ImageDraw.Draw(glow), [args.title], title_font, W / 2, cy, fill=args.glow, stroke=10, stroke_fill=args.glow)
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    im.paste(glow, mask=glow)
    d = ImageDraw.Draw(im)
    draw_lines(d, [args.title], title_font, W / 2, cy, fill="#ffffff", stroke=4, stroke_fill="#1a1024")
    if args.sub:
        draw_lines(d, [args.sub], sub_font, W / 2, cy + title_font.size * 0.75, fill="#f2e9ff", stroke=2, stroke_fill="#1a1024")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    im.save(args.out)
    print(f"{args.out} {W}x{H}")


if __name__ == "__main__":
    main()
