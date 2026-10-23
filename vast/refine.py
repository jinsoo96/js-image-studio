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
from comfy_panel import graph, panel_text, pick_expr, upload  # noqa: E402


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
    elif str(p.get("extras") or "").strip():  # 이름 없는 사람만 나오는 컷(소작인·군중): 그 사람들은 남긴다(2026-10-24 개항 p003a 를 다 지웠다)
        lines.append(f"1. Keep the unnamed people this panel needs ({p['extras']}). Remove anyone beyond them; fill the removed area with the background.")
    else:
        lines.append("1. There must be no people at all. Remove every person, face and figure; fill the area with the background.")
    if p.get("acting"):
        lines.append(f"2. Acting: {p['acting']} Make the faces and body language strong and instantly readable like a Korean webtoon.")
    if faces:
        lines.append("3. " + "; ".join(faces) + ". Keep each person's face, hairstyle and hair color identical to these, but keep the outfits in <image1>.")
    lines.append("No text, no speech bubbles.")
    return "\n".join(lines), refs




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
    # 이름 없는 사람(extras)도 기대하는 사람으로 첫 문장에 넣는다: 'exactly 0 people: nobody' 로 시작하니 컷 설명에 적은 소작인 넷·하인을
    # 남는 사람으로 보고 두 번 지웠다(2026-10-24 개항 p003a)
    extras_txt = str(p.get("extras") or "").strip()
    expected = (f"exactly {len(chars)} named {'person' if len(chars) == 1 else 'people'}: {who}" if chars else "no named characters")
    ask = (f"This webtoon panel should show {expected}" + (f", plus these unnamed people, who are expected and NOT extra: {extras_txt}" if extras_txt else "") + "."
           + f"\nPanel text: {panel_text(p)}\nAnyone else the panel text describes (an over-the-shoulder foreground figure, unnamed people) "
             "is also expected and is NOT extra."
           + "\nLook at the image. If it shows no more people than expected, answer with an empty list: different expected people can wear "
           "the same uniform, so do not call someone a duplicate just because the clothes match; compare faces, hair and build. "
           "Otherwise list every EXTRA person who should not be there: true duplicates of the same person (keep the one whose clothes match), "
           "strangers, and small background figures. Describe each extra person in one short phrase by clothes and position "
           "(e.g. 'the old woman in a red coat standing at the far left'). "
           'Answer only JSON: {"extra": ["...", "..."]} or {"extra": []}.')
    return [x for x in _vlm([img_path], ask).get("extra", []) if isinstance(x, str) and x.strip()]


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
        g = graph(remove_prompt(extras), [upload(c, img_path, cache)], 0, 0, seed + r, 8, 1024, turbo=True, edit=True, series=series)
        g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/fixed"
        hist = c.wait(c.queue(g), 600)
        img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
        c.download(img, img_path)
        log(f"  {p['id']}: 남는 사람 지움({r + 1}회) {extras}")
        changed = True
    return changed


EDITABLE = {"표정연기", "옷", "소품"}  # 편집으로 고칠 수 있는 것(소품 = 글과 다른 손의 물건). 좌우·시선·배경·인원은 구도라 다시 그린다


LIMB_ASK = ("Look only at the bodies. For each fully visible person (ignore people seen only from behind at the frame edge), count their arms "
            "and hands that are actually drawn, following each arm from the shoulder. Crossed arms count as two arms. If a person has an extra "
            "arm or hand (for example arms crossed AND another hand raised), say so. "
            'Answer only JSON: {"people": [{"who": "short description", "arms": N, "hands": N, "extra_limb": true|false}]}')


def extra_limbs(img_path: Path) -> list:
    """팔·손이 남는 사람(짧은 질문 하나로 따로 묻는다). 여러 항목을 함께 묻는 검수에선 '팔 셋'을 놓쳤고, 이것만 물으니 잡았다
    (2026-10-18 낙제생과 조교 p003: 팔짱 + 든 손). 실패하면 빈 목록."""
    people = [x for x in _vlm([img_path], LIMB_ASK).get("people", []) if isinstance(x, dict)]
    return [f"{x.get('who', '?')}: arms {x.get('arms')}, hands {x.get('hands')}" for x in people
            if x.get("extra_limb") or (x.get("arms") or 0) > 2 or (x.get("hands") or 0) > 2]


JUDGE_FAILS = {"n": 0, "last": ""}  # 검사 모델 호출 실패 횟수. staging_check 가 앞뒤로 보고 '검사 못 함'을 건다(통과로 넘기지 않는다)


