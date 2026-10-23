"""_old 폴더 비우기: 작업실의 _old/ 안 파일은 처음 본 날로부터 10일이 지나면 지운다(2026-10-23 사용자: "실험용·안 쓰는 파일은 _old 로
옮기는 건 괜찮은데 용량만 차지하니 10일 뒤에 지우는 로직").

    python tools/clean_old.py              지울 것·남은 날만 찍는다(지우지 않음)
    python tools/clean_old.py --apply      10일 지난 것을 지운다(예약작업 WEBTOON-clean-old 가 매일 부른다)
    python tools/clean_old.py --days 14    기준 날 수 바꾸기

- 대상: series/<작품>/ · exports/ · worklogs/ 아래 이름이 정확히 '_old' 인 폴더 안의 파일만. _rejected(본편에서 밀려난 판)·_stage1(1단 판)은
  컷 되돌리기(panel_versions)가 쓰는 이력이라 건드리지 않는다.
- 옮긴 파일은 고친 시각이 옛날 그대로라 그 시각으론 언제 _old 에 들어왔는지 모른다. 그래서 장부(state/old_ledger.json)에 처음 본 시각을 적고
  그때부터 센다. MCP move_file 이 _old 로 옮길 때 바로 적고, 손으로 옮긴 것은 이 스크립트가 처음 본 날로 적는다(그래서 늦어도 10일은 남는다).
- 지운 파일은 state/logs/clean_old.log 에 남기고, 비게 된 폴더도 치운다.
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


def old_dirs():
    for area in AREAS:
        if not area.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(area):
            if os.path.basename(dirpath) == "_old":
                dirnames[:] = []  # 안쪽은 아래 rglob 이 본다
                yield Path(dirpath)
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]


def load() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def note(paths, when: float | None = None):
    """_old 안에 새로 들어온 파일들의 처음 본 시각을 장부에 적는다(이미 있으면 그대로). MCP move_file 도 이걸 부른다."""
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
    ap.add_argument("--apply", action="store_true", help="10일 지난 파일을 실제로 지운다")
    ap.add_argument("--days", type=float, default=10)
    a = ap.parse_args()
    now = time.time()
    led = load()
    files = [f for d in old_dirs() for f in d.rglob("*") if f.is_file() and not f.is_symlink()]
    keys = {f.resolve().relative_to(ROOT).as_posix(): f for f in files}
    for k in keys:
        led.setdefault(k, now)  # 처음 본 파일은 지금부터 센다
    led = {k: v for k, v in led.items() if k in keys}  # 없어진 파일(손으로 지우거나 되살림)은 장부에서 뺀다
    due = sorted(k for k, v in led.items() if now - v >= a.days * 86400)
    size = sum(keys[k].stat().st_size for k in due)
    total = sum(f.stat().st_size for f in files)
    print(f"_old 폴더 {len({f.parent for f in files})}곳 · 파일 {len(files)}개 {total // 2**20}MB · {a.days:g}일 지난 것 {len(due)}개 {size // 2**20}MB")
    if a.apply and due:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as log:
            for k in due:
                keys[k].unlink()
                led.pop(k, None)
                log.write(f"{time.strftime('%Y-%m-%d %H:%M')} 지움 {k}\n")
        for d in sorted({keys[k].parent for k in due}, key=lambda p: len(p.parts), reverse=True):  # 비게 된 폴더(_old 자신은 남김)
            while d.name != "_old" and d.exists() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        print(f"지웠다 {len(due)}개 {size // 2**20}MB (기록 {LOG.relative_to(ROOT)})")
    elif due:
        print("지울 것(--apply 로 지운다):", *due[:20], sep="\n  ")
    nxt = sorted((v + a.days * 86400, k) for k, v in led.items() if k not in due)
    if nxt:
        print(f"다음으로 지울 날: {time.strftime('%Y-%m-%d', time.localtime(nxt[0][0]))} ({nxt[0][1]} 등)")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
