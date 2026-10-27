"""새로 그린 컷을 한눈에 보는 점검표를 만든다. 파드 작업이 끝날 때마다 사용자가 이걸 보고 다시 그릴 컷을 고른다.

    python tools/review.py <episode_dir>                 지난 점검표 이후 새로 생기거나 바뀐 컷
    python tools/review.py <episode_dir> --ids p013,p045  고른 컷만
    python tools/review.py <episode_dir> --all            전부

<episode_dir>/review/<날짜-시각>/sheet_01.png ... 로 저장한다(한 장에 12컷, 컷 번호·샷·장면·인물·첫 대사를 적는다).
마지막 점검 시각은 <episode_dir>/review/last_review.json 에 남긴다.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_json, load_style  # noqa: E402
from compose import Fonts  # noqa: E402

COLS, ROWS = 4, 3
CELL_W, CELL_H = 420, 420
LABEL_H = 70
PAD = 14


def label_lines(p):
    who = ",".join(p.get("characters", [])) or "-"
    first = next((d["text"] for d in p.get("dialogue", []) if d.get("text")), "")
    return f"{p['id']}  {p.get('shot', '')} · {p.get('scene', '')} · {who}", first[:30]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--ids")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    ep = Path(args.episode_dir)
    panels_dir, review_dir = ep / "panels", ep / "review"
    board = {p["id"]: p for p in load_json(ep / "storyboard.json")["panels"]}
    last_file = review_dir / "last_review.json"
    since = json.loads(last_file.read_text(encoding="utf-8"))["time"] if last_file.exists() else 0
    files = {f.stem: f for f in panels_dir.glob("p*.png")}
    if args.ids:
        picked = [i for i in args.ids.split(",") if i in files]
    elif args.all:
        picked = list(files)
    else:
        picked = [i for i, f in files.items() if f.stat().st_mtime > since]
    # 콘티 순서대로(후보 p013_v2 같은 건 본 컷 뒤에)
    order = list(board)
    picked.sort(key=lambda i: (order.index(i.split("_")[0]) if i.split("_")[0] in order else 999, i))
    if not picked:
        print("새로 그린 컷이 없다")
        return

    fonts = Fonts(load_style()["lettering"]["fonts"], 20)
    out_dir = review_dir / time.strftime("%Y%m%d-%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    per = COLS * ROWS
    sheets = []
    for s in range(0, len(picked), per):
        chunk = picked[s:s + per]
        rows = (len(chunk) + COLS - 1) // COLS
        sheet = Image.new("RGB", (PAD + COLS * (CELL_W + PAD), PAD + rows * (CELL_H + LABEL_H + PAD)), "#ffffff")
        d = ImageDraw.Draw(sheet)
        for k, pid in enumerate(chunk):
            im = Image.open(files[pid]).convert("RGB")
            im.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
            x = PAD + (k % COLS) * (CELL_W + PAD)
            y = PAD + (k // COLS) * (CELL_H + LABEL_H + PAD)
            sheet.paste(im, (x + (CELL_W - im.width) // 2, y + (CELL_H - im.height) // 2))
            head, line = label_lines(board.get(pid.split("_")[0], {"id": pid}))
            if pid != pid.split("_")[0]:
                head = head.replace(pid.split("_")[0], pid, 1)
            # NanumSquareAc 는 공백이 네모로 찍혀서 Pretendard(narration) 로 쓴다
            d.text((x, y + CELL_H + 6), head, fill="#1a1a2a", font=fonts.get("narration", 1.0))
            d.text((x, y + CELL_H + 34), line, fill="#555566", font=fonts.get("narration", 0.85))
        path = out_dir / f"sheet_{s // per + 1:02d}.png"
        sheet.save(path)
        sheets.append(path)
    review_dir.mkdir(exist_ok=True)
    last_file.write_text(json.dumps({"time": time.time(), "sheets": [str(p) for p in sheets], "ids": picked},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    print("컷: " + ", ".join(picked))
    print(f"점검표 {len(sheets)}장, {len(picked)}컷: {out_dir}")


if __name__ == "__main__":
    main()