def _vlm(images: list, text: str, max_tokens=300) -> dict:
    """그림 여러 장 + 질문 하나를 그림 보는 모델(judge_ep 가 고른 주소)에 묻고 JSON 을 받는다.
    모델에 닿지 않거나 답이 JSON 이 아니면 JUDGE_FAILS 를 올리고 빈 dict(부르는 쪽은 빈 답을 '문제 없음'으로 읽지 말고 staging_check 가 막는다).
    (2026-10-18 외출: judge 서버가 502 인데 모든 검사가 빈 답 = 통과로 넘어갔다)"""
    import base64
    import io
    from PIL import Image
    content = []
    for p in images:
        img = Image.open(p).convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
    content.append({"type": "text", "text": text})
    return _ask_json(content, max_tokens)


def _ask_json(content: list, max_tokens: int) -> dict:
    """판정 모델에 묻고 JSON 을 받는다(judge_ep.parse_json). 답이 잘리거나 깨지면 토큰을 세 배로 한 번 더 묻고, 그래도 못 받으면 JUDGE_FAILS 를 올리고 빈 dict."""
    from judge_ep import JudgeDown, chat, parse_json
    last = None
    for mt in (max_tokens, max_tokens * 3):
        try:
            return parse_json(chat(content, max_tokens=mt))
        except ValueError as e:
            last = e
        except JudgeDown as e:
            last = e
            break
    JUDGE_FAILS["n"] += 1
    JUDGE_FAILS["last"] = str(last)[:160]
    return {}


PRESENCE_ASK = """In this webtoon scene these people are present at this moment, even if this panel does not show them: {who}.
Where they are, from the neighbouring panels: {where}
This panel: {this}
Look at the picture. Does it clearly show the exact spot where one of them is (for example the seat right next to the visible person, or the bed they lie on) and that spot is EMPTY, so the person seems to have vanished?
Not a mistake: a tight close-up that leaves them out of frame; a person who sits across from the visible person or on the camera's side (their place is behind the camera, not in the picture); empty seats or tables that belong to nobody.
Answer JSON only: {{"vanished": ["name of each person whose own spot is visible and empty"]}}"""


def where_now(series, ep_dir: Path, pid: str, who: list) -> str:
    """앞뒤 컷의 컷 설명에서 그 사람의 자리를 말하는 문장을 모은다(장면 인원 검사에 넘긴다)."""
    from hf_panel import names
    nm = names(series)
    spec = {p["id"]: p for p in json.loads((ep_dir / "panels.json").read_text(encoding="utf-8"))}
    order = [b["id"] for b in json.loads((ep_dir / "storyboard.json").read_text(encoding="utf-8"))["panels"] if b["id"] in spec]
    k, loc = order.index(pid), spec[pid].get("location")
    near = [spec[i] for i in order[max(0, k - 3):k + 3] if i != pid and spec[i].get("location") == loc]
    out = []
    for c in who:
        short = re.sub(r"\s*\(.*?\)", "", nm.get(c, c)).split()[-1]
        for p in near:
            for s in re.split(r"(?<=[.;])\s+", str(p.get("action") or "")):
                if short in s and s not in out:
                    out.append(s.strip())
    return " ".join(out)[:600]


def scene_present(ep_dir: Path, pid: str) -> list:
    """앞뒤 컷(같은 장소, 앞 두 컷·뒤 두 컷)으로 '지금 이 장면에 있는' 사람을 구한다. 앞에도 있고 뒤에도 있거나(뒤에 같은 장소 컷이 없으면 앞만)
    이 컷에는 없는 인물. 사람이 사라진 컷을 잡는 데 쓴다(2026-10-18 외출: 인서트에 빈 침대, 택시 클로즈업에 빈 옆자리)."""
    sb, pj = ep_dir / "storyboard.json", ep_dir / "panels.json"
    if not sb.exists() or not pj.exists():
        return []
    spec = {p["id"]: p for p in json.loads(pj.read_text(encoding="utf-8"))}
    order = [b["id"] for b in json.loads(sb.read_text(encoding="utf-8"))["panels"] if b["id"] in spec]
    if pid not in order:
        return []
    k, loc = order.index(pid), spec[pid].get("location")
    before = [spec[i] for i in order[max(0, k - 2):k] if spec[i].get("location") == loc]
    after = [spec[i] for i in order[k + 1:k + 3] if spec[i].get("location") == loc]
    had = set(c for p in before for c in p.get("chars") or [])
    later = set(c for p in after for c in p.get("chars") or [])
    present = had & later if after else had
    return sorted(present - set(spec[pid].get("chars") or []))


