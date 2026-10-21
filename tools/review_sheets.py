"""넘기기 전 눈 검수용 시트: 회차의 본편 컷(panels_hf)을 콘티 순서로 16컷씩(컷 번호·장소·인물·대사 한 줄), 또는 예전 판과 지금 판을 나란히.
(2026-10-20 사용자 "검수 똑바로 해": 박 과장이 떠난 뒤에도 옆에 있고 폰이 갑자기 작아진 컷을 기계 검사는 못 잡고 이 시트로 눈으로 잡았다)

    .venv/Scripts/python tools/review_sheets.py <회차 폴더>                        → exports/<오늘>/검수/<작품>_<회차>_NN.jpg
    .venv/Scripts/python tools/review_sheets.py <회차 폴더> --pairs p332,p338     → 예전(panels_hf_vast/_preplate) | 지금(panels_hf) 나란히
"""
import argparse
import json
import textwrap
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = "C:/Windows/Fonts/malgun.ttf"
BOLD = "C:/Windows/Fonts/malgunbd.ttf"


def _grid(cells: list, cols: int, w: int) -> Image.Image:
    rows = [cells[k:k + cols] for k in range(0, len(cells), cols)]
    out = Image.new("RGB", (w * cols + 4 * (cols - 1), sum(max(x.height for x in r) + 4 for r in rows)), (80, 80, 80))
    y = 0
    for r in rows:
        for k, x in enumerate(r):
            out.paste(x, (k * (w + 4), y))
        y += max(x.height for x in r) + 4
    return out


def episode_sheets(ep: Path, out: Path, per: int = 16, w: int = 450) -> list:
    spec = {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))}
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))["panels"]
    f, fb = ImageFont.truetype(FONT, 15), ImageFont.truetype(BOLD, 17)
    cells = []
    for b in board:
        img = ep / "panels_hf" / f"{b['id']}.png"
        if not img.exists():
            continue
        p = spec.get(b["id"], {})
        im = Image.open(img).convert("RGB")
        im = im.resize((w, int(im.height * w / im.width)))
        im = im.crop((0, 0, w, min(im.height, 520)))
        txt = " / ".join((f"{d['who']}:" if d.get("who") else "") + d.get("text", "") for d in b.get("dialogue") or [])
        lines = textwrap.wrap(txt, 30)[:3]
        c = Image.new("RGB", (w, im.height + 30 + 20 * len(lines)), "white")
        c.paste(im, (0, 26))
        d = ImageDraw.Draw(c)
        d.text((4, 3), f"{b['id']} [{p.get('location', '')}] {','.join(p.get('chars') or [])}", fill="black", font=fb)
        for k, line in enumerate(lines):
            d.text((4, im.height + 28 + 20 * k), line, fill=(40, 40, 160), font=f)
        cells.append(c)
    files = []
    for s in range(0, len(cells), per):
        dst = out / f"{ep.parents[1].name}_{ep.name}_{s // per + 1:02d}.jpg"
        _grid(cells[s:s + per], 4, w).save(dst, quality=88)
        files.append(dst)
    return files


def pair_sheet(ep: Path, ids: list, dst: Path, w: int = 300) -> Path:
    fb = ImageFont.truetype(BOLD, 16)
    cells = []
    for i in ids:
        a, b = ep / "panels_hf_vast" / "_preplate" / f"{i}.png", ep / "panels_hf" / f"{i}.png"
        if not (a.exists() and b.exists()):
            continue
        ims = [Image.open(x).convert("RGB") for x in (a, b)]
        ims = [x.resize((w, int(x.height * w / x.width))) for x in ims]
        c = Image.new("RGB", (2 * w + 4, max(x.height for x in ims) + 24), "white")
        c.paste(ims[0], (0, 24))
        c.paste(ims[1], (w + 4, 24))
        ImageDraw.Draw(c).text((4, 2), f"{i}  예전(왼) | 지금(오)", fill="black", font=fb)
        cells.append(c)
    _grid(cells, 3, 2 * w + 4).save(dst, quality=88)
    return dst


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--pairs", help="예전 판과 지금 판을 나란히 볼 컷(쉼표)")
    a = ap.parse_args()
    ep = Path(a.episode_dir).resolve()
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / "검수"
    out.mkdir(parents=True, exist_ok=True)
    if a.pairs:
        print(pair_sheet(ep, a.pairs.split(","), out / f"{ep.parents[1].name}_{ep.name}_예전과지금_{time.strftime('%H%M')}.jpg"))
    else:
        for x in episode_sheets(ep, out):
            print(x)
        # 손만 나오는 컷·손이 많은 컷을 손 상자와 함께 크게 따로 모은다(16컷 시트 크기론 같은 쪽 손 둘을 놓친다, 2026-10-21 외출 p428·p468)
        from hand_check import scan
        r = scan(ep)
        if r["sheet"]:
            print(r["sheet"])


if __name__ == "__main__":
    main()
