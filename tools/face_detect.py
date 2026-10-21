"""애니 얼굴 찾기(YOLOv8 ONNX, deepghs/anime_face_detection face_detect_v1.4_s, MIT, models/detect/anime_face_v1.4_s/).
lbpcascade_animeface 는 이 그림체의 남자 얼굴·옆얼굴을 거의 못 찾아서(2026-10-12 두 사람 컷 7장 중 1장) 얼굴 다듬기(render.refine_faces)에 쓴다.

    python tools/face_detect.py <image.png>     찾은 얼굴 상자를 찍는다
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "detect" / "anime_face_v1.4_s"
# 손: deepghs/anime_hand_detection hand_detect_v1.0_s(같은 YOLOv8 ONNX 꼴, OpenRAIL). tools/hand_check.py 가 쓴다
HAND_DIR = ROOT / "models" / "detect" / "anime_hand_v1.0_s"
SIZE = 640
_sessions = {}


def _sess(model_dir: Path = MODEL_DIR):
    if model_dir not in _sessions:
        import onnxruntime as ort
        _sessions[model_dir] = ort.InferenceSession(str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
    return _sessions[model_dir]


def detect(img, threshold=None, iou=0.5, model_dir: Path = MODEL_DIR):
    """[(x0, y0, x1, y1, score)] 원본 좌표, 점수 높은 순. model_dir 를 HAND_DIR 로 주면 손."""
    if threshold is None:
        threshold = json.loads((model_dir / "threshold.json").read_text())["threshold"]
    img = img.convert("RGB")
    W, H = img.size
    s = SIZE / max(W, H)
    nw, nh = round(W * s), round(H * s)
    canvas = Image.new("RGB", (SIZE, SIZE), (114, 114, 114))
    px, py = (SIZE - nw) // 2, (SIZE - nh) // 2
    canvas.paste(img.resize((nw, nh), Image.BILINEAR), (px, py))
    x = (np.asarray(canvas, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
    sess = _sess(model_dir)
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    if out.shape[0] < out.shape[1]:  # (5, N) -> (N, 5)
        out = out.T
    out = out[out[:, 4] >= threshold]
    boxes = []
    for cx, cy, w, h, sc in out[:, :5]:
        x0, y0 = (cx - w / 2 - px) / s, (cy - h / 2 - py) / s
        x1, y1 = (cx + w / 2 - px) / s, (cy + h / 2 - py) / s
        boxes.append((max(0, x0), max(0, y0), min(W, x1), min(H, y1), float(sc)))
    boxes.sort(key=lambda b: -b[4])
    keep = []
    for b in boxes:  # 겹친 상자 정리(NMS)
        if all(_iou(b, k) < iou for k in keep):
            keep.append(b)
    return keep


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0


if __name__ == "__main__":
    for b in detect(Image.open(sys.argv[1])):
        print(tuple(round(v) for v in b[:4]), round(b[4], 2))