# 중립으로 묻는다: '이 사람은 이 컷에 없어야 한다'고 먼저 말하면 판정 모델이 그 말을 사실로 받아 한가운데 앉은 사람도 '없다'고 했다(2026-10-20 p104)
ABSENT_ASK = """Image 2 shows one person ({looks}). Is that same person also drawn somewhere in image 1, background included (same face, hair, glasses and build)?
Answer JSON only: {{"present": true|false, "where": "short description by clothes and position in image 1, empty if not present"}}"""


def absent_cast(series, ep_dir: Path, pid: str, img_path: Path | None = None) -> list:
    """이 컷 인물 목록(chars)에 없는데 같은 장소의 다른 컷에 나오는 이름 있는 인물이 그림에 있나. [{"name", "where"}] (못 물으면 []).
    (2026-10-20 사용자 "빡빡이 인사하고 갔는데 왜 또 옆에 있어": 장면 기준판에서 잘라 낸 컷에 먼저 떠난 박 과장(p006)·카운터의 다방 주인(p464)·
     수요일에야 오는 은영(월요일 p226)이 들어갔다. '글에 없는 사람' 검사(find_extras)는 이름 있는 인물을 사무실 직원·손님으로 보고 넘겼다)"""
    spec = {p["id"]: p for p in json.loads((Path(ep_dir) / "panels.json").read_text(encoding="utf-8"))}
    p = spec.get(pid) or {}
    loc = p.get("location")
    if not loc or loc == "none":
        return []
    # 같은 장소라도 앞뒤 여덟 컷 안(같은 장면 언저리)만: 회차 전체로 모으면 다른 날 회의 장면의 김희원·이주연을 마지막 날 사무실 컷의
    # 이름 없는 팀원에게서 찾아 지우려다 세 번 실패했다(2026-10-20 p101)
    sb = Path(ep_dir) / "storyboard.json"
    order = [b["id"] for b in json.loads(sb.read_text(encoding="utf-8"))["panels"] if b["id"] in spec] if sb.exists() else list(spec)
    k = order.index(pid) if pid in order else 0
    same = [spec[i] for i in order[max(0, k - 8):k + 9] if spec[i].get("location") == loc]
    others = sorted({c for q in same for c in q.get("chars") or []} - set(p.get("chars") or []))
    if not others:
        return []
    wear = {}
    for q in same:
        for c, o in (q.get("outfit") or {}).items():
            wear.setdefault(c, o)
    from hf_panel import names
    nm = names(Path(series))
    img = img_path or Path(ep_dir) / "panels_hf" / f"{pid}.png"
    out = []
    # 글 설명만 주고 물으면 그림 보는 모델(사내 Qwen3-VL-8B)이 한가운데 선 박 과장도 '없다'고 했다 → 그 인물의 상반신 참고 그림을 같이 보여 주고 한 명씩
    for c in others:
        ref = Path(series) / "assets" / "vast_refs" / f"{c}_bust.png"
        if not ref.exists():
            continue
        name = re.sub(r"\s*\(.*?\)", "", nm.get(c, c))
        r = _vlm([img, ref], ABSENT_ASK.format(looks=_looks(series, {"chars": [c], "outfit": wear})))
        if r.get("present") is True:
            out.append({"name": name, "where": str(r.get("where") or "")})
    return out


CHECKS = ROOT / "skills" / "staging-review" / "checks.json"


def _looks(series, p) -> str:
    """검사에 넘길 인물 생김새(이름 · 작품 설정의 생김새 첫 문장 · 이 컷의 옷). 그림 속 누가 누구인지 판정 모델이 알아보게."""
    from hf_panel import names
    series = Path(series)
    nm = names(series)
    sj = series / "series.json"
    cast = json.loads(sj.read_text(encoding="utf-8")).get("cast", {}) if sj.exists() else {}
    out = []
    for c in p.get("chars") or []:
        who = re.split(r"(?<=\.)\s", str(cast.get(c, {}).get("who", "")))[0][:140]
        wear = (p.get("outfit") or {}).get(c)
        out.append(f"{re.sub(r'\s*\(.*?\)', '', nm.get(c, c))}" + (f" ({who})" if who else "") + (f" wearing {wear}" if wear else ""))
    return "; ".join(out)


def _text_json(text: str, max_tokens=200) -> dict:
    """그림 없이 글만 판정 모델에 묻고 JSON 을 받는다(못 받으면 빈 dict, JUDGE_FAILS 를 올린다)."""
    return _ask_json([{"type": "text", "text": text}], max_tokens)


