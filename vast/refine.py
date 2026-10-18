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
from comfy_panel import LLM_MODEL, LLM_URL, graph, is_ots, pick_expr, upload  # noqa: E402


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
