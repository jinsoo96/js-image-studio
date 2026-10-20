"""같은 인물인지 기계로 확인(작붕 잡기). 컷마다 그 인물 얼굴 둘레를 잘라 CCIP(deepghs/ccip_onnx, 애니 캐릭터 동일인 판별, OpenRAIL)로
사용자가 고른 기준 얼굴(models/lora_src/cast4_picks.json)과 비교한다. 같은 인물의 다른 컷들과도 비교해, 혼자 튀는 컷을 찍는다.
(2026-10-13 사용자 "뭐 할 때마다 자꾸 작붕 일어나고 일관성 없는 게 반복" → 눈으로 고르다 놓치던 것을 기계 검사로)

    .venv/Scripts/python tools/char_check.py <episode_dir> [--only p001,p002] [--files a.png,b.png --char hero]
        → 인물별 표(컷, 기준과의 차이, 같은 인물 다른 컷들과의 차이 중간값, 판정). 차이가 클수록 다른 사람.
판정: "튐"(같은 인물의 다른 컷들보다 눈에 띄게 멂), "기준과 다름"(고른 얼굴과의 거리가 그 인물 보통보다 1.45배 넘게 멂). 컷이 4장 이상인 인물만.
인물 LoRA 를 약하게 건 인물(우빈)은 좋은 컷끼리도 멀어 기계로 못 가린다.
얼굴을 못 찾은 컷(뒷모습·소품)은 건너뛴다. 사람 눈을 대신하지는 못한다: 걸린 컷은 반드시 크게 보고 판단한다.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import ROOT, load_json  # noqa: E402

MODEL = ROOT / "models" / "detect" / "ccip"
_feat = _metr = None


def _sessions():
    global _feat, _metr
    if _feat is None:
        import onnxruntime as ort
        _feat = ort.InferenceSession(str(MODEL / "model_feat.onnx"), providers=["CPUExecutionProvider"])
        _metr = ort.InferenceSession(str(MODEL / "model_metrics.onnx"), providers=["CPUExecutionProvider"])
    return _feat, _metr


def threshold():
    return json.loads((MODEL / "metrics.json").read_text())["threshold"]


def _prep(img):
    x = np.asarray(img.convert("RGB").resize((384, 384), Image.BILINEAR), dtype=np.float32) / 255.0
    return ((x - 0.5) / 0.5).transpose(2, 0, 1)


def features(imgs):
    feat, _ = _sessions()
    out = []
    for i in range(0, len(imgs), 8):
        out.append(feat.run(None, {"input": np.stack([_prep(im) for im in imgs[i:i + 8]])})[0])
    return np.concatenate(out) if out else np.zeros((0, 768), np.float32)


def differences(f):
    _, metr = _sessions()
    return metr.run(None, {"input": f.astype(np.float32)})[0]


def char_crop(img, pick=None):
    """얼굴 둘레(머리카락·어깨까지, 얼굴 폭 3배)를 정사각으로. pick = 왼쪽부터 몇 번째 얼굴(없으면 제일 큰 얼굴). 못 찾으면 None."""
    from face_detect import detect
    faces = detect(img)
    if not faces:
        return None
    if pick is not None and len(faces) > 1:
        faces = sorted(faces, key=lambda b: b[0])
        x0, y0, x1, y1, _ = faces[min(pick, len(faces) - 1)]
    else:
        x0, y0, x1, y1, _ = max(faces, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    cx, cy, s = (x0 + x1) / 2, (y0 + y1) / 2 + (y1 - y0) * 0.35, max(x1 - x0, y1 - y0) * 3.0
    W, H = img.size
    box = (int(max(0, cx - s / 2)), int(max(0, cy - s / 2)), int(min(W, cx + s / 2)), int(min(H, cy + s / 2)))
    return img.crop(box)


def references(series_name):
    """인물별 기준 얼굴: 사용자가 고른 얼굴(models/lora_src/cast4_picks.json, 레포 밖) → 없으면 작품 에셋의 상반신 참고 그림(assets/vast_refs/<id>_bust.png).
    고른 얼굴만 보던 때 새 작품(외출·추몽)은 기준이 없어 '기준과 다름'이 비어 있었다(2026-10-20 사용자 "다른 사람처럼 보이는 경우도 있네")."""
    picks = dict(load_json(ROOT / "models" / "lora_src" / "cast4_picks.json").get(series_name, {}) if (ROOT / "models" / "lora_src" / "cast4_picks.json").exists() else {})
    for f in sorted((ROOT / "series" / series_name / "assets" / "vast_refs").glob("*_bust.png")):
        picks.setdefault(f.name[:-len("_bust.png")], str(f.relative_to(ROOT)))
    refs = {}
    for cid, rel in picks.items():
        p = ROOT / rel
        if p.exists():
            im = Image.open(p).convert("RGB")
            refs[cid] = char_crop(im) or im
    return refs


def check_episode(ep, only=None, verbose=True):
    """돌려주는 것: [(컷, 인물, 기준차이, 동료차이 중간값, 판정)]"""
    series = ep.parent.parent
    board = load_json(ep / "storyboard.json")
    refs = references(series.name)
    items = []  # (pid, cid, crop)
    for p in board["panels"]:
        if only and p["id"] not in only:
            continue
        if (p.get("composite") or {}).get("tool") in ("phone_ui", "ecg_graphic", "repeat", "sepia_flashback", "strip_panel"):
            continue
        chars = p.get("characters") or []
        f = ep / "panels" / f"{p['id']}.png"
        if len(chars) != 1 or not f.exists() or p.get("shot") in ("insert", "detail", "wide"):
            continue
        crop = char_crop(Image.open(f).convert("RGB"))
        if crop is not None:
            items.append((p["id"], chars[0], crop))
    by_char = {}
    for pid, cid, crop in items:
        by_char.setdefault(cid, []).append((pid, crop))
    rows = []
    for cid, lst in by_char.items():
        imgs = [c for _, c in lst] + ([refs[cid]] if cid in refs else [])
        feats = features(imgs)
        dm = differences(feats)
        n = len(lst)
        peers = [float(np.median([dm[i, j] for j in range(n) if j != i])) if n > 1 else float("nan") for i in range(n)]
        refd = [float(dm[i, n]) if cid in refs else float("nan") for i in range(n)]
        # 같은 인물 컷들 사이의 보통 거리보다 눈에 띄게 먼 컷만 잡는다(상대 기준). 2026-10-13 예전 작붕 컷으로 맞춤:
        # 재민 작붕 p090·p085·p081 은 동료 거리 0.037~0.099 / 기준 0.108~0.170, 좋은 컷은 0.020~0.028 / 0.044~0.081.
        # 인물 LoRA 를 약하게 건 인물(우빈 0.45)은 좋은 컷끼리도 멀어서 기계로는 못 가린다 → 눈으로 본다.
        if n >= 4:
            pm = float(np.median(peers))
            pmad = float(np.median([abs(x - pm) for x in peers]))
            rm = float(np.median(refd)) if cid in refs else float("nan")
        for i, (pid, _) in enumerate(lst):
            verdict = ""
            if n >= 4:
                if peers[i] > pm + max(3 * pmad, 0.45 * pm):
                    verdict = "튐"
                if cid in refs and refd[i] > rm * 1.45 and refd[i] > 0.09:
                    verdict = (verdict + " 기준과 다름").strip()
            rows.append((pid, cid, refd[i], peers[i], verdict))
    if verbose:
        for cid in by_char:
            print(f"== {cid}")
            for r in rows:
                if r[1] == cid:
                    print(f"  {r[0]:7s} 기준 {r[2]:.3f}  동료 {r[3]:.3f}  {r[4]}")
        flagged = [r for r in rows if r[4]]
        print(f"걸린 컷 {len(flagged)}: " + ", ".join(f"{r[0]}({r[1]} {r[4]})" for r in flagged))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--only")
    args = ap.parse_args()
    check_episode(Path(args.episode_dir), set(args.only.split(",")) if args.only else None)


if __name__ == "__main__":
    main()
