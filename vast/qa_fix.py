"""이미 그린 컷을 연출 검사하고, 걸린 컷은 그 부분만 편집으로 고친다(다시 그리지 않는다).
(2026-10-20 사용자 "이상한 경우 검사하고 이상한 부분만 부분 수정, 전체 다 엎으면 한참 걸리니까": 기준판에서 다시 그린 컷은 검사 없이 들어가
 p005 은영이 '어색한 침묵'에 화내며 소리치고, p436 인라인 스케이트가 아이스 스케이트로 나왔다)

    .venv/Scripts/python vast/qa_fix.py <작품> <회차> [--only p005,p436] [--folder panels_hf_vast] [--workers 4] [--fix]

검사는 refine.staging_check(그림 검수 + 기준표 skills/staging-review/checks.json 항목: 시선·좌우·구도·논리·소품·입과 연기·신은 것)를 컷마다 병렬로.
결과는 <회차>/review/qa_<시각>.json, 사람 확인이 남은 컷은 review/staging.json.
--fix: 걸린 항목마다 편집 하나 — 표정·옷·소품(신은 것 포함)은 그 부분만 바꾸는 편집, 시선은 고개·몸 돌리기 편집, 인원은 콕 집어 지우기,
그 밖(구도·논리·배경·몸)은 걸린 점을 그대로 적은 편집. 고친 뒤 다시 검사해 걸린 항목이 늘면 되돌린다. 옛 그림은 <폴더>/_qa_before/.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import graph, panel_text, upload  # noqa: E402
import refine as R  # noqa: E402


def _panels(ep: Path) -> dict:
    d = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    return {p["id"]: p for p in (d["panels"] if isinstance(d, dict) else d)}


def quick_check(series: Path, ep: Path, img: Path, pid: str, only: set) -> tuple:
    """기준표 항목 중 only 만(비교형 감정·입·신은 것·몸 방향 등) + 같은 사람이 둘인지. 전체 검사(staging_check)는 시선 각도 같은 사소한 것까지
    잡아 거의 모든 컷이 걸렸다(2026-10-20). (걸린 항목, 메모)"""
    import re
    from hf_panel import names
    from scene_plate import _doubled
    p = _panels(ep)[pid]
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    bp = next((x for x in board.get("panels", []) if x.get("id") == pid), {})
    nm = names(series)
    spk = sorted({d.get("who") for d in bp.get("dialogue") or [] if d.get("who") and d.get("type") not in ("monologue", "narration", "caption", "sfx")})
    speakers = ", ".join(re.sub(r"\s*\(.*?\)", "", nm.get(w, w)) for w in spk) or "nobody (only narration, no one speaks aloud)"
    probs, notes = set(), []
    for cat, found in R.run_checks(series, img, p, "", speakers, only=only).items():
        probs.add(cat)
        notes.append(f"{cat}: " + "; ".join(found))
    if p.get("chars"):
        d = _doubled(img, series, p["chars"])
        if d.get("doubled"):
            probs.add("인원")
            notes.append("인원: 같은 사람이 둘이거나 얼굴이 겹쳤다(" + d.get("why", "")[:120] + ")")
    seen = R.absent_cast(series, ep, pid, img)  # 먼저 떠난 사람·아직 안 온 사람(2026-10-20 p006 박 과장)
    if seen:
        probs.add("인원")
        notes.append("인원: 이 순간 없어야 할 인물이 보인다(" + "; ".join(f"{x['name']}: {x.get('where', '')}" for x in seen) + ")")
    return probs, " ".join(notes)


def check_all(series: Path, ep: Path, ids: list, folder: str, workers: int, log=print, only: set | None = None) -> dict:
    """{컷: {"probs": [...], "note": str}}. only 를 주면 그 항목만(quick_check). 검사 모델이 답을 못 준 항목은 '검사불가'로 남는다."""
    out = {}

    def one(pid):
        img = ep / folder / f"{pid}.png"
        try:
            if only:
                probs, note = quick_check(series, ep, img, pid, only)
                return pid, {"probs": sorted(probs), "note": note[:700]}
            r, probs = R.staging_check(series, ep, img, pid)
            return pid, {"probs": sorted(probs), "note": str(r.get("고칠점") or "")[:700]}
        except Exception as e:  # noqa: BLE001
            return pid, {"probs": ["검사불가"], "note": f"{type(e).__name__}: {str(e)[:200]}"}

    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for k, (pid, r) in enumerate(ex.map(one, ids), 1):
            out[pid] = r
            if r["probs"]:
                log(f"  [{k}/{len(ids)}] {pid}: {r['probs']} {r['note'][:140]}")
    log(f"검사 {len(ids)}컷 {int(time.time() - t0)}초, 걸린 컷 {sum(1 for r in out.values() if r['probs'])}")
    return out


def _edit(c, cache, series, dst: Path, prompt: str, seed: int):
    g = graph(prompt, [upload(c, dst, cache)], 0, 0, seed, 8, 1024, turbo=True, edit=True, series=series)
    g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/qa"
    h = c.wait(c.queue(g), 600)
    c.download(next(i for o in h["outputs"].values() for i in o.get("images", [])), dst)


def face_distance(series: Path, who: str, img: Path, pick=None) -> float | None:
    """그 인물 기준 얼굴(models/lora_src/cast4_picks.json, 없으면 상반신 참고 그림)과 이 그림 속 얼굴의 거리(CCIP, 작을수록 같은 사람). 얼굴을 못 찾으면 None."""
    from PIL import Image
    from char_check import char_crop, differences, features, references
    ref = references(series.name).get(who)
    if ref is None:
        b = series / "assets" / "vast_refs" / f"{who}_bust.png"
        if not b.exists():
            return None
        ref = char_crop(Image.open(b).convert("RGB")) or Image.open(b).convert("RGB")
    crop = char_crop(Image.open(img).convert("RGB"), pick)
    if crop is None:
        return None
    return float(differences(features([crop, ref]))[0, 1])


LOOKS_AT_VIEWER = ('Is any person in this picture looking straight at the viewer (eyes on the camera, face turned to us)? '
                   'Answer only JSON: {"looking_at_viewer": true|false}')


def _new_flaws(before: bytes, after: Path) -> list:
    """편집 뒤에 새로 생긴 결함(원본에는 없던 것): 카메라 정면 응시 · 남는 팔·손."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(before)
        b = Path(f.name)
    try:
        out = []
        if R._vlm([after], LOOKS_AT_VIEWER).get("looking_at_viewer") is True and R._vlm([b], LOOKS_AT_VIEWER).get("looking_at_viewer") is not True:
            out.append("시선이 카메라로 돌았다")
        if R.extra_limbs(after) and not R.extra_limbs(b):
            out.append("팔·손이 늘었다")
        return out
    finally:
        b.unlink(missing_ok=True)


