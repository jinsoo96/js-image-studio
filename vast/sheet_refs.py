"""캐릭터 시트에서 Qwen Image 2.1 용 참고 그림을 잘라낸다(얼굴 한 칸 · 정면 전신 하나).
(2026-10-15 시험: 시트를 통째로 넣으면 Qwen 은 시트의 줄 세운 전신·표정 칸을 그대로 베껴 그린다. 힉스필드 Seedream 은 안 그랬다.)
시트 배치가 인물마다 달라서(얼굴 2열x3행 / 3열x2행) 좌표를 박지 않고 배경색과 다른 열·행 덩어리로 찾는다.

    .venv/Scripts/python vast/sheet_refs.py <series_hf_dir> [인물 ...]
결과: <series>/assets/vast_refs/<id>_bust.png(무표정 상반신, 기본 참고) · <id>_face.png · <id>_front.png (시트가 바뀌면 다시 만든다)
"""
import json
import re
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


def one_person(img: Image.Image) -> Image.Image:
    """전신 덩어리에 사람이 둘(정면·3/4 이 팔끝으로 붙음) 들어 있으면 첫 사람만. 머리 높이(위 20%)에서 잉크 덩어리가 둘이면 그 틈에서 자른다.
    폭 기준(시트의 22%)으로는 못 가른 시트가 있었다: 상민 머리가 참고 그림에 둘 들어가 한 컷에 둘 그려질 뻔했다(2026-10-19 외출)."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    bg = np.median(np.concatenate([a[:6].reshape(-1, 3), a[:, :6].reshape(-1, 3)]), axis=0)
    ink = np.abs(a - bg).sum(axis=2) > 90
    heads = runs(ink[: int(h * 0.2)].mean(axis=0) > 0.02, min_gap=8, min_len=int(w * 0.12))
    if len(heads) < 2:
        return img
    cut = (heads[0][1] + heads[1][0]) // 2
    return img.crop((0, 0, cut, h))


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
    # 정면·반측면 전신이 팔끝으로 붙으면 한 덩어리로 잡혀 두 명이 같이 잘린다(무대 뒤 dancer·rapper, 베이스·표정에 둘씩 나옴)
    # → 사람 하나 폭(시트 폭의 약 17%)보다 넓으면 가운데쯤 잉크가 가장 적은 열에서 가른다
    if fx1 - fx0 > 0.22 * w:
        prof = ink[:, fx0:fx1].mean(axis=0)
        lo, hi = int((fx1 - fx0) * 0.3), int((fx1 - fx0) * 0.7)
        fx1 = fx0 + lo + int(np.argmin(prof[lo:hi]))
    front = one_person(im.crop(box(fx0, fx1)))
    # 머리(정면 전신의 위 19%): 표정이 늘 무표정이라 얼굴 참고로 쓴다. 얼굴 칸 첫 장은 인물마다 웃음·화남이라 그 표정을 따라 그렸다(p045 우는 컷이 웃음).
    # 30% 상반신은 시트의 기본 옷(우빈 파란 셔츠)이 보여 그 옷을 입은 사람이 한 명 더 생겼다(p003)
    bust = front.crop((0, 0, front.width, int(front.height * 0.19)))
    # 얼굴 칸: 전신은 위아래로 한 덩어리, 얼굴 칸 열은 위아래로 여러 덩어리(칸이 2~3줄). 전신끼리 붙어 한 덩어리로 잡혀도 세지 않아도 된다
    def bands(x0, x1):
        return runs(ink[:, x0:x1].mean(axis=1) > 0.01, min_gap=6, min_len=int(0.12 * h))
    grid = [cx for cx in cols[1:] if len(bands(*cx)) >= 2]
    if not grid:
        # 얼굴 칸이 엇갈려 놓였거나 뒷모습 전신과 붙은 시트(2026-10-19 외출 오동기·상민): 얼굴 칸 대신 정면 전신의 머리를 얼굴로 쓴다.
        # 그리기의 기본 참고는 어차피 이 머리(bust)라 멈출 까닭이 없다
        print(f"{sheet.name}: 얼굴 칸 열을 못 찾아 정면 머리를 얼굴 참고로 씀")
        return front, bust.copy(), bust
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


def turn_head(sheet: Path):
    """시트 두 번째 전신(3/4 측면)의 머리. 정면 머리·정면 표정 칸만 넣으면 인물이 독자를 쳐다봤다(2026-10-18 추몽 p016)
    → 독자를 봐서 다시 그릴 때 이 머리를 넣는다. 전신 열은 위아래 한 덩어리, 표정 칸 열은 여러 덩어리라 그걸로 가른다."""
    im = Image.open(sheet).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    h, w, _ = a.shape
    border = np.concatenate([a[:8].reshape(-1, 3), a[-8:].reshape(-1, 3), a[:, :8].reshape(-1, 3), a[:, -8:].reshape(-1, 3)])
    ink = np.abs(a - np.median(border, axis=0)).sum(axis=2) > 90
    bodies = []
    for x0, x1 in runs(ink.mean(axis=0) > 0.015):
        if len(runs(ink[:, x0:x1].mean(axis=1) > 0.01, min_gap=6, min_len=int(0.12 * h))) >= 2:
            break  # 표정 칸 열부터는 전신이 아니다
        if x1 - x0 > 0.22 * w:  # 전신 둘이 팔끝으로 붙었다(crop_refs 와 같은 가르기)
            prof = ink[:, x0:x1].mean(axis=0)
            lo, hi = int((x1 - x0) * 0.3), int((x1 - x0) * 0.7)
            mid = x0 + lo + int(np.argmin(prof[lo:hi]))
            bodies += [(x0, mid), (mid, x1)]
        else:
            bodies.append((x0, x1))
    if len(bodies) < 2:
        raise ValueError(f"{sheet.name}: 3/4 전신을 못 찾음")
    x0, x1 = bodies[1]
    rr = runs(ink[:, x0:x1].mean(axis=1) > 0.004, min_gap=6)
    ya, yb = rr[0] if rr else (0, h)
    body = one_person(im.crop((max(0, x0 - 24), max(0, ya - 24), min(w, x1 + 24), min(h, yb + 24))))
    return body.crop((0, 0, body.width, int(body.height * 0.19)))


def expr_tiles(series: Path, out: Path):
    """표정 시트(4x3, 12칸)를 칸마다 잘라 <id>_expr_01..12.png, 칸 이름은 시트를 만든 프롬프트(prompts/expr_<id>.txt)의 표정 목록 순서 그대로 <id>_expr.json.
    (2026-10-15 사용자 "연기 강도 강하게": Qwen 은 참고 얼굴의 표정을 그대로 따라 그리니, 컷 연기에 맞는 칸을 골라 넣는다)"""
    for s in sorted((series / "assets").glob("*_expressions.png")):
        cid = s.name[: -len("_expressions.png")]
        pf = series / "prompts" / f"expr_{cid}.txt"
        m = re.search(r":\s*([^:]+?)\.\s*$", pf.read_text(encoding="utf-8").strip(), re.S) if pf.exists() else None  # 마지막 콜론 뒤 목록(두 작품 형식이 다름)
        labels = [x.strip() for x in m.group(1).split(",")] if m else []
        if len(labels) != 12:
            print(cid, "표정 이름 12개를 못 읽음", len(labels))
            continue
        im = Image.open(s).convert("RGB")
        tw, th = im.width / 4, im.height / 3
        for k in range(12):
            r, c = divmod(k, 4)
            x0, y0 = c * tw, r * th  # 칸 가장자리는 옆 칸이 묻어날 수 있어 4% 안쪽만
            im.crop((int(x0 + tw * 0.04), int(y0 + th * 0.04), int(x0 + tw * 0.96), int(y0 + th * 0.96))).save(out / f"{cid}_expr_{k + 1:02d}.png")
        (out / f"{cid}_expr.json").write_text(json.dumps({"labels": labels}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(cid, "표정 12칸", labels)


def main():
    series = Path(sys.argv[1])
    want = set(sys.argv[2:])
    out = series / "assets" / "vast_refs"
    out.mkdir(parents=True, exist_ok=True)
    expr_tiles(series, out)
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
        try:
            turn_head(s).save(out / f"{cid}_turn.png")
        except Exception as e:
            print(cid, "3/4 머리 실패", e)
        print(cid, "전신", front.size, "얼굴", face.size)


if __name__ == "__main__":
    main()
