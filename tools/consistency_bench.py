"""판(Qwen UC · MOSS · Anima …)끼리 인물 일관성을 숫자로 견준다. 같은 인물이 혼자 나오는 컷 몇 개를 판마다 같은 시드로 그리고(comfy_panel --model),
기준 얼굴(char_check.references)과의 얼굴 거리(CCIP, 작을수록 같은 사람)·같은 판 컷끼리의 거리로 '같은 사람' 통과율을 센다.
(2026-10-20 사용자 "MOSS 테스트 방법 고도화, Qwen UC 화풍 일관성 테스트가 가능한지 확인". 문턱은 CCIP 모델의 기본값(models/detect/ccip/metrics.json),
 절차는 worklogs/2026-10-20-moss-qwen-aux.md 의 '세 판 비교 최소 절차'를 줄인 것: 지금은 인물 동일성만, 그림체 지표(CSD)는 다음)

    .venv/Scripts/python tools/consistency_bench.py <작품 폴더> <회차> <인물> [--models uc,moss,anima] [--n 6] [--seeds 77,78] [--turbo]
    → exports/<오늘>/비교/일관성_<작품>_<인물>.jpg + .json
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))


# CCIP 기본 문턱(0.178)은 '다른 캐릭터인가'를 가르는 값이라 같은 작품 안에서는 너무 너그럽다: 외출에서 사용자가 "다른 사람 같다"고 한 컷이
# 0.057~0.097, 괜찮은 컷이 0.03 안팎이었다(2026-10-20). 같은 그림체 안의 같은 사람은 이 값으로 본다
STRICT = 0.05


def pick_panels(ep: Path, who: str, n: int) -> list:
    """그 인물 혼자 나오는 컷을 샷 종류가 고르게 n 개(인서트·뒷모습 빼고)."""
    import re
    d = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    pool = {}
    for p in d["panels"] if isinstance(d, dict) else d:
        shot = (p.get("shot") or "medium").lower()
        if p.get("chars") != [who] or re.search(r"insert|detail|back view|from behind", shot + " " + str(p.get("camera") or "")):
            continue
        pool.setdefault(shot.split()[0], []).append(p["id"])
    out, keys = [], sorted(pool, key=lambda k: -len(pool[k]))
    while len(out) < n and any(pool.values()):
        for k in keys:
            if pool[k] and len(out) < n:
                out.append(pool[k].pop(len(pool[k]) // 2))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series")
    ap.add_argument("episode")
    ap.add_argument("who")
    ap.add_argument("--models", default="uc,moss,anima")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--seeds", default="77")
    ap.add_argument("--turbo", action="store_true", help="판마다 빠른 설정(터보·DMD 8스텝). 없으면 판의 기본 스텝")
    a = ap.parse_args()
    series = Path(a.series).resolve()
    ep = series / "episodes" / a.episode
    ids = pick_panels(ep, a.who, a.n)
    if not ids:
        raise SystemExit(f"{a.who} 혼자 나오는 컷이 없다")
    import qa_fix
    from char_check import threshold
    th = threshold()
    rows, t0 = {}, time.time()
    for m in a.models.split(","):
        for seed in a.seeds.split(","):
            var = f"bench_{m}_{seed}" + ("_t" if a.turbo else "")
            cmd = [sys.executable, str(ROOT / "vast/comfy_panel.py"), str(series), str(ep / "panels.json"), "--episode", ep.name, "--only", ",".join(ids),
                   "--variant", var, "--model", m, "--seed", seed] + (["--turbo"] if a.turbo else [])
            t1 = time.time()
            subprocess.run(cmd, cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"}, check=False)
            secs = time.time() - t1
            for pid in ids:
                f = ep / f"panels_hf_{var}" / f"{pid}.png"
                d = qa_fix.face_distance(series, a.who, f) if f.exists() else None
                rows.setdefault(m, []).append({"panel": pid, "seed": seed, "file": str(f), "distance": d})
            rows[m + "_secs"] = rows.get(m + "_secs", 0) + secs
    summary = {}
    for m in a.models.split(","):
        ds = [r["distance"] for r in rows.get(m, []) if r["distance"] is not None]
        found = len(ds)
        summary[m] = {"panels": len(rows.get(m, [])), "face_found": found, "same_person_rate": round(sum(d <= th for d in ds) / max(1, found), 2),
                      "strict_rate": round(sum(d <= STRICT for d in ds) / max(1, found), 2),
                      "median_distance": round(statistics.median(ds), 3) if ds else None, "secs": int(rows.get(m + "_secs", 0))}
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / "비교"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"일관성_{series.name}_{a.who}" + ("_빠른설정" if a.turbo else "")
    (out / f"{stem}.json").write_text(json.dumps({"threshold": th, "panels": ids, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    # 한 장: 행 = 판, 열 = 컷(첫 시드), 위에 기준 얼굴
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype("C:/Windows/Fonts/malgunbd.ttf", 18)
    W = 300
    ref = series / "assets" / "vast_refs" / f"{a.who}_bust.png"
    lines = [[("기준 얼굴", ref, None)]] + [[(f"{m} {r['panel']}", Path(r["file"]), r["distance"]) for r in rows.get(m, []) if r["seed"] == a.seeds.split(",")[0]]
                                          for m in a.models.split(",")]
    cells = []
    for line in lines:
        row = []
        for lab, path, d in line:
            im = Image.open(path).convert("RGB") if path.exists() else Image.new("RGB", (W, 200), "gray")
            im = im.resize((W, int(im.height * W / im.width)))
            c = Image.new("RGB", (W, im.height + 26), "white")
            c.paste(im, (0, 26))
            tag = "" if d is None else (f" {d:.3f} 같음" if d <= STRICT else f" {d:.3f} 닮음" if d <= th else f" {d:.3f} 다름")
            ImageDraw.Draw(c).text((4, 3), lab + tag, fill=(0, 110, 0) if (d is not None and d <= STRICT) else (170, 0, 0) if d is not None else "black", font=f)
            row.append(c)
        cells.append(row)
    H = sum(max(c.height for c in r) + 6 for r in cells)
    Wd = max(len(r) for r in cells) * (W + 4)
    S = Image.new("RGB", (Wd, H), (70, 70, 70))
    y = 0
    for r in cells:
        for k, c in enumerate(r):
            S.paste(c, (k * (W + 4), y))
        y += max(c.height for c in r) + 6
    S.save(out / f"{stem}.jpg", quality=86)
    for m, s in summary.items():
        print(f"{m}: 엄격 기준(≤{STRICT}) 같은 사람 {s['strict_rate'] * 100:.0f}% · 기본 문턱 {s['same_person_rate'] * 100:.0f}% "
              f"(얼굴 찾음 {s['face_found']}/{s['panels']}, 거리 중간값 {s['median_distance']}, {s['secs']}초)")
    print(out / f"{stem}.jpg", f"{int(time.time() - t0)}초")


if __name__ == "__main__":
    main()
