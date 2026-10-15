"""캐릭터 시트에서 Qwen Image 2.1 용 참고 그림을 잘라낸다(얼굴 한 칸 · 정면 전신 하나).
(2026-10-15 시험: 시트를 통째로 넣으면 Qwen 은 시트의 줄 세운 전신·표정 칸을 그대로 베껴 그린다. 힉스필드 Seedream 은 안 그랬다.)
시트 배치가 인물마다 달라서(얼굴 2열x3행 / 3열x2행) 좌표를 박지 않고 배경색과 다른 열·행 덩어리로 찾는다.

    .venv/Scripts/python vast/sheet_refs.py <series_hf_dir> [인물 ...]
결과: <series>/assets/vast_refs/<id>_bust.png(무표정 상반신, 기본 참고) · <id>_face.png · <id>_front.png (시트가 바뀌면 다시 만든다)
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def runs(mask, min_gap=10, min_len=40):
    """True 가 이어진 구간들. min_gap 보다 짧은 틈은 잇고, min_len 보다 짧은 덩어리는 버린다."""
    out, start, gap = [], None, 0
    for i, v in enumerate(mask):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap:
                out.append((start, i - gap + 1))
                start, gap = None, 0
    if start is not None:
        out.append((start, len(mask) - gap))
    return [(a, b) for a, b in out if b - a >= min_len]


def crop_refs(sheet: Path):
    im = Image.open(sheet).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    h, w, _ = a.shape
    border = np.concatenate([a[:8].reshape(-1, 3), a[-8:].reshape(-1, 3), a[:, :8].reshape(-1, 3), a[:, -8:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    ink = np.abs(a - bg).sum(axis=2) > 90  # 배경에 은은한 그라데이션·종이 결이 있어 36 은 배경까지 잡았다
    cols = runs(ink.mean(axis=0) > 0.015)
    if len(cols) < 2:
        raise ValueError(f"{sheet.name}: 덩어리 {len(cols)}개(전신과 얼굴 칸을 못 가름)")

    def box(x0, x1, y0=0, y1=None, pad=24):
        y1 = h if y1 is None else y1
        rr = runs(ink[y0:y1, x0:x1].mean(axis=1) > 0.004, min_gap=6)
        ya, yb = (y0 + rr[0][0], y0 + rr[0][1]) if rr else (y0, y1)
        return max(0, x0 - pad), max(0, ya - pad), min(w, x1 + pad), min(h, yb + pad)

    fx0, fx1 = cols[0]
    front = im.crop(box(fx0, fx1))
    # 머리(정면 전신의 위 19%): 표정이 늘 무표정이라 얼굴 참고로 쓴다. 얼굴 칸 첫 장은 인물마다 웃음·화남이라 그 표정을 따라 그렸다(p045 우는 컷이 웃음).
    # 30% 상반신은 시트의 기본 옷(우빈 파란 셔츠)이 보여 그 옷을 입은 사람이 한 명 더 생겼다(p003)
    bust = front.crop((0, 0, front.width, int(front.height * 0.19)))
    # 얼굴 칸: 전신은 위아래로 한 덩어리, 얼굴 칸 열은 위아래로 여러 덩어리(칸이 2~3줄). 전신끼리 붙어 한 덩어리로 잡혀도 세지 않아도 된다
    def bands(x0, x1):
        return runs(ink[:, x0:x1].mean(axis=1) > 0.01, min_gap=6, min_len=int(0.12 * h))
    grid = [cx for cx in cols[1:] if len(bands(*cx)) >= 2]
    if not grid:
        raise ValueError(f"{sheet.name}: 얼굴 칸 열을 못 찾음")
    gx0, gx1 = grid[0]
    ry0, ry1 = bands(gx0, gx1)[0]
    # 옆 칸과 붙어 있으면 그 줄 안에서 다시 가르고, 그래도 넓으면 정사각에 가깝게 왼쪽만
    sub = runs(ink[ry0:ry1, gx0:gx1].mean(axis=0) > 0.02, min_gap=3)
    if sub:
        gx0, gx1 = gx0 + sub[0][0], gx0 + sub[0][1]
    if gx1 - gx0 > 1.35 * (ry1 - ry0):
        gx1 = gx0 + int(1.05 * (ry1 - ry0))
    face = im.crop((max(0, gx0 - 12), max(0, ry0 - 12), min(w, gx1 + 12), min(h, ry1 + 12)))
    return front, face, bust


def main():
    series = Path(sys.argv[1])
    want = set(sys.argv[2:])
    out = series / "assets" / "vast_refs"
    out.mkdir(parents=True, exist_ok=True)
    for s in sorted((series / "assets").glob("*_sheet.png")):
        cid = s.name[: -len("_sheet.png")]
        if want and cid not in want:
            continue
        try:
            front, face, bust = crop_refs(s)
        except Exception as e:
            print(cid, "실패", e)
            continue
        front.save(out / f"{cid}_front.png")
        face.save(out / f"{cid}_face.png")
        bust.save(out / f"{cid}_bust.png")
        print(cid, "전신", front.size, "얼굴", face.size)


if __name__ == "__main__":
    main()
