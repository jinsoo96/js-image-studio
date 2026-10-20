"""검수 하네스 시험: 사용자가 지적한 실제 결함 사례(skills/staging-review/regression_cases.json)마다 나쁜 판과 고친 판에 같은 검사를 돌려
잡음(나쁜 판을 걸었나) · 헛걸림(고친 판을 걸었나)을 센다. 검사를 고치거나 판정 모델이 바뀌면 돌린다.
(2026-10-20 사용자 "내가 쿠사리 줬던 지시사항 및 중요 지침사항들도 하네스화가 잘 돼서 이뤄지는지 확인")

    .venv/Scripts/python tools/harness_selftest.py [--only p006,p203] [--workers 3]   → review 결과 state/harness_selftest_<시각>.json
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
sys.path.insert(0, str(ROOT / "tools"))
import refine as R  # noqa: E402
from hf_panel import names  # noqa: E402
from judge_ep import endpoint  # noqa: E402

CASES = ROOT / "skills" / "staging-review" / "regression_cases.json"


def _spec(ep: Path) -> dict:
    return {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))}


def _speakers(series: Path, ep: Path, pid: str) -> str:
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    bp = next((x for x in board["panels"] if x["id"] == pid), {})
    nm = names(series)
    spk = sorted({d.get("who") for d in bp.get("dialogue") or [] if d.get("who") and d.get("type") not in ("monologue", "narration", "caption", "sfx")})
    return ", ".join(re.sub(r"\s*\(.*?\)", "", nm.get(w, w)) for w in spk) or "nobody (only narration, no one speaks aloud)"


def run_check(case: dict, img: Path) -> list:
    """이 그림에 이 사례의 검사를 돌려 걸린 점 목록(비면 통과). check 를 '+' 로 이으면 모두 돌린다."""
    if "+" in case["check"]:
        return [x for part in case["check"].split("+") for x in run_check({**case, "check": part}, img)]
    series = ROOT / "series" / case["series"]
    ep = series / "episodes" / case["episode"]
    spec = _spec(ep)
    p = spec[case["panel"]]
    kind, _, arg = case["check"].partition(":")
    if kind == "absent":
        return [f"{x['name']}: {x.get('where', '')}" for x in R.absent_cast(series, ep, p["id"], img)]
    if kind == "doubled":
        from scene_plate import _doubled
        d = _doubled(img, series, p.get("chars") or [])
        return [d.get("why", "doubled")] if d.get("doubled") else []
    if kind == "limbs":
        return R.extra_limbs(img)
    if kind == "continuity":
        prev = ep / "panels_hf" / f"{arg}.png"
        return R.continuity_pair(prev, img, p, spec.get(arg), names(series))
    if kind == "checks":
        sc = ep / "panels_spec.json"
        layout = (json.loads(sc.read_text(encoding="utf-8")).get("scenes", {}).get(p.get("scene") or p.get("location") or "", "") if sc.exists() else "")
        out = R.run_checks(series, img, p, layout, _speakers(series, ep, p["id"]), only=set(arg.split(",")))
        return [f"{k}: {x}" for k, v in out.items() for x in v]
    raise ValueError(f"모르는 검사 {case['check']}")


def one(case: dict) -> dict:
    ep = ROOT / "series" / case["series"] / "episodes" / case["episode"]
    bad, good = ep / case["bad"], ep / (case.get("good") or f"panels_hf/{case['panel']}.png")
    r = {"panel": case["panel"], "check": case["check"], "user": case["user"]}
    if not bad.exists():
        return {**r, "error": f"나쁜 판이 없다: {case['bad']}"}
    f0 = R.JUDGE_FAILS["n"]
    r["bad_found"] = run_check(case, bad)
    r["good_found"] = run_check(case, good) if good.exists() else None
    r["caught"] = bool(r["bad_found"])
    r["false_alarm"] = bool(r["good_found"])
    r["judge_fail"] = R.JUDGE_FAILS["n"] - f0
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if a.only:
        cases = [c for c in cases if c["panel"] in a.only.split(",")]
    judge = endpoint()
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(one, cases))
    for r in res:
        mark = "잡음" if r.get("caught") else ("오류" if r.get("error") else "놓침")
        fa = " · 고친 판도 걸림" if r.get("false_alarm") else ""
        print(f"{r['panel']} [{r['check']}] {mark}{fa} — {r['user']}" + (f" | {r.get('error')}" if r.get("error") else "")
              + (f" | 나쁜 판: {'; '.join(r['bad_found'])[:160]}" if r.get("bad_found") else ""), flush=True)
    ok = [r for r in res if not r.get("error")]
    caught = sum(r["caught"] for r in ok)
    fa = sum(bool(r["false_alarm"]) for r in ok)
    summary = {"judge": judge[:2], "cases": len(ok), "caught": caught, "false_alarm": fa, "seconds": int(time.time() - t0)}
    print(f"판정 모델 {judge[1]}: 사례 {len(ok)} · 잡음 {caught} · 놓침 {len(ok) - caught} · 고친 판 헛걸림 {fa} · {summary['seconds']}초")
    out = ROOT / "state" / f"harness_selftest_{time.strftime('%m%d-%H%M')}.json"
    out.write_text(json.dumps({"summary": summary, "results": res}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
