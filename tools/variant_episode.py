"""회차의 다른 판(같은 콘티·말풍선, 그림만 다른 폴더)을 따로 합성해 내보낸다. 본편 그림(panels_hf)·내보내기는 건드리지 않는다.
(2026-10-22 사용자 "무대 뒤 1화, D+18 1·2화를 리스타일 쓴 판으로 힉스필드만큼 만들어서 오늘 폴더에")

    .venv/Scripts/python tools/variant_episode.py <series_hf_dir> <회차> <그림 폴더 이름> <판 이름> [--fill-from 폴더]

예) tools/variant_episode.py series/mudae-dwi-hf ep001 panels_hf_vast_restyle restyle
→ episodes/ep001_restyle/ 에 콘티·컷 설명·말풍선 자료를 복사하고 panels_hf/ 를 그 그림 폴더로 채운 뒤
  컷 놓기 → 말풍선 합성·PDF → 기계·말풍선 점검 → 예고편·epub → exports/<오늘>/<제목>_<부제>_리스타일판.*
그림 폴더에 없는 컷은 멈추고 알린다(--fill-from 을 주면 그 폴더 그림으로 채운다: 재사용 컷 등).
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from make_episode import PY, TOOLS, export, run  # noqa: E402

SKIP = {"out", "review", "__pycache__"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("episode")
    ap.add_argument("folder", help="그림 폴더(회차 안), 예: panels_hf_vast_restyle")
    ap.add_argument("variant", help="판 이름: restyle · vast")
    ap.add_argument("--fill-from", default="", help="그림 폴더에 없는 컷을 채울 폴더(회차 안)")
    a = ap.parse_args()
    series = Path(a.series_dir).resolve()
    src = series / "episodes" / a.episode
    dst = series / "episodes" / f"{a.episode}_{a.variant}"
    dst.mkdir(exist_ok=True)
    for f in src.iterdir():  # 콘티·컷 설명·말풍선 자료(그림 폴더·내보내기 제외)
        if f.name in SKIP or f.name.startswith("panels_hf"):
            continue
        if f.is_dir():
            shutil.copytree(f, dst / f.name, dirs_exist_ok=True)
        else:
            shutil.copy2(f, dst / f.name)
    pj = json.loads((src / "panels.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in (pj["panels"] if isinstance(pj, dict) else pj)]
    hf = dst / "panels_hf"
    hf.mkdir(exist_ok=True)
    missing, filled = [], []
    for i in ids:
        f = src / a.folder / f"{i}.png"
        if not f.exists() and a.fill_from and (src / a.fill_from / f"{i}.png").exists():
            f = src / a.fill_from / f"{i}.png"
            filled.append(i)
        if not f.exists():
            missing.append(i)
            continue
        shutil.copy2(f, hf / f"{i}.png")
    print(f"{dst.name}: 컷 {len(ids) - len(missing)}/{len(ids)}" + (f", {a.fill_from} 로 채움 {len(filled)}" if filled else ""), flush=True)
    if missing:
        raise SystemExit(f"그림이 없는 컷: {','.join(missing)}(그 폴더에 그린 뒤 다시 부르거나 --fill-from)")
    run([PY, TOOLS / "hf_place.py", dst], "컷 놓기", capture=True)
    run([PY, TOOLS / "run_episode.py", dst], "말풍선 합성·PDF", capture=True)
    run([PY, TOOLS / "qa_gate.py", dst], "기계 점검", allow_fail=True, capture=True)
    run([PY, TOOLS / "check_bubbles.py", dst], "말풍선 점검", allow_fail=True, capture=True)
    export(series, dst)


if __name__ == "__main__":
    main()
