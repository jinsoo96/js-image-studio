"""장면 기준판: 한 장면(같은 장소에 이어지는 컷 묶음)의 공간·자리·빛을 기준판 한 장에 묶고, 그 장면의 컷을 기준판에서 잘라 편집 모델로 다시 그린다.
(2026-10-19 사용자 "회의실 일관성 없고 의자에 박혀 있는 애도 있고, 유지돼야 하는 공간과 상황·누가 같이 있어야 하는지가 중요한데 개판, 배경도",
 "전체적으로 내용과 안 맞는 풍경, 장면 통일성 문제") 컷마다 장소 그림 한 장 + 자리 글로 따로 그리면 방·자리·시간을 매번 새로 지어냈다.

    .venv/Scripts/python vast/scene_plate.py runs   <작품> <회차>                          장면 묶음과 기준판을 쓸 묶음
    .venv/Scripts/python vast/scene_plate.py plate  <작품> <회차> <묶음> [--view front|reverse] [--tries 3]
    .venv/Scripts/python vast/scene_plate.py derive <작품> <회차> <묶음> [--only p301,p304]
    .venv/Scripts/python vast/scene_plate.py run    <작품> <회차> [--runs 묶음,…]            기준판 + 다시 그리기

기준판(episodes/<회차>/scenes/<묶음>__<view>.png + .json)은 빈 장소 그림에서 시작해 편집 모델로 사람을 몇 명씩 자리에 앉혀 만든다.
매 단계 그림 보는 모델이 인원·사람마다 자리를 보고, 틀리면 시드를 바꿔 다시. 자리는 장면 글(panels_spec scenes)을 읽은 판단 모델이 그 장소 그림 기준
위치로 옮긴다. 완성된 기준판에서 사람마다 머리·몸 상자를 찾아 둔다(.json boxes).
다시 그리기: 컷 인물 상자를 샷(클로즈업·미디엄·넓은)에 맞게 잘라 캔버스 크기로 키우고, 편집 모델이 "같은 자리·같은 배경·같은 빛, 이 순간의 연기"로
선명하게 다시 그린다(인물 얼굴 참고 그림을 같이). 옛 그림은 panels_hf_vast/_preplate/ 로.
"""
import argparse
import base64
import io
import json
import re
import shutil
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import SIZES, graph, panel_text, upload  # noqa: E402
from judge_ep import chat, parse_json  # noqa: E402

GROUP = 3  # 한 번 편집에 앉히는 사람 수(참고 얼굴 3장까지가 잘 지켜졌다)
KEEP = ("Keep everything else in <image1> exactly the same: the room or place, walls, doors, windows, furniture, every chair and object in its place, "
        "the camera angle and the light. Every person sits or stands properly: seated people are on the seat with their back against the backrest and legs "
        "under the table, nobody merged into a chair, a table or another person. Adults with realistic proportions. No text, no speech bubbles.")


def _sj(series: Path) -> dict:
    f = series / "series.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _person(series: Path, who: str) -> dict:
    from lora import person
    return person(series, who)


def _panels(ep: Path) -> list:
    d = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    return d["panels"] if isinstance(d, dict) else d


def _spec(ep: Path) -> dict:
    f = ep / "panels_spec.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def runs(ep: Path) -> list:
    """이어지는 컷 중 장면 열쇠(scene, 없으면 location)가 같은 묶음. 묶음 이름 = <열쇠>@<첫 컷>(같은 장소가 다른 날·다른 때 또 나오면 다른 묶음)."""
    out, cur = [], None
    for p in _panels(ep):
        k = p.get("scene") or p.get("location")
        if not cur or cur["key"] != k:
            cur = {"key": k, "location": p.get("location"), "ids": [], "members": []}
            cur["name"] = f"{k}@{p['id']}"
            out.append(cur)
        cur["ids"].append(p["id"])
        for c in p.get("chars", []):
            if c not in cur["members"]:
                cur["members"].append(c)
    return out