def _compare(check: dict, fill: dict, img_path: Path) -> list:
    """비교형 항목: expect(글만 보고 기대값 {이름: 값}) · observe(그림을 보고 관찰값 {이름: 값})를 따로 묻고, 둘 다 값이 있는데 맞지 않는 이름만 걸린 점으로.
    conflicts(기준표 데이터)가 있으면 그 짝만 어긋남으로 본다(예: 시선 'down' 과 'left' 는 함께 일어날 수 있어 어긋남이 아니다). 없으면 값이 다르면 어긋남."""
    want = _text_json(check["expect"].format(**fill), max_tokens=check.get("max_tokens", 200))
    got = _vlm([img_path], check["observe"].format(**fill), max_tokens=check.get("max_tokens", 200))
    skip = set(check.get("unknown", ["unknown", "unclear", "none", ""]))
    # directed(기준표 데이터): 짝을 (기대, 관찰) 순서로 본다. 입은 '말없음 → 말하는 입'만 문제다(말하는 컷에서 입을 다문 건 괜찮다)
    pairs = {tuple(x) for x in check.get("conflicts", [])} if check.get("directed") else {frozenset(x) for x in check.get("conflicts", [])}
    low = {str(k).lower(): v for k, v in got.items()}
    out = []
    for k, v in want.items():
        g = low.get(str(k).lower())
        if not (isinstance(v, str) and isinstance(g, str)) or v.lower() in skip or g.lower() in skip or v.lower() == g.lower():
            continue
        key = (v.lower(), g.lower()) if check.get("directed") else frozenset((v.lower(), g.lower()))
        if pairs and key not in pairs:
            continue
        out.append(check["problem"].format(name=k, want=v, got=g))
    return out


def _kind_check(c: dict, fill: dict, img_path: Path, series, p: dict) -> list:
    """사실 하나씩 묻는 항목(checks.json kind). 약한 판정 모델도 여럿을 한꺼번에 물으면 놓치고 하나씩 물으면 잡았다(2026-10-20 하네스 시험,
    tools/harness_selftest.py). facts: 글에서 꼭 보여야 할 사실을 예/아니오 질문으로 뽑아 하나씩 · count_each: 인물마다 그 생김새인 사람 수 ·
    fixed: 고정 질문 하나(field 가 양수면 걸림) · each: 사람마다 한 질문(expect 가 'no' 인 사람이 bad_value 면 걸림)."""
    from hf_panel import names
    nm = names(Path(series))
    out = []
    kind = c["kind"]
    if kind == "facts":
        for f in (_text_json(c["extract"].format(**fill), c.get("max_tokens", 400)).get("facts") or [])[:4]:
            if not isinstance(f, dict) or str(f.get("a", "")).lower() not in ("yes", "no"):
                continue
            got = str(_vlm([img_path], c["ask_each"].format(q=f.get("q", "")), 60).get("answer", "")).lower()
            if got in ("yes", "no") and got != f["a"].lower():
                out.append(c["problem"].format(q=f.get("q", ""), want=f["a"], got=got))
    elif kind == "count_each":
        wears = {ch: " ".join(re.split(r",| with | over | under ", (p.get("outfit") or {}).get(ch, ""))[0].split()[:4]) for ch in p.get("chars") or []}
        for ch, top in wears.items():
            if not top:
                continue
            same = sum(1 for t in wears.values() if t.lower() == top.lower())  # 같은 옷을 입은 인물이 여럿이면 그만큼은 정상
            n = _vlm([img_path], c["observe"].format(top=top), 60).get("count")
            if isinstance(n, int) and n > same:
                out.append(c["problem"].format(name=re.sub(r"\s*\(.*?\)", "", nm.get(ch, ch)), n=n))
    elif kind == "fixed":
        v = _vlm([img_path], c["observe"].format(**fill), 80).get(c["field"])
        if isinstance(v, (int, float)) and v > 0 and c.get("bad") == "positive":
            out.append(c["problem"].format(v=v))
    elif kind == "each":
        want = _text_json(c["expect"].format(**fill), c.get("max_tokens", 150))
        low = {str(k).lower(): str(v).lower() for k, v in want.items()}
        for ch in p.get("chars") or []:
            name = re.sub(r"\s*\(.*?\)", "", nm.get(ch, ch))
            if low.get(name.lower(), "no") != "no":
                continue
            got = str(_vlm([img_path], c["observe"].format(looks=_looks(series, {"chars": [ch], "outfit": p.get("outfit")})), 60).get("looking", "")).lower()
            if got == c["bad_value"]:
                out.append(c["problem"].format(name=name))
    return out


