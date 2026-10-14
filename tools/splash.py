"""회차 오프닝 이미지: 흑백 처리한 멤버들을 배경에 깔고 주인공을 컬러로 작게 앞에 세운다.

    python tools/splash.py <series_dir> <out.png> --title "무대 뒤" [--sub "부제"] [--hero hero]

배경 = characters/<멤버>/intro_v1.png 를 흑백·어둡게 해서 세로 띠로 나란히.
주인공 = characters/<hero>/full_v1.png (단색 배경 전신) 에서 rembg 로 오려낸 컷.
"""
import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import ROOT, load_json, load_style  # noqa: E402
from compose import Fonts, draw_lines  # noqa: E402

W, H = 800, 1400


def cutout(path):
    """전신 컷에서 인물만 오려낸다. rembg 의 애니 그림 전용 모델(isnet-anime)을 쓴다.
    색 기준으로 배경을 지우는 방식은 배경이 회색 그라데이션이라 실패했다(2026-10-02)."""
    os.environ.setdefault("U2NET_HOME", str(ROOT / "models" / "u2net"))  # 모델 파일을 C 드라이브가 아니라 작업 폴더에 둔다
    from rembg import new_session, remove
    out = remove(Image.open(path).convert("RGB"), session=new_session("isnet-anime"))
    return out.crop(out.getbbox())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("out")
    ap.add_argument("--title", required=True)
    ap.add_argument("--sub", default="")
    ap.add_argument("--hero", default="hero")
    ap.add_argument("--credit", default="", help="작가 표기(예: 글/그림: 김진수)")
    args = ap.parse_args()

    series = Path(args.series_dir)
    chars = load_json(series / "characters.json")
    members = [cid for cid, c in chars.items()
               if cid != args.hero and not c.get("skip_gender_style") and (series / "characters" / cid / "intro_v1.png").exists()]
    canvas = Image.new("RGB", (W, H), (14, 12, 20))

    # 배경: 멤버 흑백 띠
    n = len(members)
    sw = W // n + 30
    band_h = H
    for i, cid in enumerate(members):
        im = Image.open(series / "characters" / cid / "intro_v1.png").convert("RGB")
        scale = band_h / im.height
        im = im.resize((round(im.width * scale), band_h), Image.LANCZOS)
        left = (im.width - sw) // 2
        strip = ImageOps.grayscale(im.crop((left, 0, left + sw, band_h))).convert("RGB")
        strip = ImageEnhance.Brightness(strip).enhance(0.62)
        canvas.paste(strip, (i * (W // n) - 15, 0))
    # 아래로 갈수록 어둡게
    fade = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(fade)
    for y in range(H):
        t = max(0.0, (y - H * 0.35) / (H * 0.65))
        d.line([(0, y), (W, y)], fill=int(255 * min(1.0, t * 1.15)))
    canvas = Image.composite(Image.new("RGB", (W, H), (14, 12, 20)), canvas, fade)

    # 주인공: 컬러로 작게, 아래 가운데
    hero_path = series / "characters" / args.hero / "full_v1.png"
    if hero_path.exists():
        hero = cutout(hero_path)
        hh = int(H * 0.5)
        hero = hero.resize((round(hero.width * hh / hero.height), hh), Image.LANCZOS)
        canvas.paste(hero, ((W - hero.width) // 2, H - hh - 40), hero)

    fonts = Fonts(load_style()["lettering"]["fonts"], W // 6)
    title_font = fonts.get("shout")
    cy = int(H * 0.18)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_lines(ImageDraw.Draw(glow), [args.title], title_font, W / 2, cy, fill="#ff3d8b", stroke=10, stroke_fill="#ff3d8b")
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(glow, mask=glow)
    d = ImageDraw.Draw(canvas)
    draw_lines(d, [args.title], title_font, W / 2, cy, fill="#ffffff", stroke=4, stroke_fill="#1a1024")
    if args.sub:
        draw_lines(d, [args.sub], fonts.get("narration", 0.22), W / 2, cy + title_font.size * 0.75, fill="#f2e9ff", stroke=2, stroke_fill="#1a1024")
    if args.credit:
        draw_lines(d, [args.credit], fonts.get("narration", 0.18), W / 2, H - 22, fill="#d9cfe6", stroke=2, stroke_fill="#1a1024")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.out)
    print(f"{args.out} {W}x{H} (배경 멤버 {n}명)")


if __name__ == "__main__":
    main()
