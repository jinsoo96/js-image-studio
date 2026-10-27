"""그린 컷의 일부만 고치기(MCP edit_image·preview_image·adopt_image 이 부른다. derive·faces 는 2026-10-23 MCP 에서 빼고 손으로만). 다시 그리지 않고 그 판의 '편집' 워크플로우로.
(2026-10-20 사용자 "부분부분만 고치는 것도 가능하냐, MCP 도 고도화해서 알아서 컨피 워크플로우를 활용해 작품을 만들어야": 이날 고친 32컷
 (지우기·버스 세우기·폰 쥔 손·얼굴 맞추기·잘 된 옆 컷에서 잘라 다시)은 스크래치 스크립트로 했고 MCP 에는 컷 통째 다시 그리기밖에 없었다)

    .venv/Scripts/python vast/panel_edit.py edit     <작품> <회차> <컷> "<지시>" [--refs sangsu,eunyoung|p012|파일] [--box x0,y0,x1,y1] [--seed N]
    .venv/Scripts/python vast/panel_edit.py derive   <작품> <회차> <컷> --from <잘 된 컷> --box x0,y0,x1,y1 "<지시>" [--refs …]
    .venv/Scripts/python vast/panel_edit.py faces    <작품> <회차> p061:eunyoung,p496
    .venv/Scripts/python vast/panel_edit.py versions <작품> <회차> <컷>
    .venv/Scripts/python vast/panel_edit.py revert   <작품> <회차> <컷> <판 이름(versions 의 name)>
    .venv/Scripts/python vast/panel_edit.py preview  <작품> <회차> <컷> "<지시>" [--refs …] [--box …]  본편은 그대로, review/previews/ 에만
    .venv/Scripts/python vast/panel_edit.py design   <작품> <회차> <컷> "<바꿀 디자인>" --who <인물>   그 인물 디자인을 바꾼 모습 미리 보기
    .venv/Scripts/python vast/panel_edit.py adopt    <작품> <회차> <컷> <미리 보기 이름>          미리 보기를 본편에

- 고치는 그림은 지금 본편(panels_hf/<컷>.png). 결과는 vast 판(panels_hf_vast)에 쓰고 본편에 넣는다(옛 본편은 panels_hf/_rejected/).
- 고칠 때마다 전/후 나란히 그림을 <회차>/review/edits/<컷>_<시각>.jpg 에 남긴다(사람·에이전트가 눈으로 확인).
- edit: 지시는 "무엇을 어떻게 바꾸고 나머지는 그대로"로. <image1> 이 고칠 컷, refs 는 <image2>부터(인물 id 면 그 인물 상반신 참고 그림,
  같은 회차의 컷 번호면 그 컷 그림: 앞 컷과 옷·소품을 맞출 때).
- --box x0,y0,x1,y1(그림 크기 0~1000 비율): 그 상자만 잘라 키워 고치고 가장자리를 흐려 제자리에 붙인다. 상자 밖 픽셀은 그대로.
  인물 id(예 jaeheon, jaeheon:head)를 주면 그 사람 둘레를 찾는다: head 모자·머리·안경 / upper 윗옷·장신구(기본) / full 온몸.
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


def _refs(series: Path, refs: str, ep: Path | None = None) -> list:
    """인물 id → 상반신 참고 그림, 같은 회차 컷 번호 → 그 컷 본편 그림, 그 밖엔 작품 폴더 안 파일."""
    out = []
    for r in [x.strip() for x in (refs or "").split(",") if x.strip()]:
        f = series / "assets" / "vast_refs" / f"{r}_bust.png"
        if not f.exists() and ep is not None:
            f = next((d / f"{r}.png" for d in (ep / "panels_hf", ep / "panels_hf_vast") if (d / f"{r}.png").exists()), f)
        f = f if f.exists() else (series / r)
        if not f.exists():
            raise SystemExit(f"참고 그림이 없다: {r}(인물 id, 같은 회차 컷 번호, 작품 폴더 안 파일)")
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


def _run_edit(series: Path, src: Path, refs: list, prompt: str, seed: int, dst: Path, lift: bool = True):
    c, cache = Comfy(), {}
    g = graph(prompt, [upload(c, src, cache)] + [upload(c, r, cache) for r in refs], 0, 0, seed, 8, 1024, turbo=True, edit=True, series=series)
    g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/panel_edit"
    h = c.wait(c.queue(g), 900)
    st = h.get("status", {})
    if st.get("status_str") == "error":
        raise SystemExit("편집 실패: " + str(next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), ""))[:300])
    c.download(next(i for o in h["outputs"].values() for i in o.get("images", [])), dst)
    if lift:  # 상자 편집은 잘라 낸 조각이라 밝기를 따로 올리면 붙인 자리가 뜬다(테두리 색 맞추기가 대신한다)
        from comfy_panel import lift_dark  # 편집 모델도 어두운 그림을 더 어둡게 내놓았다(2026-10-24 개항 p015a 평균 12) → 그리기와 같은 규칙
        lift_dark(dst)


BOX_MARGIN = 0.08  # 상자 둘레로 더 잘라 가는 몫: 편집 모델이 주변을 보고 이어 그리게, 흐리게 붙일 띠도 여기서 나온다


def box_px(box: str, W: int, H: int, margin: float = BOX_MARGIN) -> tuple:
    """'x0,y0,x1,y1'(그림 크기 0~1000 비율) → 둘레를 margin 만큼 넓힌 픽셀 상자."""
    try:
        x0, y0, x1, y1 = [float(v) for v in str(box).split(",")]
    except ValueError:
        raise SystemExit("box 는 x0,y0,x1,y1 (그림 크기 0~1000 비율)")
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        raise SystemExit("box 는 0~1000 안에서 x0<x1, y0<y1")
    mx, my = (x1 - x0) * margin, (y1 - y0) * margin
    return (max(0, int((x0 - mx) * W / 1000)), max(0, int((y0 - my) * H / 1000)),
            min(W, int((x1 + mx) * W / 1000)), min(H, int((y1 + my) * H / 1000)))


def _feather(size: tuple):
    """붙일 때 쓰는 마스크: 안쪽 255, 가장자리 띠에서 0 으로 흐려진다."""
    from PIL import ImageFilter
    e = max(4, int(min(size) * 0.06))
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rectangle((e, e, size[0] - e, size[1] - e), fill=255)
    return m.filter(ImageFilter.GaussianBlur(e / 2))


def match_ring(orig: Image.Image, new: Image.Image) -> Image.Image:
    """편집한 조각의 채널 밝기를 테두리 띠에서 원본에 맞춘다: 편집 모델이 색·밝기를 조금 옮기면 붙인 자리에 네모 띠가 보였다."""
    from PIL import ImageStat
    e = max(4, int(min(orig.size) * 0.06))
    ring = Image.new("L", orig.size, 255)
    ImageDraw.Draw(ring).rectangle((e, e, orig.width - e, orig.height - e), fill=0)
    a, b = ImageStat.Stat(orig, ring).mean, ImageStat.Stat(new, ring).mean
    gains = [min(1.25, max(0.8, x / y)) if y > 1 else 1.0 for x, y in zip(a, b)]
    return Image.merge("RGB", [ch.point(lambda v, g=g: min(255, int(v * g))) for ch, g in zip(new.split(), gains)])


PARTS = {  # 얼굴 상자에서 넓히는 몫: (폭 배수, 높이 배수, 상자 가운데를 얼굴 가운데에서 얼굴 크기의 몇 배 아래로)
    "head": (2.6, 2.6, -0.15),   # 모자·머리·안경
    "upper": (4.6, 6.0, 2.0),    # 윗옷·조끼·장신구·장갑 낀 손까지 대개
    "full": (5.2, 10.0, 3.8),    # 온몸
}


def _face_square(im: Image.Image, f, scale: float = 3.0) -> tuple:
    """char_check.char_box 와 같은 꼴(얼굴 폭 scale 배 정사각, 머리·어깨까지): 인물 기준 얼굴(references)과 견줄 자르기."""
    x0, y0, x1, y1 = f[:4]
    cx, cy, s = (x0 + x1) / 2, (y0 + y1) / 2 + (y1 - y0) * 0.35, max(x1 - x0, y1 - y0) * scale
    W, H = im.size
    return (int(max(0, cx - s / 2)), int(max(0, cy - s / 2)), int(min(W, cx + s / 2)), int(min(H, cy + s / 2)))


def person_box(series: Path, img: Path, who: str, part: str = "upper") -> str:
    """인물 id 로 고칠 상자를 찾아 0~1000 비율 'x0,y0,x1,y1' 로 준다. 얼굴을 찾고, 여럿이면 CCIP 로 그 인물 기준 얼굴과 가장 가까운 얼굴을
    고른 뒤 부위만큼 넓힌다. 얼굴을 못 찾으면 '' (부르는 쪽이 상자 없이 고치거나 숫자 상자를 받는다)."""
    sys.path.insert(0, str(ROOT / "tools"))
    from face_detect import detect
    im = Image.open(img).convert("RGB")
    W, H = im.size
    faces = detect(im)
    if not faces:
        return ""
    face = max(faces, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
    if len(faces) > 1:
        from char_check import differences, features, references
        ref = references(series.name).get(who)
        if ref is not None:
            crops = [im.crop(_face_square(im, f)) for f in faces]
            d = differences(features(crops + [ref]))
            face = faces[min(range(len(faces)), key=lambda k: float(d[k, len(faces)]))]
    wm, hm, down = PARTS.get(part, PARTS["upper"])
    s = max(face[2] - face[0], face[3] - face[1])
    cx, cy = (face[0] + face[2]) / 2, (face[1] + face[3]) / 2 + down * s
    x0, x1 = max(0, cx - s * wm / 2), min(W, cx + s * wm / 2)
    y0, y1 = max(0, cy - s * hm / 2), min(H, cy + s * hm / 2)
    return f"{int(x0 * 1000 / W)},{int(y0 * 1000 / H)},{max(int(x0 * 1000 / W) + 1, int(x1 * 1000 / W))},{max(int(y0 * 1000 / H) + 1, int(y1 * 1000 / H))}"


def resolve_box(series: Path, img: Path, box: str) -> str:
    """box 가 숫자 넷이면 그대로, 인물 id(':부위')면 그 사람 둘레. 못 찾으면 멈춘다(엉뚱한 자리를 고치지 않게)."""
    box = (box or "").strip()
    if not box or re.fullmatch(r"[\d.\s]+,[\d.\s]+,[\d.\s]+,[\d.\s]+", box):
        return box
    who, _, part = box.partition(":")
    found = person_box(series, img, who.strip(), (part or "upper").strip())
    if not found:
        raise SystemExit(f"{who} 의 얼굴을 못 찾아 상자를 정하지 못했다: 숫자 상자(x0,y0,x1,y1)를 주거나 box 없이 고친다")
    print(f"상자 {box} → {found}", flush=True)
    return found


def region_edit(series: Path, src: Path, refs: list, prompt: str, seed: int, dst: Path, box: str):
    """상자 안만 고친다: 잘라 1024 쯤으로 키워 편집하고, 줄여 테두리 색을 맞춘 뒤 가장자리를 흐리게 제자리에 붙인다. 상자 밖 픽셀은 그대로.
    통째 편집은 지시 밖의 얼굴·배경도 조금씩 다시 그렸다(2026-10-27 사용자 "부분 수정도 가능하게"). 자르고 붙이는 길은 qa_fix.head_swap 과 같다."""
    box = resolve_box(series, src, box)
    im = Image.open(src).convert("RGB")
    bx = box_px(box, *im.size)
    crop = im.crop(bx)
    k = 1024 / max(crop.size)
    big = crop.resize((max(32, round(crop.width * k / 32) * 32), max(32, round(crop.height * k / 32) * 32)), Image.LANCZOS)
    tmp = dst.parent / "_qa_before" / f"{dst.stem}_boxcrop.png"
    tmp.parent.mkdir(exist_ok=True)
    big.save(tmp)
    full = ("<image1> is a close crop of a webtoon panel. " + prompt.strip() + " Keep everything else in <image1> exactly the same, "
            "with the same light, colors and art style; the edges of <image1> must still line up with the rest of the picture.")
    _run_edit(series, tmp, refs, full, seed, tmp, lift=False)
    new = match_ring(crop, Image.open(tmp).convert("RGB").resize(crop.size, Image.LANCZOS))
    im.paste(new, bx[:2], _feather(crop.size))
    im.save(dst)
    tmp.unlink(missing_ok=True)


SAME_ASK = """Image 2 is the reference portrait of {name}. Image 1 is a webtoon panel; the people in it: {names}. Panel text: {text}
Find {name} in image 1. Do {name}'s face, hair color and hairstyle match image 2? Ignore clothes, pose, camera angle and lighting.
Answer JSON only: {{"found": true|false, "match": true|false, "differs": "short, empty if it matches"}}"""


def drift(series: Path, ep: Path, pid: str, before: bytes, after: Path) -> list:
    """편집 전엔 시트(상반신 참고 그림)와 맞던 인물이 편집 뒤 달라졌나. [{"who", "differs"}].
    미즈시마 머리만 회색으로 하라는 편집이 옆의 금수 머리를 은발로 바꿨고, 에이전트는 전후 비교를 보고도 넘겼다(2026-10-24 개항 v4 p047)."""
    import refine as R
    from common import names
    p = _spec(ep, pid)
    nm = names(series)
    chars = [c for c in p.get("chars") or [] if (series / "assets" / "vast_refs" / f"{c}_bust.png").exists()]
    if not chars:
        return []
    tmp = after.parent / "_qa_before" / f"{pid}_drift_before.png"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_bytes(before)
    out = []
    for c in chars:
        name = re.sub(r"\s*\(.*?\)", "", nm.get(c, c))
        ask = SAME_ASK.format(name=name, names=", ".join(re.sub(r"\s*\(.*?\)", "", nm.get(x, x)) for x in p.get("chars") or []),
                              text=R.panel_text(p)[:400])
        ref = series / "assets" / "vast_refs" / f"{c}_bust.png"
        b, a = R._vlm([tmp, ref], ask, 120), R._vlm([after, ref], ask, 120)
        if b.get("found") is True and b.get("match") is True and a.get("found") is True and a.get("match") is False:
            out.append({"who": name, "differs": str(a.get("differs") or "")[:160]})
    tmp.unlink(missing_ok=True)
    return out


def edit(series: Path, ep: Path, pid: str, prompt: str, refs: str = "", seed: int = 0, box: str = "") -> dict:
    v, before = _start(ep, pid)
    seed = seed or int(time.time()) % 100000
    if box:
        region_edit(series, v, _refs(series, refs, ep), prompt, seed, v, box)
    else:
        _run_edit(series, v, _refs(series, refs, ep), prompt, seed, v)
    place(ep, pid)
    out = {"panel": pid, "how": "edit" + (f" box {box}" if box else ""),
           "compare": str(compare(ep, pid, before, v, (f"[상자 {box}] " if box else "") + prompt))}
    changed = drift(series, ep, pid, before, v)
    warn = []
    if changed:
        warn.append("편집 전엔 시트와 맞던 인물이 달라졌다: " + "; ".join(f"{x['who']}({x['differs']})" for x in changed)
                    + ". 그 사람을 바꾸려던 게 아니면 adopt_image(source='version') 으로 되돌리고, 지시에 바꿀 사람을 옷·자리로 더 분명히 적어 다시")
    # 복제(얼굴이 똑같은 사람들)는 편집 뒤에도 남는지 다시 본다: '서로 다르게'만 적은 편집이 머리 모양만 조금 바꾸고 '통과'로 끝났다(2026-10-24 개항 v4 p013b·p016b)
    import refine as R
    if R.run_checks(series, v, _spec(ep, pid), only={"clones"}).get("복제"):
        warn.append("아직 얼굴·머리가 똑같은 사람들이 있다. '서로 다르게'만으로는 거의 안 바뀐다: 사람마다 자리와 함께 머리 모양·얼굴형·나이 느낌·장신구를 "
                    "구체적으로 다르게 적어 다시 고친다(예: 'the woman on the left: long hair in a low bun, round face; the woman on the right: short bob, "
                    "narrow face, a hairpin')")
    if warn:
        out["warning"] = " / ".join(warn)
    return out


def preview(series: Path, ep: Path, pid: str, prompt: str, refs: str = "", seed: int = 0, how: str = "", box: str = "") -> dict:
    """본편을 건드리지 않고 고친 모습만 본다(2026-10-22 사용자 "한 컷만 고쳐서 보여주는 것, 디자인 바꾸면 어떻게 될지도 궁금하다").
    결과는 <회차>/review/previews/<컷>_<시각>.png(+ 전/후 나란히 .jpg). 마음에 들면 adopt 로 본편에 넣는다."""
    main, v = ep / "panels_hf" / f"{pid}.png", ep / "panels_hf_vast" / f"{pid}.png"
    src = main if main.exists() else v
    if not src.exists():
        raise SystemExit(f"{pid} 그림이 없다")
    d = ep / "review" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{pid}_{time.strftime('%m%d-%H%M%S')}.png"
    if box:
        shutil.copy2(src, out)
        region_edit(series, out, _refs(series, refs, ep), prompt, seed or int(time.time()) % 100000, out, box)
    else:
        _run_edit(series, src, _refs(series, refs, ep), prompt, seed or int(time.time()) % 100000, out)
    cmp = compare(ep, pid, src.read_bytes(), out, f"미리 보기: {how or prompt}")
    shutil.move(str(cmp), str(out.with_suffix(".jpg")))  # 고치기 기록(review/edits)과 섞이지 않게 미리 보기 옆에
    return {"panel": pid, "how": "preview", "preview": out.stem, "image": str(out), "compare": str(out.with_suffix(".jpg"))}


def design(series: Path, ep: Path, pid: str, who: str, change: str, seed: int = 0) -> dict:
    """인물 디자인(머리·옷·소품 등)을 바꾸면 어떻게 보일지 그 인물이 나오는 한 컷으로 미리 본다. 시트·본편은 그대로."""
    from common import names
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
    _run_edit(series, cf, _refs(series, refs, ep), full, seed or int(time.time()) % 100000, v)
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
        r = edit(series, ep, a.panel, a.text, a.refs, a.seed, a.box or "")
    elif a.cmd == "derive":
        if not (a.src and a.box):
            raise SystemExit("derive 는 --from <컷> --box x0,y0,x1,y1 가 필요하다")
        r = derive(series, ep, a.panel, a.src, a.box, a.text, a.refs, a.seed)
    elif a.cmd == "faces":
        r = faces(series, ep, a.panel)
    elif a.cmd == "preview":
        r = preview(series, ep, a.panel, a.text, a.refs, a.seed, box=a.box or "")
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