def worth_plate(run: dict, ep: Path) -> bool:
    """기준판을 쓸 묶음: 두 사람 이상이 함께 있고 컷이 셋 이상, 장소 그림이 있다(한 사람이 방 안을 돌아다니는 장면은 자세가 계속 바뀌어 한 장에 묶지 않는다)."""
    loc = run.get("location")
    return bool(loc and loc != "none" and len(run["members"]) >= 2 and len(run["ids"]) >= 3
                and (ep.parents[1] / "assets" / "locations" / f"{loc}.png").exists())


def _b64(img: Image.Image, side=1280) -> str:
    t = img.copy()
    t.thumbnail((side, side))
    buf = io.BytesIO()
    t.convert("RGB").save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _ask(img: Image.Image, text: str, max_tokens=900) -> dict:
    return parse_json(chat([{"type": "image_url", "image_url": {"url": _b64(img)}}, {"type": "text", "text": text}], max_tokens=max_tokens))


def _outfit(ep: Path, run: dict, who: str, series: Path) -> str:
    for p in _panels(ep):
        if p["id"] in run["ids"] and who in (p.get("outfit") or {}):
            return p["outfit"][who]
    return _person(series, who).get("main", "")


def seat_plan(series: Path, ep: Path, run: dict, view: str, loc_img: Image.Image, front: dict | None = None) -> dict:
    """장면 글과 컷 글을 읽고, 이 장소 그림(view) 기준으로 사람을 묶음으로 놓을 자리를 정한다.
    {"place_type": 장소 한 줄, "light": 빛, "groups": [{"where", "pose", "facing", "people": [{"who", "slot"}]}], "people": [{"who", "place"}]}
    묶음 = 붙어 있는 사람(같은 줄·같은 쪽), 서 있는 사람은 마지막. 편집 지시는 묶음마다 "어느 쪽에 n 명을, 어디를 보게: 먼 자리 A, 가운데 B, 가까운 자리 C"
    꼴로 만든다(2026-10-19 회의실 시험에서 이 꼴은 한 번에 됐고, 사람마다 자리 문장을 늘어놓은 꼴은 있던 사람이 바뀌거나 사라졌다)."""
    layout = _ask(loc_img, "Describe this empty place for staging people in it: one line naming the place and where the camera stands with what is on "
                           "the left and right (for example: a meeting room seen from the door end of a long table; the windows are on the right), and the "
                           'light/time of day. Answer only JSON: {"place_type": "...", "light": "..."}', 400)
    scene_txt = (_spec(ep).get("scenes") or {}).get(run["key"], "")
    texts = [panel_text(p) for p in _panels(ep) if p["id"] in run["ids"]][:12]
    who = "; ".join(f"{w} = {_person(series, w)['name']}" for w in run["members"])
    if front:  # 반대편 기준판은 정면 배치를 뒤집어 만든다(따로 정하면 두 기준판의 자리가 어긋난다)
        scene_txt = ("The SAME arrangement as already staged in the opposite view (" + front.get("place_type", "") + "): "
                     + " | ".join(f"{g['pose']} {g['where']}: " + ", ".join(f"{x['who']} ({x['slot']})" for x in g["people"]) for g in front["groups"])
                     + ". This image looks from the opposite end, so LEFT and RIGHT swap and near and far swap. Keep everyone in the same real seat.")
    q = (f"You stage a comic scene in this empty place: {layout.get('place_type')}. Scene arrangement text: {scene_txt or '(none)'}. "
         f"Panels of this scene: {' | '.join(texts)}. People: {who}. "
         "Decide where everyone is at the start of the scene, in terms of THIS image. Keep the arrangement text when it gives one; translate it to this view. "
         "Use LEFT and RIGHT exactly as seen in this image and keep them consistent with the place line (if the windows are on the right, the window side "
         "is the RIGHT side). "
         f"Split the people into groups of at most {GROUP}: people next to each other in the same row or on the same side of a table form a group; "
         "never mix the two sides of a table in one group; seated groups first, standing people last. For each group give where (for example: in the "
         "chairs on the RIGHT (window) side of the table), pose (seated or standing), facing (for example: turned toward the projector screen at the far end), "
         "and for each person a slot inside the group (for example: farthest from the camera, nearest the screen / in the middle / nearest the camera). "
         "Also give focus: one sentence saying where everyone looks at the start of the scene, in terms of this image (for example: the six seated people "
         "listen to the man presenting at the screen at the far end; the presenter looks at them). "
         'Answer only JSON: {"groups": [{"where": "...", "pose": "seated", "facing": "...", "people": [{"who": "<id>", "slot": "..."}]}], "focus": "..."}')
    plan = parse_json(chat([{"type": "text", "text": q}], max_tokens=1300))
    plan.update(place_type=layout.get("place_type", ""), light=layout.get("light", ""))
    groups, seen = [], set()
    for g in plan.get("groups") or []:
        ppl = [x for x in g.get("people") or [] if x.get("who") in run["members"] and x["who"] not in seen]
        for i in range(0, len(ppl), GROUP):
            part = ppl[i:i + GROUP]
            seen |= {x["who"] for x in part}
            groups.append({"where": g.get("where", ""), "pose": g.get("pose", "seated"), "facing": g.get("facing", ""), "people": part})
    rest = [w for w in run["members"] if w not in seen]  # 판단 모델이 빠뜨린 사람
    for i in range(0, len(rest), GROUP):
        groups.append({"where": "in a natural free spot of this place", "pose": "standing", "facing": "facing the others",
                       "people": [{"who": w, "slot": ""} for w in rest[i:i + GROUP]]})
    plan["groups"] = groups
    plan["people"] = [{"who": x["who"], "place": f"{g['pose']} {g['where']}, {x['slot']}, {g['facing']}".replace(" , ", " ")}
                      for g in groups for x in g["people"]]
    return plan


