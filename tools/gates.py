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
CHUNK = 18  # 컷 설명은 이만큼씩 나눠 채점(모델 입력이 길면 뒤쪽을 대충 본다)


def _llm(text: str, max_tokens=1500) -> dict:
    """관문 judge 한 번(주소는 vast/judge_ep: VLM_URL, 안 되면 VLM_FALLBACK). 닿지 않으면 예외 = 관문을 통과시키지 않는다."""
    sys.path.insert(0, str(ROOT / "vast"))
    from judge_ep import chat, parse_json
    # 판정 모델이 가끔 쉼표 빠진 JSON 을 돌려줬다(2026-10-19 콘티 관문에서 멈춤) → 세 번까지 다시 묻는다. 세 번 다 깨지면 예외(통과 아님)
    err = None
    for k in range(3):
        txt = chat([{"type": "text", "text": text if k == 0 else text + "\n\nReturn ONE valid JSON object only, no other text."}],
                   max_tokens=max_tokens, temperature=0.05 + 0.1 * k, timeout=240)
        try:
            return parse_json(txt, repair=False)  # 잘린 답은 살리지 않는다(걸린 문제가 빠진 채 통과할 수 있다)
        except ValueError as e:
            err = e
    raise err


def _hash(*files) -> str:
    h = hashlib.sha1()
    for f in files:
        if Path(f).exists():
            h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def _json_file(f: Path) -> dict:
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


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


def _panel_line(p: dict) -> str:
    """콘티 한 컷을 judge 가 읽는 한 줄로. 원작 문장 내레이션(authored)은 (원작)으로 표시한다(지어낸 결론 내레이션과 구분)."""
    if p.get("composite"):
        c = p["composite"]
        return f"{p['id']} [화면:{c.get('kind')}] " + json.dumps({k: v for k, v in c.items() if k in ('title', 'text', 'notices', 'messages', 'query', 'subtitle', 'body', 'subject', 'calls')}, ensure_ascii=False)[:300]
    # 말하는 사람(who)을 같이 준다: 없으면 두 사람 컷의 대사 순서를 judge 가 짐작해 호칭이 틀렸다고 오판했다(2026-10-19 외출 p017)
    talk = " / ".join(f"{d.get('type')}" + (f"({d['who']})" if d.get("who") else "") + ("(원작)" if d.get("authored") else "") + f":{d.get('text')}"
                      for d in p.get("dialogue", []))
    note = f" 그림:{p['note']}" if p.get("note") else ""
    return f"{p['id']} [{p.get('shot')}·{p.get('scene')}·{','.join(p.get('characters', []))}] {talk}{note}"


def beats_of(series: Path) -> dict:
    """원작 장면 목록(source/beats.json). 원작이 없는 작품은 빈 dict."""
    f = series / "source" / "beats.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def coverage(ep: Path, board: dict | None = None) -> dict:
    """이 화에 배정된 원작 장면을 컷의 beats 태그가 다 덮는지(코드 검사). 빠진 장면·없는 번호·태그 없는 그림 컷."""
    bj = beats_of(ep.parents[1])
    if not bj:
        return {}
    board = board or json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    known = {b["id"] for b in bj["beats"]}
    mine = [b["id"] for b in bj["beats"] if b.get("ep") == ep.name.split("_")[0]]
    tagged = {x for p in board["panels"] for x in p.get("beats", [])}
    untagged = [p["id"] for p in board["panels"] if not p.get("beats") and p.get("characters") and not p.get("composite")]
    return {"assigned": len(mine), "missing": [b for b in mine if b not in tagged], "unknown": sorted(tagged - known), "untagged": untagged}


BEAT_ASK = """너는 원작 각색 검수자(격리 judge)다. 아래는 원작 장면과, 콘티에서 그 장면을 담았다고 표시한 컷들이다.
각 장면의 핵심(누가 무엇을 말하고 묻고 답했는지, 내레이션이 전하는 감정, 일어난 일)이 그 컷들의 대사·그림 설명에 실제로 들어 있는지 본다.
대사를 줄이거나 말을 바꾼 것, 같은 뜻을 다르게 쓴 것은 괜찮다. 대화 한쪽이 통째로 빠졌거나, 장면의 핵심 사실·감정이 없으면 빠진 것이다.
나이를 10살 올린 각색이라 숫자(경력 연수·나이)가 다른 것은 빠진 것이 아니다.

{material}

JSON 하나로만 답한다: {{"missing": [{{"beat": "b014", "why": "빠진 것 한 줄"}}]}} (다 들어 있으면 빈 목록)"""


