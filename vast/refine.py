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
from comfy_panel import graph, pick_expr, upload  # noqa: E402


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


VLM_URL = "https://judge-gateway.invalid/v1"  # 그림을 보는 사내 Qwen(그림 검수와 같은 곳)


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
    who = "; ".join(f"{re.sub(r'\s*\(.*?\)', '', NAMES[c])}" + (f" (wears {wear[c]})" if c in wear else "") for c in chars) or "nobody"
    ask = (f"This webtoon panel should show exactly {len(chars)} {'person' if len(chars) == 1 else 'people'}: {who}."
           + (" An over-the-shoulder figure seen from behind in the foreground is also expected and is NOT extra." if p.get("camera") else "")
           + "\nLook at the image. List every EXTRA person who should not be there: duplicates of the same person (keep the one whose clothes match), "
           "strangers, and small background figures. Describe each extra person in one short phrase by clothes and position "
           "(e.g. 'the old woman in a red coat standing at the far left'). "
           'Answer only JSON: {"extra": ["...", "..."]} or {"extra": []}.')
    img = Image.open(img_path).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    body = {"model": "qwen3.8-27b", "temperature": 0.0, "max_tokens": 300, "chat_template_kwargs": {"enable_thinking": False},
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
