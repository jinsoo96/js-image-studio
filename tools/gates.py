"""관문(하네스의 검증 단계): 콘티·컷 설명을 기준표로 채점하고, 통과해야 그리기로 넘어간다.
(2026-10-18 사용자 "붙이면 끝, 작업 안내나 검수는 하네스화로". agent platform 하네스 원칙 그대로: 규칙은 에이전트 프롬프트가 아니라 격리 judge 가 강제한다.
 기준표는 skills/<스킬>/judge.json, 꼴은 하네스 s08 judge_criteria [{name, description, weight, hard}], 합격 0.8.)

    .venv/Scripts/python tools/gates.py <회차 폴더> [storyboard|panels|all]
결과: 기준별 점수·걸린 컷과 고칠 점을 찍고 review/gates.json 에 남긴다(파일 해시와 함께: 콘티를 고치면 다시 받아야 한다).
종료 코드 0 = 통과, 5 = 걸림. MCP(prepare_episode·make_episode)가 이걸로 그리기를 막는다.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LLM_URL = os.environ.get("VLM_URL", "https://judge-gateway.invalid/v1").rstrip("/")  # comfy_panel 과 같은 사내 모델
LLM_MODEL = os.environ.get("VLM_MODEL", "qwen3.8-27b")
CHUNK = 18  # 컷 설명은 이만큼씩 나눠 채점(모델 입력이 길면 뒤쪽을 대충 본다)


def _llm(text: str, max_tokens=1500) -> dict:
    body = {"model": LLM_MODEL, "temperature": 0.05, "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": text}]}
    req = urllib.request.Request(LLM_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
    with urllib.request.urlopen(req, timeout=240) as r:
        txt = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    return json.loads(re.search(r"\{.*\}", txt, re.S).group())


def _hash(*files) -> str:
    h = hashlib.sha1()
    for f in files:
        if Path(f).exists():
            h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def _criteria(skill: str) -> dict:
    return json.loads((ROOT / "skills" / skill / "judge.json").read_text(encoding="utf-8"))


ASK = """너는 웹툰 {what} 검수자(격리 judge)다. 아래 기준표로만 채점한다. 각 기준을 0.0~1.0 으로 매기고, 기준을 어긴 곳은 컷 번호와 고칠 점을 한 줄씩 쓴다.
확실한 위반만 적는다. 위반이 없으면 1.0 이고 problems 는 빈 목록이다.
기준표:
{criteria}

작품 설정:
{setting}

검수할 내용:
{material}

JSON 하나로만 답한다:
{{"scores": {{"<기준 name>": 0.0~1.0, ...}}, "problems": [{{"criterion": "<기준 name>", "panel": "p012", "fix": "고칠 점 한 줄"}}]}}"""


def _setting(series: Path) -> str:
    sj = series / "series.json"
    if not sj.exists():
        return "(작품 설정 파일 없음)"
    s = json.loads(sj.read_text(encoding="utf-8"))
    cast = "; ".join(f"{k}={v.get('ko')}({v.get('role', '')[:60]})" for k, v in s.get("cast", {}).items())
    return f"제목 {s.get('title')} · 장르 {s.get('genre', '')} · 시대 {s.get('era', '')} {s.get('era_rule', '')}\n인물: {cast}"


VERIFY = """너는 두 번째 검수자다. 첫 검수자의 지적이 실제 위반인지 엄격히 다시 본다.
같은 뜻을 다르게 쓴 것(예: '5개월'과 '다섯 달'), 취향 차이, 근거 없는 추측은 위반이 아니다. 기준표의 문장을 실제로 어긴 것만 남긴다.
기준표:
{criteria}

작품 설정:
{setting}

검수한 내용(일부):
{material}

첫 검수자의 지적:
{problems}

