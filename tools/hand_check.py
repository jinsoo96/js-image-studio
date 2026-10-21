"""손·소품 검사: 애니 손 검출기(deepghs/anime_hand_detection, face_detect.HAND_DIR)로 손을 세고, 소품 검출기(tools/prop_detect.py,
YOLOX COCO)로 폰·잔·컵·병을 재서 결함 후보를 고른다.
(2026-10-21 사용자 "손이 두 개로 보이는 경우가 종종 보인다": 외출 1화 p302 반투명 셋째 손 · p428 오른손 둘 · p468 같은 쪽 손 둘을
 눈 검수로만 잡았고 그림 보는 8B 모델은 다섯 중 넷을 놓쳤다. 손을 잘라 '왼손인가 오른손인가'를 물어도 8B 는 틀렸다 → 세는 건 검출기, 판단은 규칙)

    .venv/Scripts/python tools/hand_check.py <회차 폴더> [--folder panels_hf]   → <회차>/review/hands.json + exports/<오늘>/검수/<작품>_<회차>_손확인.jpg

check(img, chars) 가 돌려주는 것:
  strong: 손이 사람 수(컷 인물·찾은 얼굴 중 큰 쪽, 최소 1)의 두 배보다 많다 → 그리기 검수(refine.staging_check)가 '몸' 문제로 다시 그린다.
  review: 얼굴 없이 손만 둘 이상 나오는 컷(인서트) → 같은 쪽 손 둘·떠 있는 손이 잘 생기는 자리라 손 상자를 그려 크게 모아 눈으로 본다.
  소품(화면의 1% 이상인 것만, 배경의 작은 병·잔은 뺀다):
  strong: 손에 들리지 않았는데 얼굴 바로 옆(머리 높이)에 있는 컵·잔·병·폰(뜬 맥주 p488 · 손 없이 귀에 뜬 폰 p207) · 잔·컵·병이 사람 수의 두 배 초과(잔 셋 p354).
  '탁자에 닿았나'는 쓰지 않는다: 클로즈업에선 검출기가 탁자를 못 찾아 탁자 위 소주병·와인잔이 13컷이나 '떠 있다'로 걸렸다.
  review: 폰 길이가 얼굴의 0.3배 미만(라이터만 한 폰 p493 0.25, 귀에 댄 폰은 가려져 짧게 잡혀 정상 p024 도 0.28 이라 눈으로).
  젓가락은 COCO 에 없어 못 본다(p469).
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
from prop_detect import detect as props  # noqa: E402

HELD = ("cup", "wine glass", "bottle", "cell phone")
KO = {"cup": "컵", "wine glass": "와인잔", "bottle": "병", "cell phone": "폰"}


def _near(a, b, pad: float) -> bool:
    w, h = a[2] - a[0], a[3] - a[1]
    a = (a[0] - w * pad, a[1] - h * pad, a[2] + w * pad, a[3] + h * pad)
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

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
    W, H = im.size
    pr = []
    for b in props(im):  # 잔 하나를 컵·와인잔 두 종류로 겹쳐 잡는다(외출 p117): 들 수 있는 소품끼리는 종류가 달라도 겹치면 하나로
        area = max(1.0, (b[3] - b[1]) * (b[4] - b[2]))
        if b[0] in HELD and any(k[0] in HELD and max(0, min(b[3], k[3]) - max(b[1], k[1])) * max(0, min(b[4], k[4]) - max(b[2], k[2])) / area > 0.5
                                for k in pr):
            continue
        pr.append(b)
    face_h = max([b[3] - b[1] for b in faces], default=0)
    big = [b for b in pr if b[0] in HELD and (b[3] - b[1]) * (b[4] - b[2]) / (W * H) >= 0.01]
    pboxes = []
    for n, x0, y0, x1, y1, _ in big:
        box = (x0, y0, x1, y1)
        held = any(_near(box, h[:4], 0.25) for h in hs)
        by_face = any(_near(f[:4], box, 0.3) and y0 < f[3] for f in faces)  # 얼굴 둘레 안, 위쪽이 턱보다 위(머리 높이)
        if not held and by_face:
            strong.append(f"{KO[n]}이 손에 들리지 않았는데 얼굴 옆에 떠 있다")
        pboxes.append(list(map(round, box)))
    for n, x0, y0, x1, y1, _ in pr:  # 폰 크기는 크기 기준 없이(작은 폰이 바로 결함이다), 손에 들렸거나 얼굴 옆인 폰만(책상 위 먼 폰 p030 은 뺀다)
        box = (x0, y0, x1, y1)
        in_use = any(_near(box, h[:4], 0.25) for h in hs) or any(_near(f[:4], box, 0.3) for f in faces)
        if n == "cell phone" and in_use and face_h and max(x1 - x0, y1 - y0) / face_h < 0.3:
            review.append(f"폰 길이가 얼굴의 {max(x1 - x0, y1 - y0) / face_h:.2f}배로 작다")
            pboxes.append(list(map(round, (x0, y0, x1, y1))))
    drinks = [b for b in big if b[0] != "cell phone" and (b[3] - b[1]) * (b[4] - b[2]) / (W * H) >= 0.03]  # 앞쪽 큰 잔만(화면 속 사진의 잔 p364 는 뺀다)
    if len(drinks) > 2 * people:
        strong.append(f"잔·컵·병이 {len(drinks)}개로 사람 {people}명의 두 배보다 많다")
    return {"hands": len(hs), "faces": len(faces), "strong": strong, "review": review, "boxes": [list(map(round, b[:4])) for b in hs],
            "props": pboxes}


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
        for b in r.get("props", []):  # 소품은 파랑
            d.rectangle(b, outline=(40, 120, 255), width=max(3, im.width // 300))
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
    dst = ROOT / "exports" / time.strftime("%Y-%m-%d") / "검수" / f"{ep.parents[1].name}_{ep.name}_손소품확인.jpg"
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
