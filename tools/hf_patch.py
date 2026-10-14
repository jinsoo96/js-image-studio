"""힉스필드로 컷의 작은 한 부분만 고친다: 그 둘레를 잘라 Seedream 에 "이것만 바꿔서 똑같이" 로 다시 그리게 하고,
가운데 타원만 부드럽게 섞어 붙인다(컷 나머지는 그대로). 컷 전체를 다시 그리면 승인받은 표정·구도가 바뀌어서(2026-10-13 계급장 꺾쇠 → 상병 작대기).

    .venv/Scripts/python tools/hf_patch.py <그림.png> --at 0.65,0.68 [--size 0.09] --prompt-file <txt> [--name p014c]
    --at   고칠 곳 가운데(그림 폭·높이 비율), --size 잘라 낼 정사각형 반 변(그림 폭 비율)
    원본은 <그림 폴더>/_rejected/<이름>_patch<n>.png 로 남기고 그 자리에 덮어쓴다. 잘라 보낸 것·받은 것은 state/hf_patch/(임시, 지워도 됨).
"""
import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hf_gen import credits, generate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--at", required=True)
    ap.add_argument("--size", type=float, default=0.09)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--name")
    ap.add_argument("--model", default="seedream_v5_pro")
    args = ap.parse_args()
    src = Path(args.image)
    name = args.name or src.stem
    im = Image.open(src).convert("RGB")
    W, H = im.size
    cx, cy = (float(v) for v in args.at.split(","))
    half = round(args.size * W)
    x0 = min(max(round(cx * W) - half, 0), W - 2 * half)
    y0 = min(max(round(cy * H) - half, 0), H - 2 * half)
    box = (x0, y0, x0 + 2 * half, y0 + 2 * half)
    work = ROOT / "state" / "hf_patch"
    work.mkdir(parents=True, exist_ok=True)
    crop = im.crop(box)
    crop.resize((1024, 1024), Image.LANCZOS).save(work / f"{name}_in.png")
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    print(f"{name}: 자르기 {box}, 남은 크레딧 {credits()}", flush=True)
    generate(args.model, prompt, [work / f"{name}_in.png"], "1:1", "2k", work / f"{name}_out.png")
    new = Image.open(work / f"{name}_out.png").convert("RGB").resize(crop.size, Image.LANCZOS)
    # 고칠 곳 둘레 타원만 섞는다(가장자리는 원본 그대로라 이음새가 안 보이게). 그림 끝이라 자르기가 밀려도 타원은 고칠 곳에
    s = crop.size[0]
    mx, my = cx * W - x0, cy * H - y0
    mask = Image.new("L", crop.size, 0)
    ImageDraw.Draw(mask).ellipse((mx - s * 0.3, my - s * 0.3, mx + s * 0.3, my + s * 0.3), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(s * 0.08))
    rej = src.parent / "_rejected"
    rej.mkdir(exist_ok=True)
    n = 1
    while (rej / f"{name}_patch{n}.png").exists():
        n += 1
    shutil.copy2(src, rej / f"{name}_patch{n}.png")
    im.paste(Image.composite(new, crop, mask), box[:2])
    im.save(src)
    side = Image.new("RGB", (s * 2 + 6, s), "white")
    side.paste(crop, (0, 0))
    side.paste(Image.composite(new, crop, mask), (s + 6, 0))
    side.save(work / f"{name}_cmp.png")
    print(f"  고침 → {src} (원본 {rej / f'{name}_patch{n}.png'}), 남은 크레딧 {credits()}", flush=True)


if __name__ == "__main__":
    main()
