"""장면 이어짐 점검: 콘티 순서대로 같은 장소의 앞 컷과 이 컷을 나란히 보여 주고, 자세·소품·옷·얼굴·그림체·좌우가 깨진 곳을 묻는다.
(2026-10-18 외출 1화 사용자 지적: 앉아 있던 은영이 다음 컷에 서 있고, 맥주 두 잔이 세 잔·커피잔으로 바뀌고, 다음 컷에서 그림체와 얼굴이 바뀌었다.
 컷 하나씩 보는 검수로는 못 잡는다.) 근거: 영화 연출의 연속성 원칙(소품 수량·상태, 동작·위치 연속성), skills/panel-spec '연출 문법'.

    .venv/Scripts/python tools/continuity_check.py <회차 폴더> [--only p010,p012] [--json out.json] [--record]
--only 를 주면 그 컷이 들어간 짝만 본다. 걸린 짝을 "이어짐 p010→p012: ..." 로 찍고, 하나라도 있으면 종료 코드 4.
--record 는 걸린 짝의 뒤 컷을 review/staging.json 에 '이어짐'으로 남긴다(make_episode 가 쓴다).
--fix 는 같은 사람의 옷·모자·안경·장갑·머리가 바뀐 짝을 컷 설명을 어긴 쪽만 그 사람 둘레 상자로 고친다(vast/continuity_fix.py,
그리기 상자와 판정 모델이 켜져 있어야 한다). 고친 뒤 다시 본 결과로 기록한다.
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from refine import continuity_pair  # noqa: E402


def pairs(ep: Path):
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))["panels"]
    spec = {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))}
    prev = None
    for b in board:
        p = spec.get(b["id"])
        img = ep / "panels_hf" / f"{b['id']}.png"
        if b.get("composite") or not p or not img.exists():
            continue
        if "insert" in (p.get("shot") or "") or not p.get("chars"):  # 손·소품 인서트와 빈 장소는 건너뛰고 그 앞 인물 컷과 잇는다
            continue
        if prev and prev[0].get("location") == p.get("location"):
            yield prev[0], prev[1], p, img
        prev = (p, img)


def record(ep: Path, res: list):
    """본 짝의 뒤 컷에 '이어짐'을 더하거나(걸림) 지운다(통과). 다른 검수 항목은 그대로 둔다.
    이어짐 결과를 화면에만 찍으니 합성까지(until compose) 부른 답에는 안 나가, 사람이던 고양이 수인 모쿠가 아무 말 없이 PDF 로 나갔다(2026-10-24 등불 상점 1화)."""
    f = ep / "review" / "staging.json"
    cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    for prev, pid, pr in res:
        v = cur.get(pid) or {"issues": [], "note": ""}
        note = re.sub(r"\s*/ 이어짐 [^/]*", "", v.get("note", ""))
        issues = sorted(set(v.get("issues", [])) - {"이어짐"})
        if pr:
            issues = sorted(set(issues) | {"이어짐"})
            note = (note + f" / 이어짐 {prev}→{pid}: " + "; ".join(pr)).strip(" /")
        if issues:
            cur[pid] = {**v, "issues": issues, "note": note[:900], "at": v.get("at") or time.strftime("%Y-%m-%d %H:%M:%S")}
        else:
            cur.pop(pid, None)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--only")
    ap.add_argument("--json")
    ap.add_argument("--record", action="store_true", help="걸린 짝의 뒤 컷을 review/staging.json 에 '이어짐'으로 남긴다(합성·내보내기 답에 붙는다)")
    ap.add_argument("--fix", action="store_true", help="옷·모자·안경·장갑·머리가 바뀐 짝을 상자 편집으로 고친다(GPU 그리기 상자 필요)")
    a = ap.parse_args()
    ep = Path(a.episode_dir)
    only = set(a.only.split(",")) if a.only else None
    todo = [x for x in pairs(ep) if not only or x[0]["id"] in only or x[2]["id"] in only]

    from common import names
    nm = names(ep.parents[1])

    import refine as R
    UNCHECKED = "검사 못 함: 판정 모델에 닿지 않았다"

    def one(x):
        pp, pimg, p, img = x
        n0 = R.JUDGE_FAILS["n"]
        pr = continuity_pair(pimg, img, p, pp, nm)
        # 판정 모델에 닿지 못한 빈 답을 '이어짐 문제 없음'으로 읽지 않는다: LLM 상자가 안 떴는데 14짝이 7초 만에 모두 통과로 기록됐다(2026-10-27).
        # 여러 짝이 동시에 돌아 다른 짝의 실패가 섞일 수 있지만, 그 짝도 '검사 못 함'으로 남는 쪽이 안전하다
        if not pr and R.JUDGE_FAILS["n"] > n0:
            pr = [UNCHECKED]
        return pp["id"], p["id"], pr
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, todo))
    bad = [(a_, b_, pr) for a_, b_, pr in res if pr]
    for a_, b_, pr in bad:
        print(f"이어짐 {a_}→{b_}: " + "; ".join(pr), flush=True)
    unchecked = sum(1 for _, _, pr in res if pr == [UNCHECKED])
    if unchecked:
        print(f"[경고] 판정 모델에 닿지 않아 {unchecked}짝을 보지 못했다({R.JUDGE_FAILS['last']}): 통과가 아니다. LLM 상자를 켜고 다시", flush=True)
    bad_fixable = [x for x in bad if x[2] != [UNCHECKED]]
    if a.fix and bad_fixable:
        from continuity_fix import edit_ready, fix_pair
        if not edit_ready(ep.parents[1]):
            a.fix = False
    if a.fix and bad_fixable:
        spec = {x[2]["id"]: x for x in todo}
        fixed = {}
        for a_, b_, pr in bad_fixable:
            pp, _, p, _ = spec[b_]
            print(f"이어짐 고치기 {a_}→{b_}", flush=True)
            fixed[(a_, b_)] = fix_pair(ep.parents[1], ep, pp, p, pr, nm)
        res = [(x, y, fixed.get((x, y), z)) for x, y, z in res]
        bad = [(a_, b_, pr) for a_, b_, pr in res if pr]
        print(f"고친 뒤 걸린 짝 {len(bad)}", flush=True)
    print(f"이어짐 점검 {len(todo)}짝, 걸린 짝 {len(bad)}")
    if a.record:
        record(ep, res)
    if a.json:
        Path(a.json).write_text(json.dumps([{"prev": x, "panel": y, "problems": z} for x, y, z in bad], ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(4 if bad else 0)


if __name__ == "__main__":
    main()