JSON 하나로만 답한다: {{"keep": [남길 지적의 번호(0부터)]}}"""


def _verify(crit: dict, setting: str, material: str, probs: list) -> list:
    """첫 judge 의 지적을 두 번째 judge 가 걸러낸다(오탐이 꼭 지킬 기준을 막으면 그리기가 멈춘다: '5개월' 대 '다섯 달')."""
    if not probs:
        return probs
    listing = "\n".join(f"{k}. [{p.get('criterion')}] {p.get('panel', '')}: {p.get('fix', '')}" for k, p in enumerate(probs))
    try:
        keep = _llm(VERIFY.format(criteria=json.dumps(crit["criteria"], ensure_ascii=False), setting=setting, material=material[:12000],
                                  problems=listing), max_tokens=400).get("keep", [])
    except Exception:  # noqa: BLE001  확인을 못 하면 첫 판정을 그대로 둔다
        return probs
    return [probs[k] for k in keep if isinstance(k, int) and 0 <= k < len(probs)]


def _score(crit: dict, results: list) -> tuple:
    """조각별 결과를 합쳐 기준별 최저 점수·가중 평균·hard 위반을 낸다."""
    names = [c["name"] for c in crit["criteria"]]
    probs = [p for r in results for p in r.get("problems", []) if isinstance(p, dict)]
    # 검증을 거쳐 지적이 하나도 안 남은 기준은 1.0(점수만 낮게 주고 근거가 없던 경우)
    low = {n: (min([float(r.get("scores", {}).get(n, 1.0)) for r in results] or [1.0]) if any(p.get("criterion") == n for p in probs) else 1.0) for n in names}
    wsum = sum(c["weight"] for c in crit["criteria"])
    total = sum(low[c["name"]] * c["weight"] for c in crit["criteria"]) / wsum
    hard = [c["name"] for c in crit["criteria"] if c["hard"] and any(p.get("criterion") == c["name"] for p in probs)]
    return low, total, hard, probs


def judge_storyboard(ep: Path) -> dict:
    crit = _criteria("storyboard")
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    lines = []
    for p in board["panels"]:
        if p.get("composite"):
            c = p["composite"]
            lines.append(f"{p['id']} [화면:{c.get('kind')}] " + json.dumps({k: v for k, v in c.items() if k in ('title', 'text', 'notices', 'messages', 'query', 'subtitle')}, ensure_ascii=False)[:200])
            continue
        talk = " / ".join(f"{d.get('type')}:{d.get('text')}" for d in p.get("dialogue", []))
        lines.append(f"{p['id']} [{p.get('shot')}·{p.get('scene')}·{','.join(p.get('characters', []))}] {talk}")
    setting, material = _setting(ep.parents[1]), "\n".join(lines)
    r = _llm(ASK.format(what="콘티", criteria=json.dumps(crit["criteria"], ensure_ascii=False, indent=1), setting=setting, material=material))
    r["problems"] = _verify(crit, setting, material, [p for p in r.get("problems", []) if isinstance(p, dict)])
    low, total, hard, probs = _score(crit, [r])
    return {"gate": "storyboard", "score": round(total, 3), "scores": low, "hard_failed": hard, "problems": probs,
            "passed": total >= crit["threshold"] and not hard, "hash": _hash(ep / "storyboard.json")}


def _talk(panel: dict) -> str:
    """컷의 말·생각 대사를 말풍선 자리와 함께('(top-right) 눈 좀 붙여.'). 말풍선 자리 관문(balloon_side)이 본다."""
    lines = [f"({d.get('pos', '?')}) {d.get('text', '')}" for d in panel.get("dialogue", []) if d.get("type") in ("speech", "thought")]
    return " / ".join(lines) or "없음"


def judge_panels(ep: Path) -> dict:
    sys.path.insert(0, str(ROOT / "tools"))
    from panels_from_spec import staging_lint
    crit = _criteria("panel-spec")
    panels = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    board = {p["id"]: p for p in json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))["panels"]}
    lint = staging_lint(panels, board)
    results = []
    for k in range(0, len(panels), CHUNK):
        part = panels[k:k + CHUNK]
        mat = "\n".join(
            f"{p['id']} 대사:{_talk(board.get(p['id'], {}))}\n"
            f"  {json.dumps({x: p.get(x) for x in ('chars', 'location', 'shot', 'action', 'acting', 'camera', 'extras')}, ensure_ascii=False)}"
            for p in part)
        setting, mat = _setting(ep.parents[1]), re.sub(r" SPACE \(identical.*?\"", '"', mat)
        r = _llm(ASK.format(what="컷 설명", criteria=json.dumps(crit["criteria"], ensure_ascii=False, indent=1), setting=setting, material=mat))
        r["problems"] = _verify(crit, setting, mat, [p for p in r.get("problems", []) if isinstance(p, dict)])
        results.append(r)
    low, total, hard, probs = _score(crit, results)
    probs = [{"criterion": "lint", "panel": ln.split()[0], "fix": ln} for ln in lint] + probs
    return {"gate": "panels", "score": round(total, 3), "scores": low, "hard_failed": hard, "problems": probs,
            "passed": total >= crit["threshold"] and not hard and not lint, "hash": _hash(ep / "panels.json", ep / "storyboard.json")}


def run(ep: Path, which: str = "all") -> dict:
    out = json.loads((ep / "review" / "gates.json").read_text(encoding="utf-8")) if (ep / "review" / "gates.json").exists() else {}
    if which in ("storyboard", "all"):
        out["storyboard"] = judge_storyboard(ep)
    if which in ("panels", "all") and (ep / "panels.json").exists():
        out["panels"] = judge_panels(ep)
    out["at"] = time.strftime("%Y-%m-%d %H:%M")
    (ep / "review").mkdir(exist_ok=True)
    (ep / "review" / "gates.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def status(ep: Path) -> dict:
    """지금 파일 기준으로 관문이 통과돼 있나(콘티·컷 설명을 고치면 해시가 달라져 다시 받아야 한다)."""
    f = ep / "review" / "gates.json"
    g = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    sb, pn = g.get("storyboard", {}), g.get("panels", {})
    return {"storyboard": bool(sb.get("passed")) and sb.get("hash") == _hash(ep / "storyboard.json"),
            "panels": bool(pn.get("passed")) and pn.get("hash") == _hash(ep / "panels.json", ep / "storyboard.json")}


def report(g: dict) -> str:
    lines = []
    for k, label in (("storyboard", "콘티"), ("panels", "컷 설명")):
        r = g.get(k)
        if not r:
            continue
        lines.append(f"관문 {label}: {'통과' if r['passed'] else '걸림'} (점수 {r['score']}"
                     + (f", 꼭 지킬 기준 위반 {', '.join(r['hard_failed'])}" if r["hard_failed"] else "") + ")")
        for p in r["problems"][:40]:
            lines.append(f"  - {p.get('panel', '')} [{p.get('criterion', '')}] {p.get('fix', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    ep = Path(sys.argv[1]).resolve()
    g = run(ep, sys.argv[2] if len(sys.argv) > 2 else "all")
    print(report(g))
    st = status(ep)
    sys.exit(0 if all(st.values()) else 5)