FACE = ("<image2> is {name}'s face. In <image1>, redraw only {name}'s face and hair so it is clearly the same person as <image2>: the same face "
        "shape, eyes, eyebrows, nose, mouth, hairline and hairstyle, the same age ({age}). Keep {name}'s expression ({acting}), head angle, gaze, "
        "pose, hands, clothes and everything else in <image1> exactly the same: the other people, the place, the light, the framing and the art style.")


# BFS head swap 작성자 프롬프트 그대로(huggingface.co/Alissonerdx/BFS-Best-Face-Swap docs/qwen-image-2.1.md): 시선·고개·미세 표정은 <image1>(컷)에서
HEAD_SWAP = ("head_swap: start with <image1> as the base image, keeping its lighting, environment, and background. "
             "remove the head from <image1> completely and replace it with the head from <image2>, strictly preserving the hair, "
             "eye color, nose structure from <image2>. "
             "copy the direction of the eye, head rotation, micro expressions from <image1>, high quality, sharp details, 4k")


def head_swap_ready(series: Path) -> bool:
    from saved_workflow import models, pick
    try:
        return bool(models()[pick(series)[0]]["roles"].get("head_swap"))
    except Exception:  # noqa: BLE001
        return False


def head_swap(c, cache, series: Path, dst: Path, ref: Path, pick, seed: int):
    """컷 dst 에서 그 인물 머리·어깨 둘레만 잘라 1024 쯤으로 키워 BFS 로 얼굴을 바꾸고, 줄여 가장자리를 흐리게 다시 붙인다.
    여럿 나오는 컷에서 다른 사람 머리를 바꾸지 않게, 작은 얼굴도 크게 보고 바꾸게(작성자: 작은 얼굴·급한 각도에 약함). 얼굴을 못 찾으면 False."""
    from PIL import Image, ImageDraw, ImageFilter
    from char_check import char_box
    im = Image.open(dst).convert("RGB")
    box = char_box(im, pick, scale=3.2)
    if not box:
        return False
    crop = im.crop(box)
    k = 1024 / max(crop.size)
    big = crop.resize((max(32, round(crop.width * k / 32) * 32), max(32, round(crop.height * k / 32) * 32)), Image.LANCZOS)
    tmp = dst.parent / "_qa_before" / f"{dst.stem}_headcrop.png"
    big.save(tmp)
    g = graph(HEAD_SWAP, [upload(c, tmp, cache), upload(c, ref, cache)], 0, 0, seed, 25, 1024, turbo=False, edit=True, series=series, role="head_swap")
    g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/face"
    h = c.wait(c.queue(g), 900)
    c.download(next(i for o in h["outputs"].values() for i in o.get("images", [])), tmp)
    new = Image.open(tmp).convert("RGB").resize(crop.size, Image.LANCZOS)
    m = Image.new("L", crop.size, 0)
    e = max(4, int(min(crop.size) * 0.08))
    ImageDraw.Draw(m).rectangle((e, e, crop.width - e, crop.height - e), fill=255)
    im.paste(new, box[:2], m.filter(ImageFilter.GaussianBlur(e / 2)))
    im.save(dst)
    return True