def verify_beats(ep: Path, board: dict, chunk: int = 10) -> list:
    """태그가 있는 장면을 judge 가 실제 내용과 대조한다(태그만 달고 내용을 뺀 컷을 잡는다). 두 번째 judge 가 오탐을 거른다."""
    bj = beats_of(ep.parents[1])
    by_id = {p["id"]: p for p in board["panels"]}
    tag = {}
    for p in board["panels"]:
        for b in p.get("beats", []):
            tag.setdefault(b, []).append(p["id"])
    mine = [b for b in bj.get("beats", []) if b.get("ep") == ep.name.split("_")[0] and b["id"] in tag]
    probs = []
    for k in range(0, len(mine), chunk):
        part = mine[k:k + chunk]
        material = "\n\n".join(f"[{b['id']}] 원작: {b['what']}\n" + "\n".join("  " + _panel_line(by_id[i]) for i in tag[b["id"]]) for b in part)
        try:
            r = _llm(BEAT_ASK.format(material=material), max_tokens=900)
        except Exception as e:  # noqa: BLE001  확인을 못 하면 통과로 넘기지 않는다
            probs.append({"criterion": "coverage", "panel": part[0]["id"], "fix": f"원작 대조 검사를 못 했다({type(e).__name__})"})
            continue
        found = [{"criterion": "coverage", "panel": m.get("beat", ""), "fix": f"원작 장면 {m.get('beat')}: {m.get('why', '')}"}
                 for m in r.get("missing", []) if isinstance(m, dict) and m.get("beat") in tag]
        crit = {"criteria": [{"name": "coverage", "description": "원작 장면의 대화·핵심 사실·감정이 그 컷들에 들어 있다. 대사를 줄이거나 바꾼 것은 위반이 아니다."}]}
        probs += _verify(crit, "원작 각색(나이 +10)", material, found)
    return probs


def judge_storyboard(ep: Path) -> dict:
    crit = _criteria("storyboard")
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    lines = [_panel_line(p) for p in board["panels"]]
    setting, material = _setting(ep.parents[1]), "\n".join(lines)
    r = _llm(ASK.format(what="콘티", criteria=json.dumps(crit["criteria"], ensure_ascii=False, indent=1), setting=setting, material=material))
    r["problems"] = _verify(crit, setting, material, [p for p in r.get("problems", []) if isinstance(p, dict) and p.get("criterion") != "coverage"])
    # 원작이 있으면 장면 목록과 대조(2026-10-19 사용자 "스토리를 빼먹고 공간·에셋만 맞춰 작업했다, 내용 빼트리지 말고")
    cov = coverage(ep, board)
    if cov:
        if cov["missing"]:
            r["problems"].append({"criterion": "coverage", "panel": "-", "fix": f"원작 장면이 빠졌다(어느 컷에도 beats 태그 없음): {', '.join(cov['missing'])}"})
        if cov["unknown"]:
            r["problems"].append({"criterion": "coverage", "panel": "-", "fix": f"beats.json 에 없는 장면 번호: {', '.join(cov['unknown'])}"})
        if not cov["missing"]:
            r["problems"] += verify_beats(ep, board)
    low, total, hard, probs = _score(crit, [r])
    return {"gate": "storyboard", "score": round(total, 3), "scores": low, "hard_failed": hard, "problems": probs,
            "coverage": cov or None, "passed": total >= crit["threshold"] and not hard,
            "hash": _hash(ep / "storyboard.json", ep.parents[1] / "source" / "beats.json")}


CONSIST = """Below are webtoon panel descriptions (JSON per panel). Inside ONE panel, find fields that contradict each other:
- a person's clothes, hair or carried item described in action/acting/camera with a different colour or garment than that person's outfit
  (e.g. outfit says "white dress shirt" but action says "his dark shirt");
- a person on the LEFT in action but on the RIGHT in camera (or facing opposite ways).
Only report clear contradictions between fields of the same panel, not style or missing detail.

{material}

Answer JSON only: {{"problems": [{{"panel": "p012", "fix": "action says dark shirt but outfit says white dress shirt: make them the same"}}]}}"""


def consistency(mat: str) -> list:
    """한 컷 안의 칸끼리 어긋난 곳(옷 색·좌우)만 따로 묻는다. 기준 열한 개를 한 번에 묻는 채점에서는 outfit '흰 셔츠' ↔ action
    'dark shirt' 를 놓쳐 그림이 둘 중 하나를 골랐다(2026-10-24 야근의 온도 3화 p002). 연출 확인(run_checks)처럼 한 번에 한 가지를 묻는다."""
    r = _llm(CONSIST.format(material=mat), max_tokens=900)
    return [{"criterion": "self_consistent", "panel": str(p.get("panel", "")), "fix": str(p.get("fix", ""))}
            for p in r.get("problems", []) if isinstance(p, dict) and p.get("fix")]


def _talk(panel: dict) -> str:
    """컷의 말·생각 대사를 말풍선 자리와 함께('(top-right) 눈 좀 붙여.'). 말풍선 자리 관문(balloon_side)이 본다."""
    lines = [f"({d.get('pos', '?')}) {d.get('text', '')}" for d in panel.get("dialogue", []) if d.get("type") in ("speech", "thought")]
    return " / ".join(lines) or "없음"