def run_checks(series, img_path: Path, p: dict, layout: str = "", speakers: str = "", only: set | None = None) -> dict:
    """기준표(skills/staging-review/checks.json)의 항목을 돌려 {항목 이름(category): [걸린 점]}. 항목마다 이 컷의 글을 주고 글대로인지만 묻는다.
    어느 항목을 돌릴지는 인물 수로만 정한다(글 속 문구로 켜고 끄지 않는다)."""
    spec = json.loads(CHECKS.read_text(encoding="utf-8"))
    sj = Path(series) / "series.json"
    era = (json.loads(sj.read_text(encoding="utf-8")).get("era_rule") if sj.exists() else "") or ""
    n = len(p.get("chars") or [])
    fill = {"text": panel_text(p), "layout": layout or "(none)", "looks": _looks(series, p) or "(none)",
            "era": f" Objects must fit this period: {era}" if era else "", "common": spec.get("common", "")}
    from hf_panel import names
    nm = names(Path(series))
    fill["names"] = ", ".join(re.sub(r"\s*\(.*?\)", "", nm.get(c, c)) for c in p.get("chars") or [])
    fill["speakers"] = speakers or "(not given)"
    out = {}
    for c in spec["checks"]:
        if only and c["name"] not in only:
            continue
        when = c.get("when", "always")
        if (when == "people" and not (n or p.get("extras"))) or (when == "two_or_more" and n < 2):
            continue
        if c.get("kind"):  # 사실 하나씩 묻는 항목(_kind_check)
            found = _kind_check(c, fill, img_path, series, p)
        elif "expect" in c:  # 비교형: 글에서 기대값, 그림에서 관찰값을 따로 받아 코드가 비교한다(판정 모델 혼자 비교하면 좌우를 놓쳤다, 외출 p016)
            found = _compare(c, fill, img_path)
        else:
            found = [str(x) for x in _vlm([img_path], c["ask"].format(**fill), max_tokens=c.get("max_tokens", 300)).get("problems", []) if x]
        if found and spec.get("verify") and c.get("verify", True):  # 걸린 점만 두 번째 판정이 다시 거른다(트집·좌우 착각을 덜어 낸다)
            listing = "\n".join(f"{k}. {x}" for k, x in enumerate(found))
            keep = _vlm([img_path], spec["verify"].format(text=fill["text"], problems=listing), max_tokens=120).get("keep")
            if isinstance(keep, list):
                found = [found[k] for k in keep if isinstance(k, int) and 0 <= k < len(found)]
        if found:
            out.setdefault(c["category"], []).extend(found)
    return out