def fix_face(c, cache, series: Path, ep: Path, folder: str, p: dict, who: str, seed: int, log=print, tries: int = 2) -> tuple:
    """얼굴이 다른 사람처럼 보이는 컷: 그 인물 상반신 참고 그림을 <image2> 로 주고 얼굴·머리만 다시 그리는 편집. 고친 뒤 얼굴 거리를 다시 재
    가장 가까워진 판만 남긴다(멀어지거나 얼굴을 못 찾으면 원본). (전 거리, 후 거리).
    (2026-10-20 사용자 "캐릭터 얼굴 일관성도 신경 써 줘, 다른 사람처럼 보이는 경우도 있네": 외출 상수가 컷마다 나이·얼굴형이 달라 보였다)"""
    from lora import person
    dst = ep / folder / f"{p['id']}.png"
    keep = ep / folder / "_qa_before"
    keep.mkdir(exist_ok=True)
    if not (keep / dst.name).exists():
        shutil.copy(dst, keep / dst.name)
    who_p = person(series, who)
    pick = None
    if len(p.get("chars") or []) > 1:  # 여럿이면 그 인물이 왼쪽부터 몇 번째인지(장면 배치 LEFT/RIGHT 를 몰라도 얼굴 거리로 가장 가까운 쪽을 고른다)
        ds = [(face_distance(series, who, dst, k), k) for k in range(len(p["chars"]))]
        ds = [x for x in ds if x[0] is not None]
        pick = min(ds)[1] if ds else None
    d0 = face_distance(series, who, dst, pick)
    if d0 is None:
        log(f"  {p['id']}: {who} 얼굴을 못 찾음 → 그대로")
        return None, None
    best = (d0, dst.read_bytes())
    prompt = FACE.format(name=who_p["name"], age=re.split(r"(?<=\.)\s", who_p["who"])[0][:100], acting=p.get("acting") or "as in <image1>")
    ref = series / "assets" / "vast_refs" / f"{who}_bust.png"
    orig = dst.read_bytes()
    # 얼굴 바꾸기(BFS)는 고를 때만(WEBTOON_HEAD_SWAP=1). 2026-10-21 외출 5컷 시험: 옆얼굴(p061)은 고개·시선을 지켰지만
    # 강 PM(p403)은 정면 참고 그림을 따라 고개를 돌려 카메라를 봤고 거리도 0.035 → 0.079 로 멀어졌다(exports/2026-10-21/비교/외출_얼굴고치기_예전vsBFS.jpg)
    swap = os.environ.get("WEBTOON_HEAD_SWAP") == "1" and head_swap_ready(series)
    for t in range(tries):
        dst.write_bytes(orig)
        if swap:
            if not head_swap(c, cache, series, dst, ref, pick, seed + t):
                break
        else:
            g = graph(prompt, [upload(c, dst, cache), upload(c, ref, cache)], 0, 0, seed + t, 8, 1024, turbo=True, edit=True, series=series)
            g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/face"
            h = c.wait(c.queue(g), 600)
            c.download(next(i for o in h["outputs"].values() for i in o.get("images", [])), dst)
        d1 = face_distance(series, who, dst, pick)
        log(f"  {p['id']}: {who} 얼굴 {'바꾸기(BFS)' if swap else '맞추기'} {t + 1}회 거리 {d0:.3f} → {d1 if d1 is None else round(d1, 3)}")
        if d1 is not None and d1 < best[0]:
            # 거리만 보고 남기면 안 된다: 정면 참고 그림을 따라 시선이 카메라로 돌거나(p061·p471·p325·p007) 손이 하나 더 생겼다(p495)
            # (2026-10-20 사용자 "어디 보고 말함", "손이 두 개임") → 원본에 없던 정면 응시·남는 팔다리가 생기면 버린다
            bad = _new_flaws(orig, dst)
            if bad:
                log(f"  {p['id']}: 얼굴은 가까워졌지만 {bad} → 버림")
                continue
            best = (d1, dst.read_bytes())
    dst.write_bytes(best[1])
    if best[0] < d0 and folder == "panels_hf_vast":
        from sync_vast import place
        place(ep, p["id"])
    return d0, best[0]


