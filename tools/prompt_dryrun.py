"""작품마다 그리기 지시·참고 그림을 GPU 없이 만들어 보고 문제를 찾는다(그림 안 그림, 파일 안 씀, 돈 0).
판정 서버가 꺼져 있으면 얼굴 방향 고르기(person_views)는 건너뛴다. 도구를 고친 뒤 그림을 그리기 전에 먼저 돌린다.

    .venv/Scripts/python tools/prompt_dryrun.py [--per-episode 5]

작품·회차마다 인물이 있는 컷을 고르게 몇 개 골라 vast/comfy_panel.qwen_refs 로 지시를 만들고 본다:
  - 지시가 비었거나 참고 그림 파일이 없다
  - 컷 인물의 이름이 지시에 없다(그리기 모델이 그 사람을 모른다)
  - 다른 작품의 제목·인물 이름·장소 키가 섞였다(작품 사이 새어 듦). 이름은 tools/hardcode_scan.names_of 로 각 작품 설정에서 모은다
  - 장면 배치(SPACE)가 컷에 없는 인물을 부르는데 '화면 밖' 안내가 없다(off_frame_note)
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-episode", type=int, default=5, help="회차마다 볼 컷 수(인물 컷을 고르게)")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    import comfy_panel as C
    import hardcode_scan
    from hf_panel import names

    series = sorted(p for p in (ROOT / "series").iterdir() if p.is_dir() and not p.name.startswith("_"))
    own = {sd.name: hardcode_scan.names_of(sd) for sd in series}
    bad, n, t0 = [], 0, time.time()
    for sd in series:
        mine = set(own[sd.name])
        # 다른 작품 이름: 따옴표 키·폴더·slug(코드용)는 빼고 사람이 읽는 이름만. 이 작품에도 있는 이름은 빼고
        foreign = {t: s for s, d in own.items() if s != sd.name for t, what in d.items()
                   if what in ("작품 제목", "인물 이름", "장소 키") and t not in mine and len(t) >= 3}
        NAMES = names(sd)
        for ep in sorted(p for p in (sd / "episodes").iterdir() if p.is_dir() and (p / "panels.json").exists()):
            spec = [p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8")) if p.get("chars")]
            k = a.per_episode
            pick = [spec[int(i * (len(spec) - 1) / (k - 1))] for i in range(k)] if len(spec) > k else spec
            for p in pick:
                n += 1
                where = f"{sd.name} {ep.name} {p['id']}"
                try:
                    prompt, refs = C.qwen_refs(sd, p, ep.name, expr=True)
                except Exception as e:  # noqa: BLE001
                    bad.append(f"{where}: 지시 만들기 오류 {type(e).__name__}: {e}"[:220])
                    continue
                if not prompt.strip():
                    bad.append(f"{where}: 빈 지시")
                miss = [str(r) for r in refs if isinstance(r, Path) and not r.exists()]
                if miss:
                    bad.append(f"{where}: 참고 그림 없음 {miss[:2]}")
                for c in p["chars"]:
                    short = re.sub(r"\s*\(.*?\)", "", NAMES.get(c, c)).split()[-1]
                    if not re.search(rf"\b{re.escape(short)}\b", prompt):
                        bad.append(f"{where}: 컷 인물 {c}({short}) 이 지시에 없다")
                leak = sorted(t for t in foreign if t in prompt)
                if leak:
                    bad.append(f"{where}: 다른 작품 이름이 섞임 {leak[:3]}")
                kept = re.search(r"SPACE \(identical in every panel of this scene\):", prompt)
                if kept and C.off_frame_note(prompt[kept.start():], p["chars"], NAMES) and "outside the frame" not in prompt:
                    bad.append(f"{where}: 배치가 컷 밖 인물을 부르는데 '화면 밖' 안내가 없다")
        print(f"{sd.name}: 지금까지 지시 {n}개", flush=True)
    print(f"\n그리기 지시 {n}개({time.time() - t0:.0f}초), 문제 {len(bad)}")
    for b in bad:
        print("  문제:", b)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
