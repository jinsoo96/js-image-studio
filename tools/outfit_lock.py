"""장면 옷 고정: 같은 장면 안에서 한 인물의 모자·안경·장갑이 컷마다 바뀌지 않게 컷 설명(panels.json 의 outfit)을 맞춘다.
(2026-10-27 사용자 "이런 일이 안 생기게 도구를 고쳐야지": 개항 1화 v4 검수에서 이어짐 12짝. p008a 는 'no hat' 인데 다음 컷 p009 는 모자 말이 없어
 그리기 모델이 작품 시대 규칙('men in fedoras')대로 중절모를 씌웠고, p027a→p028 의 학생모도 같았다. 그림이 아니라 글의 빈칸이 원인이었다.)

    .venv/Scripts/python tools/outfit_lock.py <회차 폴더> [--write]     바꿀 것을 보이고, --write 면 panels.json 에 쓴다

규칙(작품 이름·문구를 박지 않는다: skills/panel-spec/outfit_rules.json + 작품 series.json 의 outfit_rules):
- 장면 = 컷의 scene(없으면 location). 콘티 순서대로 본다.
- 소품 갈래(모자·안경·장갑)마다 컷 글이 '있다'(그 낱말) / '없다'(no …·bare-headed) / 말 없음 중 하나.
- 말 없는 컷은 같은 장면에서 앞서 말한 컷의 그 구절을 이어받는다. 장면 첫 컷까지 말이 없으면 인물 생김새(series.json cast 의 who)에
  그 갈래 낱말이 있으면 그 짧은 구절(예 'round wire-rimmed glasses')을, 없으면 갈래의 '없음' 구절(예 'bare-headed')을 붙인다.
  '없음' 구절은 긍정꼴로만 쓴다: 그리기 지시의 부정문('no glasses')은 오히려 그것을 그리게 했다는 보고가 있어 비워 둔 갈래는 붙이지 않는다.
- 손에 든 것('cap in hand')은 쓴 것이 아니다: 다음 컷엔 '없음'으로 잇는다.
- 같은 장면에서 '있다'와 '없다'가 컷마다 엇갈리는데 그 컷의 행동·연기에 벗고 쓰는 말(change_verbs)이 없으면 conflicts 로 알린다(관문 lint).
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "skills" / "panel-spec" / "outfit_rules.json"


def load_rules(series: Path | None = None) -> dict:
    """공용 규칙에 작품 series.json 의 outfit_rules 를 합친다(갈래를 더하거나 낱말을 늘린다)."""
    r = json.loads(RULES.read_text(encoding="utf-8"))
    extra = {}
    if series is not None and (series / "series.json").exists():
        extra = json.loads((series / "series.json").read_text(encoding="utf-8")).get("outfit_rules") or {}
    for name, c in (extra.get("categories") or {}).items():
        base = r["categories"].setdefault(name, {"words": [], "negatives": [], "none": ""})
        base["words"] = sorted(set(base.get("words", [])) | set(c.get("words", [])))
        base["negatives"] = sorted(set(base.get("negatives", [])) | set(c.get("negatives", [])))
        if c.get("none"):
            base["none"] = c["none"]
    for k in ("negation", "change_verbs"):
        r[k] = sorted(set(r.get(k, [])) | set(extra.get(k, [])))
    return r


def looks(series: Path | None) -> dict:
    """인물 id → 생김새 글(series.json cast 의 who, 없으면 cast.json 의 글). 안경처럼 그 사람 얼굴의 것인 소품을 컷마다 같은 말로 적으려고.
    기본 옷(main)은 넣지 않는다: 컷 글이 옷을 적었는데 모자만 말이 없으면 그 장면엔 안 쓴 것으로 본다(실내 컷에 학생모를 씌우지 않게)."""
    out = {}
    if series is None:
        return out
    sj = series / "series.json"
    if sj.exists():
        cast = json.loads(sj.read_text(encoding="utf-8")).get("cast") or []
        items = cast.items() if isinstance(cast, dict) else [tuple(x) for x in cast if isinstance(x, (list, tuple)) and len(x) == 2]
        for cid, v in items:
            if isinstance(v, dict):
                out[cid] = " ".join(str(v.get(k) or "") for k in ("who", "look", "appearance"))
    cj = series / "cast.json"
    if cj.exists():
        for cid, v in json.loads(cj.read_text(encoding="utf-8")).items():
            if isinstance(v, dict) and cid not in out:
                out[cid] = " ".join(str(x) for x in v.values() if isinstance(x, str))
    return out


def _items(text: str) -> list:
    return [x.strip() for x in re.split(r"[;,]", text or "") if x.strip()]


def state(text: str, cat: dict, rules: dict) -> tuple:
    """글에서 이 갈래가 ('has' 씀 / 'none' 안 씀 / 'held' 손에 듦, 구절) 또는 (None, '') 인가. 구절 = 그 말이 든 쉼표 조각."""
    words = [w.lower() for w in cat.get("words", [])]
    negs = [w.lower() for w in cat.get("negatives", [])]
    negation = [w.lower() for w in rules.get("negation", [])]
    held = [w.lower() for w in rules.get("held_words", [])]
    found = None
    for item in _items(text):
        low = item.lower()
        if any(n in low for n in negs):
            return "none", item
        toks = re.findall(r"[a-z][a-z'-]*", low)
        for i, t in enumerate(toks):
            if t in words:
                before, after = toks[max(0, i - 4):i], toks[i + 1:i + 2]
                if any(h in low for h in held):  # 'helmet off under his arm' = 벗어 든 것(안 씀보다 먼저 본다)
                    found = found or ("held", item)
                elif any(n in before for n in negation) or any(n in after for n in ("off", "removed")):
                    return "none", item
                else:
                    found = ("has", item) if not found or found[0] == "held" else found
    return found or (None, "")


FILLER = {"behind", "with", "and", "a", "an", "the", "his", "her", "their", "in", "of", "wearing", "wears", "wore", "on"}


def short(item: str, words: list) -> str:
    """생김새 글의 긴 조각에서 그 소품 말과 앞 꾸밈말 몇 개만('… eyes behind round wire-rimmed glasses' → 'round wire-rimmed glasses')."""
    toks = re.findall(r"[A-Za-z][A-Za-z'-]*", item)
    low = [t.lower() for t in toks]
    i = next((k for k, t in enumerate(low) if t in {w.lower() for w in words}), None)
    if i is None:
        return item.strip()
    head = toks[max(0, i - 3):i]
    while head and head[0].lower() in FILLER:
        head = head[1:]
    return " ".join(head + [toks[i]])


def _scene_key(p: dict) -> str:
    return p.get("scene") or p.get("location") or ""


def _changes_on(p: dict, rules: dict) -> bool:
    text = " ".join(str(p.get(k) or "") for k in ("action", "acting", "camera")).lower()
    return any(v in text for v in rules.get("change_verbs", []))


def lock(panels: list, rules: dict, look: dict | None = None) -> list:
    """panels(콘티 순서)의 outfit 을 고쳐 쓴다. 바꾼 것 [(컷, 인물, 갈래, 붙인 구절)]."""
    look = look or {}
    seen = {}  # (장면, 인물, 갈래) → 그 장면에서 마지막으로 말한 구절
    changes = []
    prev_scene = None
    for p in panels:
        key = _scene_key(p)
        if key != prev_scene:  # 장면이 바뀌면(같은 장소라도 사이에 다른 장면이 끼면) 처음부터
            seen, prev_scene = {}, key
        outfit = p.get("outfit")
        if not isinstance(outfit, dict):
            outfit = {}
        for c in p.get("chars") or []:
            text = str(outfit.get(c) or "")
            for name, cat in rules["categories"].items():
                st, phrase = state(text, cat, rules)
                if st in ("has", "none"):
                    seen[(key, c, name)] = phrase
                    continue
                if st == "held":  # 손에 든 모자는 쓴 게 아니다: 다음 컷엔 '없음'으로 잇는다
                    seen[(key, c, name)] = cat.get("none", "")
                    continue
                add = seen.get((key, c, name))
                if add is None:
                    own, own_phrase = state(look.get(c, ""), cat, rules)
                    add = short(own_phrase, cat.get("words", [])) if own == "has" else cat.get("none", "")
                    seen[(key, c, name)] = add
                if add:
                    text = (text.rstrip(" ,;") + ", " + add) if text.strip() else add
                    changes.append((p["id"], c, name, add))
            if text != str(outfit.get(c) or ""):
                outfit[c] = text
        if outfit:
            p["outfit"] = outfit
    return changes


def conflicts(panels: list, rules: dict) -> list:
    """같은 장면 같은 인물의 갈래가 컷마다 있다↔없다로 엇갈리는데 그 컷에 벗고 쓰는 말이 없는 곳(관문 lint 줄)."""
    out = []
    last = {}
    prev_scene = None
    for p in panels:
        key = _scene_key(p)
        if key != prev_scene:
            last, prev_scene = {}, key
        outfit = p.get("outfit") if isinstance(p.get("outfit"), dict) else {}
        for c in p.get("chars") or []:
            for name, cat in rules["categories"].items():
                st, phrase = state(str(outfit.get(c) or ""), cat, rules)
                if not st:
                    continue
                was = last.get((c, name))
                # 손에 든 것과 쓴 것 사이는 이야기(벗어 들었다·다시 썼다)로 본다: 씀↔안 씀만 엇갈림이다
                if was and was[0] != st and "held" not in (st, was[0]) and not _changes_on(p, rules):
                    out.append(f"{p['id']} {c} 의 {name} 가 같은 장면 앞 컷 {was[1]} 과 다르다('{was[2]}' → '{phrase}'): 이야기상 벗거나 쓰는 게 아니면 같게 적는다")
                last[(c, name)] = (st, p["id"], phrase)
    return out


def order_by_board(panels: list, ep: Path) -> list:
    sb = ep / "storyboard.json"
    if not sb.exists():
        return panels
    pos = {b["id"]: i for i, b in enumerate(json.loads(sb.read_text(encoding="utf-8")).get("panels", []))}
    return sorted(panels, key=lambda p: pos.get(p["id"], 10 ** 6))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    ep = Path(a.episode_dir).resolve()
    series = ep.parents[1]
    f = ep / "panels.json"
    raw = json.loads(f.read_text(encoding="utf-8"))
    panels = raw["panels"] if isinstance(raw, dict) else raw
    rules = load_rules(series)
    ordered = order_by_board(panels, ep)
    ch = lock(ordered, rules, looks(series))
    for pid, c, name, add in ch:
        print(f"옷 고정 {pid} {c} {name}: + '{add}'", flush=True)
    bad = conflicts(ordered, rules)
    for line in bad:
        print("옷 엇갈림", line, flush=True)
    print(f"장면 옷 고정: 붙인 구절 {len(ch)}개, 엇갈림 {len(bad)}곳", flush=True)
    if a.write and ch:
        f.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
