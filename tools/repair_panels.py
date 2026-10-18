"""그린 컷 손보기(하네스의 고치기 사다리를 이미 그린 컷에 다시 돌린다): 연출 확인 → 좌우만 틀리면 뒤집기, 시선·방향은 편집으로 돌리기,
표정·옷은 편집, 그래도 안 되면 걸린 점을 붙여 다시 그리기. 본편(panels_hf)의 컷을 고치고, 원본은 panels_hf/_rejected/ 에 둔다.
(2026-10-18 외출 1화: 다시 그려도 박 과장이 또 정면이었다. 편집으로 돌리면 한 번에 됐다)

    .venv/Scripts/python tools/repair_panels.py <회차 폴더> --only p004,p007
"""
import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import SIZES, graph, qwen_refs, upload  # noqa: E402
from refine import staging_pass  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--only", required=True)
    a = ap.parse_args()
    ep = Path(a.episode_dir).resolve()
    series = ep.parents[1]
    panels = {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))}
    c, cache = Comfy(), {}
    stamp = time.strftime("%m%d%H%M")
    (ep / "panels_hf" / "_rejected").mkdir(parents=True, exist_ok=True)
    for pid in a.only.split(","):
        p, dst = panels[pid], ep / "panels_hf" / f"{pid}.png"
        shutil.copy2(dst, ep / "panels_hf" / "_rejected" / f"{pid}_repair_{stamp}.png")
        prompt, refs = qwen_refs(series, p, expr=True)
        w, h = SIZES[p.get("aspect", "16:9")]

        def render(sd, turned=False, note=""):
            pr, rf = (qwen_refs(series, p, expr=False, turned=True) if turned else (prompt, refs))
            if note:
                pr += "\n\nTHE LAST ATTEMPT HAD THESE MISTAKES, AVOID THEM: " + note
            hist = c.wait(c.queue(graph(pr, [upload(c, r, cache) for r in rf], w, h, sd, 8, 1024, True)), 1200)
            c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), dst)

        staging_pass(c, cache, dst, series, ep, p, random.randrange(2 ** 31), render, 1, lambda m: print(m, flush=True))
        (ep / "panels" / f"{pid}.png").unlink(missing_ok=True)  # 다음 합성 때 새 그림으로 놓는다


if __name__ == "__main__":
    main()