ONLY: set = set()  # --checks 로 고른 항목(고친 뒤 다시 검사할 때도 같은 항목만)


def fix_one(c, cache, series: Path, ep: Path, folder: str, p: dict, probs: set, note: str, seed: int, log=print) -> set:
    """걸린 항목만 편집으로. 돌려주는 것: 남은 항목."""
    dst = ep / folder / f"{p['id']}.png"
    keep = ep / folder / "_qa_before"
    keep.mkdir(exist_ok=True)
    if not (keep / dst.name).exists():
        shutil.copy(dst, keep / dst.name)
    before = dst.read_bytes()
    r = {"고칠점": note}
    if probs - {"검사불가"} <= R.EDITABLE:
        _edit(c, cache, series, dst, R.staging_edit_prompt(series, p, r, probs), seed)
        how = "표정·옷·소품 편집"
    elif probs - {"검사불가"} <= {"시선", "좌우시선"}:
        ok = R.fix_facing(c, cache, dst, series, ep, p, seed, log)
        how = "시선 편집" + ("" if ok else "(안 됨)")
    elif "인원" in probs and probs - {"검사불가", "인원"} <= R.EDITABLE:
        ok = R.fix_count(c, cache, dst, p, seed, log)
        how = "남는 사람 지우기" + ("" if ok else "(안 됨)")
    else:  # 구도·논리·배경·몸 등: 걸린 점을 그대로 적은 편집(구도는 편집으로 다 안 될 수 있다)
        prompt = (f"Edit <image1>, a Korean webtoon panel, to fix only these problems: {note[:700]} The panel should show: {panel_text(p)[:600]} "
                  "Keep everything else in <image1> exactly the same: the same people, faces, hair, clothes, the place, the light and the art style.")
        _edit(c, cache, series, dst, prompt, seed)
        how = "걸린 점 편집"
    if ONLY:
        p2, n2 = quick_check(series, ep, dst, p["id"], ONLY)
        r2 = {"고칠점": n2}
    else:
        r2, p2 = R.staging_check(series, ep, dst, p["id"])
    if len(p2) > len(probs):
        dst.write_bytes(before)
        log(f"  {p['id']}: {how} 뒤 더 나빠져 되돌림 {sorted(p2)}")
        return probs
    if folder == "panels_hf_vast":  # 본편에도 넣는다(vast 판에만 두면 합성에 안 들어갔다)
        from sync_vast import place
        place(ep, p["id"])
    log(f"  {p['id']}: {how} → {'통과' if not p2 else '남음 ' + str(sorted(p2))}")
    R.record_review(ep, p["id"], p2, str(r2.get("고칠점") or ""))
    return p2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series")
    ap.add_argument("episode")
    ap.add_argument("--only")
    ap.add_argument("--folder", default="panels_hf_vast")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--record", action="store_true", help="검사 뒤 독자 눈 거르기를 하고 review/staging.json 에 남긴다(--fix 없이)")
    ap.add_argument("--from-report", help="검사를 다시 하지 않고 이 qa_*.json 의 걸린 컷만 고친다")
    ap.add_argument("--checks", help="이 기준표 항목만(쉼표, 예: emotion,mouth,footwear,body_view) + 같은 사람이 둘인지. 없으면 전체 연출 검사")
    ap.add_argument("--faces", help="얼굴 맞추기만: '컷:인물' 쉼표 목록(예 p481:sangsu,p061:eunyoung). 인물을 빼면 그 컷의 첫 인물")
    a = ap.parse_args()
    global ONLY
    ONLY = set(a.checks.split(",")) if a.checks else set()
    series = Path(a.series).resolve()
    ep = series / "episodes" / a.episode
    panels = _panels(ep)
    if a.faces:
        c, cache = Comfy(), {}
        for k, item in enumerate(a.faces.split(",")):
            pid, _, who = item.partition(":")
            p = panels[pid]
            r = fix_face(c, cache, series, ep, a.folder, p, who or p["chars"][0], 900 + 7 * k)
            print(f"{pid}: 얼굴 거리 {r[0]} → {r[1]}", flush=True)
        return
    if a.from_report:
        res = json.loads(Path(a.from_report).read_text(encoding="utf-8"))
    else:
        ids = a.only.split(",") if a.only else [pid for pid in panels if (ep / a.folder / f"{pid}.png").exists() and panels[pid].get("chars")]
        res = check_all(series, ep, ids, a.folder, a.workers, only=ONLY or None)
        rep = ep / "review" / f"qa_{time.strftime('%Y%m%d-%H%M')}.json"
        rep.parent.mkdir(exist_ok=True)
        rep.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print("보고서", rep)
    if a.record:
        # 그리기 때와 같은 끝손질: 독자 눈 거르기(짜임 결함·생김새는 남김) 뒤 review/staging.json 에 남긴다(MCP review_panels deep).
        # 이미 그린 회차는 그리기 때 검사가 없던 판(생김새·이어짐 검사 전)이라 다시 볼 길이 필요했다(2026-10-24 사용자 '개항 인물·상황 안 지켜진 컷 많다')
        n = 0
        for pid, r in sorted(res.items()):
            probs = set(r["probs"])
            if probs:
                probs, note = R.reader_filter(ep, ep / a.folder / f"{pid}.png", pid, probs, r["note"])
                n += bool(probs)
                R.record_review(ep, pid, probs, note)
            else:
                R.record_review(ep, pid, None)
        print(f"사람 확인으로 남긴 컷 {n}개(review/staging.json)", flush=True)
    if not a.fix:
        return
    c, cache = Comfy(), {}
    left = {}
    for k, (pid, r) in enumerate(sorted(res.items())):
        probs = set(r["probs"]) - {"검사불가"}
        if not probs:
            continue
        left[pid] = sorted(fix_one(c, cache, series, ep, a.folder, panels[pid], probs, r["note"], 300 + k))
    rest = {k: v for k, v in left.items() if v}
    print(f"고침 {len(left)}컷, 남은 컷 {len(rest)}: {rest}")


if __name__ == "__main__":
    main()
