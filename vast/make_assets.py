"""vast(Qwen Image 2.1 편집)로 인물 에셋 늘리기: 시트(D: 의 assets) 얼굴·몸을 그대로 두고 표정·옷만 바꿔 한 장씩 만든다(크레딧 0).
(2026-10-15 사용자 "D 드라이브 에셋 설정하면서, 맨몸부터 시작해서 싹 다 많이"). 맨몸(누드)은 만들지 않는다(작업실 내용의 선, CLAUDE.md):
베이스는 몸선이 보이는 회색 기본복(반팔·반바지)으로, 비율 확인과 옷 갈아입히기 출발점으로 쓴다.

    .venv/Scripts/python vast/make_assets.py <series_hf_dir> <인물> base           회색 기본복 정면 전신 → vast_refs/<id>_base.png
    .venv/Scripts/python vast/make_assets.py <series_hf_dir> <인물> expr [--only 1,2]  센 표정 12가지 → vast_refs/<id>_xp_01..12.png
    .venv/Scripts/python vast/make_assets.py <series_hf_dir> <인물|all> subtle      잔잔한 표정 20가지(series.json expressions) → <id>_xs_NN.png
결과 표정은 <id>_expr.json 의 extra 에 이름과 같이 적혀 comfy_panel.py --expr 가 표정 시트 12칸과 함께 고른다.
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
from comfy_panel import graph, upload  # noqa: E402

# 표정 시트(12칸)에 없는 센 감정 위주. 얼굴과 어깨만 나오는 같은 구도에서 표정만 바꾼다
STRONG = [
    "sobbing hard: tears streaming down both cheeks, mouth open in a wail, eyebrows pulled up and together, face scrunched",
    "crying silently: tears running down the cheeks, biting the lower lip, eyebrows twisted up in pain, red wet eyes",
    "eyes welling up with tears, trembling pressed lips, trying hard not to cry",
    "furious shouting: mouth wide open, eyebrows slammed down, nostrils flared, an anger vein on the forehead",
    "cold contempt: narrowed eyes looking down, one corner of the mouth curled in a slight sneer",
    "horrified: eyes wide with tiny pupils, mouth agape, face pale, gloom lines over the forehead",
    "stunned disbelief: frozen, eyes wide and blank, lips slightly parted",
    "bitter forced smile: the mouth smiles but the eyes are sad and glistening",
    "smug confident smirk: half-lidded eyes, lopsided grin, chin slightly raised",
    "flustered: beet-red blush hatching, big sweat drops, eyes darting to the side, wobbly mouth",
    "exhausted and hollow: dark circles under the eyes, blank unfocused stare, slack mouth",
    "guilty and ashamed: eyes cast down and away, lips pressed tight, shoulders drawn in",
]
NEUTRAL = ("neutral", "serious", "composed", "deadpan", "cool")
# 잔잔한 표정 묶음(subtle). 센 표정만 있으면 차분한 연기에 우쭐한 미소·오열이 골라졌다(2026-10-19 외출 "calm and confident" → smug smirk).
# 작품 설정(series.json)의 "expressions" 목록이 있으면 그걸 쓴다. 이름: 설명 꼴, 파일은 <id>_xs_NN.png
SUBTLE = [
    "warm soft smile: relaxed eyes, gentle closed-mouth smile",
    "polite social smile: small courteous smile, the eyes not fully smiling",
    "bright cheerful laugh: eyes crinkled, open-mouthed happy laugh",
    "quietly listening: calm attentive eyes, relaxed closed mouth",
    "curious bright eyes: eyebrows slightly raised, eyes wide and interested, a small smile",
    "thoughtful: eyes looking slightly down to the side, lips pressed, considering",
    "tired: heavy eyelids, a slight frown, faint dark circles",
    "wistful: soft sad eyes, a faint closed smile",
    "awkward smile: stiff small smile, eyebrows slightly up, uneasy",
    "dazed: blank unfocused eyes, slightly parted lips",
    "uneasy: eyebrows slightly knit, eyes avoiding, tense lips",
    "sulky: lips pushed out a little, eyes looking down, quietly disappointed",
    "calm explaining: composed face, level eyebrows, mouth mid-speech",
    "doubtful questioning: one eyebrow slightly raised, level gaze, relaxed mouth",
    "approving smile: small satisfied smile, a slight nod",
    "flushed tipsy smile: cheeks lightly pink, relaxed happy half-lidded eyes",
    "embarrassed with red ears: ears turning red, eyes looking away, a suppressed smile",
    "faint amused smirk: one corner of the mouth up, quietly amused",
    "sigh: eyes half-closed, exhaling, shoulders dropping",
    "radiant smile: a glowing happy face, bright sparkling eyes",
]


def subtle_list(series: Path) -> list:
    sj = series / "series.json"
    if sj.exists():
        lst = json.loads(sj.read_text(encoding="utf-8")).get("expressions")
        if lst:
            return lst
    return SUBTLE


# 일부러 못생기게 만든 조연(생김새 글에 ugly·homely): '매력적으로 유지'를 빼고 생김새를 지키라고 쓴다.
# 그 문장 때문에 표정 칸마다 딸기코·뻐드렁니가 지워져 조연이 미남이 됐다(2026-10-23 개항 재헌·도경)
HOMELY = re.compile(r"\b(ugly|homely)\b", re.I)
KEEP_HOMELY = ("Keep all of the face's homely features exactly as they are (small eyes, nose, teeth, skin, face shape): do not make it prettier, "
               "no chibi.")


def subtle_prompt(ex, homely=False):
    return (f"Change only the facial expression of the person in <image1> to: {ex}. Subtle, natural, film-like acting for a quiet drama: "
            "small but clearly readable changes in the eyebrows, eyelids, eyes and mouth, no exaggerated webtoon marks. Keep exactly the same "
            "person, face shape, age, hairstyle, hair color, outfit, framing, plain background and art style." + (" " + KEEP_HOMELY if homely else ""))


def source_face(vr: Path, cid: str) -> Path:
    """편집 출발 얼굴: 표정 시트에서 무표정에 가까운 칸(크고 선명), 없으면 시트 머리 자른 것."""
    jf = vr / f"{cid}_expr.json"
    if jf.exists():
        labels = json.loads(jf.read_text(encoding="utf-8"))["labels"]
        for k, lb in enumerate(labels, 1):
            if any(w in lb.lower() for w in NEUTRAL):
                return vr / f"{cid}_expr_{k:02d}.png"
        if not (vr / f"{cid}_bust.png").exists():  # 시트에서 머리를 못 자른 인물(무대 뒤 subvocal)
            return vr / f"{cid}_expr_01.png"
    return vr / f"{cid}_bust.png"


BASE = ("Change only the clothes of the person in <image1>: a plain fitted heather-gray crew-neck short-sleeve t-shirt and plain gray "
        "knee-length shorts, barefoot, nothing else, no accessories. Keep exactly the same person, face, hairstyle, hair color, body "
        "proportions, height, standing pose, framing, plain background and art style. Neutral face.")


def expr_prompt(ex, homely=False):
    return (f"Change only the facial expression of the person in <image1> to: {ex}. Strong, instantly readable Korean webtoon acting: "
            "eyebrows, eyelids, eyes, mouth and cheeks all change together; webtoon expression marks are welcome when they fit. Keep exactly "
            "the same person, face shape, hairstyle, hair color, outfit, framing, plain background and art style. "
            + (KEEP_HOMELY if homely else "Keep the face attractive and on-model (no chibi, no gag face)."))


TURN = ("turn this photo into a character reference sheet: a 3x2 grid on a plain grey background with a close-up portrait of {pr} face, then {pr} full "
        "outfit from the front, back, side, three-quarter front and three-quarter back, cropped at the neck")  # 캐릭터 시트 LoRA 작성자의 문장 그대로


def split_turnaround(src: Path, vr: Path, cid: str) -> list:
    """캐릭터 시트(얼굴 + 여러 방향 전신)를 사람 하나씩 잘라 <id>_view_face.png 와 <id>_view_<방향>.png(front·back·side·34front·34back)로.
    시트 배치는 그릴 때마다 달랐다(2열 3행·한 줄에 셋·한 명만, 목에서 잘린 칸): 자리 차례로 방향을 정하면 개항 8명 중 6명이 못 잘렸다
    (2026-10-23). → 흰 가로줄로 줄을 나누고, 줄 안 사람은 sheet_refs.person_columns(가장 옅은 골짜기)로 가르고, 방향은 그림 보는 모델이 본다.
    모델에 닿지 않으면 얼굴 칸만 남긴다(방향을 짐작해 잘못된 뒷모습을 주느니 없는 게 낫다)."""
    import base64
    import io
    import numpy as np
    from PIL import Image
    import sheet_refs as SR
    from judge_ep import JudgeDown, chat, parse_json
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    h, w, _ = a.shape
    # 칸 사이 가로줄: 고르고(줄 안 표준편차 작음) 위아래 이웃보다 밝은 줄. 줄 색이 시트마다 달라(흰색 235~·밝은 회색 205~) 한 값으로 못 잡았고,
    # 1~3px 라 틈을 잇지 않는다(2026-10-23 개항 도경·재헌·금수)
    rm, rs = a.mean(axis=(1, 2)), a.std(axis=(1, 2))
    sep = np.array([rs[y] < 6 and rm[y] > np.median(rm[max(0, y - 15):y + 16]) + 4 for y in range(h)])
    bands = SR.runs(~sep, min_gap=0, min_len=40)
    if len(bands) < 2:
        return []
    y0, y1 = bands[0]
    im.crop((0, y0, w, y1)).save(vr / f"{cid}_view_face.png")
    made = [f"{cid}_view_face.png"]
    best, crops = {}, []
    for y0, y1 in bands[1:]:
        sub = a[y0:y1]
        bg = np.median(np.concatenate([sub[:4].reshape(-1, 3), sub[-4:].reshape(-1, 3)]), axis=0)
        crops += [im.crop((max(0, x0 - 6), y0, min(w, x1 + 6), y1)) for x0, x1 in SR.person_columns(np.abs(sub - bg).sum(axis=2) > 60, w)
                  if x1 - x0 >= 0.08 * w]
    tall = max((c.height for c in crops), default=0)
    for crop in crops:
        if crop.height < 0.45 * tall:  # 다리만 나온 줄 같은 짧은 칸(2026-10-23 개항 도경 옆모습이 다리만)
            continue
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        ask = ("This is one figure cut from a character turnaround sheet. From which direction do we see this person? "
               'Reply with JSON only: {"view": "front" | "back" | "side" | "34front" | "34back", "head": true or false}. '
               "side = full profile; 34front = three-quarter view from the front; 34back = three-quarter view from behind; head = is the head visible.")
        try:
            r = parse_json(chat([{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}},
                                 {"type": "text", "text": ask}], max_tokens=60, timeout=90))
        except (JudgeDown, ValueError) as ex:
            print(f"  {cid}: 방향을 못 물어봄({str(ex)[:80]}), 얼굴 칸만 남김", flush=True)
            return made
        view = str(r.get("view", "")).replace("3/4", "34").replace("-", "").replace(" ", "").lower()
        if view not in ("front", "back", "side", "34front", "34back"):
            continue
        score = (bool(r.get("head")), crop.height * crop.width)  # 머리가 보이는 칸, 그다음 큰 칸
        if view not in best or score > best[view][0]:
            best[view] = (score, crop)
    for view, (_, crop) in best.items():
        crop.save(vr / f"{cid}_view_{view}.png")
        made.append(f"{cid}_view_{view}.png")
    return made


def _homely(vr: Path, cid: str) -> bool:
    from lora import person
    try:
        return bool(HOMELY.search(person(vr.parents[1], cid).get("who", "")))
    except SystemExit:  # 설정에 없는 인물
        return False


def jobs_for(vr: Path, cid: str, what: str, only, seed: int, res: int):
    """(출발 그림, 프롬프트, 결과 경로, 시드, 크기, 표정 이름) 목록. 이미 있는 결과는 건너뛴다(끊겼다 다시 돌려도 이어짐)."""
    out = []
    homely = what in ("subtle", "expr", "both") and _homely(vr, cid)
    if what == "subtle":
        src = source_face(vr, cid)
        if not src.exists():
            print(f"{cid}: 출발 얼굴이 없어 표정 건너뜀")
            return out
        for k, ex in enumerate(subtle_list(vr.parents[1]), 1):
            f = vr / f"{cid}_xs_{k:02d}.png"
            if (only and k not in only) or f.exists():
                continue
            out.append((src, subtle_prompt(ex, homely), f, seed + 100 + k, res, ex.split(":")[0]))
        return out
    if what == "turn":  # 캐릭터 시트(얼굴 + 앞·뒤·옆·3/4 전신): 뒷모습·옆모습 참고 그림(2026-10-20 보조 도구 시험)
        src = vr / f"{cid}_front.png"
        if not src.exists():
            print(f"{cid}: 정면 전신이 없어 캐릭터 시트 건너뜀")
        elif not (vr / f"{cid}_turnaround.png").exists():
            from lora import person
            pr = "her" if re.search(r"woman|girl", person(vr.parents[1], cid).get("who", "")) else "his"
            out.append((src, TURN.format(pr=pr), vr / f"{cid}_turnaround.png", seed, 1024, None))
        return out
    if what in ("base", "both"):
        src = vr / f"{cid}_front.png"
        if not src.exists():
            print(f"{cid}: 정면 전신이 없어 베이스 건너뜀(vast/sheet_refs.py 가 시트를 못 자름)")
        elif not (vr / f"{cid}_base.png").exists():
            out.append((src, BASE, vr / f"{cid}_base.png", seed, 1024, None))
    if what in ("expr", "both"):
        src = source_face(vr, cid)
        if not src.exists():
            print(f"{cid}: 출발 얼굴이 없어 표정 건너뜀")
            return out
        for k, ex in enumerate(STRONG, 1):
            f = vr / f"{cid}_xp_{k:02d}.png"
            if (only and k not in only) or f.exists():
                continue
            out.append((src, expr_prompt(ex, homely), f, seed + k, res, ex.split(":")[0]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("cid", help="인물 id, 또는 all(시트가 있는 인물 전부)")
    ap.add_argument("what", choices=["base", "expr", "both", "subtle", "turn"],
                    help="subtle = 잔잔한 표정 묶음(<id>_xs_NN, series.json expressions) · turn = 캐릭터 시트(<id>_turnaround + 칸마다 <id>_view_*)")
    ap.add_argument("--only", help="표정 번호(1부터) 일부만")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--res", type=int, default=1024, help="표정 그림 크기(면적 기준). 768 이면 장당 더 빠르다")
    args = ap.parse_args()
    series = Path(args.series_dir)
    vr = series / "assets" / "vast_refs"
    cids = [s.name[: -len("_sheet.png")] for s in sorted((series / "assets").glob("*_sheet.png"))] if args.cid == "all" else [args.cid]
    if args.cid == "all" and not cids:  # 새 작품은 assets/<id>_sheet.png 대신 vast_refs/<id>_front.png 만 있다
        cids = sorted(f.name[: -len("_front.png")] for f in vr.glob("*_front.png"))
    only = {int(x) for x in args.only.split(",")} if args.only else None
    jobs = [(cid,) + j for cid in cids for j in jobs_for(vr, cid, args.what, only, args.seed, args.res)]
    print(f"할 일 {len(jobs)}장", flush=True)
    c, cache = Comfy(), {}
    # 전부 한꺼번에 큐에 넣는다: 한 장씩 넣고 기다리면 올리기·확인 사이에 GPU 가 논다(2026-10-15 사용자 "존나 오래 걸리노")
    queued = []
    for cid, src, prompt, outp, seed, res, label in jobs:
        if args.what == "turn":  # 캐릭터 시트 LoRA 는 터보를 쓰면 격자가 깨진다(작성자): 역할 워크플로우의 25스텝, 터보 끔
            g = graph(prompt, [upload(c, src, cache)], 0, 0, seed, 25, res, turbo=False, edit=True, series=series, role="asset_sheet")
        else:
            g = graph(prompt, [upload(c, src, cache)], 0, 0, seed, 8, res, turbo=True, edit=True, series=series,
                      role="asset_expression" if label else "asset_base")  # 표정(label 있음)·기본 몸은 각자 서버 워크플로우(판별)를 쓴다
        queued.append((cid, c.queue(g), outp, label))
    t0 = time.time()
    for n, (cid, pid, outp, label) in enumerate(queued, 1):
        hist = c.wait(pid, 1800)
        img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
        c.download(img, outp)
        if label:
            jf = vr / f"{cid}_expr.json"
            meta = json.loads(jf.read_text(encoding="utf-8")) if jf.exists() else {"labels": []}
            extra = {x["file"]: x for x in meta.get("extra", [])}
            extra[outp.name] = {"file": outp.name, "label": label}
            meta["extra"] = sorted(extra.values(), key=lambda x: x["file"])
            jf.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        if outp.name.endswith("_turnaround.png"):
            cells = split_turnaround(outp, vr, cid)
            print(f"  {cid}: 방향 그림 {len(cells)}개로 자름 {cells}" + ("" if cells else " (줄을 못 나눔, 시트만 남김)"), flush=True)
        print(f"[{n}/{len(queued)}] {outp.name} {label or ('캐릭터 시트' if args.what == 'turn' else '베이스')} (누적 {time.time() - t0:.0f}초)", flush=True)


if __name__ == "__main__":
    main()
