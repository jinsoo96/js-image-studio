"""콘티 표(storyboard_spec.json) → storyboard.json. 에이전트가 파이썬(make_storyboard.py)을 쓰지 않고 MCP set_storyboard 로 컷 목록만 넣는다.
(2026-10-23 미사일 스튜디오 시험: 새 작품 콘티를 쓰려고 27B 모델이 파이썬 파일 전체를 write_file 로 쓰려 했다. 코드는 깨지기 쉽고 덮어쓰기 권한도 필요했다.
컷 설명 표 panels_spec.json + panels_from_spec.py 와 같은 방식)

    .venv/Scripts/python tools/storyboard_from_spec.py <episode_dir>

storyboard_spec.json:
  {"title_card": "1화 「부제」"(선택), "cuts": [
     {"id": "p002", "shot": "wide|medium|close|full|insert", "scene": "<장소 이름 또는 none>", "chars": ["<인물 id>"],
      "dialogue": [{"type": "speech|thought|narration|monologue|sfx", "text": "…", "who": "<인물 id>", "pos": "top-left|top-right|above|below"}],
      "gap_after": 400(선택, 장면이 바뀌는 곳), "aspect": "close|landscape|wide|portrait"(선택), "composite": {…}(선택, 메신저·카드 등)}]}
  dialogue 한 줄은 ["speech", "대사", "top-left", "who"] 처럼 목록이어도 된다. 표지(제목 카드)가 없으면 맨 앞에 p000 으로 넣는다.
"""
import json
import re
import sys
from pathlib import Path

ASPECT_OF = {"close": "close", "medium": "landscape", "wide": "wide", "full": "portrait", "insert": "landscape"}
SHOTS = {"close": "close", "close-up": "close", "closeup": "close", "extreme close-up": "close", "medium": "medium", "medium shot": "medium",
         "wide": "wide", "wide shot": "wide", "establishing": "wide", "full": "full", "full shot": "full", "insert": "insert", "insert shot": "insert",
         "detail": "insert"}
TYPES = {"speech": "speech", "말": "speech", "대사": "speech", "thought": "thought", "생각": "thought", "속마음": "thought",
         "narration": "narration", "caption": "narration", "해설": "narration", "자막": "narration", "monologue": "monologue", "독백": "monologue",
         "sfx": "sfx", "효과음": "sfx"}


def norm_line(x) -> dict:
    """대사 한 줄을 storyboard 의 dialogue 항목으로. 목록 [종류, 글, 자리, 말하는 인물] 이나 객체를 받는다."""
    if isinstance(x, (list, tuple)):
        x = {"type": x[0], "text": x[1], **({"pos": x[2]} if len(x) > 2 and x[2] else {}), **({"who": x[3]} if len(x) > 3 and x[3] else {})}
    d = dict(x)
    d["type"] = TYPES.get(str(d.get("type", "speech")).strip().lower(), "speech")
    d["text"] = str(d.get("text", "")).strip()
    if d["type"] == "narration" and "box" not in d:
        d["box"] = True  # 해설은 네모 캡션(서술어 없는 '1932년, 경성' 같은 줄도 표지판 검사에 걸리지 않게)
    if d["type"] == "sfx":
        d.setdefault("pos", "top-right")
        d.setdefault("scale", 1.5)
    return {k: v for k, v in d.items() if v not in ("", None)}


def norm_cut(c: dict) -> dict:
    shot = SHOTS.get(str(c.get("shot", "medium")).strip().lower(), "medium")
    out = {"id": c["id"], "shot": shot, "aspect": c.get("aspect") or ASPECT_OF[shot], "mode": "drama", "scene": c.get("scene") or c.get("location") or "none",
           "characters": list(c.get("chars") or c.get("characters") or []),
           "dialogue": [norm_line(x) for x in (c.get("dialogue") or c.get("lines") or [])]}
    for k in ("gap_after", "composite"):
        if c.get(k):
            out[k] = c[k]
    # 옛 콘티에서 옮겨 온 컷의 다른 칸(prompt·layout·count 등 옛 도구가 읽는 것)은 그대로 둔다
    out.update({k: v for k, v in c.items() if k not in out and k not in ("chars", "lines", "location", "mode")})
    return out


def build(ep: Path) -> dict:
    spec = json.loads((ep / "storyboard_spec.json").read_text(encoding="utf-8"))
    sj = json.loads((ep.parents[1] / "series.json").read_text(encoding="utf-8-sig")) if (ep.parents[1] / "series.json").exists() else {}
    n = int(re.search(r"\d+", ep.name).group())
    cuts = [norm_cut(c) for c in spec.get("cuts", [])]
    if spec.get("cover", True) and not (cuts and cuts[0].get("composite")):
        cuts.insert(0, {"id": "p000", "shot": "wide", "aspect": "wide", "mode": "drama", "scene": "none", "characters": [], "dialogue": [], "gap_after": 400,
                        "composite": {"tool": "phone_ui", "kind": "title", "title": sj.get("title", ep.parents[1].name),
                                      "subtitle": spec.get("title_card") or sj.get("logline", ""), "credit": sj.get("credit", "")}})
    board = {"series": sj.get("slug", ep.parents[1].name), "episode": n, "version": re.search(r"_(v\d+)", ep.name).group(1) if "_v" in ep.name else "v1",
             "title": sj.get("title", ""), "title_card": spec.get("title_card") or f"{n}화", "credit": sj.get("credit", ""), "color_harmonize": 0,
             "why": "storyboard_spec.json(MCP set_storyboard)이 정본, tools/storyboard_from_spec.py 가 만든다.", "panels": cuts}
    (ep / "storyboard.json").write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    return board


if __name__ == "__main__":
    b = build(Path(sys.argv[1]).resolve())
    P = b["panels"]
    print("컷", len(P), "인물 컷", sum(1 for p in P if p["characters"] and not p.get("composite")), "합성", sum(1 for p in P if p.get("composite")))
