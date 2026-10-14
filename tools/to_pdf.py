"""합성된 세로 스크롤 웹툰을 읽기 좋은 PDF 로 만든다.

    python tools/to_pdf.py <episode_dir> [--page-height 1800] [--quality 88]

out/<ep>_full.png 를 페이지로 나누되 컷 사이 여백(배경색만 있는 줄)에서만 자른다.
그래서 컷이 페이지 중간에서 잘리지 않는다. 여백이 없을 만큼 긴 구간만 어쩔 수 없이 최대 높이에서 자른다.
결과: out/<ep>.pdf
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import load_style  # noqa: E402

Image.MAX_IMAGE_PIXELS = None  # 한 화 전체가 1억 픽셀을 넘는다


def gap_rows(im, bg):
    """배경색만 있는 줄이면 True 인 배열."""
    a = np.asarray(im.convert("RGB"), dtype=np.int16)
    return (np.abs(a - np.array(bg, dtype=np.int16)).max(axis=2) <= 6).all(axis=1)


def gap_centers(is_gap, min_run):
    """min_run 줄 이상 이어지는 여백의 가운데 줄들. 내레이션 박스와 그 컷 사이처럼 좁은 여백은 빼서 둘이 갈라지지 않게 한다."""
    centers, start = [], None
    for y, g in enumerate(np.append(is_gap, False)):
        if g and start is None:
            start = y
        elif not g and start is not None:
            if y - start >= min_run:
                centers.append((start + y) // 2)
            start = None
    return np.array(centers, dtype=np.int64)


def cut_points(is_gap, target, max_h, min_run=50):
    H = len(is_gap)
    centers = gap_centers(is_gap, min_run)
    cuts, top = [0], 0
    while H - top > max_h:
        lo, hi = top + int(target * 0.5), min(top + max_h, H - 1)
        cand = centers[(centers > lo) & (centers < hi)]
        # 목표 높이에 가장 가까운 컷 사이 여백에서 자르고, 없으면 최대 높이에서 자른다
        cut = int(cand[np.argmin(np.abs(cand - (top + target)))]) if len(cand) else hi
        cuts.append(cut)
        top = cut
    cuts.append(H)
    return cuts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--page-height", type=int, default=1800)
    ap.add_argument("--quality", type=int, default=88)
    args = ap.parse_args()

    ep = Path(args.episode_dir)
    src = ep / "out" / f"{ep.name}_full.png"
    if not src.exists():
        sys.exit(f"합성본이 없다: {src} (먼저 compose.py)")
    bg_hex = load_style()["lettering"]["background"].lstrip("#")
    bg = tuple(int(bg_hex[i:i + 2], 16) for i in (0, 2, 4))
    full = Image.open(src).convert("RGB")
    cuts = cut_points(gap_rows(full, bg), args.page_height, int(args.page_height * 1.45))
    pages = [full.crop((0, a, full.width, b)) for a, b in zip(cuts, cuts[1:]) if b - a > 40]
    out = ep / "out" / f"{ep.name}.pdf"
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=96, quality=args.quality)
    heights = [p.height for p in pages]
    print(f"{out} {len(pages)}쪽, 쪽 높이 {min(heights)}~{max(heights)}px, {out.stat().st_size / 2**20:.1f}MB")


if __name__ == "__main__":
    main()
