"""컷 설명 표(panels_spec.json) → panels.json. 작품을 가리지 않는 공용 변환기(회차마다 build_panels.py 를 코드로 쓰지 않아도 되게).
(2026-10-16 시험: Qwen 은 콘티는 잘 썼지만 build_panels.py 를 코드로 쓰다 잘리고 없는 함수를 지어냈다 → 표를 채우는 MCP set_panels 로)

    .venv/Scripts/python tools/panels_from_spec.py <회차 폴더>

panels_spec.json:
  "panels": {"p003": {"chars": ["jaemin"], "outfit": {"jaemin": "white dress shirt, navy slacks"}, "location": "office",
                      "shot": "wide shot", "action": "영어로 그 컷에서 일어나는 일", "acting": "표정·몸짓", "camera": "어깨 너머 숏이면"}}
  "scenes": {"office": "장면 배치 한 문장(누가 화면 왼쪽·오른쪽에서 어디를 보나)"}   ← 인물이 있는 그 장소의 컷마다 붙는다
            같은 장소가 한 화 안에서 다른 때(배치가 바뀐 뒤)에 또 나오면 그 컷에 "scene": "office_night" 처럼 자리표 이름을 적는다(없으면 장소 이름)
  "reuse":  {"p010": "episodes/ep001/panels_hf/p012.png"}                         ← 앞 화 그림을 그대로 쓰는 컷
"""
import json
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[1]
ASPECT ={"landscape": "3:2", "close": "4:3", "wide": "16:9", "portrait": "2:3"}  # 콘티 aspect → 그림 비율(2화 v2 build_panels 와 같음)


def norm_shot(s: str) -> str:
    """샷 이름을 그리기 코드가 아는 꼴로(close-up · wide shot · medium shot · full shot · insert shot). 낱말로 본다.
    (2026-10-18 새 웹툰 생성기 시험: 에이전트가 'close shot' 으로 써서 클로즈업 처리(장소를 뒤로, 배치 문장 빼기)가 안 걸렸다)"""
    t = (s or "medium shot").lower()
    if "close" in t:
        return "extreme close-up" if "extreme" in t else "close-up"
    for word, name in (("wide", "wide shot"), ("long", "wide shot"), ("establish", "wide shot"), ("medium", "medium shot"),
                       ("mid", "medium shot"), ("full", "full shot"), ("insert", "insert shot")):
        if word in t:
            return name
    return s


SIDE_ASK = """Below are webtoon panels. For each panel, on which side of the picture (as the viewer sees it) is each listed person, according to the
panel text and the scene layout? Answer "unknown" for a person when the text and layout do not say.
{items}
Answer JSON only: {{"<panel id>": {{"<person name>": "left" | "right" | "unknown"}}}}"""


