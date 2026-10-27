"""작업실 공용 함수: 작업실 위치, JSON 읽기, 합성 설정, 작품의 인물 영어 이름.
(옛 tools/comfy.py 의 공용 부분과 tools/hf_panel.py 의 names() 를 모음, 2026-10-27. 옛 SDXL 그리기 경로는 지움)"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_CFG = ROOT / "tools" / "compose.json"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_style():
    """합성 설정(tools/compose.json): lettering(폭·간격·배경·말풍선·글꼴), sizes(그림이 없는 컷의 자리 크기). 글꼴 상대 경로는 작업실 기준으로 푼다."""
    cfg = load_json(COMPOSE_CFG)
    fonts = cfg["lettering"].get("fonts", {})
    cfg["lettering"]["fonts"] = {k: [str(ROOT / p) if not Path(p).is_absolute() else p for p in v] for k, v in fonts.items()}
    return cfg


def names(series):
    """작품의 인물 id → 영어 이름(cast.json, '_' 로 시작하는 칸은 뺌). 컷 프롬프트가 인물을 부르는 이름."""
    cast = json.loads((Path(series) / "cast.json").read_text(encoding="utf-8"))
    return {k: v["en"] for k, v in cast.items() if not k.startswith("_")}
