"""소품 찾기: YOLOX-s(COCO 80종, Megvii, Apache-2.0, models/detect/yolox_s/model.onnx). 폰·잔·컵·병·노트북·탁자·사람 상자.
(2026-10-21: 작아진 폰 · 공중에 뜬 맥주 · 잔 셋 같은 소품 결함을 그림 보는 8B 가 놓쳤다 → 세고 재는 건 검출기로)

    python tools/prop_detect.py <image.png>      찾은 소품 상자를 찍는다
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "detect" / "yolox_s" / "model.onnx"
SIZE = 640
COCO = {0: "person", 39: "bottle", 40: "wine glass", 41: "cup", 42: "fork", 43: "knife", 44: "spoon", 45: "bowl",
        56: "chair", 60: "dining table", 63: "laptop", 67: "cell phone", 73: "book"}
_sess = None


def _session():
    global _sess
    if _sess is None:
        import onnxruntime as ort
        _sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    return _sess


def detect(img: Image.Image, threshold: float = 0.35, iou: float = 0.5) -> list:
    """[(이름, x0, y0, x1, y1, 점수)] 원본 좌표. COCO 중 COCO 표에 있는 것만."""
    img = img.convert("RGB")
    W, H = img.size
    r = min(SIZE / W, SIZE / H)
    nw, nh = int(W * r), int(H * r)
    canvas = np.full((SIZE, SIZE, 3), 114, dtype=np.uint8)
    canvas[:nh, :nw] = np.asarray(img.resize((nw, nh), Image.BILINEAR))[:, :, ::-1]  # YOLOX 는 BGR, 왼쪽 위에 붙이고 0~255 그대로
    x = canvas.transpose(2, 0, 1)[None].astype(np.float32)
    out = _session().run(None, {"images": x})[0][0]
    grids, strides = [], []
    for s in (8, 16, 32):
        g = SIZE // s
        yv, xv = np.meshgrid(np.arange(g), np.arange(g), indexing="ij")
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((g * g, 1), s))
    grids, strides = np.concatenate(grids), np.concatenate(strides)
    out[:, :2] = (out[:, :2] + grids) * strides
    out[:, 2:4] = np.exp(out[:, 2:4]) * strides
    scores = out[:, 4:5] * out[:, 5:]
    cls, sc = scores.argmax(1), scores.max(1)
    keep = [(c, b, s) for c, b, s in zip(cls, out[:, :4], sc) if s >= threshold and int(c) in COCO]
    boxes = []
    for c, (cx, cy, w, h), s in keep:
        boxes.append((COCO[int(c)], max(0, (cx - w / 2) / r), max(0, (cy - h / 2) / r), min(W, (cx + w / 2) / r), min(H, (cy + h / 2) / r), float(s)))
    boxes.sort(key=lambda b: -b[5])
    final = []
    for b in boxes:  # 같은 종류끼리 겹친 상자 정리
        if all(b[0] != k[0] or _iou(b[1:5], k[1:5]) < iou for k in final):
            final.append(b)
    return final


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0


if __name__ == "__main__":
    for b in detect(Image.open(sys.argv[1])):
        print(b[0], tuple(round(v) for v in b[1:5]), round(b[5], 2))
