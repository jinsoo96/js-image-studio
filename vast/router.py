"""컷마다 그릴 판(모델·워크플로우)을 고른다. 사용자나 에이전트가 'Qwen 으로, Anima 로'를 정하지 않아도 된다.
(2026-10-20 사용자 "내가 qwen 을 쓸지 anima 를 쓸지 어떻게 알아, 알아서 골라 쓰게 해야지, 하나의 워크플로우만 쓰는 게 아니라")

판의 능력은 서버 워크플로우 데이터(extra.webtoon)에서 읽는다(판 이름을 코드에 박지 않는다):
  refs(참고 그림으로 얼굴·장소를 맞추나) · style(webtoon | anime | realistic, 없으면 label·use_for 글에서 짐작) · strengths(예: text_in_image)
  · 모델 파일이 서버에 다 있나(missing).
고르는 순서(앞에서 정해지면 그다음 칸이 고친다):
  1. 이번 작업에 지정(vast_draw model, --model) → 그대로
  2. 컷 설명에 지정(panels_spec 의 model) → 그대로
  3. 바탕 판: 작품 설정 series.json model → 없거나 "auto" 면 작품 그림체(series.json style_family, 없으면 prompts/style*.txt 글)와 같은 style 의 판 중
     참고 그림을 받는 판(작품 그림체 갈래는 series.json style_family, 없으면 webtoon) → 그림체 LoRA(loras.json _style)가 켜져 있으면 그 LoRA 를 학습한 판(그 그림체를 배운 판)
  4. 이름 있는 인물이 나오는 컷인데 바탕 판이 참고 그림을 못 받고 그 판에 그 인물 LoRA 도 없으면 → 같은 style 에서 참고 그림을 받는 판(없으면 바탕 판 그대로, 이유에 알림).
     글로만 그리면 얼굴이 컷마다 달라진다(2026-10-20 MOSS·Anima 일관성 시험)
  5. 사람 없는 컷에 그림 속 글자(text_in_image)가 필요하면 → 같은 style 에서 그 일을 잘하는 판(strengths)
한 화 안에서 style 이 다른 판은 섞지 않는다(4·5 는 같은 style 안에서만 옮긴다).
"""
import json
import re
from pathlib import Path

from saved_workflow import default_model, models

STYLE_WORDS = {"anime": r"anime|애니", "realistic": r"photo|realistic|실사|photoreal"}


def model_style(v: dict) -> str:
    meta = v.get("meta") or {}
    if meta.get("style"):
        return str(meta["style"])
    txt = f"{v.get('label', '')} {v.get('use_for', '')}"
    return next((k for k, pat in STYLE_WORDS.items() if re.search(pat, txt, re.I)), "webtoon")


def series_style(series: Path) -> str:
    """작품 그림체 갈래: series.json style_family, 없으면 webtoon. 그림체 글(style.txt)에서 짐작하지 않는다: 외출의 'realistic adult proportions'
    를 실사로, D+18 의 'anime-style eyes' 를 애니로 읽어 판을 바꿨다(2026-10-20 시험)."""
    sj = series / "series.json"
    d = json.loads(sj.read_text(encoding="utf-8")) if sj.exists() else {}
    return str(d.get("style_family") or "webtoon")


def _ready(m: dict) -> dict:
    return {k: v for k, v in m.items() if not v.get("missing")}


def base_model(series: Path, m: dict | None = None) -> tuple:
    """(바탕 판, 이유)."""
    m = m or models()
    ok = _ready(m) or m
    sj = series / "series.json"
    want = str((json.loads(sj.read_text(encoding="utf-8")) if sj.exists() else {}).get("model") or "").strip().lower()
    if want and want != "auto" and want in ok:
        return want, "작품 설정의 판"
    from lora import STYLE, registry
    st = registry(series).get(STYLE) or {}
    if st.get("on") and st.get("file") and str(st.get("model") or "").lower() in ok:
        return st["model"].lower(), "작품 그림체 LoRA 를 학습한 판"
    fam = series_style(series)
    same = [k for k, v in ok.items() if model_style(v) == fam]
    pick = sorted(same, key=lambda k: (not ok[k].get("refs"), not ok[k].get("default"), k))
    if pick:
        return pick[0], f"작품 그림체({fam})와 같은 판" + (" 중 참고 그림을 받는 판" if ok[pick[0]].get("refs") else "")
    return default_model(), f"작품 그림체({fam})에 맞는 판이 없어 기본 판"


def route(series, p: dict, forced: str | None = None) -> tuple:
    """(이 컷을 그릴 판, 이유)."""
    m = models()
    ok = _ready(m) or m
    if forced and forced.lower() in m:
        return forced.lower(), "이번 작업에 지정"
    if str(p.get("model") or "").lower() in ok:
        return p["model"].lower(), "컷 설명에 지정"
    series = Path(series)
    base, why = base_model(series, m)
    fam = model_style(ok.get(base) or m[base])
    chars = p.get("chars") or []
    if chars and not (ok.get(base) or {}).get("refs"):
        from lora import registry
        reg = registry(series)
        if not all(str((reg.get(c) or {}).get("model") or "").lower() == base and (reg.get(c) or {}).get("on") for c in chars):
            alt = sorted((k for k, v in ok.items() if v.get("refs") and model_style(v) == fam), key=lambda k: (not ok[k].get("default"), k))
            if alt:
                return alt[0], f"인물이 나와 얼굴을 맞추려면 참고 그림을 받는 판({base} 는 글로만 그린다)"
            # 그림체가 다른 판으로 옮기면 한 화 안에서 그림체가 갈린다: 바탕 판 그대로 두고 알린다
            return base, f"같은 그림체({fam})에 참고 그림을 받는 판이 없어 글로 그린다: 얼굴이 컷마다 다를 수 있다(lora_train 으로 인물 LoRA 를 학습하면 맞는다)"
    if not chars and p.get("text_in_image"):
        alt = [k for k, v in ok.items() if "text_in_image" in ((v.get("meta") or {}).get("strengths") or []) and model_style(v) == fam]
        if alt and base not in alt:
            return alt[0], "그림 속 글자가 필요한 사람 없는 컷"
    return base, why


if __name__ == "__main__":
    import sys
    s = Path(sys.argv[1]).resolve()
    d = json.loads((s / "episodes" / sys.argv[2] / "panels.json").read_text(encoding="utf-8"))
    from collections import Counter
    picks = [(p["id"],) + route(s, p) for p in (d["panels"] if isinstance(d, dict) else d)]
    print(base_model(s), Counter((x[1], x[2]) for x in picks).most_common())
