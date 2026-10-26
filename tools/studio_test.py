"""미사일 스튜디오(또는 `ms ask --json` 을 받는 어느 에이전트 CLI)로 웹툰 MCP 를 시나리오대로 시험하고 자동으로 채점한다.
(2026-10-24 사용자 "미사일 스튜디오에서 무한 테스트, 검수가 별로면 도구를 고치고 다시": 손으로 돌리던 시험을 되풀이할 수 있게)

    .venv/Scripts/python tools/studio_test.py [시나리오 이름 ...] [--provider anthropic] [--file tests/studio_scenarios.json]

시나리오 파일(tests/studio_scenarios.json): [{"name": "...", "prompt": "사용자 말", "provider": "선택", "gpu": false, "then": ["같은 대화에서 이어 할 말", ...],
  "checks": {"must_call": ["contact_sheet"], "max_tool_errors": 1, "max_seconds": 180, "no_false_done": true, "answer_has": ["낱말"]}}]
"each_series": true 면 작품마다 하나씩 펼친다: 말·사본·치우기의 {name}·{ep_n}·{p1}·{char} 같은 칸을 그 작품 폴더에서 채운다(series_vars).
돌리기 전에 --list 로 펼친 말과 사본을 본다(돈 0). --series 로 작품을 고른다.
채점: 도구 오류 수 · 꼭 부를 도구 · 시간 · 시작만 한 작업을 '다 됐다'고 말하지 않았나(no_false_done) · 답에 들어갈 낱말.
결과: 화면 + state/logs/studio_test_<날짜>.md(시나리오마다 통과·실패와 까닭, 도구 호출 줄).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MS = os.environ.get("MS_EXE", r"D:\missile-studio\.venv\Scripts\ms.exe")
# 끝났다고 단정하는 말끝만('"다 됐어?" 하시면' 처럼 사용자 말을 옮긴 것은 세지 않는다)
DONE_WORDS = re.compile(r"(다 됐(습니다|어요|다\b)|완료(했|됐|되었)(습니다|어요)|끝났(습니다|어요)|다 나왔(습니다|어요)|다 그렸(습니다|어요))")


def ask(text: str, provider: str = "", conversation: str = "", timeout: int = 1500) -> dict:
    cmd = [MS, "ask", "--json"] + (["--provider", provider] if provider else []) + (["--conversation", conversation] if conversation else []) + [text]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    raw = p.stdout
    i = 0 if raw.startswith("{") else raw.find("\n{") + 1  # 승인 상자가 앞에 찍힐 수 있다
    try:
        d = json.loads(raw[i:])
    except ValueError:
        return {"error": "JSON 아님: " + (raw[:300] + p.stderr[-300:]).replace("\n", " "), "events": [], "text": ""}
    d["approval"] = raw[:i].strip() if i else ""
    return d


def wait_jobs(max_min: float):
    """웹툰 MCP 작업(state/mcp_jobs)이 더 이상 돌지 않을 때까지(최대 max_min 분) 기다린다."""
    end = time.time() + max_min * 60
    jobs = ROOT / "state" / "mcp_jobs"
    while time.time() < end:
        running = [f for f in jobs.glob("*.status.json") if time.time() - f.stat().st_mtime < 6 * 3600
                   and json.loads(f.read_text(encoding="utf-8")).get("state") in ("starting", "running")]
        if not running:
            return
        time.sleep(20)


def summarize(d: dict) -> dict:
    calls, errors, running = [], [], set()
    tok = [0, 0]  # 대화 모델 입력·출력 토큰(보고서에 시나리오마다): Claude 로 돌린 시험에 돈이 얼마 들었는지 나중에 기록을 뒤져야 했다(2026-10-24)
    for e in d.get("events", []):
        if e["type"] == "usage":
            tok[0] += int(e.get("input_tokens") or 0)
            tok[1] += int(e.get("output_tokens") or 0)
        elif e["type"] == "tool_call":
            calls.append((e["name"], json.dumps(e.get("arguments"), ensure_ascii=False)[:160]))
        elif e["type"] == "tool_result":
            txt = e.get("text") or ""
            if e.get("is_error") or txt.lstrip().startswith('{"error"') or "denied" in txt[:80]:
                errors.append(txt[:200].replace("\n", " "))
            if '"state": "running"' in txt or '"result_ready": false' in txt:
                running.add(e["name"])
            # 끝난 작업: result_ready 는 긴 결과(review·log_tail) 뒤에 있어 잘린 결과 글에선 안 보였다 → state 로도 본다
            if '"result_ready": true' in txt or (txt.lstrip().startswith('{"job"') and re.search(r'"state": "(done|needs_review)"', txt[:300])):
                running.discard("webtoon-studio__job_status")
                running.clear()
        elif e["type"] == "error":
            errors.append(str(e.get("message"))[:200])
    return {"calls": calls, "errors": errors, "still_running": bool(running), "tokens": tok}


def judge(sc: dict, turns: list, seconds: float) -> list:
    ck = sc.get("checks") or {}
    fails = []
    called = {n.split("__")[-1] for t in turns for n, _ in t["sum"]["calls"]}
    for need in ck.get("must_call", []):
        if need not in called:
            fails.append(f"{need} 를 부르지 않음")
    n_err = sum(len(t["sum"]["errors"]) for t in turns)
    if n_err > ck.get("max_tool_errors", 2):
        fails.append(f"도구 오류 {n_err}번")
    if seconds > ck.get("max_seconds", 600):
        fails.append(f"{seconds:.0f}초(한도 {ck.get('max_seconds')})")
    for t in turns:
        if ck.get("no_false_done", True) and t["sum"]["still_running"] and DONE_WORDS.search(t["text"]):
            fails.append("시작만 한 작업을 '다 됐다'고 답함")
        if t.get("error"):
            fails.append(t["error"][:120])
    last = turns[-1]["text"] if turns else ""
    for w in ck.get("answer_has", []):
        if w not in last:
            fails.append(f"답에 '{w}' 없음")
    return fails


def set_aside(sc: dict):
    """시나리오의 setup.set_aside(작업실 기준 경로 목록)를 series/_tmp/studio_test_<시각>/ 로 옮긴다(지우지 않는다). 새 회차·새 판을 만드는
    시나리오가 지난 시험의 결과(3화·v2)가 이미 있어 매번 다른 길로 갔다(2026-10-24): 같은 출발점에서 돌게 한다."""
    stamp = time.strftime("%m%d-%H%M%S")
    # 글로브(series/*lantern*): 새 작품 시나리오는 에이전트가 고르는 작품 이름(slug)이 판마다 달랐다(deungbul-shop · lantern-shop · deungbul-store)
    paths = [p for rel in (sc.get("setup") or {}).get("set_aside", [])
             for p in (sorted(ROOT.glob(rel)) if any(ch in rel for ch in "*?[") else [ROOT / rel])]
    # set_aside_title: 작품 제목(series.json title)으로 고른다. 글로브로 쫓아도 다음 판엔 또 다른 이름(denpo-shop)이 나왔다
    titles = set((sc.get("setup") or {}).get("set_aside_title", []))
    paths += [sj.parent for sj in sorted((ROOT / "series").glob("*/series.json"))
              if titles and json.loads(sj.read_text(encoding="utf-8")).get("title") in titles and sj.parent not in paths]
    for src in paths:
        rel = src.relative_to(ROOT).as_posix()
        src = src.resolve()
        if not src.exists():
            continue
        if (ROOT / "series").resolve() not in src.parents:
            raise SystemExit(f"set_aside 는 series/ 안만: {rel}")
        dst = ROOT / "series" / "_tmp" / f"studio_test_{stamp}" / src.relative_to((ROOT / "series").resolve())
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        print(f"  옮겨 둠 {rel} → {dst.relative_to(ROOT)}", flush=True)
    # setup.copy: [[바탕, 시험용]] 시험이 고칠 판을 바탕에서 새로 떠 둔다(지난 시험이 남긴 판에 기대지 않는다)
    import shutil
    for src_rel, dst_rel in (sc.get("setup") or {}).get("copy", []):
        src, dst = (ROOT / src_rel).resolve(), (ROOT / dst_rel).resolve()
        if (ROOT / "series").resolve() not in dst.parents:
            raise SystemExit(f"copy 는 series/ 안으로만: {dst_rel}")
        # 시험용 자리에 이미 무언가 있으면(지난 시험·다른 A/B 가 남긴 판) 지우지 않고 _tmp 로 옮긴 뒤 새로 뜬다.
        # 예전엔 여기서 멈춰, 같은 v9 에 A/B 결과가 있던 날 GPU 시나리오가 시작도 못 했다(2026-10-26)
        if dst.exists():
            moved = ROOT / "series" / "_tmp" / f"studio_test_{stamp}" / dst.relative_to((ROOT / "series").resolve())
            moved.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(moved)
            print(f"  옮겨 둠 {dst_rel} → {moved.relative_to(ROOT)}", flush=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("_old", "_rejected", "_stage1", "*.log"))
        print(f"  떠 둠 {src_rel} → {dst_rel}", flush=True)


CLEAN_ROOTS = ("series", "exports")


def snapshot(sc: dict) -> set:
    """setup.cleanup 글로브에 지금 걸리는 것(시나리오 전부터 있던 것). 끝나고 이것은 지우지 않는다."""
    return {p.resolve() for g in (sc.get("setup") or {}).get("cleanup", []) for p in ROOT.glob(g)}


def cleanup(sc: dict, before: set):
    """setup.cleanup 글로브 중 이 시나리오가 새로 만든 것만 지운다(시험 사본 회차·그 내보내기·모아보기).
    시험이 남긴 사본·PDF 가 작품 폴더와 exports 에 쌓여 사용자가 '보기도 싫다'고 했다(2026-10-24). 전부터 있던 것(개항 화풍시안 등)은 건드리지 않는다."""
    import shutil
    for g in (sc.get("setup") or {}).get("cleanup", []):
        for p in ROOT.glob(g):
            rp = p.resolve()
            if rp in before or not any((ROOT / r).resolve() in rp.parents for r in CLEAN_ROOTS):
                continue
            shutil.rmtree(rp) if rp.is_dir() else rp.unlink()
            print(f"  치움 {rp.relative_to(ROOT.resolve())}", flush=True)


def series_vars(sd: Path) -> dict | None:
    """작품 하나의 시나리오 칸 값(박지 않고 그 작품 폴더에서 읽는다). 그린 인물 컷이 둘 안 되면 None.
    series·title·name(괄호 뗀 부르는 이름)·ep(가장 앞 회차)·ep_n(1화)·copy(그 회차의 안 쓰는 판 이름, 시험용 사본)·copy_v·p1·p2(그린 인물 컷)·
    char(p2 인물의 한글 이름)."""
    sys.path.insert(0, str(ROOT / "tools"))
    from mcp_smoke import title_of
    eps = sorted(p for p in (sd / "episodes").iterdir() if p.is_dir() and re.fullmatch(r"ep\d{3}", p.name) and (p / "panels.json").exists()) \
        if (sd / "episodes").is_dir() else []
    for ep in eps:
        panels = [p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))
                  if p.get("chars") and (ep / "panels_hf" / f"{p['id']}.png").exists()]
        if len(panels) < 2:
            continue
        used = [int(m.group(1)) for d in (sd / "episodes").iterdir() if (m := re.fullmatch(rf"{ep.name}_v(\d+)", d.name))]
        v = max(used + [8]) + 1  # 시험 사본은 실제 판 번호 뒤로(지운 뒤에도 같은 이름이 되풀이된다)
        p1, p2 = panels[len(panels) // 3], panels[2 * len(panels) // 3]
        cast = json.loads((sd / "cast.json").read_text(encoding="utf-8")) if (sd / "cast.json").exists() else {}
        c = p2["chars"][0]
        char = re.sub(r"\s*\(.*?\)", "", str((cast.get(c) or {}).get("ko") or c))
        title = title_of(sd) or sd.name
        # name: 사람이 부르는 짧은 이름(괄호 뗌: '개항(開肛)' → 개항, 'D+18 (힉스필드 판)' → D+18). MCP 가 이 이름으로 작품을 찾는지도 시험이 된다
        return {"series": sd.name, "title": title, "name": re.sub(r"\s*[\(（].*?[\)）]", "", title).strip() or title, "ep": ep.name, "ep_n": f"{int(ep.name[2:5])}화",
                "copy": f"{ep.name}_v{v}", "copy_v": f"v{v}", "p1": p1["id"], "p2": p2["id"], "char": char}
    return None


def _fill(x, vals: dict):
    if isinstance(x, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(vals.get(m.group(1), m.group(0))), x)
    if isinstance(x, list):
        return [_fill(v, vals) for v in x]
    if isinstance(x, dict):
        return {k: _fill(v, vals) for k, v in x.items()}
    return x


def expand(scenarios: list, only: set) -> list:
    """each_series: true 인 시나리오는 작품마다 하나씩(이름 '<이름>@<작품 폴더>'), 칸({title}·{p1} …)은 series_vars 로 채운다.
    시나리오가 개항 문장으로 박혀 있어 다른 작품에선 같은 시험을 못 돌렸다(2026-10-24)."""
    out = []
    for sc in scenarios:
        if not sc.get("each_series"):
            out.append(sc)
            continue
        for sd in sorted(p for p in (ROOT / "series").iterdir() if p.is_dir() and not p.name.startswith("_")):
            if only and sd.name not in only:
                continue
            vals = series_vars(sd)
            if vals:
                out.append({**_fill({k: v for k, v in sc.items() if k != "each_series"}, vals), "name": f"{sc['name']}@{sd.name}"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="시나리오 이름. 작품마다 펼치는 시나리오는 '<이름>' 이면 모든 작품, '<이름>@<작품 폴더>' 면 그 작품만")
    ap.add_argument("--provider", default="")
    ap.add_argument("--file", default=str(ROOT / "tests" / "studio_scenarios.json"))
    ap.add_argument("--gpu", action="store_true", help="GPU 를 쓰는 시나리오(gpu: true)도 돌린다(파드 시간 = 돈)")
    ap.add_argument("--series", default="", help="작품마다 펼치는 시나리오를 이 작품 폴더들로만(쉼표)")
    ap.add_argument("--list", action="store_true", help="돌리지 않고 고른 시나리오의 펼친 말·사본만 보인다(돈 0)")
    a = ap.parse_args()
    scenarios = expand(json.loads(Path(a.file).read_text(encoding="utf-8")), {s.strip() for s in a.series.split(",") if s.strip()})
    a.names = [n for n in a.names for n in ([s["name"] for s in scenarios if s["name"] == n or s["name"].startswith(n + "@")] or [n])]
    # manual: 한 번 생긴 상태(걸린 컷 등)에 기대는 시나리오는 이름으로 부를 때만
    pick = [s for s in scenarios if (s["name"] in a.names) if a.names] or [s for s in scenarios if not a.names and not s.get("manual") and (a.gpu or not s.get("gpu"))]
    if a.list:
        for s in pick:
            print(f"- {s['name']}{' (GPU)' if s.get('gpu') else ''}: {s['prompt']}")
            for src, dst in (s.get("setup") or {}).get("copy", []):
                print(f"    사본 {src} → {dst}")
        return
    report =[f"# 스튜디오 시험 {time.strftime('%Y-%m-%d %H:%M')} (공급자 {a.provider or '기본'})", ""]
    passed = 0
    for sc in pick:
        before = snapshot(sc)
        set_aside(sc)
        t0, turns, conv = time.time(), [], ""
        for turn in [sc["prompt"]] + list(sc.get("then") or []):
            # 이어 할 말은 글이나 {"say": 글, "wait_jobs_min": 분}: 앞 차례가 띄운 작업이 끝날 때까지 기다렸다가 말한다
            text, wait_min = (turn, 0) if isinstance(turn, str) else (turn["say"], float(turn.get("wait_jobs_min", 0)))
            if wait_min:
                wait_jobs(wait_min)
            d = ask(text, sc.get("provider") or a.provider, conv)
            conv = d.get("conversation_id") or conv
            turns.append({"text": d.get("text", ""), "sum": summarize(d), "error": d.get("error", "")})
        secs = time.time() - t0
        fails = judge(sc, turns, secs)
        wait_jobs(float((sc.get("setup") or {}).get("cleanup_wait_min", 20)) if (sc.get("setup") or {}).get("cleanup") else 0)
        cleanup(sc, before)
        passed += not fails
        tin, tout = sum(t["sum"]["tokens"][0] for t in turns), sum(t["sum"]["tokens"][1] for t in turns)
        head = (f"## {'통과' if not fails else '실패'} · {sc['name']} · {secs:.0f}초 · 도구 {sum(len(t['sum']['calls']) for t in turns)}번"
                f" · 토큰 입력 {tin:,} 출력 {tout:,}")
        print(head + ("" if not fails else " · " + "; ".join(fails)), flush=True)
        report += [head] + [f"- 실패: {f}" for f in fails] + [""]
        for k, t in enumerate(turns, 1):
            report += [f"### 차례 {k}"] + [f"- `{n}` {args}" for n, args in t["sum"]["calls"]] + [f"- 오류: {e}" for e in t["sum"]["errors"]]
            report += ["", "> " + t["text"][:600].replace("\n", "\n> "), ""]
    report.insert(1, f"{passed}/{len(pick)} 통과")
    out = ROOT / "state" / "logs" / f"studio_test_{time.strftime("%Y%m%d-%H%M%S")}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"{passed}/{len(pick)} 통과 → {out}")
    sys.exit(0 if passed == len(pick) else 1)


if __name__ == "__main__":
    main()
