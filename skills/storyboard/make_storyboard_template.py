"""콘티 틀(새 작품의 첫 회차용, MCP new_episode 가 복사한다). 이야기를 받아 아래 '컷' 부분만 고쳐 쓴다. 규칙은 skills/storyboard/SKILL.md.
제목·크레딧은 작품 설정(../../series.json)에서 읽는다.

    python series/<작품>/episodes/<회차>/make_storyboard.py  → storyboard.json
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = json.loads((HERE.parents[1] / "series.json").read_text(encoding="utf-8"))
EP = int(re.search(r"\d+", HERE.name).group())
ASPECT_OF = {"close": "close", "medium": "landscape", "wide": "wide", "full": "portrait", "insert": "landscape"}
CREDIT = SPEC.get("credit", "")
P = []


def add(pid, shot, scene, chars, *dlg, aspect=None, **kw):
    """컷 하나. shot: close·medium·wide·full·insert, scene: 장소 이름(series.json locations, 없으면 "none"),
    chars: 인물 id 목록, dlg: (종류, 대사, 자리[, 모양]) 여럿."""
    d = []
    for x in dlg:
        item = {"type": x[0], "text": x[1]}
        if len(x) > 2 and x[2]:
            item["pos"] = x[2]
        if x[0] == "monologue" and len(x) > 3:
            item["style"] = x[3]
        d.append(item)
    P.append({"id": pid, "shot": shot, "aspect": aspect or ASPECT_OF[shot], "mode": "drama", "scene": scene, "characters": chars, "dialogue": d, **kw})


def chat(pid, name, messages, *dlg):
    """메신저 화면 컷. messages: (나인가, 글) 목록. name: 상대 이름."""
    add(pid, "full", "none", [], *dlg, composite={"tool": "phone_ui", "kind": "chat", "name": name, "unread": False,
                                                  "messages": [m if isinstance(m, dict) else {"me": m[0], "text": m[1]} for m in messages]})


S, T, N, M = "speech", "thought", "narration", "monologue"  # 말 · 속생각 · 해설 · 독백
TL, TR, AB, BL = "top-left", "top-right", "above", "below"  # 말풍선 자리: 화면 왼쪽 인물은 TL, 오른쪽 인물은 TR

# ── 제목 카드
add("p001", "wide", "none", [], aspect="wide", gap_after=400,
    composite={"tool": "phone_ui", "kind": "title", "title": SPEC["title"], "subtitle": SPEC.get("logline", ""), "credit": CREDIT})

# ── 컷: 아래 예시를 지우고 이야기대로 쓴다(장면마다 넓은 컷 하나로 공간을 보여 주고, 대화는 어깨 너머 숏으로)
# add("p002", "wide", "<장소>", ["<인물1>", "<인물2>"], (N, "해설 한 줄.", AB))
# add("p003", "close", "<장소>", ["<인물1>"], (S, "대사.", TL))

board = {"series": SPEC["slug"], "episode": EP, "version": "v1", "title": SPEC["title"], "title_card": f"{EP}화",
         "credit": CREDIT, "color_harmonize": 0, "why": "make_storyboard.py 가 정본.", "panels": P}
(HERE / "storyboard.json").write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
print("컷", len(P), "인물 컷", sum(1 for p in P if p["characters"] and not p.get("composite")), "합성", sum(1 for p in P if p.get("composite")))
