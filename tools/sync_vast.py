"""vast 판에서 다시 그린 컷을 본편(panels_hf)에 넣는다. 예전 본편 그림은 panels_hf/_rejected/<id>_<시각>.png 로 옮긴다.
(2026-10-20: make_episode 는 본편 그림이 없을 때만 vast 판을 복사해, 장면 기준판·검사 고치기로 다시 그린 166컷과 전날 다시 그린 13컷이
 PDF 에 안 들어갔다. 사용자가 본 p005 화난 은영·p436 아이스 스케이트는 예전 그림이었다)

    .venv/Scripts/python tools/sync_vast.py <회차 폴더> [--only p005,p436] [--all-newer]

--only: 그 컷들을 넣는다(다시 그리는 도구가 컷마다 부른다). --all-newer: vast 판이 본편보다 새로운 컷을 모두.
합성용 panels/ 는 place_panels.py(합성 단계)가 본편이 더 새로우면 다시 만든다.
"""
import argparse
import shutil
import time
from pathlib import Path


def place(ep: Path, pid: str) -> bool:
    """vast 판 <id> 를 본편으로. 본편과 같은 그림이면 하지 않는다. 했으면 True."""
    src = ep / "panels_hf_vast" / f"{pid}.png"
    dst = ep / "panels_hf" / f"{pid}.png"
    if not src.exists():
        return False
    if dst.exists():
        if dst.read_bytes() == src.read_bytes():
            return False
        rej = ep / "panels_hf" / "_rejected"
        rej.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(rej / f"{pid}_{time.strftime('%Y%m%d-%H%M%S')}.png"))
    dst.parent.mkdir(exist_ok=True)
    shutil.copy2(src, dst)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--only")
    ap.add_argument("--all-newer", action="store_true")
    a = ap.parse_args()
    ep = Path(a.episode_dir)
    if a.only:
        ids = a.only.split(",")
    elif a.all_newer:
        ids = [f.stem for f in sorted((ep / "panels_hf_vast").glob("p*.png"))
               if not (ep / "panels_hf" / f.name).exists() or f.stat().st_mtime > (ep / "panels_hf" / f.name).stat().st_mtime]
    else:
        raise SystemExit("--only 또는 --all-newer")
    done = [i for i in ids if place(ep, i)]
    print(f"본편에 넣음 {len(done)}컷: {','.join(done)}")


if __name__ == "__main__":
    main()