def _edit(c, cache, series: Path, src: Path, refs: list, prompt: str, seed: int, out: Path) -> Path:
    names = [upload(c, src, cache)] + [upload(c, r, cache) for r in refs]
    g = graph(prompt, names, 0, 0, seed, 8, 1024, turbo=True, edit=True, series=series)
    h = c.wait(c.queue(g), timeout=900)
    st = h.get("status", {})
    if st.get("status_str") == "error":
        raise RuntimeError(next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "edit error"))
    img = next(i for o in h["outputs"].values() for i in o.get("images", []))
    return c.download(img, out)


def _check(img_path: Path, prev_path: Path, n_prev: int, added: list, series: Path) -> dict:
    """이 단계가 맞나: 앞 단계 그림(A)과 새 그림(B)을 나란히 보고, A 에 있던 사람이 B 에서도 같은 사람(얼굴·머리·옷·자리)인지, 새로 늘어난 사람이
    몇 명이고 넣으려던 생김새와 맞는지, 몸이 의자·탁자에 박혔는지. 인원수만 세면 있던 사람이 다른 얼굴로 바뀐 것을 못 잡았다(2026-10-19 회의실).
    {"kept": 그대로인 사람 수, "added_ok": 맞게 들어온 새 사람 수, "count": B 인원, "problems": [...], "merged": bool}"""
    want = "; ".join(f"{_person(series, w)['who'][:90]}" for w in added)
    q = (f"Image A is the previous step, image B is after adding people. A has {n_prev} people. We wanted to add {len(added)} new people that look like: "
         f"{want or '(none)'}. Compare carefully. Answer only JSON: "
         '{"kept": <how many of the people in A are still in B as the SAME person: same face, hair, clothes and place>, '
         '"added_ok": <how many new people in B match the wanted looks, each once>, "count": <total people in B>, '
         '"problems": ["replaced, missing, duplicated or misplaced people"], "merged": <true if any body is merged into a chair, table or another person>}')
    try:
        content = [{"type": "text", "text": "Image A:"}]
        if prev_path is not None:
            content.append({"type": "image_url", "image_url": {"url": _b64(Image.open(prev_path), 1024)}})
        content += [{"type": "text", "text": "Image B:"}, {"type": "image_url", "image_url": {"url": _b64(Image.open(img_path), 1024)}}, {"type": "text", "text": q}]
        return parse_json(chat(content, max_tokens=600))
    except Exception as e:  # noqa: BLE001
        return {"kept": 0, "added_ok": 0, "count": -1, "problems": [f"check failed: {e}"], "merged": True}


