"""소개컷으로 콜라주 표지를 만든다. SDXL 은 한 장에 8명을 제대로 못 그리므로 따로 그린 컷을 합친다.

    python tools/collage.py <series_dir> <out.png> --title "무대 뒤" [--sub "부제"] [--hero hero]

위쪽에 멤버들(hero 를 뺀 모든 캐릭터) 상반신을 4열로, 아래쪽에 주인공을 크게 놓고 가운데에 제목을 얹는다.
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import load_json, load_style  # noqa: E402
from compose import Fonts, draw_lines  # noqa: E402

W, H = 800, 1400
COLS = 4
CELL_W, CELL_H = 190, 250
GAP = 10


def bust(path, w, h, top=0.04, height=0.5):
    """소개컷 위쪽(얼굴~가슴)을 w x h 로 잘라낸다."""
    im = Image.open(path).convert("RGB")
    crop_h = int(im.height * height)
    crop_w = int(crop_h * w / h)
    left = max(0, (im.width - crop_w) // 2)
    box = (left, int(im.height * top), left + crop_w, int(im.height * top) + crop_h)
    return im.crop(box).resize((w, h), Image.LANCZOS)


def gradient():
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(20 + 90 * t), int(14 + 10 * t), int(40 + 50 * (1 - t))))
    return bg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("out")
    ap.add_argument("--title", required=True)
    ap.add_argument("--sub", default="")
    ap.add_argument("--hero", default="hero")
    ap.add_argument("--view", default="intro")
    args = ap.parse_args()

    series = Path(args.series_dir)
    chars = load_json(series / "characters.json")
    members = [cid for cid in chars if cid != args.hero and (series / "characters" / cid / f"{args.view}_v1.png").exists()]
    canvas = gradient()

    rows = (len(members) + COLS - 1) // COLS
    grid_w = COLS * CELL_W + (COLS - 1) * GAP
    x0, y0 = (W - grid_w) // 2, 40
    for i, cid in enumerate(members):
        cell = bust(series / "characters" / cid / f"{args.view}_v1.png", CELL_W, CELL_H)
        x = x0 + (i % COLS) * (CELL_W + GAP)
        y = y0 + (i // COLS) * (CELL_H + GAP)
        canvas.paste(cell, (x, y))
        ImageDraw.Draw(canvas).rectangle([x, y, x + CELL_W - 1, y + CELL_H - 1], outline=(255, 214, 236), width=2)

    hero_path = series / "characters" / args.hero / f"{args.view}_v1.png"
    hero_top = y0 + rows * (CELL_H + GAP) + 150
    if hero_path.exists():
        hero = Image.open(hero_path).convert("RGB")
        hw = W - 160
        hero = hero.resize((hw, round(hero.height * hw / hero.width)), Image.LANCZOS)
        hero = hero.crop((0, 0, hw, min(hero.height, H - hero_top)))
        # 주인공 컷 위쪽 경계를 배경에 녹인다
        mask = Image.new("L", hero.size, 255)
        md = ImageDraw.Draw(mask)
        for yy in range(80):
            md.line([(0, yy), (hw, yy)], fill=int(255 * yy / 80))
        canvas.paste(hero, ((W - hw) // 2, hero_top), mask)

    fonts = Fonts(load_style()["lettering"]["fonts"], W // 7)
    title_font = fonts.get("shout")
    cy = y0 + rows * (CELL_H + GAP) + 70
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_lines(ImageDraw.Draw(glow), [args.title], title_font, W / 2, cy, fill="#ff3d8b", stroke=10, stroke_fill="#ff3d8b")
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(glow, mask=glow)
    d = ImageDraw.Draw(canvas)
    draw_lines(d, [args.title], title_font, W / 2, cy, fill="#ffffff", stroke=4, stroke_fill="#1a1024")
    if args.sub:
        draw_lines(d, [args.sub], fonts.get("narration", 0.24), W / 2, cy + title_font.size * 0.72, fill="#f2e9ff", stroke=2, stroke_fill="#1a1024")
    canvas.save(args.out)
    print(f"{args.out} {W}x{H} (멤버 {len(members)}명)")


if __name__ == "__main__":
    main()
