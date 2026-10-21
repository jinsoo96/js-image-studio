"""손 검사: 애니 손 검출기(deepghs/anime_hand_detection, face_detect.HAND_DIR)로 손을 세어 결함 후보를 고른다.
(2026-10-21 사용자 "손이 두 개로 보이는 경우가 종종 보인다": 외출 1화 p302 반투명 셋째 손 · p428 오른손 둘 · p468 같은 쪽 손 둘을
 눈 검수로만 잡았고 그림 보는 8B 모델은 다섯 중 넷을 놓쳤다. 손을 잘라 '왼손인가 오른손인가'를 물어도 8B 는 틀렸다 → 세는 건 검출기, 판단은 규칙)

    .venv/Scripts/python tools/hand_check.py <회차 폴더> [--folder panels_hf]   → <회차>/review/hands.json + exports/<오늘>/검수/<작품>_<회차>_손확인.jpg

check(img, chars) 가 돌려주는 것:
  strong: 손이 사람 수(컷 인물·찾은 얼굴 중 큰 쪽, 최소 1)의 두 배보다 많다 → 그리기 검수(refine.staging_check)가 '몸' 문제로 다시 그린다.
  review: 얼굴 없이 손만 둘 이상 나오는 컷(인서트) → 같은 쪽 손 둘·떠 있는 손이 잘 생기는 자리라 손 상자를 그려 크게 모아 눈으로 본다.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from face_detect import HAND_DIR, detect  # noqa: E402

BOLD = "C:/Windows/Fonts/malgunbd.ttf"


def hands(img: Image.Image) -> list:
    """[(x0, y0, x1, y1, score)]. 검출기가 손 하나를 상자 두세 개로 잡는 일이 잦아(외출 p118 모은 손 · p408 펜 쥔 손),
    작은 상자의 35% 넘게가 큰 상자와 겹치면 하나로 본다(붙은 진짜 손 둘이 합쳐질 수는 있지만 손이 많은지 세는 데엔 덜 거는 쪽)."""
    out = []
    for b in sorted(detect(img, model_dir=HAND_DIR), key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        area = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
        inside = any(max(0, min(b[2], k[2]) - max(b[0], k[0])) * max(0, min(b[3], k[3]) - max(b[1], k[1])) / area > 0.35 for k in out)
        if not inside:
            out.append(b)
    return out


def check(img_path: Path, chars=None) -> dict:
    """{"hands": n, "faces": n, "strong": [..], "review": [..], "boxes": [..]}"""
    im = Image.open(img_path).convert("RGB")
    hs, faces = hands(im), detect(im)
    people = max(1, len(chars or []), len(faces))
    strong, review = [], []
    if len(hs) > 2 * people:
        strong.append(f"손이 {len(hs)}개로 사람 {people}명의 두 배보다 많다")
    if not faces and len(hs) >= 2:
        review.append(f"얼굴 없이 손 {len(hs)}개: 같은 쪽 손 둘·떠 있거나 비쳐 보이는 손이 아닌지")
    return {"hands": len(hs), "faces": len(faces), "strong": strong, "review": review, "boxes": [list(map(round, b[:4])) for b in hs]}


def sheet(ep: Path, found: dict, folder: str = "panels_hf") -> Path | None:
    """걸린 컷을 손 상자를 그려 크게(한 줄에 둘) 모은 시트."""
    ids = [pid for pid, r in found.items() if r["strong"] or r["review"]]
    if not ids:
        return None
    font = ImageFont.truetype(BOLD, 26)
    W = 900
    cells = []
    for pid in ids:
        r = found[pid]
        im = Image.open(ep / folder / f"{pid}.png").convert("RGB")
        d = ImageDraw.Draw(im)
        for k, b in enumerate(r["boxes"]):
            d.rectangle(b, outline=(255, 40, 40), width=max(3, im.width // 300))
            d.text((b[0] + 4, b[1] + 2), str(k + 1), fill=(255, 40, 40), font=font)
        im = im.resize((W, int(im.height * W / im.width)))
        msg = "; ".join(r["strong"] + r["review"])
        c = Image.new("RGB", (W, im.height + 64), "white")
        c.paste(im, (0, 64))
        dr = ImageDraw.Draw(c)
        dr.text((6, 2), f"{pid}  손 {r['hands']} · 얼굴 {r['faces']}", fill="black", font=font)
        dr.text((6, 32), msg[:70], fill=(200, 0, 0) if r["strong"] else (60, 60, 60), font=ImageFont.truetype(BOLD, 20))
        cells.append(c)
    rows = [cells[k:k + 2] for k in range(0, len(cells), 2)]
    out = Image.new("RGB", (2 * W + 6, sum(max(x.height for x in r) + 6 for r in rows)), (70, 70, 70))
    y = 0
    for r in rows:
        for k, x in enumerate(r):
            out.paste(x, (k * (W + 6), y))
        y += max(x.height for x in r) + 6
    dst = ROOT / "exports" / time.strftime("%Y-%m-%d") / "검수" / f"{ep.parents[1].name}_{ep.name}_손확인.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, quality=85)
    return dst


def scan(ep: Path, folder: str = "panels_hf") -> dict:
    """회차 전 컷. 결과는 <회차>/review/hands.json, 걸린 컷 시트 경로를 함께 돌려준다."""
    spec = {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))}
    found = {}
    for pid, p in spec.items():
        f = ep / folder / f"{pid}.png"
        if f.exists():
            found[pid] = check(f, p.get("chars"))
    (ep / "review").mkdir(exist_ok=True)
    (ep / "review" / "hands.json").write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"found": found, "sheet": sheet(ep, found, folder)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode")
    ap.add_argument("--folder", default="panels_hf")
    a = ap.parse_args()
    ep = Path(a.episode).resolve()
    r = scan(ep, a.folder)
    hit = {k: v for k, v in r["found"].items() if v["strong"] or v["review"]}
    for pid, v in hit.items():
        print(pid, "강" if v["strong"] else "눈", "; ".join(v["strong"] + v["review"]))
    print(f"{len(r['found'])}컷 중 강 {sum(1 for v in hit.values() if v['strong'])} · 눈 확인 {sum(1 for v in hit.values() if not v['strong'])}"
          + (f" → {r['sheet']}" if r["sheet"] else ""))


if __name__ == "__main__":
    main()