def resolve_balloon_sides(ep: Path, panels: list, scenes: dict) -> list:
    """두 사람 이상 나오는 컷의 대사에 who(말하는 인물 id)가 있으면, 그 인물이 화면 어느 쪽인지를 판정 모델이 컷 글·장면 배치를 읽고 답하게 해
    말풍선 자리(top-left·top-right)를 정하고 storyboard.json 에 쓴다. 사람이 TL·TR 을 고르다 반대로 붙였다(2026-10-18 외출 p037).
    글 속 'LEFT/RIGHT' 를 정규식으로 찾던 방식은 표현이 조금만 달라도 못 찾았다(2026-10-19 사용자 "문구 정규식은 땜질"). 바꾼 대사 목록을 돌려준다."""
    sys.path.insert(0, str(ROOT_DIR / "tools"))
    sys.path.insert(0, str(ROOT_DIR / "vast"))
    from common import names
    sb = ep / "storyboard.json"
    board = json.loads(sb.read_text(encoding="utf-8"))
    NAMES = names(ep.parents[1])
    short = {c: re.sub(r"\s*\(.*?\)", "", n).split()[-1] for c, n in NAMES.items()}
    spec = {p["id"]: p for p in panels}
    todo = [b for b in board["panels"] if b["id"] in spec and len(spec[b["id"]].get("chars") or []) >= 2
            and any(d.get("who") in NAMES for d in b.get("dialogue", []) if d.get("type") in ("speech", "thought"))]
    if not todo:
        return []
    from judge_ep import JudgeDown, chat, parse_json
    sides = {}
    for k in range(0, len(todo), 12):
        part = todo[k:k + 12]
        items = "\n".join(f"[{b['id']}] people: {', '.join(short[c] for c in spec[b['id']]['chars'] if c in short)}; panel text: "
                          f"{spec[b['id']].get('action', '')} {spec[b['id']].get('camera', '')}; scene layout: {scenes.get(spec[b['id']].get('scene') or spec[b['id']].get('location') or '', '')}"
                          for b in part)
        try:
            txt = chat([{"type": "text", "text": SIDE_ASK.format(items=items)}], max_tokens=900)
            sides.update(parse_json(txt))
        except (JudgeDown, ValueError) as e:
            print(f"말풍선 자리: 판정 모델에 묻지 못해 그대로 둔다({type(e).__name__})")
    changed = []
    for b in todo:
        got = {str(n).lower(): str(v).lower() for n, v in (sides.get(b["id"]) or {}).items()}
        for d in b["dialogue"]:
            side = got.get(short.get(d.get("who"), "").lower())
            if d.get("type") not in ("speech", "thought") or side not in ("left", "right"):
                continue
            pos = "top-left" if side == "left" else "top-right"
            if d.get("pos") != pos:
                changed.append(f"{b['id']} {d['who']}: {d.get('pos')} → {pos}")
                d["pos"] = pos
    if changed:
        sb.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def structure_lint(panels, board, series: Path | None = None) -> list:
    """컷 설명의 구조 검사(글 속 문구가 아니라 칸이 있는지만 본다). 연출 내용(자세·탈것·소품·부정어·연기·공간)은 컷 설명 관문의
    기준표(skills/panel-spec/judge.json)가 컷 글을 읽고 채점한다. 같은 장면 안 모자·안경·장갑이 씀↔안 씀으로 엇갈리면 여기서 잡는다(outfit_lock)."""
    from outfit_lock import conflicts, load_rules
    order = {pid: i for i, pid in enumerate(board)}
    out = [ln for ln in conflicts(sorted(panels, key=lambda p: order.get(p["id"], 10 ** 6)), load_rules(series))]
    for p in panels:
        lines = [d for d in (board.get(p["id"], {}).get("dialogue") or []) if d.get("type") in ("speech", "thought")]
        if len(p["chars"]) >= 2 and lines and not all(d.get("who") for d in lines):
            out.append(f"{p['id']} 두 사람 컷의 대사에 who(말하는 인물)가 없다(말풍선 자리를 정할 수 없다)")
    return out


def _names(series: Path) -> dict:
    cast = json.loads((series / "cast.json").read_text(encoding="utf-8")) if (series / "cast.json").exists() else {}
    return {k: re.sub(r"\s*\(.*?\)", "", v.get("en") or k) for k, v in cast.items() if isinstance(v, dict)}


def normalize(sp: dict, names: dict) -> dict:
    """그리기·검수는 acting·camera·action·extras 를 글로, outfit 을 {인물: 글} 로 읽는다. 에이전트가 acting 을 {인물: 연기} 객체로 넣자
    그대로 통과해 그리기 단계에서 8컷 모두 TypeError 로 멈췄다(2026-10-24 미사일 스튜디오 새 작품). 들어온 꼴을 여기서 한 번 맞춘다."""
    sp = dict(sp)
    for k in ("acting", "camera", "action", "extras", "look", "text_in_image"):
        v = sp.get(k)
        if isinstance(v, dict):
            sp[k] = "; ".join(f"{names.get(c, c)}: {x}" for c, x in v.items() if str(x).strip())
        elif isinstance(v, (list, tuple)):
            sp[k] = " ".join(str(x) for x in v if str(x).strip())
    o = sp.get("outfit")
    if isinstance(o, str) and o.strip():
        sp["outfit"] = {c: o for c in (sp.get("chars") or [])}
    return sp


