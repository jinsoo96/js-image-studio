"""2단째 다듬기: 1단에서 그린 컷을 Qwen Image 2.1 편집으로 한 번 더 손본다(구도·배경은 그대로, 인원·연기·얼굴만).
(2026-10-16 사용자 "노드 잇고 구성하면서 하는 게 품질이 더 좋을 수도". 1단만으로는 사람이 한 명 더 나오거나 연기가 약한 컷이 남았다)

    .venv/Scripts/python vast/refine.py <series_hf_dir> <panels.json> <1단 그림 폴더> --only p045,p003 [--out 폴더] [--seed 7]
결과: <1단 폴더>_refined/<id>.png (1단 그림은 그대로)
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import LLM_MODEL, LLM_URL, graph, is_ots, pick_expr, upload, wants_camera  # noqa: E402


def refine_prompt(series, p, expr=True):
    """컷 설명에서 다듬기 지시를 만든다. <image1> = 1단 그림, <image2>… = 인물 얼굴(표정 칸이 있으면 그 표정)."""
    from hf_panel import names
    NAMES = names(series)
    vr = series / "assets" / "vast_refs"
    refs, faces = [], []
    for cid in p.get("chars", []):
        pk = pick_expr(series, cid, NAMES[cid], p.get("acting") or p["action"]) if expr else None
        f = vr / pk[0] if pk else vr / f"{cid}_bust.png"
        if f.exists():
            refs.append(f)
            faces.append(f"<image{len(refs) + 1}> is {NAMES[cid]}'s face" + (f" with the target expression ({pk[1]})" if pk else ""))
    n = len(p.get("chars", []))
    who = ", ".join(re.sub(r"\s*\(.*?\)", "", NAMES[c]) for c in p.get("chars", []))
    lines = ["Edit <image1>, a Korean webtoon panel. Keep the art style, background, lighting, camera angle, framing and composition the same. Fix only these:"]
    if n:
        cam = " plus the over-the-shoulder foreground figure seen from behind" if p.get("camera") else ""
        lines.append(f"1. There must be exactly {n} {'person' if n == 1 else 'people'} ({who}){cam}. Remove every other person, including small "
                     "background figures and any duplicate of the same person; fill the removed area with the background.")
        # 같은 사람이 둘이면 둘 다 그 이름에 맞아서 못 지웠다(p003 우빈 둘) → 옷으로 누가 진짜인지 정해 준다
        wear = "; ".join(f"{re.sub(r'\s*\(.*?\)', '', NAMES[c])} wears {o}" for c, o in (p.get("outfit") or {}).items() if c in p.get("chars", []))
        if wear:
            lines.append(f"   Who is who: {wear}. If two people look like the same person, keep only the one wearing these clothes and remove the other.")
    else:
        lines.append("1. There must be no people at all. Remove every person, face and figure; fill the area with the background.")
    if p.get("acting"):
        lines.append(f"2. Acting: {p['acting']} Make the faces and body language strong and instantly readable like a Korean webtoon.")
    if faces:
        lines.append("3. " + "; ".join(faces) + ". Keep each person's face, hairstyle and hair color identical to these, but keep the outfits in <image1>.")
    lines.append("No text, no speech bubbles.")
    return "\n".join(lines), refs


VLM_URL, VLM_MODEL = LLM_URL, LLM_MODEL  # 그림을 보는 모델(comfy_panel 과 같은 설정: 환경변수 VLM_URL·VLM_MODEL)


def find_extras(img_path: Path, series, p) -> list:
    """그림을 보는 사내 Qwen 에게 컷 설명에 없는 사람을 옷·자리로 짧게 묘사하게 한다. 없으면 []."""
    import base64
    import io
    import urllib.request
    from PIL import Image
    from hf_panel import names
    NAMES = names(series)
    chars = p.get("chars", [])
    wear = {c: o for c, o in (p.get("outfit") or {}).items()}
    # 같은 교복을 입은 두 소년을 '같은 사람 둘'로 보고 동만을 지웠다(2026-10-18 추몽 p032, 세 번) → 인물마다 생김새(작품 설정 who 의 앞 문장)를 같이 준다
    looks = {}
    sj = Path(series) / "series.json"
    if sj.exists():
        looks = {c: re.split(r"(?<=\.)\s", v.get("who", ""))[0][:140] for c, v in json.loads(sj.read_text(encoding="utf-8")).get("cast", {}).items()}
    who = "; ".join(f"{re.sub(r'\s*\(.*?\)', '', NAMES[c])}" + (f" ({looks[c]})" if looks.get(c) else "") + (f" wearing {wear[c]}" if c in wear else "")
                    for c in chars) or "nobody"
    ask = (f"This webtoon panel should show exactly {len(chars)} {'person' if len(chars) == 1 else 'people'}: {who}."
           + (" An over-the-shoulder figure seen from behind in the foreground is also expected and is NOT extra." if is_ots(p.get("camera")) else "")
           + (f" These unnamed people are also expected and are NOT extra: {p['extras']}." if p.get("extras") else "")
           + "\nLook at the image. If it shows no more people than expected, answer with an empty list: different expected people can wear "
           "the same uniform, so do not call someone a duplicate just because the clothes match; compare faces, hair and build. "
           "Otherwise list every EXTRA person who should not be there: true duplicates of the same person (keep the one whose clothes match), "
           "strangers, and small background figures. Describe each extra person in one short phrase by clothes and position "
           "(e.g. 'the old woman in a red coat standing at the far left'). "
           'Answer only JSON: {"extra": ["...", "..."]} or {"extra": []}.')
    img = Image.open(img_path).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    body = {"model": VLM_MODEL, "temperature": 0.0, "max_tokens": 300, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": ask}]}]}
    req = urllib.request.Request(VLM_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        return [x for x in json.loads(m.group(0)).get("extra", []) if isinstance(x, str) and x.strip()] if m else []
    except Exception as e:
        print(f"  사람 세기 실패: {str(e)[:100]}", flush=True)
        return []


def remove_prompt(extras: list) -> str:
    """지울 사람을 콕 집은 짧은 지시. 긴 다듬기 지시로는 같은 사람 둘 중 하나를 못 지웠다(p003), 짧게 집어 주니 11초 만에 지웠다."""
    return ("Remove " + "; also remove ".join(extras) + ". Fill the empty space with the matching background. "
            "Keep everything else in <image1> exactly the same: the other people, their faces, poses and clothes, the room, lighting and framing.")


def fix_extras(c, cache, img_path: Path, series, p, seed=7, rounds=2, log=print) -> bool:
    """1단 그림에서 남는 사람을 찾아 지운다(그 자리에 덮어씀, 원본은 _stage1/ 에). 고쳤으면 True."""
    changed = False
    for r in range(rounds):
        extras = find_extras(img_path, series, p)
        if not extras:
            break
        if not changed:
            keep = img_path.parent / "_stage1"
            keep.mkdir(exist_ok=True)
            (keep / img_path.name).write_bytes(img_path.read_bytes())
        g = graph(remove_prompt(extras), [upload(c, img_path, cache)], 0, 0, seed + r, 8, 1024, turbo=True, edit=True)
        g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/fixed"
        hist = c.wait(c.queue(g), 600)
        img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
        c.download(img, img_path)
        log(f"  {p['id']}: 남는 사람 지움({r + 1}회) {extras}")
        changed = True
    return changed


EDITABLE = {"표정연기", "옷"}  # 편집으로 고칠 수 있는 것. 좌우·시선·배경·인원은 구도라 다시 그린다


LIMB_ASK = ("Look only at the bodies. For each fully visible person (ignore people seen only from behind at the frame edge), count their arms "
            "and hands that are actually drawn, following each arm from the shoulder. Crossed arms count as two arms. If a person has an extra "
            "arm or hand (for example arms crossed AND another hand raised), say so. "
            'Answer only JSON: {"people": [{"who": "short description", "arms": N, "hands": N, "extra_limb": true|false}]}')


def extra_limbs(img_path: Path) -> list:
    """팔·손이 남는 사람(짧은 질문 하나로 따로 묻는다). 여러 항목을 함께 묻는 검수에선 '팔 셋'을 놓쳤고, 이것만 물으니 잡았다
    (2026-10-18 낙제생과 조교 p003: 팔짱 + 든 손). 실패하면 빈 목록."""
    import base64
    import io
    import urllib.request
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    body = {"model": VLM_MODEL, "temperature": 0.0, "max_tokens": 300, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": LIMB_ASK}]}]}
    req = urllib.request.Request(VLM_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        people = json.loads(re.search(r"\{.*\}", txt, re.S).group())["people"]
    except Exception:
        return []
    return [f"{x.get('who', '?')}: arms {x.get('arms')}, hands {x.get('hands')}" for x in people
            if x.get("extra_limb") or (x.get("arms") or 0) > 2 or (x.get("hands") or 0) > 2]


GAZE_ASK = ("Look only at where each person in this webtoon panel is looking. Is any person's face turned to the front with the eyes looking "
            "straight out of the picture at the viewer, as if posing for the camera? A person looking at another person, at an object, down, "
            "to the side or away does not count. Answer JSON only: {\"at_viewer\": [\"short description of each person looking at the viewer\"]} "
            "(empty list if nobody).")


def camera_gaze(img_path: Path) -> list:
    """독자(카메라)를 정면으로 보는 사람(짧은 질문 하나로 따로 묻는다). 여러 항목 검수에선 놓쳤다
    (2026-10-18 추몽 사용자 "왜 시점을 자꾸 독자 보는 거로 하냐", p016 다락에서 정면 미소가 통과). 실패하면 빈 목록."""
    import base64
    import io
    import urllib.request
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    body = {"model": VLM_MODEL, "temperature": 0.0, "max_tokens": 200, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": GAZE_ASK}]}]}
    req = urllib.request.Request(VLM_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        return [str(x) for x in json.loads(re.search(r"\{.*\}", txt, re.S).group()).get("at_viewer", []) if x]
    except Exception:
        return []


def _vlm(images: list, text: str, max_tokens=300) -> dict:
    """그림 여러 장 + 질문 하나를 그림 보는 모델에 묻고 JSON 을 받는다. 실패하면 빈 dict."""
    import base64
    import io
    import urllib.request
    from PIL import Image
    content = []
    for p in images:
        img = Image.open(p).convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
    content.append({"type": "text", "text": text})
    body = {"model": VLM_MODEL, "temperature": 0.0, "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(VLM_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        return json.loads(re.search(r"\{.*\}", txt, re.S).group())
    except Exception:
        return {}


# 연출 논리(2026-10-18 외출 1화 사용자 지적: 버스에 거꾸로 앉음, 운전석 반대, 대화 상대 대신 독자를 보는 사람, 앉아야 할 사람이 서 있음,
# 맥주 두 잔이 세 잔·커피잔, 대사하는 사람 뒤통수). 여러 항목을 한꺼번에 묻는 검수(vision_review)는 이걸 다 통과시켰다 → 따로 묻는다.
# 근거: 영화 연출의 연속성 원칙(180도 법칙·시선 일치·소품 연속성), skills/panel-spec 의 '연출 문법'.
LOGIC_ASK = """You are a strict script supervisor checking ONE webtoon panel for staging mistakes a careful reader would notice.
The panel should show: {desc}
Check only these and answer from what you actually see:
1. Real-world logic: does anything work the wrong way? For example passengers in a bus or train sitting backwards relative to the direction of travel, a car driven from the wrong side (in Korea the driver sits on the LEFT side of the car behind the steering wheel), a person standing or leaning while the description has them seated, objects floating or in impossible places.
2. Conversation: if two or more people are talking or interacting, are they turned toward each other? A person whose face and body point at the viewer instead of the person they are talking to is a mistake.
3. Props: do the number and kind of drinks, cups and key objects match the description (for example exactly two beer mugs, not three; beer mugs, not coffee cups)?
4. Faces: if a person is speaking or reacting in this moment, is their face visible? Only the back of the head is a mistake unless the description asks for a back view.
Only report clear mistakes. Do not report small differences in exactly where someone's eyes point, as long as people who talk look toward each other.
A screen or monitor seen from behind is normal when the camera looks at the person from behind their screen (the screen faces them and lights their face).
Answer JSON only: {{"problems": ["short description", ...]}} (empty list if none)."""

FACING_ASK = """Several people are in this webtoon panel and they are dealing with each other. For EACH person, look at the direction of their FACE and SHOULDERS:
do they point toward the other person (profile or three-quarter view toward them), or do they point straight toward the viewer like posing for a group photo?
A person whose shoulders and face are square to the viewer while talking to someone beside them is a mistake, even if their eyes glance sideways.
Answer JSON only: {"facing_viewer": ["short description of each person facing the viewer instead of the others"]} (empty list if none)."""


def facing_check(img_path: Path) -> list:
    """대화하는 사람이 상대가 아니라 독자 쪽으로 몸·얼굴을 두고 서 있나(외출 p004 박희재: 눈만 옆을 보고 몸은 정면이라 시선 검사를 통과했다)."""
    return [str(x) for x in _vlm([img_path], FACING_ASK).get("facing_viewer", []) if x]


SIDE_ASK = """Look at the main people in this webtoon panel. Is the person on the LEFT half of the picture a man or a woman, and the person on the RIGHT half?
Answer JSON only: {"left": "man" | "woman" | "unclear", "right": "man" | "woman" | "unclear"}"""


def expected_sides(series, p: dict, layout: str = "") -> dict:
    """컷 설명(action·camera)에 적힌 화면 좌우를 인물 성별로: {"left": "man", "right": "woman"}. 못 읽으면 {}."""
    from hf_panel import names
    if len(p.get("chars") or []) != 2:
        return {}
    NAMES = names(series)
    sj = Path(series) / "series.json"
    cast = json.loads(sj.read_text(encoding="utf-8")).get("cast", {}) if sj.exists() else {}
    text = " ".join(str(p.get(k) or "") for k in ("action", "camera")) + " " + layout  # 장면 배치 문장(panels_spec scenes)도 본다
    side, sex = {}, {}
    for cid in p["chars"]:
        short = re.sub(r"\s*\(.*?\)", "", NAMES.get(cid, cid)).split()[-1]
        m = re.search(rf"{re.escape(short)}[^.;]{{0,45}}?\b(LEFT|RIGHT)\b", text)
        w = str(cast.get(cid, {}).get("who", "")).lower()
        g = "woman" if re.search(r"\bwoman\b|\bgirl\b", w) else "man" if re.search(r"\bman\b|\bboy\b", w) else ""
        if m and g:
            side[cid], sex[cid] = m.group(1).lower(), g
    if len(side) != 2 or len(set(side.values())) != 2 or len(set(sex.values())) != 2:
        return {}
    return {s: sex[c] for c, s in side.items()}


def side_check(series, img_path: Path, p: dict, layout: str = "") -> list:
    """180도 법칙: 장면 배치의 좌우와 그림의 좌우가 같은가(외출 p010·p012 를 다시 그릴 때마다 상수·은영이 바뀌었다)."""
    want = expected_sides(series, p, layout)
    if not want:
        return []
    got = _vlm([img_path], SIDE_ASK)
    if got.get("left") == want.get("right") and got.get("right") == want.get("left"):
        return [f"좌우가 바뀌었다: 왼쪽이 {want['left']}, 오른쪽이 {want['right']} 여야 한다"]
    return []


def logic_check(img_path: Path, p: dict, era: str = "") -> list:
    desc = " ".join(str(p.get(k) or "") for k in ("action", "acting", "camera"))
    desc = re.sub(r"\s*SPACE \(identical in every panel of this scene\):.*$", "", desc, flags=re.S)
    ask = LOGIC_ASK.format(desc=desc)
    if era:  # 시대 소품(외출 1화: 2008년에 스마트폰·태블릿)
        ask = ask.replace("Only report clear mistakes.", f"5. Period: {era} Any object from a later time is a mistake.\nOnly report clear mistakes.")
    return [str(x) for x in _vlm([img_path], ask).get("problems", []) if x]


CONT_ASK = """Image 1 is the previous panel and image 2 is the next panel of the SAME scene in a webtoon (continuous time).
Panel 1 shows: {who1}. Panel 2 shows: {who2}. Compare only the people who appear in BOTH panels; a different person in the next
panel (a shot of the other speaker, a close-up of one person after a wide shot) is normal, not a mistake.
What happens in panel 2: {desc}
List differences between the two panels that a reader would notice as continuity mistakes, unless the text above explains them:
a person who was sitting is now standing (or the reverse); the number or kind of drinks, cups or props on the table changes; clothes, hairstyle or glasses change; a person looks like a different person (face, age, build); the drawing style changes (line, coloring, proportions, realism); the left-right positions of the people swap.
Only report clear mistakes. Answer JSON only: {{"problems": ["short description", ...]}} (empty list if none)."""


def continuity_pair(prev_img: Path, img: Path, p: dict, prev: dict | None = None, names: dict | None = None) -> list:
    """같은 장면의 앞 컷과 이 컷을 나란히 보고 이어짐이 깨진 곳(자세·소품·옷·얼굴·그림체·좌우).
    컷마다 누가 나오는지 알려 준다: 주고받는 대화 컷(이동원 → 상수)을 '같은 사람이 바뀌었다'로 오판했다(외출 p078→p077)."""
    if prev is not None and not set(prev.get("chars") or []) & set(p.get("chars") or []):
        return []  # 겹치는 인물이 없으면(다른 사람 컷) 사람 이어짐은 볼 게 없다
    nm = names or {}
    who = lambda q: ", ".join(nm.get(c, c) for c in (q.get("chars") or [])) or "no main characters"  # noqa: E731
    desc = " ".join(str(p.get(k) or "") for k in ("action", "acting"))
    desc = re.sub(r"\s*SPACE \(identical in every panel of this scene\):.*$", "", desc, flags=re.S)
    ask = CONT_ASK.format(desc=desc, who1=who(prev or {}), who2=who(p))
    return [str(x) for x in _vlm([prev_img, img], ask).get("problems", []) if x]


def staging_check(series, ep_dir: Path, img_path: Path, pid: str):
    """그림 검수(mcp/vision_review: 인원·좌우시선·옷·표정연기·배경·기타, 두 번 보고 둘 다 걸린 것만) + 팔·손 개수(따로 묻기)로 본다. (결과, 걸린 항목)
    팔·손이 남으면 '몸' 으로 걸고, 구도처럼 다시 그린다(편집으로 팔 하나만 지우기는 잘 안 된다)."""
    sys.path.insert(0, str(ROOT / "mcp"))
    import vision_review as V
    V.DIRS[:] = [img_path.parent.name]
    r = V.review(series, ep_dir, [pid], workers=2)[0]
    probs = set(V._probs(r)) if r.get("판정") == "다시" else set()
    limbs = extra_limbs(img_path)
    if limbs:
        probs.add("몸")
        r["고칠점"] = (str(r.get("고칠점") or "") + " 팔·손이 남는다: " + "; ".join(limbs)).strip()
    p = next((x for x in json.loads((ep_dir / "panels.json").read_text(encoding="utf-8")) if x["id"] == pid), {})
    if p.get("chars") and not wants_camera(p) and not is_ots(p.get("camera")):  # 어깨 너머 숏은 앞사람(카메라 쪽)을 보는 게 맞다(p047)
        seen = camera_gaze(img_path)
        if seen:
            probs.add("시선")
            r["고칠점"] = (str(r.get("고칠점") or "") + " 독자를 쳐다본다: " + "; ".join(seen)).strip()
    if len(p.get("chars") or []) >= 2 and not is_ots(p.get("camera")) and not re.search(r"from behind|back view|backs", str(p.get("camera") or "") + str(p.get("action") or ""), re.I):
        facing = facing_check(img_path)
        if facing:
            probs.add("시선")
            r["고칠점"] = (str(r.get("고칠점") or "") + " 상대 대신 독자 쪽을 향함: " + "; ".join(facing)).strip()
    sc = ep_dir / "panels_spec.json"
    layout = (json.loads(sc.read_text(encoding="utf-8")).get("scenes", {}).get(p.get("location") or "", "") if sc.exists() else "")
    swapped = side_check(series, img_path, p, layout)
    if swapped:
        probs.add("좌우")
        r["고칠점"] = (str(r.get("고칠점") or "") + " " + swapped[0]).strip()
    if p.get("chars") or p.get("extras"):
        sj = Path(series) / "series.json"
        era = (json.loads(sj.read_text(encoding="utf-8")).get("era_rule") if sj.exists() else "") or ""
        bad = logic_check(img_path, p, era)
        if bad:
            probs.add("논리")
            r["고칠점"] = (str(r.get("고칠점") or "") + " 연출 논리: " + "; ".join(bad)).strip()
    return r, probs


def staging_edit_prompt(series, p, r, probs) -> str:
    from hf_panel import names
    NAMES = names(series)
    parts = []
    if "표정연기" in probs:
        parts.append(f"Change only the facial expressions and body language to match this acting: {p.get('acting') or p['action']} "
                     "Make the emotion strong and instantly readable like a Korean webtoon.")
    if "옷" in probs:
        wear = "; ".join(f"{re.sub(r'\s*\(.*?\)', '', NAMES[c])} wears {o}" for c, o in (p.get("outfit") or {}).items() if c in p.get("chars", []))
        if wear:
            parts.append(f"Change the clothes to match: {wear}.")
    if r.get("고칠점"):
        parts.append(f"Note: {r['고칠점']}")
    parts.append("Keep everything else in <image1> exactly the same: who is in the panel, their positions and facing directions, faces, hair, "
                 "the room, lighting, camera angle and framing.")
    return " ".join(parts)


def staging_pass(c, cache, dst: Path, series, ep_dir: Path, p, seed, render, retries=1, log=print):
    """3단 연출 확인(2026-10-16 사용자 "시점·표정·공간 자각·카메라 각도를 잘 고려해야").
    표정·옷만 틀리면 편집으로 고치고, 좌우·시선·배경·인원이 틀리면 시드를 바꿔 다시 그린다. 더 나빠지면 나은 판으로 되돌린다."""
    pid = p["id"]
    best = None
    attempt = 0
    while True:
        r, probs = staging_check(series, ep_dir, dst, pid)
        if best is None or len(probs) < best[0]:
            best = (len(probs), dst.read_bytes(), probs)
        if not probs:
            log(f"  {pid}: 연출 확인 통과" + (f" (애매, 사람 눈으로: {r.get('기타', '')[:120]})" if r.get("판정") == "애매" else ""))
            return
        log(f"  {pid}: 연출 확인 걸림 {sorted(probs)} {str(r.get('고칠점', ''))[:120]}")
        # 좌우만 바뀌었으면(그림에 글자가 없을 때) 다시 그리지 않고 뒤집는다. 다시 그리면 또 바뀌었다(외출 p012)
        if "좌우" in probs and not p.get("text_in_image") and not getattr(staging_pass, "_flipped", {}).get(pid):
            from PIL import Image, ImageOps
            ImageOps.mirror(Image.open(dst).convert("RGB")).save(dst)
            staging_pass._flipped = {**getattr(staging_pass, "_flipped", {}), pid: True}
            log(f"  {pid}: 좌우를 뒤집음(장면 배치와 반대로 그려졌다)")
            continue
        if probs <= EDITABLE:
            before = dst.read_bytes()
            keep = dst.parent / "_stage1"
            keep.mkdir(exist_ok=True)
            (keep / f"{pid}_before_check.png").write_bytes(before)
            g = graph(staging_edit_prompt(series, p, r, probs), [upload(c, dst, cache)], 0, 0, seed % 100000 + 17, 8, 1024, turbo=True, edit=True)
            g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/checked"
            hist = c.wait(c.queue(g), 600)
            c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), dst)
            r2, p2 = staging_check(series, ep_dir, dst, pid)
            if len(p2) > len(probs):
                dst.write_bytes(before)
                log(f"  {pid}: 편집 뒤 더 나빠져 되돌림 {sorted(p2)}")
            else:
                log(f"  {pid}: 편집으로 손봄 → {'통과' if not p2 else '남음 ' + str(sorted(p2))}")
            return
        if attempt >= retries:
            break
        attempt += 1
        # 다시 그릴 때 지난 판에서 걸린 점을 그리기 지시에 붙인다(시드만 바꾸면 같은 실수를 되풀이했다)
        note = str(r.get("고칠점") or "")[:600]
        try:
            render(seed + 7919 * attempt, turned="시선" in probs, note=note)
        except TypeError:  # note·turned 를 모르는 옛 render
            render(seed + 7919 * attempt)
        log(f"  {pid}: 다시 그림({attempt}회)")
    if best and best[1] != dst.read_bytes() and best[0] < len(probs):
        dst.write_bytes(best[1])
        log(f"  {pid}: 가장 나은 판으로 되돌림")
    log(f"  {pid}: 연출 확인 남음 {sorted(best[2])} → 사람 눈으로 확인")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("panels_json")
    ap.add_argument("src_dir", help="1단 그림 폴더(예: episodes/ep002_v2/panels_hf_vast)")
    ap.add_argument("--only", required=True)
    ap.add_argument("--out")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--steps", type=int, default=8, help="터보 8(기본)")
    ap.add_argument("--no-expr", action="store_true")
    args = ap.parse_args()
    series, src = Path(args.series_dir), Path(args.src_dir)
    out = Path(args.out) if args.out else src.with_name(src.name + "_refined")
    out.mkdir(parents=True, exist_ok=True)
    panels = {p["id"]: p for p in json.loads(Path(args.panels_json).read_text(encoding="utf-8"))}
    c, cache = Comfy(), {}
    for pid in args.only.split(","):
        p = panels[pid]
        prompt, refs = refine_prompt(series, p, not args.no_expr)
        names = [upload(c, src / f"{pid}.png", cache)] + [upload(c, r, cache) for r in refs]
        g = graph(prompt, names, 0, 0, args.seed, args.steps, 1024, turbo=args.steps <= 8, edit=True)
        g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/refined"
        t = time.time()
        hist = c.wait(c.queue(g), 900)
        img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
        c.download(img, out / f"{pid}.png")
        (out / f"{pid}.txt").write_text(prompt, encoding="utf-8")
        print(f"{pid}: 다듬기 {time.time() - t:.0f}초 → {out / (pid + '.png')}", flush=True)


if __name__ == "__main__":
    main()