def _score(r: dict, n_prev: int, n_add: int) -> float:
    """있던 사람 보존을 가장 무겁게(무너지면 앞 단계가 헛일), 그다음 새 사람, 박힘."""
    kept = min(int(r.get("kept") or 0), n_prev)
    added = min(int(r.get("added_ok") or 0), n_add)
    return 3 * (kept == n_prev) + 2 * (added == n_add) + (r.get("count") == n_prev + n_add) + (not r.get("merged")) + 0.1 * (kept + added)


def build_plate(series: Path, ep: Path, run: dict, view: str = "front", tries: int = 3, log=print) -> Path:
    """빈 장소 그림에서 시작해 GROUP 명씩 앉혀 기준판을 만든다. 단계마다 tries 번까지 시드를 바꿔 가장 나은 판을 고른다."""
    loc = series / "assets" / "locations" / (f"{run['location']}.png" if view == "front" else f"{run['location']}_reverse.png")
    if not loc.exists():
        raise SystemExit(f"장소 그림이 없다: {loc}")
    d = ep / "scenes"
    d.mkdir(exist_ok=True)
    stem = f"{run['name'].replace('@', '_')}__{view}"
    front = None
    if view != "front":
        fj = d / f"{run['name'].replace('@', '_')}__front.json"
        front = json.loads(fj.read_text(encoding="utf-8"))["plan"] if fj.exists() else None
    plan = seat_plan(series, ep, run, view, Image.open(loc), front)
    log(f"[{run['name']} {view}] {plan['place_type']} · 묶음 " + " | ".join(f"{g['pose']} {g['where']}: " + ", ".join(f"{x['who']}({x['slot']})" for x in g['people']) for g in plan["groups"]))
    c, cache = Comfy(), {}
    vr = series / "assets" / "vast_refs"
    cur, placed, steps = loc, [], []
    for gi, g in enumerate(plan["groups"]):
        group = g["people"]
        gids = [x["who"] for x in group]
        verb = "Seat" if "seat" in str(g["pose"]).lower() else "Place"
        slots = "; ".join(f"{x['slot'] or 'next'}: the person in <image{k}> ({_person(series, x['who'])['name']}, "
                          f"{_person(series, x['who'])['who'][:70]}; wearing {_outfit(ep, run, x['who'], series)[:70]})"
                          for k, x in enumerate(group, 2))
        n = len(group)
        if placed:
            # 있던 사람은 이름 없이 묶음 자리로만(이름으로 부르면 새 참고 얼굴과 엮어 바꿔 그렸다)
            prev = " and ".join(f"{len(h['people'])} {'people' if len(h['people']) > 1 else 'person'} {h['pose']} {h['where']}"
                                for h in plan["groups"][:gi])
            intro = (f"<image1> is {plan['place_type']}, with {prev}. Keep those {len(placed)} people exactly as they are. "
                     f"{verb} {n} more {'people' if n > 1 else 'person'} {g['where']}, {g['facing']}: ")
        else:
            intro = f"<image1> is {plan['place_type']}, empty. {verb} {n} {'people' if n > 1 else 'person'} {g['where']}, {g['facing']}: "
        prompt = intro + slots + ". Same faces and hair as the references. " + KEEP
        placed_next = placed + [x["who"] for x in group]
        best = None
        for t in range(tries):
            out = d / f"{stem}.step{gi + 1}.try{t + 1}.png"
            _edit(c, cache, series, cur, [vr / f"{x['who']}_bust.png" for x in group], prompt, 1000 + gi * 10 + t, out)
            r = _check(out, cur if placed else None, len(placed), [x["who"] for x in group], series)
            s = _score(r, len(placed), len(group))
            log(f"  단계 {gi + 1} {gids} 시도 {t + 1}: 그대로 {r.get('kept')}/{len(placed)} 새로 {r.get('added_ok')}/{len(group)} 인원 {r.get('count')} "
                f"박힘 {r.get('merged')} {str(r.get('problems'))[:100]}")
            if best is None or s > best[0]:
                best = (s, out, r)
            if s >= 7:
                break
        steps.append({"people": [x["who"] for x in group], "file": best[1].name, "check": best[2]})
        cur, placed = best[1], placed_next
    # 마지막: 장면 시작의 시선으로 고개·몸을 돌린다. 참고 얼굴이 정면이라 앉힌 사람이 모두 카메라를 봐 단체 사진처럼 됐다(2026-10-19 회의실).
    # 사람 수가 달라지면 돌리기 전 판을 쓴다
    if plan.get("focus") and len(placed) > 1:
        out = d / f"{stem}.focus.png"
        prompt = (f"<image1> is {plan['place_type']} with {len(placed)} people. {plan['focus']} Turn their heads and upper bodies accordingly, eyes on what "
                  "they attend to, so we see them in profile or three-quarter view, not looking at the camera. Keep every person's face, hair, clothes and "
                  "place exactly the same, keep the place, the light and the camera exactly the same. No one added or removed.")
        _edit(c, cache, series, cur, [], prompt, 4242, out)
        r = _check(out, cur, len(placed), [], series)
        log(f"  시선 돌리기: 그대로 {r.get('kept')}/{len(placed)} 인원 {r.get('count')} 박힘 {r.get('merged')}")
        if r.get("count") == len(placed) and not r.get("merged"):
            cur = out
        steps.append({"focus": plan["focus"], "file": out.name, "check": r, "used": cur == out})
    final = d / f"{stem}.png"
    shutil.copy(cur, final)
    boxes = locate(Image.open(final), series, run["members"])
    (d / f"{stem}.json").write_text(json.dumps({"run": run, "view": view, "plan": plan, "steps": steps, "boxes": boxes,
                                                "at": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"기준판 → {final}")
    return final


def locate(img: Image.Image, series: Path, who: list) -> dict:
    """기준판에서 사람마다 머리·몸 상자(0~1000)."""
    desc = "; ".join(f"{w}: {_person(series, w)['name']}, {_person(series, w)['who'][:100]}" for w in who)
    q = (f"Find these people in the image: {desc}. For each person you can see give the head box and the whole visible body box as [x1, y1, x2, y2] "
         'normalized to 0-1000 (0,0 top-left). Answer only JSON: {"<id>": {"head": [..], "body": [..]}}')
    try:
        r = _ask(img, q, 700)
    except Exception:  # noqa: BLE001
        return {}
    return {k: v for k, v in r.items() if isinstance(v, dict) and len(v.get("head") or []) == 4 and len(v.get("body") or []) == 4}


def crop_box(img: Image.Image, boxes: dict, who: list, shot: str, aspect: str) -> tuple:
    """컷 인물 상자를 샷에 맞게 자를 자리(left, top, width, height). 넓은 컷은 기준판 전체.
    클로즈업은 머리가 높이의 약 40%, 한 사람 클로즈업은 너비를 그 사람 머리 폭의 약 3배로 좁혀 옆사람이 덜 걸리게(옆사람까지 잘라 넣으면 편집 모델이
    그 사람에게도 같은 얼굴을 입혔다, 2026-10-19 회의실 p311·p314). 미디엄은 머리부터 허리 아래까지."""
    W, H = img.size
    tw, th = SIZES.get(aspect, SIZES["16:9"])
    ar = tw / th
    hs = [boxes[w]["head"] for w in who if w in boxes]
    if not hs or "wide" in shot:
        cw, ch = (W, W / ar) if W / H < ar else (H * ar, H)
        return (W - cw) / 2, (H - ch) / 2, cw, ch
    bs = [boxes[w]["body"] for w in who if w in boxes]
    x1, x2 = min(b[0] for b in hs) * W / 1000, max(b[2] for b in hs) * W / 1000
    y1, y2 = min(b[1] for b in hs) * H / 1000, max(b[3] for b in hs) * H / 1000
    head_h = max(b[3] - b[1] for b in hs) * H / 1000
    head_w = max(b[2] - b[0] for b in hs) * W / 1000
    if "close" in shot:
        ch = max(head_h / 0.4, (y2 - y1) * 2.2)
        cy = (y1 + y2) / 2 + ch * 0.12
        cw = ch * ar
        if len(hs) == 1:
            cw = min(cw, head_w * 3.2)
            ch = cw / ar
    else:
        by2 = max(b[3] for b in bs) * H / 1000
        ch = max((by2 - y1) * 1.15, head_h * 4.5)
        cy = y1 + ch * 0.45
        cw = max(ch * ar, (x2 - x1) * 1.6)
        ch = cw / ar
    if cw > W or ch > H:
        k = min(W / cw, H / ch)
        cw, ch = cw * k, ch * k
    cx = (x1 + x2) / 2
    return max(0, min(W - cw, cx - cw / 2)), max(0, min(H - ch, cy - ch / 2)), cw, ch


def crop_for(img: Image.Image, boxes: dict, who: list, shot: str, aspect: str) -> Image.Image:
    l, t, cw, ch = crop_box(img, boxes, who, shot, aspect)
    tw, th = SIZES.get(aspect, SIZES["16:9"])
    return img.crop((int(l), int(t), int(l + cw), int(t + ch))).resize((tw, th), Image.LANCZOS)


MAX_UPSCALE = 4.0  # 이보다 크게 키워야 하는 작은 자리(멀리 선 사람의 클로즈업)는 자르지 않고, 그 부근을 배경 참고로 넣어 새로 그린다(p306 상수가 둘로)


def _doubled(img_path: Path, series: Path, who: list) -> dict:
    """같은 사람이 둘로 그려졌거나 얼굴이 반투명하게 겹쳤나(편집 경로의 흔한 실패). {"doubled": bool, "why": str}. 판정이 안 되면 doubled False."""
    want = "; ".join(f"{_person(series, w)['name']} ({_person(series, w)['who'][:70]})" for w in who)
    q = (f"This comic panel should show these named people, each exactly once: {want}. Look for drawing errors: the same person drawn twice, "
         "two overlapping or semi-transparent copies of one face or head (a ghost double), or two copies of one body. Background strangers are fine. "
         'Answer only JSON: {"doubled": true|false, "why": "short reason"}')
    try:
        r = _ask(Image.open(img_path), q, 300)
        return {"doubled": bool(r.get("doubled")), "why": str(r.get("why", ""))}
    except Exception:  # noqa: BLE001
        return {"doubled": False, "why": "check failed"}


def derive(series: Path, ep: Path, run: dict, only=None, log=print, force_gen=None) -> list:
    """묶음의 컷(인물이 있는 것)을 기준판에서 잘라 다시 그린다. 컷의 view 에 맞는 기준판(front · reverse)이 없으면 그 컷은 건너뛴다."""
    from hf_panel import names
    NAMES = names(series)
    c, cache = Comfy(), {}
    vr = series / "assets" / "vast_refs"
    outdir = ep / "panels_hf_vast"
    keep = outdir / "_preplate"
    keep.mkdir(exist_ok=True)
    done = []
    for p in _panels(ep):
        if p["id"] not in run["ids"] or (only and p["id"] not in only) or not p.get("chars"):
            continue
        view = p.get("view") or "front"
        stem = ep / "scenes" / f"{run['name'].replace('@', '_')}__{view}"
        if not stem.with_suffix(".png").exists():
            log(f"{p['id']}: {view} 기준판 없음, 건너뜀")
            continue
        meta = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
        plate = Image.open(stem.with_suffix(".png")).convert("RGB")
        who = p["chars"]
        boxes = meta.get("boxes") or {}
        shot = p.get("shot") or ""
        aspect = p.get("aspect") or "16:9"
        l, t, cw, ch = crop_box(plate, boxes, who, shot, aspect)
        tw, th = SIZES.get(aspect, SIZES["16:9"])
        # 새로 그리기 경로(기준판 부근을 배경 참고로 생성): 작은 자리, 모든 클로즈업, 그리고 부르는 쪽이 고른 컷.
        # 클로즈업을 잘라 편집하면 같은 얼굴이 둘이거나 반투명하게 겹친 컷이 12장 나왔다(2026-10-20 외출 전 장면 롤아웃: p007·p055·p220·p493 …)
        small = th / max(ch, 1) > MAX_UPSCALE
        gen = small or "close" in shot or p["id"] in (force_gen or set())
        lg, tg, cwg, chg = crop_box(plate, boxes, who, "medium shot", aspect) if gen else (l, t, cw, ch)
        crop = plate.crop((int(lg), int(tg), int(lg + cwg), int(tg + chg))).resize((tw, th), Image.LANCZOS)
        cf = ep / "scenes" / f"crop_{p['id']}.png"
        crop.save(cf)
        # 얼굴 참고 그림은 얼굴·머리에만: 상반신 참고 그림의 백팩 끈이 클로즈업에 따라왔다(p312·p315)
        faces = " ".join(f"<image{k + 2}> is {NAMES[w]}'s face: use it for {NAMES[w]}'s face and hair only, not the clothes." for k, w in enumerate(who))
        text = re.sub(r"\s*SPACE \(identical.*$", "", panel_text(p), flags=re.S)
        # 옷은 컷 설명(outfit)대로, 없으면 작품 설정의 평소 옷. 안 적으면 옆자리 사람 옷을 입혔다(p311 강 차장이 이동원의 조끼)
        text += " " + " ".join(f"{NAMES[w]} wears {(p.get('outfit') or {}).get(w) or _person(series, w).get('main', '')}." for w in who)
        alone = (f" Only {NAMES[who[0]]} is in this close-up: anyone else partly visible at the edges becomes a soft, out-of-focus background shape and "
                 f"never gets {NAMES[who[0]]}'s face." if len(who) == 1 and "close" in shot else
                 " Every person appears exactly once and keeps their own face and clothes; nobody is drawn twice.")
        gen_prompt = (f"<image1> shows the place, the light and who sits where around this moment of the scene. Draw a sharp, detailed Korean webtoon panel "
                      f"({shot}) in this same place with the same light and the same background behind the person. {faces}{alone} This moment: {text} "
                      "Clean line art, no text, no speech bubbles.")
        edit_prompt = (f"<image1> is an enlarged crop of one moment of a scene. Redraw it as a sharp, detailed Korean webtoon panel ({shot}), keeping its "
                       "composition: the same people in the same seats and positions, the same furniture, walls, windows and the same light behind them. "
                       f"{faces}{alone} This moment: {text} Keep the framing of <image1>; do not add or remove people except as this moment says. "
                       "Clean line art, no blur, no text, no speech bubbles.")
        dst = outdir / f"{p['id']}.png"
        if dst.exists() and not (keep / dst.name).exists():
            shutil.copy(dst, keep / dst.name)
        t0 = time.time()
        refs = [vr / f"{w}_bust.png" for w in who]

        def draw_gen(seed):
            src = plate
            if len(who) == 1 and "close" in shot:
                # 혼자 나오는 클로즈업: 배경 참고 속 다른 사람 자리를 흐리게 지운다. 글이 '화면 밖의 그녀를 본다'고 하면 참고 그림에 찍힌 그 사람까지
                # 그려 둘 다에 이 사람 얼굴을 입혔다(2026-10-20 p224·p341 상수가 둘)
                from PIL import ImageDraw, ImageFilter
                W, H = plate.size
                mask = Image.new("L", plate.size, 0)
                md = ImageDraw.Draw(mask)
                for w2, b2 in boxes.items():
                    if w2 not in who:
                        x1, y1, x2, y2 = b2["body"]
                        md.rectangle([x1 * W / 1000, y1 * H / 1000, x2 * W / 1000, y2 * H / 1000], fill=255)
                src = Image.composite(plate.filter(ImageFilter.GaussianBlur(28)), plate, mask.filter(ImageFilter.GaussianBlur(10)))
            lg2, tg2, cwg2, chg2 = crop_box(plate, boxes, who, "medium shot", aspect)
            src.crop((int(lg2), int(tg2), int(lg2 + cwg2), int(tg2 + chg2))).resize((tw, th), Image.LANCZOS).save(cf)
            names = [upload(c, cf, cache)] + [upload(c, r, cache) for r in refs]
            g = graph(gen_prompt, names, tw, th, seed, 8, 1024, turbo=True, series=series, chars=who)
            h = c.wait(c.queue(g), timeout=900)
            img = next(i for o in h["outputs"].values() for i in o.get("images", []))
            c.download(img, dst)

        if gen:
            draw_gen(7 + len(done))
        else:
            _edit(c, cache, series, cf, refs, edit_prompt, 7 + len(done), dst)
        how = "새로 그림(배경 참고)" if gen else "다시 그림"
        # 같은 사람이 둘이거나 얼굴이 겹쳤나: 걸리면 새로 그리기로 한 번 더(시드를 바꿔)
        d = _doubled(dst, series, who)
        if d.get("doubled"):
            draw_gen(101 + len(done))
            d2 = _doubled(dst, series, who)
            how += f" → 겹침({d.get('why', '')[:60]}) → 새로 그림" + (" (아직 겹침, 사람 눈으로)" if d2.get("doubled") else "")
        from sync_vast import place  # 본편(panels_hf)에도 넣는다: vast 판에만 두면 합성(PDF)에 안 들어갔다(2026-10-20)
        place(ep, p["id"])
        log(f"{p['id']}: 기준판({view})에서 {how} {int(time.time() - t0)}초 → {dst}")
        done.append(p["id"])
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["runs", "plate", "derive", "run"])
    ap.add_argument("series")
    ap.add_argument("episode")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--view", default="front")
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--only")
    ap.add_argument("--runs")
    ap.add_argument("--gen", help="derive: 이 컷들은 새로 그리기 경로로(쉼표)")
    a = ap.parse_args()
    series = Path(a.series).resolve()
    ep = series / "episodes" / a.episode
    rs = runs(ep)
    if a.cmd == "runs":
        for r in rs:
            print(("*" if worth_plate(r, ep) else " "), r["name"], len(r["ids"]), r["members"])
        return
    pick = lambda name: next((r for r in rs if r["name"] == name or r["key"] == name), None)  # noqa: E731
    if a.cmd in ("plate", "derive"):
        r = pick(a.run)
        if not r:
            raise SystemExit(f"묶음 {a.run} 이 없다. runs 로 본다")
        if a.cmd == "plate":
            build_plate(series, ep, r, a.view, a.tries)
        else:
            derive(series, ep, r, set(a.only.split(",")) if a.only else None, force_gen=set(a.gen.split(",")) if a.gen else None)
        return
    targets = [pick(n) for n in a.runs.split(",")] if a.runs else [r for r in rs if worth_plate(r, ep)]
    for r in [t for t in targets if t]:
        views = sorted({p.get("view") or "front" for p in _panels(ep) if p["id"] in r["ids"] and p.get("chars")})
        for v in views:
            if (series / "assets" / "locations" / (f"{r['location']}.png" if v == "front" else f"{r['location']}_reverse.png")).exists():
                build_plate(series, ep, r, v, a.tries)
        derive(series, ep, r)


if __name__ == "__main__":
    main()
