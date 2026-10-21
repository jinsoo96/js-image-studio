"""학습한 LoRA 의 중간 파일(loras.json 의 checkpoints, lora.py train --chunk)을 같은 시드·같은 컷으로 그려 한 장에 놓고 고른다.
학습에 쓰지 않은 본편 컷(정답 = 힉스필드 원본)을 골라 정답 | LoRA 끔 | 중간 파일 × 강도 순으로 놓는다.
(2026-10-21 조사: 그림체 LoRA 는 학습 데이터 밖의 컷으로, 같은 시드로, 스텝별로 비교해 너무 배운 것(데이터 컷 베끼기·얼룩)과 덜 배운 것을 가른다)

    .venv/Scripts/python vast/lora_eval.py <작품 폴더> [style|<인물>] [--strengths 1.0] [--panels 4] [--seed 77] [--files a,b] [--full]
그림체(style)는 샷 종류가 다른 컷 넷(클로즈업 · 둘 이상 미디엄 · 사람 없는 배경 · 와이드), 인물은 그 인물이 혼자 나오는 컷을 고른다.
loras.json 은 고치지 않는다: 칸마다 WEBTOON_LORA_OVERRIDE 로 그리는 프로세스에서만 바꾼다(그림체를 볼 때는 인물 LoRA 를 끈다).
서버에서 학습이 돌고 있으면 끝날 때까지 기다렸다 그린다(학습 구간 사이에 한 장씩 끼어 한 장에 26분씩 걸렸다, 2026-10-21).
결과: exports/<날짜>/비교/<작품>_<누구>_LoRA_중간파일.jpg. 고른 파일은 lora.py set <작품> <누구> --file <파일> 로 등록한다.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
import lora  # noqa: E402


def _kind(p: dict) -> str:
    return "none" if not p.get("chars") else re.split(r"[ ,]", (p.get("shot") or "medium").strip())[0]


def held_out(series: Path, who: str, count: int) -> list:
    """[(회차, 컷)] 학습 데이터(lora/<누구>/data.json 의 from)에 없는 본편 컷. 회차 폴더에 panels_hf 원본이 있는 것만."""
    info = json.loads((series / "lora" / who / "data.json").read_text(encoding="utf-8"))
    used = {it.get("from") for it in info["items"]}
    pools = {}
    for pj in sorted((series / "episodes").glob("*/panels.json")):
        ep = pj.parent
        if "vast" in ep.name:
            continue
        d = json.loads(pj.read_text(encoding="utf-8"))
        for p in d["panels"] if isinstance(d, dict) else d:
            src = ep / "panels_hf" / f"{p['id']}.png"
            if not src.exists() or str(src.relative_to(series)).replace("\\", "/") in used or (ep / "panels_hf_vast" / src.name).exists():
                continue
            if who == lora.STYLE:
                k = _kind(p)
                key = "group" if k in ("medium", "wide") and len(p.get("chars") or []) > 1 else k
            else:
                key = "solo" if (p.get("chars") or []) == [who] else None
            if key:
                pools.setdefault(key, []).append((ep.name, p))
    order = ["close-up", "group", "none", "wide", "medium", "tight"] if who == lora.STYLE else ["solo"]
    out = []
    while len(out) < count and any(pools.get(k) for k in order):
        for k in order:
            if pools.get(k) and len(out) < count:
                lst = pools[k]
                out.append(lst.pop(len(lst) // 2))  # 회차 가운데서
    return out


def wait_training(log=print) -> None:
    """서버 큐에 학습(…TrainLora…) 작업이 돌거나 기다리는 동안 기다린다."""
    sys.path.insert(0, str(ROOT / "vast"))
    from comfy_client import Comfy
    c, said = Comfy(), False
    while True:
        q = c.req("/queue") or {}
        busy = [x for x in (q.get("queue_running") or []) + (q.get("queue_pending") or [])
                if any("TrainLora" in v.get("class_type", "") for v in x[2].values())]
        if not busy:
            return
        if not said:
            log("서버에서 학습이 돌고 있어 끝날 때까지 기다린다", flush=True)
            said = True
        time.sleep(30)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series")
    ap.add_argument("who", nargs="?", default="style")
    ap.add_argument("--strengths", default="1.0", help="쉼표로(예 0.6,0.8,1.0). LoRA 끔 칸은 늘 넣는다")
    ap.add_argument("--panels", type=int, default=4)
    ap.add_argument("--seed", type=int, default=77)
    ap.add_argument("--files", help="checkpoints 대신 이 파일들(쉼표)")
    ap.add_argument("--full", action="store_true", help="터보 없이 40스텝(최종 확인용, 느림)")
    a = ap.parse_args()
    series = Path(a.series).resolve()
    who = lora.STYLE if a.who in ("style", "그림체") else a.who
    entry = lora.registry(series).get(who) or {}
    files = a.files.split(",") if a.files else [c["file"] for c in entry.get("checkpoints") or []] or [entry.get("file")]
    files = [f for f in files if f]
    if not files:
        sys.exit(f"{who} 에 학습한 파일이 없다(lora/loras.json)")
    strengths = [float(x) for x in a.strengths.split(",")]
    picks = held_out(series, who, a.panels)
    if not picks:
        sys.exit("학습에 안 쓴 본편 컷이 없다")
    cols = [("off", None, 0.0)] + [(f"ev_{Path(f).stem.split('_steps_')[0].split('_')[-1]}_{s:g}", f, s) for f in files for s in strengths]
    by_ep = {}
    for ep, p in picks:
        by_ep.setdefault(ep, []).append(p["id"])
    print("컷:", {ep: ids for ep, ids in by_ep.items()}, "· 칸:", [c[0] for c in cols], flush=True)
    wait_training()
    for var, f, s in cols:
        ov = {k: {"on": False} for k, e in lora.registry(series).items() if isinstance(e, dict) and who == lora.STYLE and k != who}  # 그림체만 본다
        ov[who] = {"on": f is not None, **({"file": f, "strength": s, "multi": s} if f else {})}
        env = {**os.environ, "PYTHONUTF8": "1", "WEBTOON_LORA_OVERRIDE": json.dumps(ov, ensure_ascii=False)}
        for ep, ids in by_ep.items():
            cmd = [sys.executable, str(ROOT / "vast/comfy_panel.py"), str(series), str(series / "episodes" / ep / "panels.json"), "--episode", ep,
                   "--only", ",".join(ids), "--variant", var, "--seed", str(a.seed), "--no-continuity"] + ([] if a.full else ["--turbo"])
            subprocess.run(cmd, cwd=ROOT, env=env, check=False)
    font = ImageFont.truetype("C:/Windows/Fonts/malgunbd.ttf", 16)
    W = 320
    heads = [("정답(학습에 안 쓴 원본)", "hf")] + [("LoRA 끔" if f is None else f"{var.split('_')[1]}스텝 · {s:g}", var) for var, f, s in cols]
    rows = []
    for ep, p in picks:
        row = []
        for lab, var in heads:
            src = series / "episodes" / ep / ("panels_hf" if var == "hf" else f"panels_hf_{var}") / f"{p['id']}.png"
            im = Image.open(src).convert("RGB") if src.exists() else Image.new("RGB", (W, 240), "gray")
            im = im.resize((W, int(im.height * W / im.width)))
            cell = Image.new("RGB", (W, im.height + 24), "white")
            cell.paste(im, (0, 24))
            ImageDraw.Draw(cell).text((4, 2), f"{ep} {p['id']} {lab}", fill="black", font=font)
            row.append(cell)
        rows.append(row)
    sheet = Image.new("RGB", (len(heads) * (W + 4), sum(max(c.height for c in r) + 6 for r in rows)), (60, 60, 60))
    y = 0
    for r in rows:
        for k, c in enumerate(r):
            sheet.paste(c, (k * (W + 4), y))
        y += max(c.height for c in r) + 6
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / "비교" / f"{series.name}_{a.who}_LoRA_중간파일{'_40스텝' if a.full else ''}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=86)
    print(out)


if __name__ == "__main__":
    main()