CONT_ASK = """Image 1 is the previous panel and image 2 is the next panel of the SAME scene in a webtoon (continuous time).
Panel 1 shows: {who1}. Panel 2 shows: {who2}. Compare only the people who appear in BOTH panels; a different person in the next
panel (a shot of the other speaker, a close-up of one person after a wide shot) is normal, not a mistake.
What happens in panel 2: {desc}
List differences between the two panels that a reader would notice as continuity mistakes, unless the text above explains them:
a person who was sitting is now standing (or the reverse); people who sat together at one table now sit at different tables or far apart; the number or kind of drinks, cups or props on the table changes; a handheld object such as a phone changes size a lot; clothes, hairstyle or glasses change; a person looks like a different person (face, age, build); the drawing style changes (line, coloring, proportions, realism); the left-right positions of the people swap.
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
    """그린 컷을 그 컷의 글과 맞대어 본다. (결과, 걸린 항목)
    ① 여러 항목 검수(mcp/vision_review: 인원·좌우시선·옷·표정연기·배경, 두 번 보고 둘 다 걸린 것만) ② 팔·손 개수(글과 무관한 몸 구조)
    ③ 장면에 있는데 이 컷에서 사라진 사람(앞뒤 컷) ④ 기준표 항목(skills/staging-review/checks.json: 시선·몸 방향·좌우·구도·논리·소품).
    ④는 항목마다 이 컷의 글을 통째로 주고 글대로인지만 묻는다. 검사 쪽에 고정 규칙이나 글 속 문구로 켜고 끄는 조건을 두지 않는다."""
    sys.path.insert(0, str(ROOT / "mcp"))
    import vision_review as V
    fails0 = JUDGE_FAILS["n"]
    V.DIRS[:] = [img_path.parent.name]
    r = V.review(series, ep_dir, [pid], workers=2)[0]
    probs = set(V._probs(r)) if r.get("판정") == "다시" else set()
    if r.get("error") or r.get("raw"):  # 검수 모델이 답을 못 줬다: 통과로 넘기지 않는다
        JUDGE_FAILS["n"] += 1
        JUDGE_FAILS["last"] = str(r.get("error") or "형식 밖 답")[:160]
    notes = [str(r.get("고칠점") or "")]
    limbs = extra_limbs(img_path)
    if limbs:
        probs.add("몸")
        notes.append("팔·손이 남는다: " + "; ".join(limbs))
    # 손 세기는 검출기로(tools/hand_check.py): 8B 판정은 셋째 손·같은 쪽 손 둘을 놓쳤다(2026-10-21 외출 p302·p428·p468)
    p0 = next((x for x in json.loads((ep_dir / "panels.json").read_text(encoding="utf-8")) if x["id"] == pid), {})
    from hand_check import check as hand_check
    hc = hand_check(img_path, p0.get("chars"))
    if hc["strong"]:
        probs.add("몸")
    notes += hc["strong"] + hc["review"]
    p = next((x for x in json.loads((ep_dir / "panels.json").read_text(encoding="utf-8")) if x["id"] == pid), {})
    sc = ep_dir / "panels_spec.json"
    layout = (json.loads(sc.read_text(encoding="utf-8")).get("scenes", {}).get(p.get("scene") or p.get("location") or "", "") if sc.exists() else "")
    absent = scene_present(ep_dir, pid)
    if absent:
        from hf_panel import names
        nm = names(series)
        vanished = _vlm([img_path], PRESENCE_ASK.format(who=", ".join(nm.get(c, c) for c in absent),
                                                       where=where_now(series, ep_dir, pid, absent) or layout[:300], this=panel_text(p))).get("vanished", [])
        if vanished:
            # '빈자리'(사람이 모자람)는 '인원'(사람이 남음)과 다르게 둔다: 같은 이름이라 사람 수 맞추기가 모자란 컷에서 사람을 더 지웠다(외출 p077)
            probs.add("빈자리")
            notes.append("장면에 있는 사람이 사라진 것처럼 자리가 비어 보인다(" + ", ".join(map(str, vanished))
                         + "): 더 가까이 잡아 빈자리가 안 보이게 하거나 그 사람의 일부를 화면 끝에")
    seen = absent_cast(series, ep_dir, pid, img_path)
    if seen:
        probs.add("인원")
        notes.append("인원: 이 순간 없어야 할 인물이 보인다(" + "; ".join(f"{x['name']}: {x.get('where', '')}" for x in seen) + ")")
    board = json.loads((ep_dir / "storyboard.json").read_text(encoding="utf-8")) if (ep_dir / "storyboard.json").exists() else {}
    bp = next((x for x in (board.get("panels") or []) if x.get("id") == pid), {})
    from hf_panel import names as _nm
    nmap = _nm(series)
    spk = sorted({d.get("who") for d in bp.get("dialogue") or [] if d.get("who") and d.get("type") not in ("monologue", "narration", "caption", "sfx")})
    speakers = ", ".join(re.sub(r"\s*\(.*?\)", "", nmap.get(w, w)) for w in spk) or "nobody (only narration, no one speaks aloud)"
    for cat, found in run_checks(series, img_path, p, layout, speakers).items():
        probs.add(cat)
        notes.append(f"{cat}: " + "; ".join(found))
    if JUDGE_FAILS["n"] > fails0:
        probs.add("검사불가")
        notes.append(f"검사 모델이 답하지 못한 항목이 {JUDGE_FAILS['n'] - fails0}개({JUDGE_FAILS['last'][:80]})")
    r["고칠점"] = " ".join(x for x in notes if x).strip()
    return r, probs


def staging_edit_prompt(series, p, r, probs) -> str:
    """편집으로 고칠 수 있는 항목(EDITABLE)을 그 컷의 글에 맞추는 편집 지시. 고칠 내용은 검수가 적은 걸린 점과 컷의 글에서 온다."""
    from hf_panel import names
    NAMES = names(series)
    parts = []
    if "표정연기" in probs:
        parts.append(f"Change only the facial expressions and body language to match this acting: {p.get('acting') or p['action']}")
    if "소품" in probs:
        parts.append(f"Fix only the objects people hold or use so they match the panel text: {panel_text(p)[:400]}")
    if "옷" in probs:
        wear = "; ".join(f"{re.sub(r'\s*\(.*?\)', '', NAMES[c])} wears {o}" for c, o in (p.get("outfit") or {}).items() if c in p.get("chars", []))
        if wear:
            parts.append(f"Change the clothes to match: {wear}.")
    if r.get("고칠점"):
        parts.append(f"Problems found: {r['고칠점']}")
    parts.append("Keep everything else in <image1> exactly the same: who is in the panel, their positions and facing directions, faces, hair, "
                 "the room, lighting, camera angle and framing.")
    return " ".join(parts)


def record_review(ep_dir: Path, pid: str, issues=None, note: str = ""):
    """사람 확인이 남은 컷을 <회차>/review/staging.json 에 남긴다(issues 가 없으면 그 컷을 지운다). 작업 결과(mcp job_status)가 로그 문장을
    긁지 않고 이 파일을 읽는다."""
    f = Path(ep_dir) / "review" / "staging.json"
    cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    if issues:
        cur[pid] = {"issues": sorted(issues), "note": note[:600], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    else:
        cur.pop(pid, None)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")


def staging_pass(c, cache, dst: Path, series, ep_dir: Path, p, seed, render, retries=1, log=print):
    """3단 연출 확인(2026-10-16 사용자 "시점·표정·공간 자각·카메라 각도를 잘 고려해야").
    표정·옷만 틀리면 편집으로 고치고, 좌우·시선·배경·인원이 틀리면 시드를 바꿔 다시 그린다. 더 나빠지면 나은 판으로 되돌린다.
    끝나면 사람 확인이 남은 컷을 review/staging.json 에 남긴다(record_review)."""
    left, note = _staging_pass(c, cache, dst, series, ep_dir, p, seed, render, retries, log)
    record_review(ep_dir, p["id"], left, note)


def _staging_pass(c, cache, dst: Path, series, ep_dir: Path, p, seed, render, retries=1, log=print):
    """staging_pass 의 몸통. (남은 항목, 메모)를 돌려준다(통과면 빈 집합)."""
    pid = p["id"]
    best = None
    attempt = 0
    while True:
        r, probs = staging_check(series, ep_dir, dst, pid)
        if best is None or len(probs) < best[0]:
            best = (len(probs), dst.read_bytes(), probs)
        if not probs:
            log(f"  {pid}: 연출 확인 통과" + (f" (애매, 사람 눈으로: {r.get('기타', '')[:120]})" if r.get("판정") == "애매" else ""))
            return set(), ""
        log(f"  {pid}: 연출 확인 걸림 {sorted(probs)} {str(r.get('고칠점', ''))[:120]}")
        if probs == {"검사불가"}:  # 검사를 못 했다: 다시 그려도 소용없다. 통과로도 넘기지 않는다
            log(f"  {pid}: 연출 확인 남음 ['검사불가'] → 사람 눈으로 확인(검사 모델에 닿지 않음)")
            return probs, str(r.get("고칠점") or "")
        # 좌우만 바뀌었으면(그림에 글자가 없을 때) 다시 그리지 않고 뒤집는다. 다시 그리면 또 바뀌었다(외출 p012)
        if "좌우" in probs and not p.get("text_in_image") and not getattr(_staging_pass, "_flipped", {}).get(pid):
            from PIL import Image, ImageOps
            ImageOps.mirror(Image.open(dst).convert("RGB")).save(dst)
            _staging_pass._flipped = {**getattr(_staging_pass, "_flipped", {}), pid: True}
            log(f"  {pid}: 좌우를 뒤집음(장면 배치와 반대로 그려졌다)")
            continue
        if probs <= EDITABLE:
            before = dst.read_bytes()
            keep = dst.parent / "_stage1"
            keep.mkdir(exist_ok=True)
            (keep / f"{pid}_before_check.png").write_bytes(before)
            g = graph(staging_edit_prompt(series, p, r, probs), [upload(c, dst, cache)], 0, 0, seed % 100000 + 17, 8, 1024, turbo=True, edit=True, series=series)
            g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/checked"
            hist = c.wait(c.queue(g), 600)
            c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), dst)
            r2, p2 = staging_check(series, ep_dir, dst, pid)
            if len(p2) > len(probs):
                dst.write_bytes(before)
                log(f"  {pid}: 편집 뒤 더 나빠져 되돌림 {sorted(p2)}")
                return probs, str(r.get("고칠점") or "")
            log(f"  {pid}: 편집으로 손봄 → {'통과' if not p2 else '남음 ' + str(sorted(p2))}")
            return p2, str(r2.get("고칠점") or "")
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
    # 고치기 사다리의 마지막 칸: 다시 그려도 안 고쳐진 시선·방향은 편집으로 돌린다(외출 p004 박 과장: 다시 그리면 또 정면, 편집은 한 번에 됐다)
    if best[2] and best[2] <= {"시선", "좌우시선"} and fix_facing(c, cache, dst, series, ep_dir, p, seed, log):
        return set(), ""
    # 인원이 남으면(같은 사람 둘 등) 콕 집은 지우기 편집 + 사람 수만 따로 세서 확인(외출 p008: 다시 그리기·남는 사람 지우기로 안 되던 것이 한 번에 됐다)
    if "인원" in best[2] and fix_count(c, cache, dst, p, seed, log):
        return set(), ""
    log(f"  {pid}: 연출 확인 남음 {sorted(best[2])} → 사람 눈으로 확인")
    return best[2], str(r.get("고칠점") or "")


def fix_count(c, cache, dst: Path, p: dict, seed, log=print) -> bool:
    """글에 없는 사람(중복·낯선 사람)이 남으면 한 명씩 위치·옷으로 짚어 지우고, 글에 없는 사람이 없어질 때까지 최대 3번(맞으면 True,
    아니면 원본으로 되돌리고 False). '글에 적힌 사람보다 많은가'는 find_extras 가 컷의 글을 읽고 판단한다(사람 수를 글의 숫자 낱말로 세지 않는다).
    이 순간 없어야 할 이름 있는 인물(absent_cast: 먼저 떠난 사람 등)도 같이 지운다."""
    series, ep_dir = Path(dst).parents[3], Path(dst).parents[1]
    before = dst.read_bytes()

    def left():
        return find_extras(dst, series, p) + [f"{x['name']} ({x.get('where', '')})" for x in absent_cast(series, ep_dir, p["id"], dst)]

    for k in range(3):
        extra = left()
        if not extra:
            log(f"  {p['id']}: 글에 없는 사람 없음 → 통과")
            return True
        prompt = (f"In <image1>, remove {extra[0]} entirely; fill the space with the background around them. Keep everyone else, the place, the light "
                  "and the framing exactly the same.")
        g = graph(prompt, [upload(c, dst, cache)], 0, 0, seed % 100000 + 41 + k, 8, 1024, turbo=True, edit=True, series=series)
        g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/count_fix"
        hist = c.wait(c.queue(g), 600)
        c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), dst)
        log(f"  {p['id']}: 남는 사람 콕 집어 지움({k + 1}회): {extra[0]}")
    if not left():
        log(f"  {p['id']}: 글에 없는 사람 없음 → 통과")
        return True
    dst.write_bytes(before)
    log(f"  {p['id']}: 남는 사람을 못 지움 → 되돌림")
    return False


TURN = ("In <image1>, change only where these people look and which way their face and shoulders turn, so the picture matches the panel text. "
        "Problems: {problems}. Panel text: {text}. Keep everything else in <image1> exactly the same: who is in the panel, faces, clothes, hands and "
        "what they hold, positions, the place, the light, the camera angle and the framing.")


def fix_facing(c, cache, dst: Path, series, ep_dir: Path, p, seed, log=print) -> bool:
    """시선·몸 방향이 글과 다른 사람만 편집으로 돌린다. 다시 확인해 시선 문제가 없어지면 True(아니면 원본으로 되돌리고 False)."""
    gaze = run_checks(series, dst, p).get("시선", [])
    if not gaze:
        return False
    before = dst.read_bytes()
    g = graph(TURN.format(problems="; ".join(dict.fromkeys(gaze)), text=panel_text(p)[:500]), [upload(c, dst, cache)], 0, 0, seed % 100000 + 29, 8, 1024,
              turbo=True, edit=True, series=series)
    g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/turned"
    hist = c.wait(c.queue(g), 600)
    c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), dst)
    left = run_checks(series, dst, p).get("시선", [])
    if left:
        dst.write_bytes(before)
        log(f"  {p['id']}: 방향 편집도 안 됨({'; '.join(left)[:80]}) → 되돌림")
        return False
    log(f"  {p['id']}: 시선·방향을 편집으로 글에 맞춤 → 통과")
    return True


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
        g = graph(prompt, names, 0, 0, args.seed, args.steps, 1024, turbo=args.steps <= 8, edit=True, series=series)
        g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/refined"
        t = time.time()
        hist = c.wait(c.queue(g), 900)
        img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
        c.download(img, out / f"{pid}.png")
        (out / f"{pid}.txt").write_text(prompt, encoding="utf-8")
        print(f"{pid}: 다듬기 {time.time() - t:.0f}초 → {out / (pid + '.png')}", flush=True)


if __name__ == "__main__":
    main()
