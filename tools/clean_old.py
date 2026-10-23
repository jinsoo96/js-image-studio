"""_tmp 폴더 비우기와 _old 폴더 보고.
- _tmp/ : 시험용 결과물을 두는 곳. 처음 본 날로부터 10일이 지나면 지운다(예약작업 WEBTOON-clean-old 가 매일 --apply).
- _old/ : 안 쓰게 된 옛 판(시트·장소·참고 그림 등). 자동으로 지우지 않는다. 10일 넘은 것을 목록으로만 알리고,
  지우는 건 사람이 시킬 때만(--include-old). 2026-10-23 사용자: "자동 10일은 위험하다, 테스트 파일이면 몰라도".

    python tools/clean_old.py                      _tmp·_old 크기와 10일 넘은 것만 찍는다(지우지 않음)
    python tools/clean_old.py --apply              _tmp 안 10일 지난 것을 지운다
    python tools/clean_old.py --apply --include-old   _old 안 10일 지난 것까지 지운다(사용자가 시켰을 때만)
    python tools/clean_old.py --days 14            기준 날 수 바꾸기

- 대상은 series/<작품>/ · exports/ · worklogs/ 아래 이름이 정확히 '_tmp'·'_old' 인 폴더 안의 파일뿐. _rejected(본편에서 밀려난 판)·
  _stage1(1단 판)은 컷 되돌리기(panel_versions)가 쓰는 이력이라 건드리지 않는다.
- 옮긴 파일은 고친 시각이 옛날 그대로라 그 시각으론 언제 들어왔는지 모른다. 그래서 장부(state/old_ledger.json)에 처음 본 시각을 적고
  그때부터 센다. MCP move_file 이 옮길 때 바로 적고, 손으로 옮긴 것은 이 스크립트가 처음 본 날로 적는다(그래서 늦어도 10일은 남는다).
- 지운 파일은 state/logs/clean_old.log 에 남기고, 비게 된 폴더도 치운다(_tmp·_old 자신은 남김).
"""
import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "state" / "old_ledger.json"
LOG = ROOT / "state" / "logs" / "clean_old.log"
AREAS = [ROOT / "series", ROOT / "exports", ROOT / "worklogs"]
AUTO, KEEP = "_tmp", "_old"  # 저절로 지우는 곳 · 보고만 하는 곳


def bins():
    """(폴더 이름, 폴더 경로): 작업실 저장소 안의 _tmp·_old 폴더."""
    for area in AREAS:
        if not area.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(area):
            name = os.path.basename(dirpath)
            if name in (AUTO, KEEP):
                dirnames[:] = []  # 안쪽은 아래 rglob 이 본다
                yield name, Path(dirpath)
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]


def load() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def note(paths, when: float | None = None):
    """_tmp·_old 안에 새로 들어온 파일들의 처음 본 시각을 장부에 적는다(이미 있으면 그대로). MCP move_file 도 이걸 부른다."""
    led = load()
    t = when or time.time()
    for p in paths:
        for f in ([p] if p.is_file() else p.rglob("*")):
            if f.is_file():
                led.setdefault(f.resolve().relative_to(ROOT).as_posix(), t)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=0), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="_tmp 안 10일 지난 파일을 실제로 지운다")
    ap.add_argument("--include-old", action="store_true", help="_old 안 10일 지난 것까지 지운다(사용자가 시켰을 때만)")
    ap.add_argument("--days", type=float, default=10)
    a = ap.parse_args()
    now = time.time()
    led = load()
    kind, files = {}, []
    for name, d in bins():
        for f in d.rglob("*"):
            if f.is_file() and not f.is_symlink():
                files.append(f)
                kind[f.resolve().relative_to(ROOT).as_posix()] = name
    keys = {f.resolve().relative_to(ROOT).as_posix(): f for f in files}
    for k in keys:
        led.setdefault(k, now)  # 처음 본 파일은 지금부터 센다
    led = {k: v for k, v in led.items() if k in keys}  # 없어진 파일(손으로 지우거나 되살림)은 장부에서 뺀다
    old_enough = {k for k, v in led.items() if now - v >= a.days * 86400}
    for name in (AUTO, KEEP):
        mine = [k for k in keys if kind[k] == name]
        due = [k for k in mine if k in old_enough]
        print(f"{name}: 파일 {len(mine)}개 {sum(keys[k].stat().st_size for k in mine) // 2**20}MB · {a.days:g}일 지난 것 {len(due)}개 "
              f"{sum(keys[k].stat().st_size for k in due) // 2**20}MB" + (" (자동으로 지우지 않음)" if name == KEEP else ""))
    targets = sorted(k for k in old_enough if kind[k] == AUTO or (a.include_old and kind[k] == KEEP))
    if a.apply and targets:
        size = sum(keys[k].stat().st_size for k in targets)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as log:
            for k in targets:
                keys[k].unlink()
                led.pop(k, None)
                log.write(f"{time.strftime('%Y-%m-%d %H:%M')} 지움 {k}\n")
        for d in sorted({keys[k].parent for k in targets}, key=lambda p: len(p.parts), reverse=True):  # 비게 된 폴더
            while d.name not in (AUTO, KEEP) and d.exists() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        print(f"지웠다 {len(targets)}개 {size // 2**20}MB (기록 {LOG.relative_to(ROOT)})")
    old_due = sorted(k for k in old_enough if kind[k] == KEEP)
    if old_due and not (a.apply and a.include_old):
        print(f"_old 에서 {a.days:g}일 넘은 것(지우려면 사용자 확인 뒤 --apply --include-old):", *old_due[:20], sep="\n  ")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
