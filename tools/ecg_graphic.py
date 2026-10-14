"""심박 모니터 화면 컷을 그림 모델 없이 직접 그린다. 모델은 모니터 화면에 파형 대신 엉뚱한 모양·가짜 글자를 넣는다.

    python tools/ecg_graphic.py <episode_dir> <컷 id> [--beats 9] [--size 1216x832]

검은 화면에 초록 파형이 빠르게 치솟는 모니터(베젤·유리 반사·번짐 포함)를 <episode_dir>/panels/<컷 id>.png 로 저장한다.
콘티에는 "composite": {"tool": "ecg_graphic"} 로 적어 렌더에서 빠지게 한다.
"""
import argparse
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("panel_id")
    ap.add_argument("--beats", type=int, default=9)
    ap.add_argument("--size", default="1216x832")
    args = ap.parse_args()
    W, H = map(int, args.size.split("x"))
    rnd = random.Random(args.panel_id)

    # 방 조명(따뜻한 갈색) 속 모니터 베젤
    img = Image.new("RGB", (W, H), (42, 36, 40))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(58 - 20 * t), int(48 - 16 * t), int(52 - 14 * t)))
    bx, by, bw, bh = int(W * 0.06), int(H * 0.08), int(W * 0.88), int(H * 0.84)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=28, fill=(28, 30, 34), outline=(70, 74, 80), width=4)
    sx, sy, sw, sh = bx + 34, by + 34, bw - 68, bh - 68
    d.rectangle([sx, sy, sx + sw, sy + sh], fill=(4, 10, 8))

    # 격자
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    for x in range(sx, sx + sw, 40):
        g.line([(x, sy), (x, sy + sh)], fill=(30, 90, 50, 60))
    for y in range(sy, sy + sh, 40):
        g.line([(sx, y), (sx + sw, y)], fill=(30, 90, 50, 60))
    img.paste(grid, (0, 0), grid)

    # 파형: 기준선 위로 점점 커지는 QRS 스파이크
    base = sy + sh * 0.62
    pts, n = [], args.beats
    step = sw / n
    for i in range(n):
        x0 = sx + i * step
        amp = sh * (0.30 + 0.30 * i / max(1, n - 1)) * rnd.uniform(0.9, 1.1)
        seg = [(0.00, 0), (0.30, 0), (0.36, -0.08), (0.42, 0), (0.48, 0.10), (0.52, -1.0), (0.57, 0.25), (0.62, 0),
               (0.78, -0.12), (0.88, 0), (1.00, 0)]
        for fx, fy in seg:
            pts.append((x0 + fx * step, base + fy * amp))
    line = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(line)
    ld.line(pts, fill=(90, 255, 140, 255), width=5, joint="curve")
    glow = line.filter(ImageFilter.GaussianBlur(10))
    img.paste(glow, (0, 0), glow)
    img.paste(glow, (0, 0), glow)
    img.paste(line, (0, 0), line)
    # 맨 끝 점(지금 순간)을 밝게
    ex, ey = pts[-1]
    dot = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(dot).ellipse([ex - 14, ey - 14, ex + 14, ey + 14], fill=(200, 255, 210, 255))
    img.paste(dot.filter(ImageFilter.GaussianBlur(6)), (0, 0), dot.filter(ImageFilter.GaussianBlur(6)))

    # 유리 반사
    refl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(refl).polygon([(sx, sy), (sx + sw * 0.35, sy), (sx + sw * 0.15, sy + sh), (sx, sy + sh)],
                                 fill=(255, 255, 255, 14))
    img.paste(refl, (0, 0), refl)

    out = Path(args.episode_dir) / "panels" / f"{args.panel_id}.png"
    img.save(out)
    print(f"{out.name} (심박 모니터 그래픽)")


if __name__ == "__main__":
    main()