def main():
    ep = Path(sys.argv[1]).resolve()
    series = ep.parents[1]
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    spec = json.loads((ep / "panels_spec.json").read_text(encoding="utf-8")) if (ep / "panels_spec.json").exists() else {}
    by_id = {p["id"]: p for p in board["panels"]}
    scenes, reuse, P = spec.get("scenes", {}), spec.get("reuse", {}), spec.get("panels", {})

    (ep / "panels_hf").mkdir(exist_ok=True)
    (ep / "panels").mkdir(exist_ok=True)
    copied, bad_reuse = 0, []
    for pid, src in reuse.items():
        s = series / src
        if not s.exists():
            bad_reuse.append(f"{pid}({src})")
            continue
        dst = ep / "panels_hf" / f"{pid}.png"
        if not dst.exists():
            shutil.copy2(s, dst)
            copied += 1
        placed = ep / "panels" / f"{pid}.png"
        if not placed.exists():
            im = Image.open(dst).convert("RGB")
            im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS).save(placed)

    panels, unknown = [], []
    names = _names(series)
    for pid, sp in P.items():
        sp = normalize(sp, names)
        if pid not in by_id:
            unknown.append(pid)
            continue
        chars = sp.get("chars", [])
        action = sp.get("action", "")
        loc = sp.get("location")
        key = sp.get("scene") or loc  # 자리표 이름(같은 장소라도 때가 다르면 배치가 다르다)
        if key in scenes and chars and not sp.get("camera"):  # 어깨 너머 숏은 카메라 칸에 배치가 있다
            action = f"{action} SPACE (identical in every panel of this scene): {scenes[key]}"
        e = {"id": pid, "chars": chars, "outfit": sp.get("outfit", {}), "location": loc, "shot": norm_shot(sp.get("shot", "medium shot")),
             "aspect": sp.get("aspect") or ASPECT.get(by_id[pid].get("aspect", "landscape"), "3:2"), "action": action}
        for k in ("acting", "camera", "refs_extra", "outfit_sheets", "extras", "text_in_image", "look", "view", "scene"):
            if sp.get(k):
                e[k] = sp[k]
        panels.append(e)
    missing = [p["id"] for p in board["panels"] if p["id"] not in P and p["id"] not in reuse and not p.get("composite")]
    no_act = [p["id"] for p in panels if p["chars"] and "acting" not in p]
    crowded = [p["id"] for p in panels if "close-up" in p["shot"] and len(p["chars"]) > 1 and not p.get("camera")]  # 클로즈업은 한 사람(panel-spec 규칙)
    # 장면 옷 고정: 모자·안경·장갑 말이 없는 컷은 같은 장면 앞 컷을 이어받는다(말이 없으면 그리기 모델이 시대 규칙대로 씌웠다, 2026-10-27 개항 v4)
    from outfit_lock import load_rules, lock, looks, order_by_board
    locked = lock(order_by_board(panels, ep), load_rules(series), looks(series))
    if locked:
        print(f"장면 옷 고정: 모자·안경·장갑 구절 {len(locked)}개를 이어받음(예: {locked[0][0]} {locked[0][1]} '{locked[0][3]}')")
    lint = structure_lint(panels, {p["id"]: p for p in board["panels"]}, series)
    (ep / "panels.json").write_text(json.dumps(panels, ensure_ascii=False, indent=1), encoding="utf-8")
    for line in resolve_balloon_sides(ep, panels, scenes):
        print("말풍선 자리:", line)
    for line in lint:
        print("구조:", line)
    print(f"새로 그릴 컷 {len(panels)}, 가져다 쓴 컷 {len(reuse)}(이번에 복사 {copied}), 빠진 컷 {missing}, 연기 없는 인물 컷 {no_act}"
          + (f", 콘티에 없는 번호 {unknown}" if unknown else "") + (f", 없는 재사용 그림 {bad_reuse}" if bad_reuse else "")
          + (f", 인물 둘 이상인 클로즈업 {crowded}(한 사람만 남기거나 medium·어깨 너머 숏 camera 로)" if crowded else ""))


if __name__ == "__main__":
    main()
