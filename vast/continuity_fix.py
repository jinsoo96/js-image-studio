"""장면 이어짐 자동 고치기: continuity_check 가 잡은 짝에서 같은 사람의 옷·모자·안경·장갑·머리가 바뀐 것을, 컷 설명(outfit)을 어긴 쪽만
그 사람 둘레 상자(panel_edit.region_edit)로 다른 쪽 컷에 맞춘다. 고친 뒤 짝을 다시 보고 차이가 줄었고 다른 사람 얼굴이 안 바뀌었을 때만
본편에 넣는다(옛 본편은 panels_hf/_rejected/). 자세·자리·소품·배경·카메라 차이는 고치지 않는다(다시 그리기 몫).
(2026-10-27 사용자 "이런 일이 안 생기게 도구를 고치든가": 개항 1화 v4 이어짐 12짝은 기록만 되고 고쳐지지 않았다)

    .venv/Scripts/python tools/continuity_check.py <회차 폴더> --fix [--only …]    (그리기 상자와 판정 모델이 켜져 있어야 한다)
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
import panel_edit as PE  # noqa: E402
import refine as R  # noqa: E402

FIX_ASK = """Image 1 is the previous panel and image 2 the next panel of the same webtoon scene.
These continuity mistakes were found between them:
{problems}
The written outfit of each person who appears in both panels (the plan the artist must follow):
{outfits}
For each mistake that is about one person's clothes, hat or cap, glasses, gloves, jewelry or hairstyle, decide which image is wrong:
the image whose drawing does not follow that person's written outfit for that image. If both follow it, or the plan does not say, the wrong one is image 2.
Skip mistakes about poses, where people stand, props on tables, the background, the camera or a different face.
Answer JSON only: {{"fixes": [{{"image": 1 or 2, "who": "<person id from the list>", "part": "head" or "upper" or "full", "change": "<in English, what to change on that person so it matches the other image and the plan, e.g. 'remove the black fedora; he is bare-headed with combed-back gray hair'>"}}]}} (empty list if none of the mistakes is about clothes, hats, glasses, gloves, jewelry or hair)."""


def _clean(name: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", name or "")


def plan(prev: dict, prev_img: Path, p: dict, img: Path, problems: list, names: dict) -> list:
    """어느 컷의 누구를 어떻게 고칠지 [{image, who, part, change}]. 옷·모자·안경·장갑·장신구·머리만."""
    both = [c for c in (p.get("chars") or []) if c in (prev.get("chars") or [])]
    if not both:
        return []
    o1, o2 = prev.get("outfit") or {}, p.get("outfit") or {}
    outfits = "\n".join(f"- {c} ({_clean(names.get(c, c))}): image 1: {o1.get(c) or '(not written)'}; image 2: {o2.get(c) or '(not written)'}" for c in both)
    r = R._vlm([prev_img, img], FIX_ASK.format(problems="\n".join(f"- {x}" for x in problems), outfits=outfits), 500)
    out = []
    for f in r.get("fixes") or []:
        if not isinstance(f, dict):
            continue
        who, image, change = str(f.get("who") or "").strip(), f.get("image"), str(f.get("change") or "").strip()
        if who in both and image in (1, 2, "1", "2") and change:
            out.append({"image": int(image), "who": who, "part": f.get("part") if f.get("part") in PE.PARTS else "upper", "change": change})
    return out


def _say(msg: str):
    print(msg, flush=True)  # 작업 로그가 자라야 멈춤 지킴이가 끊지 않는다


def edit_ready(series: Path, wait_s: int = 180, log=_say) -> bool:
    """편집 워크플로우를 서버에서 불러올 수 있나(새로 켠 서버면 작업실 사본을 올리며 기다린다). 못 하면 고치지 않는다:
    코드 그래프로 빠지면 서버가 거절했다(2026-10-27)."""
    from saved_workflow import load_ui, models, workflow_for
    t0 = time.time()
    while True:
        try:
            models(refresh=True)
            load_ui(workflow_for("edit", series))
            return True
        except Exception as e:  # noqa: BLE001
            if time.time() - t0 > wait_s:
                log(f"이어짐 고치기 건너뜀: 서버에 편집 워크플로우가 없다({type(e).__name__}: {e})")
                return False
            log("편집 워크플로우를 서버에 올리는 중… 다시 확인")
            time.sleep(15)


def fix_pair(series: Path, ep: Path, prev: dict, p: dict, problems: list, names: dict, log=_say) -> list:
    """짝 하나를 고친다. 고친 뒤 남은 이어짐 문제 목록을 돌려준다(못 고쳤으면 처음 그대로)."""
    prev_img, img = ep / "panels_hf" / f"{prev['id']}.png", ep / "panels_hf" / f"{p['id']}.png"
    fixes = plan(prev, prev_img, p, img, problems, names)
    if not fixes:
        log(f"  이어짐 {prev['id']}→{p['id']}: 옷·모자·머리 차이가 아니어서 고치지 않음(사람 확인)")
        return problems
    left = problems
    for f in fixes:
        target, other = (prev, p) if f["image"] == 1 else (p, prev)
        tid, oid = target["id"], other["id"]
        timg = ep / "panels_hf" / f"{tid}.png"
        name = _clean(names.get(f["who"], f["who"]))
        box = PE.person_box(series, timg, f["who"], f["part"])
        instr = (f"In <image1>, change {name}: {f['change']}. Make {name}'s clothes, hat, glasses, gloves and hair match {name} in <image2> "
                 f"and the outfit '{(target.get('outfit') or {}).get(f['who']) or ''}'. Keep {name}'s face, expression, pose and hands.")
        before = timg.read_bytes()
        try:
            res = PE.preview(series, ep, tid, instr, refs=oid, seed=int(time.time()) % 100000, how=f"이어짐 {prev['id']}→{p['id']}: {f['change']}", box=box)
        except SystemExit as e:
            log(f"  이어짐 {tid}: 고치기 실패 {e}")
            continue
        new = Path(res["image"])
        a, b = (new, ep / "panels_hf" / f"{p['id']}.png") if f["image"] == 1 else (prev_img, new)
        after = R.continuity_pair(a, b, p, prev, names)
        drift = PE.drift(series, ep, tid, before, new)
        if len(after) < len(left) and not drift:
            PE.adopt(ep, tid, res["preview"])
            log(f"  이어짐 {tid} 고침({name}: {f['change'][:80]}) 상자 {box or '통째'} · 남은 차이 {len(left)}→{len(after)}")
            left = after
        else:
            why = "다른 사람 얼굴이 바뀜" if drift else f"차이가 줄지 않음({len(left)}→{len(after)})"
            log(f"  이어짐 {tid}: 고친 판을 버림({why}), 미리 보기 {res['preview']}")
    return left
