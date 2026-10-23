"""힉스필드 판: panels_hf/<id>.png 를 합성용 panels/<id>.png 로(가로 1600). 합성 재료(TV 화면 속 무대 등, id 가 콘티에 없고 parts/<id>.png 로 쓰이는 것)는 parts/ 로.
이미 놓인 시험 컷·장소 컷은 panels.json 에 없어 건드리지 않는다.

    python tools/hf_place.py <episode_dir>
"""
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if len(sys.argv) < 2:  # 예전엔 회차를 안 주면 D+18 1화를 건드렸다(작품을 박지 않는다, 2026-10-24)
    raise SystemExit(__doc__)
E = Path(sys.argv[1])
board = {p["id"] for p in json.loads((E / "storyboard.json").read_text(encoding="utf-8"))["panels"]}
miss = []
for p in json.loads((E / "panels.json").read_text(encoding="utf-8")):
    src = E / "panels_hf" / f"{p['id']}.png"
    if not src.exists():
        miss.append(p["id"]); continue
    dst = E / ("panels" if p["id"] in board else "parts") / f"{p['id']}.png"
    dst.parent.mkdir(exist_ok=True)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        continue
    im = Image.open(src).convert("RGB")
    im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS).save(dst)
print("빠진 컷", miss)
