"""그림 한 장을 TV 화면 안에 넣은 컷을 만든다(방송 화면을 보는 장면).
모델에게 "TV 화면 속 아이돌"을 시키면 화면 뒤에 거대한 얼굴을 그리는 등 구도가 무너져서(1화 p016),
무대 위 인물을 따로 그리고 TV 베젤·유리 반사·방 어둠을 그래픽으로 씌운다.

    python tools/tv_frame.py <episode_dir> <원본 png(에피소드 폴더 기준)> <컷 id> [--size 1216x832]
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("source")
    ap.add_argument("panel_id")
    ap.add_argument("--size", default="1216x832")
    args = ap.parse_args()
    ep = Path(args.episode_dir)
    W, H = map(int, args.size.split("x"))

    # 어두운 거실 벽(따뜻한 갈색 그라데이션)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(46 - 16 * t), int(38 - 12 * t), int(40 - 10 * t)))
    # TV 베젤과 화면
    bx, by, bw, bh = int(W * 0.07), int(H * 0.08), int(W * 0.86), int(H * 0.80)
    d.rounded_rectangle([bx - 6, by - 6, bx + bw + 6, by + bh + 6], radius=16, fill=(12, 12, 14))
    sx, sy, sw, sh = bx + 18, by + 18, bw - 36, bh - 36
    src = Image.open(ep / args.source).convert("RGB")
    scale = max(sw / src.width, sh / src.height)
    src = src.resize((round(src.width * scale), round(src.height * scale)), Image.LANCZOS)
    left, top = (src.width - sw) // 2, int((src.height - sh) * 0.25)
    screen = src.crop((left, top, left + sw, top + sh))
    # 방송 화면 느낌: 살짝 밝고 차갑게 + 가는 주사선
    screen = Image.blend(screen, Image.new("RGB", screen.size, (210, 225, 255)), 0.06)
    lines = Image.new("L", screen.size, 0)
    ld = ImageDraw.Draw(lines)
    for y in range(0, sh, 4):
        ld.line([(0, y), (sw, y)], fill=18)
    screen.paste((0, 0, 0), (0, 0), lines)
    img.paste(screen, (sx, sy))
    # 유리 반사(대각선 옅은 띠)
    glare = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glare).polygon([(sx, sy), (sx + sw * 0.32, sy), (sx + sw * 0.12, sy + sh), (sx, sy + sh)], fill=22)
    img.paste((255, 255, 255), (0, 0), glare.filter(ImageFilter.GaussianBlur(8)))
    # 화면 빛이 베젤 아래 벽에 번짐
    bloom = Image.new("L", (W, H), 0)
    ImageDraw.Draw(bloom).rectangle([sx, sy + sh, sx + sw, H], fill=40)
    img.paste((150, 170, 220), (0, 0), bloom.filter(ImageFilter.GaussianBlur(40)))

    out = ep / "panels" / f"{args.panel_id}.png"
    img.save(out)
    print(f"{out.name} (TV 화면: {args.source})")


if __name__ == "__main__":
    main()
