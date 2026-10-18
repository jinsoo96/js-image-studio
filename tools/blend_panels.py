"""두 그림을 겹쳐 한 컷으로(겹쳐 보이기·이중 노출). 그림 모델이 한 장에 못 그리는 연출용(2026-10-18 추몽 p022: 46세 얼굴 위로 17세 얼굴이 반투명하게).

    .venv/Scripts/python tools/blend_panels.py <episode_dir> <컷 id> <밑 그림> <겹칠 그림> [--alpha 0.42] [--soft 0.35]
밑 그림 크기에 맞춰 겹칠 그림을 맞추고, 가운데는 alpha 만큼, 가장자리로 갈수록 옅게(soft = 옅어지는 폭) 겹친다.
결과는 <episode>/panels_hf/<컷 id>.png (원래 그림은 panels_hf/_rejected/ 에 둔다). 그다음 make_episode 를 다시 부르면 놓기·합성이 이어진다.
"""
import argparse
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image


def blend(base: Image.Image, over: Image.Image, alpha: float, soft: float) -> Image.Image:
    over = over.convert("RGB").resize(base.size, Image.LANCZOS)
    w, h = base.size
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)  # 가운데 0, 모서리 약 1.4
    m = np.clip((1.0 - r) / max(soft, 1e-3), 0, 1) * alpha
    a, b = np.asarray(base.convert("RGB"), dtype=np.float32), np.asarray(over, dtype=np.float32)
    out = a * (1 - m[..., None]) + b * m[..., None]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("pid")
    ap.add_argument("base")
    ap.add_argument("over")
    ap.add_argument("--alpha", type=float, default=0.42)
    ap.add_argument("--soft", type=float, default=0.35)
    a = ap.parse_args()
    ep = Path(a.episode_dir)
    base, over = Image.open(ep / a.base), Image.open(ep / a.over)
    dst = ep / "panels_hf" / f"{a.pid}.png"
    if dst.exists():
        rej = ep / "panels_hf" / "_rejected"
        rej.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, rej / f"{a.pid}_before_blend_{time.strftime('%m%d%H%M')}.png")
    blend(base, over, a.alpha, a.soft).save(dst)
    for stale in (ep / "panels" / f"{a.pid}.png",):  # 놓은 그림은 다시 놓게 지운다(hf_place 가 panels_hf 에서 다시 만든다)
        if stale.exists():
            stale.unlink()
    print("겹침", dst, f"alpha {a.alpha} soft {a.soft}")


if __name__ == "__main__":
    main()
