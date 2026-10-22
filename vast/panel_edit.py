"""그린 컷의 일부만 고치기(MCP edit_panel·derive_panel·fix_faces·panel_versions·revert_panel 이 부른다). 다시 그리지 않고 그 판의 '편집' 워크플로우로.
(2026-10-20 사용자 "부분부분만 고치는 것도 가능하냐, MCP 도 고도화해서 알아서 컨피 워크플로우를 활용해 작품을 만들어야": 이날 고친 32컷
 (지우기·버스 세우기·폰 쥔 손·얼굴 맞추기·잘 된 옆 컷에서 잘라 다시)은 스크래치 스크립트로 했고 MCP 에는 컷 통째 다시 그리기밖에 없었다)

    .venv/Scripts/python vast/panel_edit.py edit     <작품> <회차> <컷> "<지시>" [--refs sangsu,eunyoung|파일] [--seed N]
    .venv/Scripts/python vast/panel_edit.py derive   <작품> <회차> <컷> --from <잘 된 컷> --box x0,y0,x1,y1 "<지시>" [--refs …]
    .venv/Scripts/python vast/panel_edit.py faces    <작품> <회차> p061:eunyoung,p496
    .venv/Scripts/python vast/panel_edit.py versions <작품> <회차> <컷>
    .venv/Scripts/python vast/panel_edit.py revert   <작품> <회차> <컷> <판 이름(versions 의 name)>
    .venv/Scripts/python vast/panel_edit.py preview  <작품> <회차> <컷> "<지시>" [--refs …]      본편은 그대로, review/previews/ 에만
    .venv/Scripts/python vast/panel_edit.py design   <작품> <회차> <컷> "<바꿀 디자인>" --who <인물>   그 인물 디자인을 바꾼 모습 미리 보기
    .venv/Scripts/python vast/panel_edit.py adopt    <작품> <회차> <컷> <미리 보기 이름>          미리 보기를 본편에

- 고치는 그림은 지금 본편(panels_hf/<컷>.png). 결과는 vast 판(panels_hf_vast)에 쓰고 본편에 넣는다(옛 본편은 panels_hf/_rejected/).
- 고칠 때마다 전/후 나란히 그림을 <회차>/review/edits/<컷>_<시각>.jpg 에 남긴다(사람·에이전트가 눈으로 확인).
- edit: 지시는 "무엇을 어떻게 바꾸고 나머지는 그대로"로. <image1> 이 고칠 컷, refs 는 <image2>부터(인물 id 면 그 인물 상반신 참고 그림).
- derive: 같은 장면의 잘 된 컷에서 상자(0~1000 비율)를 잘라 키운 뒤 이 컷의 순간으로 다시 그린다(자리·테이블·배경이 그 컷과 같다).
- faces: 인물 기준 얼굴로 얼굴·머리만 다시 그리고 얼굴 거리(CCIP)가 가까워진 판만 남긴다.
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import SIZES, graph, upload  # noqa: E402
from sync_vast import place  # noqa: E402


def _ep(series: Path, episode: str) -> Path:
    ep = series / "episodes" / episode
    if not (ep / "panels.json").exists():
        raise SystemExit(f"회차가 없다: {ep}")
    return ep


def _spec(ep: Path, pid: str) -> dict:
    d = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    return next((p for p in (d["panels"] if isinstance(d, dict) else d) if p["id"] == pid), {"id": pid, "chars": []})


def _refs(series: Path, refs: str) -> list:
    out = []
    for r in [x.strip() for x in (refs or "").split(",") if x.strip()]:
        f = series / "assets" / "vast_refs" / f"{r}_bust.png"
        f = f if f.exists() else (series / r)
        if not f.exists():
            raise SystemExit(f"참고 그림이 없다: {r}(인물 id 나 작품 폴더 안 파일)")
        out.append(f)
    return out


def _start(ep: Path, pid: str) -> tuple:
    """본편 그림을 vast 판 자리로 가져온다(고칠 원본). (vast 판 경로, 원본 바이트)"""
    main, v = ep / "panels_hf" / f"{pid}.png", ep / "panels_hf_vast" / f"{pid}.png"
    src = main if main.exists() else v
    if not src.exists():
        raise SystemExit(f"{pid} 그림이 없다")
    v.parent.mkdir(exist_ok=True)
    before = src.read_bytes()
    if src != v:
        v.write_bytes(before)
    return v, before


def compare(ep: Path, pid: str, before: bytes, after: Path, how: str) -> Path:
    """전/후 나란히 한 장(<회차>/review/edits/<컷>_<시각>.jpg)."""
    import io
    a = Image.open(io.BytesIO(before)).convert("RGB")
    b = Image.open(after).convert("RGB")
    W = 640
    a, b = (x.resize((W, int(x.height * W / x.width))) for x in (a, b))
    out = Image.new("RGB", (2 * W + 8, max(a.height, b.height) + 30), "white")
    out.paste(a, (0, 30))
    out.paste(b, (W + 8, 30))
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/malgunbd.ttf", 18)
    except OSError:
        f = ImageFont.load_default()
    ImageDraw.Draw(out).text((6, 5), f"{pid} 고치기 전(왼) | 후(오) · {how}"[:110], fill="black", font=f)
    d = ep / "review" / "edits"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{pid}_{time.strftime('%m%d-%H%M%S')}.jpg"
    out.save(dst, quality=88)
    return dst


def _run_edit(series: Path, src: Path, refs: list, prompt: str, seed: int, dst: Path):
    c, cache = Comfy(), {}
    g = graph(prompt, [upload(c, src, cache)] + [upload(c, r, cache) for r in refs], 0, 0, seed, 8, 1024, turbo=True, edit=True, series=series)
    g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/panel_edit"
    h = c.wait(c.queue(g), 900)
    st = h.get("status", {})
    if st.get("status_str") == "error":
        raise SystemExit("편집 실패: " + str(next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), ""))[:300])
    c.download(next(i for o in h["outputs"].values() for i in o.get("images", [])), dst)


def edit(series: Path, ep: Path, pid: str, prompt: str, refs: str = "", seed: int = 0) -> dict:
    v, before = _start(ep, pid)
    _run_edit(series, v, _refs(series, refs), prompt, seed or int(time.time()) % 100000, v)
    place(ep, pid)
    return {"panel": pid, "how": "edit", "compare": str(compare(ep, pid, before, v, prompt))}


def preview(series: Path, ep: Path, pid: str, prompt: str, refs: str = "", seed: int = 0, how: str = "") -> dict:
    """본편을 건드리지 않고 고친 모습만 본다(2026-10-22 사용자 "한 컷만 고쳐서 보여주는 것, 디자인 바꾸면 어떻게 될지도 궁금하다").
    결과는 <회차>/review/previews/<컷>_<시각>.png(+ 전/후 나란히 .jpg). 마음에 들면 adopt 로 본편에 넣는다."""
    main, v = ep / "panels_hf" / f"{pid}.png", ep / "panels_hf_vast" / f"{pid}.png"
    src = main if main.exists() else v
    if not src.exists():
        raise SystemExit(f"{pid} 그림이 없다")
    d = ep / "review" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{pid}_{time.strftime('%m%d-%H%M%S')}.png"
    _run_edit(series, src, _refs(series, refs), prompt, seed or int(time.time()) % 100000, out)
    cmp = compare(ep, pid, src.read_bytes(), out, f"미리 보기: {how or prompt}")
    shutil.move(str(cmp), str(out.with_suffix(".jpg")))  # 고치기 기록(review/edits)과 섞이지 않게 미리 보기 옆에
    return {"panel": pid, "how": "preview", "preview": out.stem, "image": str(out), "compare": str(out.with_suffix(".jpg"))}


def design(series: Path, ep: Path, pid: str, who: str, change: str, seed: int = 0) -> dict:
    """인물 디자인(머리·옷·소품 등)을 바꾸면 어떻게 보일지 그 인물이 나오는 한 컷으로 미리 본다. 시트·본편은 그대로."""
    from hf_panel import names
    name = re.sub(r"\s*\(.*?\)", "", names(series).get(who, who))
    prompt = (f"In <image1>, change {name}'s design: {change}. Keep {name}'s face, expression, pose and hands, and keep everything else "
              "(the other people, the place, the light, the framing and the art style) exactly the same.")
    return preview(series, ep, pid, prompt, seed=seed, how=f"{name} 디자인: {change}")


def adopt(ep: Path, pid: str, name: str) -> dict:
    """미리 보기(preview 의 이름, 예 p020_1022-151233)를 본편에 넣는다. 옛 본편은 panels_hf/_rejected/ 로(place 와 같은 길)."""
    f = ep / "review" / "previews" / f"{name}.png"
    if not f.exists():
        raise SystemExit(f"미리 보기가 없다: {f.name}")
    v = ep / "panels_hf_vast" / f"{pid}.png"
    v.parent.mkdir(exist_ok=True)
    before = (ep / "panels_hf" / f"{pid}.png").read_bytes() if (ep / "panels_hf" / f"{pid}.png").exists() else f.read_bytes()
    shutil.copy2(f, v)
    place(ep, pid)
    return {"panel": pid, "how": "adopt", "from": name, "compare": str(compare(ep, pid, before, v, f"미리 보기 {name} 를 본편에"))}


def derive(series: Path, ep: Path, pid: str, src_pid: str, box: str, prompt: str, refs: str = "", seed: int = 0) -> dict:
    anchor = ep / "panels_hf" / f"{src_pid}.png"
    if not anchor.exists():
        raise SystemExit(f"잘라 올 컷 {src_pid} 그림이 없다")
    x0, y0, x1, y1 = [float(x) for x in box.split(",")]
    im = Image.open(anchor).convert("RGB")
    W, H = im.size
    crop = im.crop((int(x0 * W / 1000), int(y0 * H / 1000), int(x1 * W / 1000), int(y1 * H / 1000)))
    tw, th = SIZES.get(_spec(ep, pid).get("aspect") or "", (0, 0))
    if not tw:  # 컷 비율이 없으면 자른 상자 비율 그대로 면적 약 150만 화소
        k = (1.5e6 / (crop.width * crop.height)) ** 0.5
        tw, th = int(crop.width * k) // 16 * 16, int(crop.height * k) // 16 * 16
    cf = ep / "scenes" / f"crop_{pid}_from_{src_pid}.png"
    cf.parent.mkdir(exist_ok=True)
    crop.resize((tw, th), Image.LANCZOS).save(cf)
    v, before = _start(ep, pid)
    full = (f"<image1> is an enlarged crop of another panel of the same scene. Redraw it as a sharp, detailed Korean webtoon panel of this moment, "
            f"keeping the same place, table, objects, light and the same people's faces, hair and clothes: {prompt} Clean line art, no text, no speech bubbles.")
    _run_edit(series, cf, _refs(series, refs), full, seed or int(time.time()) % 100000, v)
    place(ep, pid)
    return {"panel": pid, "how": f"derive from {src_pid}", "compare": str(compare(ep, pid, before, v, f"{src_pid} 에서 잘라: {prompt}"))}


def faces(series: Path, ep: Path, items: str) -> list:
    import qa_fix
    c, cache = Comfy(), {}
    out = []
    for k, it in enumerate([x.strip() for x in items.split(",") if x.strip()]):
        pid, _, who = it.partition(":")
        p = _spec(ep, pid)
        who = who or (p.get("chars") or [""])[0]
        if not who:
            out.append({"panel": pid, "error": "인물이 없는 컷"})
            continue
        v, before = _start(ep, pid)
        d0, d1 = qa_fix.fix_face(c, cache, series, ep, "panels_hf_vast", p, who, 9100 + 7 * k)
        place(ep, pid)
        out.append({"panel": pid, "who": who, "distance_before": d0, "distance_after": d1,
                    "kept": d0 is not None and d1 is not None and d1 < d0, "compare": str(compare(ep, pid, before, v, f"{who} 얼굴 맞추기"))})
    return out


def versions(ep: Path, pid: str) -> list:
    """이 컷의 예전 판들(새 것부터): 본편에서 밀려난 판, 고치기 전, 기준판 전, 1단 판. name 으로 revert 한다."""
    spots = [(ep / "panels_hf" / "_rejected", f"{pid}_*.png"), (ep / "panels_hf" / "_rejected", f"{pid}.png"),
             (ep / "panels_hf_vast" / "_qa_before", f"{pid}.png"), (ep / "panels_hf_vast" / "_preplate", f"{pid}.png"),
             (ep / "panels_hf_vast" / "_stage1", f"{pid}*.png")]
    out = []
    for d, pat in spots:
        for f in d.glob(pat) if d.exists() else []:
            out.append({"name": f"{d.name}/{f.name}", "time": time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime)), "_t": f.stat().st_mtime})
    out.sort(key=lambda x: -x["_t"])
    return [{k: v for k, v in x.items() if k != "_t"} for x in out]


def revert(ep: Path, pid: str, name: str) -> dict:
    sub, _, fname = name.partition("/")
    base = {"_rejected": ep / "panels_hf" / "_rejected"}.get(sub, ep / "panels_hf_vast" / sub)
    f = base / fname
    if not f.exists() or not fname.startswith(pid):
        raise SystemExit(f"판이 없다: {name}(versions 로 이름을 본다)")
    v, before = _start(ep, pid)
    v.write_bytes(f.read_bytes())
    place(ep, pid)
    return {"panel": pid, "how": f"revert to {name}", "compare": str(compare(ep, pid, before, v, f"되돌림: {name}"))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["edit", "derive", "faces", "versions", "revert", "preview", "design", "adopt"])
    ap.add_argument("series")
    ap.add_argument("episode")
    ap.add_argument("panel")
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--refs", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--from", dest="src")
    ap.add_argument("--box")
    ap.add_argument("--who", default="", help="design: 디자인을 바꿀 인물 id")
    a = ap.parse_args()
    series = Path(a.series).resolve()
    ep = _ep(series, a.episode)
    if a.cmd == "edit":
        r = edit(series, ep, a.panel, a.text, a.refs, a.seed)
    elif a.cmd == "derive":
        if not (a.src and a.box):
            raise SystemExit("derive 는 --from <컷> --box x0,y0,x1,y1 가 필요하다")
        r = derive(series, ep, a.panel, a.src, a.box, a.text, a.refs, a.seed)
    elif a.cmd == "faces":
        r = faces(series, ep, a.panel)
    elif a.cmd == "preview":
        r = preview(series, ep, a.panel, a.text, a.refs, a.seed)
    elif a.cmd == "design":
        if not a.who:
            raise SystemExit("design 은 --who <인물 id> 가 필요하다")
        r = design(series, ep, a.panel, a.who, a.text, a.seed)
    elif a.cmd == "adopt":
        r = adopt(ep, a.panel, a.text)
    elif a.cmd == "versions":
        r = versions(ep, a.panel)
    else:
        r = revert(ep, a.panel, a.text)
    print("RESULT " + json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