def judge_panels(ep: Path) -> dict:
    sys.path.insert(0, str(ROOT / "tools"))
    from panels_from_spec import structure_lint
    crit = _criteria("panel-spec")
    panels = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    board = {p["id"]: p for p in json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))["panels"]}
    lint = structure_lint(panels, board)
    scenes = (_json_file(ep / "panels_spec.json").get("scenes") or {})
    # 바뀌지 않았고 지난번에 문제없이 지나간 묶음은 다시 채점하지 않는다: 채점 모델이 돌릴 때마다 손대지 않은 컷에서 새 문제를 집어
    # 대사 한 줄 고치는 일이 관문 왕복 34번이 됐다(2026-10-24 스튜디오 시험). 바뀐 묶음·문제가 있던 묶음만 다시 본다
    cache_f = ep / "review" / "gates_panels_cache.json"
    cache = _json_file(cache_f) if cache_f.exists() else {}
    crit_key = hashlib.sha1((json.dumps(crit, ensure_ascii=False, sort_keys=True) + ASK + CONSIST).encode()).hexdigest()[:10]  # 묻는 말이 바뀌면 다시 채점
    new_cache = {}
    results = []
    for k in range(0, len(panels), CHUNK):
        part = panels[k:k + CHUNK]
        mat = "\n".join(
            f"{p['id']} 대사:{_talk(board.get(p['id'], {}))}\n"
            f"  {json.dumps({x: p.get(x) for x in ('chars', 'outfit', 'location', 'view', 'shot', 'action', 'acting', 'camera', 'extras')}, ensure_ascii=False)}"
            for p in part)
        mat = re.sub(r" SPACE \(identical.*?\"", '"', mat)
        # 장면 배치(자리표)는 장소마다 한 번: 공간 기준(누가 어디 앉아 있고 어느 각도에서 누가 보여야 하나)을 채점하려면 필요하다
        locs = {p.get("scene") or p.get("location") for p in part}
        if any(scenes.get(l) for l in locs):
            mat = "장면 배치:\n" + "\n".join(f"  {l}: {scenes[l]}" for l in sorted(x for x in locs if x and scenes.get(x))) + "\n\n" + mat
        setting = _setting(ep.parents[1])
        key = hashlib.sha1((crit_key + setting + mat).encode()).hexdigest()[:16]
        if key in cache and not cache[key].get("problems"):
            r = cache[key]
        else:
            r = _llm(ASK.format(what="컷 설명", criteria=json.dumps(crit["criteria"], ensure_ascii=False, indent=1), setting=setting, material=mat))
            first = [p for p in r.get("problems", []) if isinstance(p, dict) and p.get("criterion") != "self_consistent"]
            r["problems"] = _verify(crit, setting, mat, first + consistency(mat))
        new_cache[key] = r
        results.append(r)
    (ep / "review").mkdir(exist_ok=True)
    cache_f.write_text(json.dumps(new_cache, ensure_ascii=False), encoding="utf-8")
    low, total, hard, probs = _score(crit, results)
    probs = [{"criterion": "lint", "panel": ln.split()[0], "fix": ln} for ln in lint] + probs
    return {"gate": "panels", "score": round(total, 3), "scores": low, "hard_failed": hard, "problems": probs,
            "passed": total >= crit["threshold"] and not hard and not lint, "hash": _hash(ep / "panels.json", ep / "storyboard.json")}


def run(ep: Path, which: str = "all") -> dict:
    out = json.loads((ep / "review" / "gates.json").read_text(encoding="utf-8")) if (ep / "review" / "gates.json").exists() else {}
    if which in ("storyboard", "all"):
        # 콘티가 그대로이고 지난번에 지나갔으면 다시 채점하지 않는다(채점 모델이 같은 콘티를 돌릴 때마다 다르게 걸었다)
        old = out.get("storyboard") or {}
        if not (old.get("passed") and old.get("hash") == _hash(ep / "storyboard.json", ep.parents[1] / "source" / "beats.json")):
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
    return {"storyboard": bool(sb.get("passed")) and sb.get("hash") == _hash(ep / "storyboard.json", ep.parents[1] / "source" / "beats.json"),
            "panels": bool(pn.get("passed")) and pn.get("hash") == _hash(ep / "panels.json", ep / "storyboard.json")}


def report(g: dict) -> str:
    lines = []
    for k, label in (("storyboard", "콘티"), ("panels", "컷 설명")):
        r = g.get(k)
        if not r:
            continue
        lines.append(f"관문 {label}: {'통과' if r['passed'] else '걸림'} (점수 {r['score']}"
                     + (f", 꼭 지킬 기준 위반 {', '.join(r['hard_failed'])}" if r["hard_failed"] else "") + ")")
        cov = r.get("coverage")
        if cov:
            lines.append(f"  원작 장면 {cov['assigned']}개 중 빠짐 {len(cov['missing'])}"
                         + (f", 장면 태그 없는 인물 컷 {len(cov['untagged'])}({', '.join(cov['untagged'][:8])})" if cov.get("untagged") else ""))
        for p in r["problems"][:40]:
            lines.append(f"  - {p.get('panel', '')} [{p.get('criterion', '')}] {p.get('fix', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    ep = Path(sys.argv[1]).resolve()
    g = run(ep, sys.argv[2] if len(sys.argv) > 2 else "all")
    print(report(g))
    st = status(ep)
    sys.exit(0 if all(st.values()) else 5)
