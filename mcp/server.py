"""이미지 스튜디오 작업실 MCP 서버(id 는 webtoon-studio 그대로). agent app(agent platform 데스크톱)의 MCP 로 붙여 agent platform 에이전트(Qwen 등)가 작업실을 다루게 한다.

    D:\\webtoon-studio\\.venv\\Scripts\\python.exe D:\\webtoon-studio\\mcp\\server.py                    stdio(이 PC 의 agent app 가 직접 띄움)
    D:\\webtoon-studio\\.venv\\Scripts\\python.exe D:\\webtoon-studio\\mcp\\server.py --http [--port 7440]  주소(http://127.0.0.1:7440/mcp)

주소 방식(2026-10-19 사용자 "어느 PC 에서든 김진수 agent app 계정으로"): agent app 의 MCP 목록은 PC 마다 따로라 다른 PC 의 agent app 는 이 PC 의 stdio 서버를 못 띄운다.
이 PC(24시간)가 유일한 작업실로 남고, 예약작업 WEBTOON-MCP-http(mcp/serve_http.cmd)가 --http 로 띄운 것을 Cloudflare 터널
https://webtoon.js-96.com/mcp + Access(서비스 토큰 헤더) 로 연다. WEBTOON_PUBLIC_URL 이 있으면 결과 파일을 /files/ 링크로 준다.

원칙
- 한 번 호출은 30초 안에 끝낸다(agent platform 브리지 시간 제한). 작화·합성·내보내기는 작업(job)으로 띄우고 job_status 로 본다.
- 크레딧을 쓰는 도구(draw_panels·redraw_panels)는 confirm_credits 가 예상 비용 이상이어야 돌고, 한 번에 40컷까지.
- vast_* 도구는 회사 공용 vast.ai GPU 로 크레딧 없이 그린다. 그리는 판은 vast ComfyUI 에 저장된 웹툰 워크플로우가 정한다(draw_models 로 목록·모델 확인), 작품마다 set_model, 한 번만 바꿀 땐 vast_draw 의 model. 한 번에 80컷까지, 인스턴스를 켜고 끄는 도구는 없다.
- 지우는 도구는 없다. 다시 그릴 컷의 원본은 panels_hf/_rejected/ 로 옮긴다.
- stdout 은 MCP 전송로다. 출력은 전부 반환값으로만(print 금지). 무거운 import 는 맨 위에서(Windows 워커 스레드 데드락).
"""
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # 맨 위에서 미리 읽는다(툴 안 첫 import 는 Windows 에서 멈출 수 있다)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import FileResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import vision_review  # noqa: E402  그림 검수(그림을 보는 모델에 컷 그림 + 컷 설명)
sys.path.insert(0, str(ROOT / "tools"))
import new_series as ns  # noqa: E402  새 작품 열기(설정 한 장 → 작품 폴더)
import gates  # noqa: E402  하네스 관문(콘티·컷 설명 격리 judge, 통과해야 그린다)
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv" / "Scripts" / "python.exe").exists() else sys.executable
TOOLS = ROOT / "tools"
JOBS = ROOT / "state" / "mcp_jobs"
SHEETS = ROOT / "exports" / "_sheets"  # 링크(/files/)가 나가는 곳. state 아래에 두면 경로만 줄 수 있어 에이전트가 agent app ShareFile 로 옮기려다 실패했다(2026-10-19)
# WEBTOON_QUEUE_FRONT: agent app 에서 시킨 작업은 공용 ComfyUI 큐의 앞에 넣는다(comfy_client.queue). 작업실의 큰 배치(장소 시안·한 화 그리기) 뒤에 서서
# 한 컷이 10분 걸렸고, 에이전트가 기다리다 멈췄다(2026-10-19 agent app)
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "WEBTOON_QUEUE_FRONT": "1"}
COST_PER_PANEL = 1.25  # Seedream 5.0 Pro 1.5k
MAX_PANELS = 40
SERIES_ALIAS = {"d-plus-18": "d-plus-18-hf", "d+18": "d-plus-18-hf", "dplus18": "d-plus-18-hf",
                "mudae-dwi": "mudae-dwi-hf", "무대뒤": "mudae-dwi-hf", "무대 뒤": "mudae-dwi-hf"}
DOCS = {"CLAUDE": "CLAUDE.md", "README": "README.md", "LESSONS": "LESSONS.md", "OPEN_ISSUES": "worklogs/OPEN-ISSUES.md",
        "ACTING_GUIDE": "worklogs/acting/GUIDE.md"}
SERIES_DOCS = {"README": "README.md", "SERIES": "SERIES.md", "STYLE": "STYLE.md", "ASSETS": "ASSETS.md"}
# 원격 주소(예: https://webtoon.js-96.com). 있으면 결과 파일을 링크로 준다: 다른 PC 의 agent app 에선 D:\ 경로를 못 연다
PUBLIC = os.environ.get("WEBTOON_PUBLIC_URL", "").rstrip("/")
FILE_DIRS = ("exports", "series")  # /files/ 로 내주는 곳(그 밖은 404)
FILE_TYPES = {".pdf", ".epub", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4"}

mcp = FastMCP("webtoon-studio", instructions=(
    "웹툰 하네스(이미지 스튜디오의 웹툰 템플릿). 붙이면 끝: 별도 시스템 프롬프트 없이 이 MCP 만으로 새 작품·다음 화를 만든다. "
    "일을 시작하기 전에 guide() 를 한 번 부른다(단계·관문·규칙). 작업 결과는 JSON(state·review·drawn·links)이다. "
    "콘티·컷 설명은 prepare_episode 의 관문(격리 judge)을 통과해야 make_episode 가 그린다. 그린 그림은 검수가 스스로 다시 그린다. "
    "오래 걸리는 일은 job_id 를 돌려주니 기다리지 말고 알린 뒤 답을 끝낸다. 사용자에게는 한국어로 답한다."))


# ── 내부 도우미 ───────────────────────────────────────────────
def _link(path) -> str:
    """결과 파일의 원격 링크(WEBTOON_PUBLIC_URL 이 있고 FILE_DIRS 안의 파일일 때만, 아니면 빈 문자열)."""
    if not PUBLIC:
        return ""
    try:
        rel = Path(path).resolve().relative_to(ROOT)
    except ValueError:
        return ""
    return f"{PUBLIC}/files/" + urllib.parse.quote(rel.as_posix()) if rel.parts and rel.parts[0] in FILE_DIRS else ""


@mcp.custom_route("/files/{path:path}", methods=["GET"])
async def files(request):
    """결과 파일 내려주기(주소 방식에서만 쓰인다). 터널 쪽은 Cloudflare Access 가 막고, 여기선 경로·종류만 거른다."""
    f = (ROOT / request.path_params.get("path", "")).resolve()
    if f.is_file() and f.suffix.lower() in FILE_TYPES and any(f.is_relative_to(ROOT / d) for d in FILE_DIRS):
        return FileResponse(f)
    return PlainTextResponse("없음", status_code=404)


def _series(name: str) -> Path:
    key = SERIES_ALIAS.get(name.strip().lower(), name.strip())
    if not (ROOT / "series" / key).is_dir() and (ROOT / "series" / f"{key}-hf").is_dir():  # new_series 의 slug 로도 부른다
        key = f"{key}-hf"
    p = ROOT / "series" / key
    if not (p.is_dir() and (p / "episodes").is_dir()) or key.startswith("_"):
        known = sorted(d.name for d in (ROOT / "series").iterdir() if d.is_dir() and not d.name.startswith("_"))
        raise ValueError(_fail("unknown_series", given=name, candidates=_near(name, known) or known, series=known))
    return p


def _episode(series: str, episode: str) -> Path:
    m = re.fullmatch(r"\s*(?:ep)?\s*0*(\d+)\s*(?:화)?\s*", episode or "", re.I)  # "1화"·"ep2"·"2" 도 받는다(agent app 에이전트가 이렇게 불렀다)
    episode = f"ep{int(m.group(1)):03d}" if m else (episode or "").strip()
    sd = _series(series)
    p = sd / "episodes" / episode
    if not re.fullmatch(r"ep\d{3}(_v\d+)?(_vast)?", episode) or not p.is_dir():
        have = sorted(d.name for d in (sd / "episodes").iterdir() if d.is_dir())
        raise ValueError(_fail("unknown_episode", given=episode, series=sd.name, candidates=_near(episode, have) or have, episodes=have))
    return p


def _json(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _run(cmd, timeout=25) -> str:
    # stdin 은 꼭 막는다: 자식이 MCP 전송로(stdin)를 물려받으면 기다리며 멈추거나 MCP 메시지를 가로챈다(힉스필드 CLI 가 20초 멈췄다)
    r = subprocess.run(cmd, cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    return out[-4000:] if out else f"(출력 없음, 종료 코드 {r.returncode})"


def _credits() -> float | None:
    try:
        out = _run([PY, "-c", "import sys; sys.path.insert(0, r'%s'); import hf_gen; print(hf_gen.credits())" % TOOLS], timeout=20)
        return float(re.findall(r"[\d.]+", out)[-1])
    except Exception:
        return None


def _panels_json(ep: Path) -> list:
    return _json(ep / "panels.json", []) or []


def _alive(pid) -> bool:
    """프로세스가 살아 있나. Windows 에서 os.kill(pid, 0) 은 '살아 있나 보기'가 아니라 TerminateProcess(그 작업을 죽임)이거나 오류라
    (2026-10-18 시험: 도는 작업이 있어도 못 알아채고 두 번째 작업을 띄움) 프로세스 종료 코드로 본다."""
    if not pid:
        return False
    if os.name == "nt":
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _running_job(ep: Path):
    for st in JOBS.glob("*.status.json"):
        s = _json(st, {})
        spec = _json(st.with_name(st.name.replace(".status.json", ".json")), {})
        if s.get("state") == "running" and spec.get("episode_dir") == str(ep) and _alive(s.get("pid")):
            return st.name.replace(".status.json", "")
    return None


class JobBusy(Exception):
    """그 회차(작품)에 아직 도는 작업이 있음. 메시지는 JSON(도는 작업 번호·상태): 에이전트가 그 번호로 job_wait 한다."""


def _ensure_idle(ep: Path):
    """도는 작업이 있으면 JobBusy. 작업을 띄우는 도구가 파일을 옮기기 전에 부르고, _start_job 도 마지막에 한 번 더 본다.
    도구마다 다른 문장('도는 작업이 있다. 끝난 뒤 부른다')만 돌려줬더니 에이전트가 기다릴 번호를 몰라 같은 호출을 몇 초 간격으로 되풀이했다(2026-10-19 agent app)."""
    busy = _running_job(ep)
    if busy:
        raise JobBusy(json.dumps({"error": "busy", "running_job": busy, "running": json.loads(job_status(busy, tail_lines=5))}, ensure_ascii=False))


class RepeatedJob(Exception):
    """같은 입력으로 같은 작업을 짧은 간격에 다시 부름. 메시지는 JSON(이전 작업 번호·상태) — MCP 가 도구 오류로 돌려준다."""


REPEAT_MIN = float(os.environ.get("WEBTOON_REPEAT_MIN", "30"))


def _fingerprint(kind: str, ep: Path, steps: list) -> str:
    """작업의 지문 = 종류 + 명령 + 그 작업이 읽는 입력 파일(콘티·컷 설명) 내용. 입력이 바뀌면(컷 설명 수정·시드 변경) 다른 작업이다."""
    import hashlib
    h = hashlib.sha1(json.dumps([kind, [s.get("cmd") for s in steps]], ensure_ascii=False, default=str).encode())
    for name in ("make_storyboard.py", "build_panels.py", "storyboard.json", "panels.json", "panels_spec.json"):
        if (ep / name).exists():
            h.update((ep / name).read_bytes())
    return h.hexdigest()[:16]


def _start_job(kind: str, ep: Path, steps: list, note: str = "") -> str:
    """작업을 띄운다(모든 작업 도구가 여기를 거친다). 같은 지문의 작업을 REPEAT_MIN 분 안에 다시 부르면 띄우지 않고 RepeatedJob:
    에이전트가 '사람 눈으로 확인'을 실패로 읽고 같은 컷만 열 번 다시 그렸다(2026-10-19 agent app, D+18 2화 p022). 도구마다 막지 않고 여기 한 곳에서."""
    JOBS.mkdir(parents=True, exist_ok=True)
    _ensure_idle(ep)
    fp = _fingerprint(kind, ep, steps)
    since = time.time() - REPEAT_MIN * 60
    for f in sorted(JOBS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.name.endswith(".status.json") or f.stat().st_mtime < since:
            continue
        old = _json(f, {})
        if old.get("fingerprint") == fp:
            raise RepeatedJob(json.dumps({"error": "repeated_job", "previous_job": old.get("id"),
                                          "previous": json.loads(job_status(old.get("id"), tail_lines=5))}, ensure_ascii=False))
    job_id = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    spec = {"id": job_id, "kind": kind, "episode_dir": str(ep), "note": note, "steps": steps, "fingerprint": fp}
    f = JOBS / f"{job_id}.json"
    f.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([PY, str(ROOT / "mcp" / "job_runner.py"), str(f)], cwd=ROOT, env=ENV, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return job_id


_ATTEMPTS: dict = {}


def _attempt(kind: str, given) -> int:
    """같은 실패(종류+받은 값)를 15분 안에 몇 번째 보는지. 오류 JSON 의 attempt 로 실어, 에이전트가 같은 호출을 되풀이하는 중임을 스스로 알게 한다."""
    key = f"{kind}|{json.dumps(given, ensure_ascii=False, sort_keys=True, default=str)}"
    now = time.time()
    hist = [x for x in _ATTEMPTS.get(key, []) if now - x < 900] + [now]
    _ATTEMPTS[key] = hist
    return len(hist)


def _fail(error: str, **data) -> str:
    """구조화된 오류 JSON(도구 오류로 올릴 때 ValueError(_fail(...)))."""
    return json.dumps({"error": error, **data, "attempt": _attempt(error, data)}, ensure_ascii=False)


def _near(given: str, pool: list, n: int = 5) -> list:
    """받은 이름과 가까운 실제 이름(같은 숫자·앞부분 일치·글자 포함 순)."""
    g = (given or "").strip().lower()
    num = re.sub(r"\D", "", g)
    score = []
    for x in pool:
        xl = x.lower()
        s = (3 if num and re.sub(r"\D", "", xl) == num else 0) + (2 if xl.startswith(g) or g.startswith(xl) else 0) + (1 if g and (g in xl or xl in g) else 0)
        if num and s == 0:
            d = abs(int(re.sub(r"\D", "", xl) or 0) - int(num)) if re.sub(r"\D", "", xl).isdigit() and num.isdigit() else 999
            s = 0.5 if d <= 2 else 0
        if s:
            score.append((-s, x))
    return [x for _, x in sorted(score)][:n]


def _pid_key(x: str):
    """컷 번호의 숫자·꼬리 글자 (22, "") — p22 · P022 · 22 · 22p · p10b 를 같은 방식으로 본다."""
    s = (x or "").strip().lower()
    m = re.fullmatch(r"p?0*(\d+)([a-z]*)", s)
    if m and not (m.group(2) == "p" and not s.startswith("p")):
        return int(m.group(1)), m.group(2)
    m = re.fullmatch(r"0*(\d+)\s*p", s)  # "22p"(사용자가 컷 번호를 이렇게 부른다)
    return (int(m.group(1)), "") if m else None


def _catalog(ep: Path) -> dict:
    """회차의 컷 전부 {번호: {status, board, spec, reuse}}. status: new(컷 설명이 있어 그린다) · composite(합성 재료) · reuse(앞 화·다른 판 그림을 그대로) ·
    no_description(콘티엔 있는데 컷 설명이 없다)."""
    board = (_json(ep / "storyboard.json", {}) or {}).get("panels", [])
    draw = {x["id"]: x for x in _panels_json(ep)}
    reuse = (_json(ep / "panels_spec.json", {}) or {}).get("reuse", {})
    out = {}
    for b in board:
        i = b["id"]
        if i in draw:
            st = "new"
        elif b.get("composite"):
            st = "composite"
        elif i in reuse or (ep / "panels" / f"{i}.png").exists():
            st = "reuse"
        else:
            st = "no_description"
        out[i] = {"status": st, "board": b, "spec": draw.get(i), "reuse": reuse.get(i)}
    return out


def _pids(ep: Path, text, pool: str = "draw") -> list:
    """컷 번호 입력을 실제 번호로 푼다(p22·P22·22·22p·["p022"] 모두 p022). pool="draw": 컷 설명이 있어 그릴 수 있는 컷만, "any": 콘티의 컷 전부.
    풀리지 않으면 ValueError(JSON): 못 푼 값마다 reason(unknown · reuse · composite · no_description)과 가까운 실제 번호(candidates), 그 컷이 재사용이면 그림 출처."""
    raw = _ids(text)
    if not raw:
        return []
    cat = _catalog(ep)
    byk = {}
    for i in cat:
        byk.setdefault(_pid_key(i), []).append(i)
    out, bad = [], []
    for g in raw:
        i = g if g in cat else next(iter(byk.get(_pid_key(g)) or []), None) if len(byk.get(_pid_key(g)) or []) == 1 else None
        if i is None:
            bad.append({"given": g, "reason": "unknown", "candidates": _near(g, list(cat))})
        elif pool == "draw" and cat[i]["status"] != "new":
            bad.append({"given": g, "panel": i, "reason": cat[i]["status"], **({"reuse_source": cat[i]["reuse"]} if cat[i]["reuse"] else {})})
        else:
            out.append(i)
    if bad:
        raise ValueError(_fail("unknown_panels" if any(b["reason"] == "unknown" for b in bad) else "not_drawable", episode=ep.name, problems=bad,
                               drawable=sum(1 for v in cat.values() if v["status"] == "new")))
    return out


def _ids(text) -> list:
    """컷 번호 목록. 쉼표·빈칸으로 나눈 글, JSON 배열 글('["p022"]'), 목록 무엇이든 받는다. "30" 처럼 숫자만 주면 p030.
    (agent app 가 JSON 배열 글로 넘겨 '["p022"]' 를 번호로 읽고 '없는 번호'로 거절했다, 2026-10-19)"""
    items = text if isinstance(text, (list, tuple)) else re.split(r"[,\s\[\]\"']+", str(text or ""))
    return [f"p{int(x):03d}" if x.isdigit() else x for x in (str(i).strip() for i in items) if x]


# ── 읽기 도구 ─────────────────────────────────────────────────
@mcp.tool()
def guide() -> str:
    """웹툰 하네스 안내: 단계(작품 열기 → 콘티 → 컷 설명 → 그리기 → 이어짐 → 합성·출판), 단계마다 통과해야 하는 관문, 돈·장비·내용 규칙.
    어떤 에이전트든 일을 시작하기 전에 꼭 한 번 부른다(이 MCP 는 따로 시스템 프롬프트가 필요 없다)."""
    return (ROOT / "mcp" / "HARNESS.md").read_text(encoding="utf-8")


@mcp.tool()
def read_doc(doc: str, series: str = "", offset: int = 0, max_chars: int = 12000) -> str:
    """작업실 문서를 읽는다. doc: CLAUDE · README · LESSONS · OPEN_ISSUES · ACTING_GUIDE(작업실 공통),
    또는 series 를 주면 README · SERIES · STYLE · ASSETS(그 작품). 긴 문서는 offset 으로 이어 읽는다."""
    d = doc.strip().upper()
    if series:
        if d not in SERIES_DOCS:
            return f"작품 문서는 {', '.join(SERIES_DOCS)} 중 하나"
        p = _series(series) / SERIES_DOCS[d]
    else:
        if d not in DOCS:
            return f"문서는 {', '.join(DOCS)} 중 하나(작품 문서는 series 를 같이 준다)"
        p = ROOT / DOCS[d]
    t = p.read_text(encoding="utf-8")
    part = t[offset:offset + max_chars]
    more = f"\n\n(… 전체 {len(t)}자 중 {offset}~{offset + len(part)}. 이어 읽으려면 offset={offset + len(part)})" if offset + len(part) < len(t) else ""
    return part + more


@mcp.tool()
def read_skill(name: str = "") -> str:
    """이미지 스튜디오 스킬(작업 노하우 설명서 skills/<이름>/SKILL.md)을 읽는다. 비우면 스킬 목록과 웹툰 템플릿.
    스킬: storyboard(콘티) · panel-spec(컷 설명) · continuity(이어 그리기) · asset(에셋) · staging-review(연출 검수) · lettering-publish(식자·출판).
    그 일을 시작하기 전에 해당 스킬을 읽고 순서·규칙대로 한다."""
    sk = ROOT / "skills"
    n = (name or "").strip().lower()
    if not n:
        return (sk / "README.md").read_text(encoding="utf-8") + "\n\n---\n\n" + (sk / "webtoon" / "TEMPLATE.md").read_text(encoding="utf-8")
    if n in ("webtoon", "template", "웹툰"):
        return (sk / "webtoon" / "TEMPLATE.md").read_text(encoding="utf-8")
    f = sk / n / "SKILL.md"
    if not re.fullmatch(r"[a-z-]+", n) or not f.exists():
        have = sorted(d.name for d in sk.iterdir() if (d / "SKILL.md").exists())
        return f"스킬이 없다: {name}. 있는 스킬: {', '.join(have)}"
    return f.read_text(encoding="utf-8")


@mcp.tool()
def status() -> str:
    """작품별 회차 목록(제목·컷 수·그린 컷·합성 여부), 진행 중인 작업, 남은 힉스필드 크레딧."""
    lines = []
    for s in sorted(d for d in (ROOT / "series").iterdir() if d.is_dir() and not d.name.startswith("_")):
        lines.append(f"# {s.name}")
        for ep in sorted(d for d in (s / "episodes").iterdir() if d.is_dir()):
            b = _json(ep / "storyboard.json", {}) or {}
            panels = b.get("panels", [])
            pj = _panels_json(ep)
            drawn = sum(1 for p in pj if (ep / "panels_hf" / f"{p['id']}.png").exists())
            pdf = next(ep.glob("out/*.pdf"), None)
            lines.append(f"- {ep.name}: {b.get('title_card', '')} | 컷 {len(panels)} | 새로 그릴 컷 {drawn}/{len(pj)} 그림 | "
                         f"합성 {'있음 ' + pdf.name if pdf else '없음'}")
    running = [st.name.replace('.status.json', '') for st in JOBS.glob('*.status.json') if (_json(st, {}) or {}).get('state') == 'running']
    c = _credits()
    lines.append(f"\n진행 중인 작업: {', '.join(running) or '없음'}")
    lines.append(f"남은 크레딧: {c if c is not None else '확인 실패'}")
    lines.append("정본·다음 할 일은 read_doc('OPEN_ISSUES') 와 각 작품 README.")
    return "\n".join(lines)


@mcp.tool()
def credits() -> str:
    """힉스필드 남은 크레딧(개인 결제 계정)."""
    c = _credits()
    return f"남은 크레딧 {c}" if c is not None else "크레딧 확인 실패(힉스필드 CLI 로그인·위치를 확인)"


@mcp.tool()
def list_panels(series: str, episode: str, only_missing: bool = False) -> str:
    """회차의 컷 목록: 번호·장면·인물·첫 대사, 그 컷 그림이 있는지(새로 그리는 컷은 panels_hf, 합성·재사용 컷은 panels)."""
    ep = _episode(series, episode)
    b = _json(ep / "storyboard.json", {}) or {}
    new_ids = {p["id"] for p in _panels_json(ep)}
    out = []
    for p in b.get("panels", []):
        pid = p["id"]
        kind = "합성" if p.get("composite") else ("새로" if pid in new_ids else "재사용")
        has = (ep / "panels_hf" / f"{pid}.png").exists() if kind == "새로" else (ep / "panels" / f"{pid}.png").exists()
        if only_missing and has:
            continue
        line = next((d.get("text", "") for d in p.get("dialogue", [])), "")
        out.append(f"{pid} [{kind}{'' if has else ', 없음'}] {p.get('scene', '')} {','.join(p.get('characters', []))} | {line[:40]}")
    if not out:
        return "빠진 컷 없음(전부 그림이 있다)" if only_missing and b.get("panels") else "컷이 없다(storyboard.json 확인)"
    return "\n".join(out)


def _folder(folder: str) -> str:
    """그림 폴더: 비우면 힉스필드 본편(panels_hf), "vast" 면 vast 판(panels_hf_vast)."""
    f = (folder or "").strip().lower()
    if f in ("", "hf", "panels_hf"):
        return "panels_hf"
    if f in ("vast", "panels_hf_vast"):
        return "panels_hf_vast"
    raise ValueError('folder 는 비우거나(힉스필드 본편) "vast"')


def _ep_dirs(series: str = ""):
    sers = [_series(series)] if series else sorted(d for d in (ROOT / "series").iterdir() if d.is_dir() and (d / "episodes").is_dir() and not d.name.startswith("_"))
    for s in sers:
        for e in sorted(d for d in (s / "episodes").iterdir() if d.is_dir() and (d / "storyboard.json").exists()):
            yield s, e


def _images_of(ep: Path, pid: str) -> dict:
    out = {}
    for key, rel in (("final", f"panels/{pid}.png"), ("hf", f"panels_hf/{pid}.png"), ("vast", f"panels_hf_vast/{pid}.png")):
        f = ep / rel
        if f.exists():
            out[key] = _link(f) or str(f)
    return out


@mcp.tool()
def find(query: str, series: str = "", limit: int = 12) -> str:
    """찾기: 컷 번호('p22'·'22p'·'p022'), 인물·장소 이름, 대사·행동 글의 낱말로 작품·회차·컷을 찾아 정확한 식별자와 상태를 준다. 파일을 읽어 짐작하지 말고 이걸로 먼저 찾는다.
    컷 번호를 주면 그 번호가 있는 모든 회차(vast 판 복사본 포함)와 컷마다 status(new = 컷 설명이 있어 그린다 · reuse = 앞 화·다른 판 그림을 그대로 · composite = 합성 재료 ·
    no_description)와 그림이 어디 있는지(images)를 준다. 글을 주면 낱말이 모두 든 컷(대사·행동·연기·장소·인물 이름)을 준다. series 로 좁힌다."""
    q = (query or "").strip()
    if not q:
        return _fail("empty_query")
    key = _pid_key(q) if len(q) <= 6 else None
    toks = [x for x in re.split(r"\s+", q.lower()) if x]
    hits, cast_hits = [], []
    for s, e in _ep_dirs(series):
        cat = _catalog(e)
        cast = _json(s / "cast.json", {}) or {}
        names = {c: f"{v.get('ko', '')} {v.get('en', '')}".lower() for c, v in cast.items() if isinstance(v, dict)}
        for pid, c in cat.items():
            b, sp = c["board"], c["spec"] or {}
            if key is not None:
                ok = _pid_key(pid) == key
            else:
                hay = " ".join([pid, str(b.get("scene", "")), " ".join(d.get("text", "") for d in b.get("dialogue", [])),
                                " ".join(f"{x} {names.get(x, '')}" for x in b.get("characters", [])),
                                str(sp.get("action", "")), str(sp.get("acting", "")), str(sp.get("location", ""))]).lower()
                ok = all(t in hay for t in toks)
            if ok:
                dl = [d.get("text", "") for d in b.get("dialogue", []) if d.get("text")]
                hits.append({"series": s.name, "episode": e.name, "panel": pid, "status": c["status"], "shot": b.get("shot"), "scene": b.get("scene"),
                             "characters": b.get("characters", []), "text": (dl[0] if dl else "")[:80], "images": _images_of(e, pid)})
    if key is None:
        for s in ([_series(series)] if series else sorted(d for d in (ROOT / "series").iterdir() if d.is_dir() and (d / "series.json").exists() and not d.name.startswith("_"))):
            cast = _json(s / "cast.json", {}) or {}
            for c, v in cast.items():
                if isinstance(v, dict) and all(t in f"{c} {v.get('ko', '')} {v.get('en', '')}".lower() for t in toks):
                    cast_hits.append({"series": s.name, "character": c, "ko": v.get("ko"), "role": v.get("role")})
    n = max(1, min(int(limit), 40))
    out = {"query": q, "total": len(hits), "hits": hits[:n]}
    if cast_hits:
        out["characters"] = cast_hits[:n]
    if not hits and not cast_hits:
        out["error"] = "nothing_found"
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def panel_info(series: str, episode: str, panel: str) -> str:
    """컷 하나의 모든 것(JSON): status(new · reuse · composite · no_description), 콘티(장면·구도·인물·대사), 컷 설명(장소·옷·행동·연기·카메라), 그림이 있는 곳(images 링크),
    재사용이면 출처, 검수에서 사람 확인으로 남은 것(review). 컷 설명 파일(build_panels.py, 22KB)을 읽어 그 컷을 찾는 대신 이걸 쓴다. 번호는 p22·22 처럼 줘도 풀어 준다."""
    ep = _episode(series, episode)
    pid = _pids(ep, panel, "any")[0]
    c = _catalog(ep)[pid]
    return json.dumps({"series": _series(series).name, "episode": ep.name, "panel": pid, "status": c["status"], "storyboard": c["board"], "description": c["spec"],
                       "reuse_source": c["reuse"], "images": _images_of(ep, pid),
                       "review": (_json(ep / "review" / "staging.json", {}) or {}).get(pid)}, ensure_ascii=False)


@mcp.tool()
def contact_sheet(series: str, episode: str, ids: str = "", scene: str = "", folder: str = "") -> list:
    """검수용 모아보기를 만들어 그림 자체와 JSON(sheet 경로·link 원격 링크)을 돌려준다(그림을 직접 보고 판단한다). ids(쉼표) 또는 scene(장소 이름)으로 고른다.
    비우면 새로 그린 컷 전부. folder="vast" 면 vast 로 그린 컷(panels_hf_vast). 사용자에게는 link 를 준다."""
    ep = _episode(series, episode)
    sub = _folder(folder)
    pj = _panels_json(ep)
    want = _pids(ep, ids, "any")
    sel = [p for p in pj if (not want or p["id"] in want) and (not scene or p.get("location") == scene)]
    if want:  # 재사용·합성 컷도 번호로 고를 수 있게
        sel_ids = [p["id"] for p in sel] + [i for i in want if i not in {p["id"] for p in sel}]
    else:
        sel_ids = [p["id"] for p in sel]
    tiles = []
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\malgunbd.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
    for pid in sel_ids[:24]:
        f = ep / sub / f"{pid}.png"
        if not f.exists() and sub == "panels_hf":  # 재사용·합성 컷은 panels 에만 있다
            f = ep / "panels" / f"{pid}.png"
        if not f.exists():
            continue
        im = Image.open(f).convert("RGB")
        im.thumbnail((420, 420))
        ImageDraw.Draw(im).text((6, 4), pid, fill="yellow", font=font, stroke_width=3, stroke_fill="black")
        tiles.append(im)
    if not tiles:
        return "고른 컷 중 그림이 있는 컷이 없다"
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    h = max(t.height for t in tiles)
    sheet = Image.new("RGB", (424 * cols, (h + 4) * rows), "gray")
    for k, t in enumerate(tiles):
        sheet.paste(t, ((k % cols) * 424, (k // cols) * (h + 4)))
    SHEETS.mkdir(parents=True, exist_ok=True)
    out = SHEETS / f"{_series(series).name}_{ep.name}_{time.strftime('%H%M%S')}.jpg"
    sheet.save(out, quality=85)
    # 그림을 결과에 같이 싣는다: 링크만 주면 링크가 잠겨 있어(Cloudflare Access) 에이전트가 그림을 못 보고 모아보기를 되풀이하다
    # agent app 반복 방지에 끊겼다(2026-10-19). agent app 는 MCP 결과의 그림을 그대로 모델에 넘긴다(agent-workflow connector_mcp injector)
    view = sheet.copy()
    view.thumbnail((1280, 1280))
    buf = io.BytesIO()
    view.save(buf, "JPEG", quality=80)
    return [json.dumps({"sheet": str(out), "link": _link(out), "panels": len(tiles)}, ensure_ascii=False), McpImage(data=buf.getvalue(), format="jpeg")]


@mcp.tool()
def file_link(paths: str, series: str = "") -> str:
    """작업실 안 그림·PDF 의 원격 링크(사용자가 브라우저로 여는 주소). paths 는 쉼표로 여러 개. 이 PC 의 파일이라 agent app 작업 공간의 도구(ShareFile·cp)로는 못 옮긴다."""
    out = {}
    for one in [x.strip() for x in re.split(r"[,\n]+", paths or "") if x.strip()]:
        try:
            f = _existing(one, series)
            out[one] = _link(f) or "(link unavailable: not under exports/ or series/, or no public URL)"
        except (NotFound, ValueError) as e:
            out[one] = json.loads(str(e)) if str(e).startswith("{") else str(e)
    return json.dumps(out, ensure_ascii=False)


SYNC_REVIEW_MAX = 3


@mcp.tool()
def review_panels(series: str, episode: str, ids: str = "", scene: str = "", folder: str = "") -> str:
    """그림 검수(크레딧 안 씀): 그림을 보는 모델(qwen3.8-27b)이 컷 그림과 그 컷 설명(인물·옷·구도·카메라·연기·대사)을 맞대어
    인원·좌우·시선·옷·표정·배경을 판정하고 '다시' 컷엔 고칠 점을 준다. ids(쉼표) 또는 scene(장소 이름)으로 고르고, 비우면 새로 그린 컷 전부.
    folder="vast" 면 vast 로 그린 컷을 본다(vast 판은 다시 그려도 크레딧이 안 드니 vast_draw 로 시드만 바꿔 다시).
    3컷 이하는 바로 답하고, 더 많으면 작업으로 돌려 job_status 로 본다. '다시' 컷은 사용자에게 보여 주고 동의를 받은 뒤 고칠 점을 build_panels.py 에 반영해 redraw_panels."""
    ep = _episode(series, episode)
    sd = _series(series)
    sub = _folder(folder)
    vision_review.DIRS[:] = [sub] if sub != "panels_hf" else ["panels_hf", "panels"]
    try:
        pick = vision_review.pick_ids(ep, ",".join(_pids(ep, ids, "any")), scene)
    except ValueError as e:
        raise
    if not pick:
        return "검수할 컷이 없다(ids·scene 을 확인)"
    if len(pick) <= SYNC_REVIEW_MAX:  # 컷마다 모델을 두 번 부른다: 8컷은 한 호출이 60초를 넘겨 agent app 가 끊었다
        res = vision_review.review(sd, ep, pick)
        bad = [r["id"] for r in res if r.get("판정") == "다시" or r.get("error")]
        unsure = [r["id"] for r in res if r.get("판정") == "애매"]
        return (f"그림 검수 {len(res)}컷(컷마다 두 번 보고 둘 다 걸린 것만 '다시'), 다시 {len(bad)}컷\n" + "\n".join(vision_review.fmt(r) for r in res)
                + f"\n\n다시 그릴 후보: {', '.join(bad) or '없음'} · 애매(한 번만 걸림, 사람 눈으로 확인): {', '.join(unsure) or '없음'}\n"
                "(모델 판정이라 틀릴 수 있다. 사용자에게 contact_sheet 경로와 함께 보여 주고 확인받는다)")
    out = ROOT / "state" / "mcp_reviews" / f"{sd.name}_{ep.name}_{time.strftime('%m%d-%H%M%S')}.md"
    job = _start_job("review", ep, [{"label": f"그림 검수 {len(pick)}컷", "cmd": [PY, str(ROOT / "mcp" / "vision_review.py"), str(sd), ep.name,
                                                                          "--ids", ",".join(pick), "--out", str(out)]
                                                                  + (["--folder", sub] if sub != "panels_hf" else [])}])
    return f"작업 시작 {job}: {len(pick)}컷 검수(6컷씩 동시, 약 {max(1, len(pick) // 6) * 5}초). job_status('{job}', tail_lines=80) 로 결과를 본다"


# ── 파일 도구(작업실 안에서만) ────────────────────────────────
TEXT_EXT = {".py", ".json", ".md", ".txt", ".html"}
BACKUPS = ROOT / "state" / "mcp_backups"


def _inside(rel: str) -> Path:
    p = (ROOT / rel.replace("\\", "/").lstrip("/")).resolve()
    if ROOT.resolve() not in p.parents and p != ROOT.resolve():
        raise ValueError("작업실(D:/webtoon-studio) 밖은 다루지 않는다")
    if any(part in {".git", ".venv", "models"} for part in p.relative_to(ROOT.resolve()).parts):
        raise ValueError(".git·.venv·models 는 다루지 않는다")
    return p


class NotFound(Exception):
    """없는 경로. 메시지는 JSON(받은 경로·비슷한 실제 경로 후보)."""


def _seg_ok(given: str, real: str) -> bool:
    return real == given or real.startswith(given)  # 작품 이름은 앞부분만 줘도 된다(d-plus-18 → d-plus-18-hf)


def _existing(rel: str, series: str = "", want_dir: bool = False) -> Path:
    """있는 파일·폴더를 찾는다. 작업실 기준 → series 폴더 기준 → 받은 경로의 마디가 순서대로 들어 있는 실제 경로(마디는 앞부분 일치 허용).
    하나로 좁혀지면 그것, 아니면 NotFound(후보 목록). 에이전트가 series/·episodes/·-hf 를 빼고 부르거나 series 를 따로 넘겨,
    '파일 없음'만 받고 경로를 바꿔 가며 수십 번 다시 불렀다(2026-10-19 agent app, build_panels.py 를 18번)."""
    norm = rel.replace("\\", "/").strip()
    root_name = ROOT.name.lower()
    low = norm.lower()
    if root_name in low:  # 'D:/webtoon-studio/series/...' 같은 절대 경로
        norm = norm[low.index(root_name) + len(root_name):]
    norm = norm.lstrip("/")
    tries = [norm]
    if series:
        try:
            tries.insert(0, (_series(series) / norm).relative_to(ROOT).as_posix())
        except ValueError:
            pass
    for r in tries:
        q = _inside(r)
        if q.exists() and q.is_dir() == want_dir:
            return q
    parts = [x for x in norm.split("/") if x and x != "."]
    if not parts:
        return _inside(".")
    name = parts[-1]
    found = []
    for f in (ROOT / "series").rglob(name if "*" not in name else "_"):
        if f.is_dir() != want_dir or any(x.startswith((".", "_")) for x in f.relative_to(ROOT).parts[:-1]):
            continue
        real = f.relative_to(ROOT).parts[:-1]
        k = 0
        for seg in real:
            if k < len(parts) - 1 and _seg_ok(parts[k], seg):
                k += 1
        if k == len(parts) - 1:
            found.append(f)
    if series:
        sp = _series(series)
        found = [f for f in found if sp in f.parents] or found
    if len(found) > 1:  # 받은 마디와 글자 차이가 가장 적은 것 하나면 그것
        cost = lambda f: len(f.relative_to(ROOT).as_posix()) - len(norm)  # noqa: E731
        best = min(cost(f) for f in found)
        top = [f for f in found if cost(f) == best]
        if len(top) == 1:
            found = top
    if len(found) == 1:
        return found[0]
    raise NotFound(json.dumps({"error": "not_found", "path": rel, "candidates": [f.relative_to(ROOT).as_posix() for f in found[:8]],
                               "root": "paths are relative to D:/webtoon-studio, e.g. series/<series>/episodes/<episode>/build_panels.py"},
                              ensure_ascii=False))


def _writable(p: Path) -> bool:
    rel = p.relative_to(ROOT.resolve()).as_posix()
    if p.suffix.lower() not in TEXT_EXT:
        return False
    if re.match(r"series/[^/_][^/]*/", rel):  # 작품 폴더의 글 파일(콘티·컷 설명·문서·프롬프트). 그림 폴더는 확장자에서 걸린다
        return not re.search(r"/(panels_hf|panels|parts|out|review)/", rel)
    return rel.startswith("worklogs/") or rel in {"LESSONS.md", "reader/index.html"}


@mcp.tool()
def list_files(path: str = "series", series: str = "") -> str:
    """작업실 안 폴더의 파일 목록(작업실 기준 경로, 예: series/d-plus-18-hf/episodes/ep002_v2. series 를 주면 그 작품 폴더 기준). 그림 폴더는 개수만 센다."""
    p = _existing(path, series, want_dir=True)
    out = []
    for c in sorted(p.iterdir()):
        if c.is_dir():
            n = sum(1 for _ in c.iterdir())
            out.append(f"[폴더] {c.name}/ ({n}개)")
        else:
            out.append(f"{c.name} ({c.stat().st_size // 1024}KB){' · 쓰기 가능' if _writable(c.resolve()) else ''}")
    return "\n".join(out[:300]) or "(비어 있음)"


@mcp.tool()
def read_file(path: str, offset: int = 0, max_chars: int = 12000, series: str = "") -> str:
    """작업실 안 글 파일 읽기(작업실 기준 경로, series 를 주면 그 작품 폴더 기준). 긴 파일은 offset 으로 이어 읽는다. 콘티(make_storyboard.py)·컷 설명(build_panels.py)·문서를 볼 때."""
    p = _existing(path, series)
    if p.suffix.lower() not in TEXT_EXT:
        return "글 파일(.py .json .md .txt .html)만 읽는다. 그림은 contact_sheet 로"
    t = p.read_text(encoding="utf-8")
    part = t[offset:offset + max_chars]
    more = f"\n\n(… 전체 {len(t)}자 중 {offset}~{offset + len(part)}. 이어 읽으려면 offset={offset + len(part)})" if offset + len(part) < len(t) else ""
    return part + more


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """작업실 글 파일 쓰기(덮어쓰기). 쓸 수 있는 곳: 작품 폴더(series/<작품>-hf/...)의 .py .json .md .txt, worklogs/, LESSONS.md, reader/index.html.
    도구(tools/)·그림·에셋·.git 은 쓸 수 없다. 덮어쓰기 전 원본은 state/mcp_backups/ 에 남긴다. 파이썬 파일은 쓴 뒤 문법을 검사한다."""
    p = _inside(path)
    if not _writable(p):
        return f"여기는 쓸 수 없다: {path}"
    if "—" in content:
        return "em dash(—) 는 쓰지 않는다(작업실 규칙). 쉼표·마침표로 바꿔 다시"
    note = ""
    if p.exists():
        bk = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / p.relative_to(ROOT.resolve())
        bk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bk)
        note = f" (원본 백업 {bk.relative_to(ROOT)})"
    if p.suffix == ".py":
        try:
            compile(content, str(p), "exec")
        except SyntaxError as e:
            return f"파이썬 문법 오류라 쓰지 않았다: {e.msg} (줄 {e.lineno})"
    if p.suffix == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            return f"JSON 오류라 쓰지 않았다: {e}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"썼다 {p.relative_to(ROOT)} ({len(content)}자){note}"


# 한 번에 약 8천 토큰까지만 쓰는 모델(agent app 의 Qwen)도 한 화 콘티(약 16KB)·컷 설명(약 22KB)을 쓸 수 있게:
# start_file → append_file 여러 번 → check_file, 고칠 땐 edit_file. 문법 검사는 다 쓴 뒤 check_file 에서(조각마다는 못 함). 2026-10-16
def _guard(path: str, content: str):
    p = _inside(path)
    if not _writable(p):
        raise ValueError(f"여기는 쓸 수 없다: {path}")
    if "—" in content:
        raise ValueError("em dash(—) 는 쓰지 않는다(작업실 규칙). 쉼표·마침표로 바꿔 다시")
    return p


def _backup(p: Path) -> str:
    if not p.exists():
        return ""
    bk = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / p.relative_to(ROOT.resolve())
    bk.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, bk)
    return f" (원본 백업 {bk.relative_to(ROOT)})"


@mcp.tool()
def start_file(path: str, content: str) -> str:
    """긴 파일을 나눠 쓰기 시작한다: 첫 조각으로 새로 쓴다(있으면 백업 뒤 덮어씀). 이어서 append_file 로 나머지 조각을 붙이고 마지막에 check_file.
    한 화 콘티·컷 설명처럼 한 번에 다 못 쓰는 파일에 쓴다. 조각은 4천 자 안팎으로."""
    try:
        p = _guard(path, content)
    except ValueError as e:
        return str(e)
    note = _backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"시작 {p.relative_to(ROOT)} ({len(content)}자){note}. append_file 로 이어 쓰고 끝나면 check_file"


@mcp.tool()
def append_file(path: str, content: str) -> str:
    """start_file 로 시작한 파일 끝에 조각을 붙인다(줄바꿈은 content 에 넣는다). 지금까지 길이를 돌려준다."""
    try:
        p = _guard(path, content)
    except ValueError as e:
        return str(e)
    if not p.exists():
        return "파일이 없다. start_file 로 먼저 시작한다"
    with p.open("a", encoding="utf-8") as f:
        f.write(content)
    return f"붙였다 {p.relative_to(ROOT)} (지금 {p.stat().st_size}바이트)"


@mcp.tool()
def edit_file(path: str, old: str, new: str, series: str = "") -> str:
    """파일에서 old 글을 new 로 바꾼다(old 는 파일에 정확히 한 번 있어야 한다). 컷 하나·대사 한 줄처럼 일부만 고칠 때. 파이썬이면 바꾼 뒤 문법 검사."""
    path = _existing(path, series).relative_to(ROOT).as_posix()
    try:
        p = _guard(path, new)
    except ValueError as e:
        return str(e)
    t = p.read_text(encoding="utf-8")
    n = t.count(old)
    if n != 1:
        return f"바꿀 글이 {n}번 나온다(정확히 한 번이어야 한다). read_file 로 확인하고 더 길게 잡는다"
    t2 = t.replace(old, new, 1)
    if p.suffix == ".py":
        try:
            compile(t2, str(p), "exec")
        except SyntaxError as e:
            return f"바꾸면 파이썬 문법 오류라 안 바꿨다: {e.msg} (줄 {e.lineno})"
    note = _backup(p)
    p.write_text(t2, encoding="utf-8")
    return f"고쳤다 {p.relative_to(ROOT)}{note}"


@mcp.tool()
def check_file(path: str, series: str = "") -> str:
    """나눠 쓴 파일을 검사한다: .py 는 파이썬 문법, .json 은 JSON. 오류면 줄 번호와 그 근처를 보여 준다(edit_file 로 고친다)."""
    p = _existing(path, series)
    t = p.read_text(encoding="utf-8")
    try:
        if p.suffix == ".py":
            compile(t, str(p), "exec")
        elif p.suffix == ".json":
            json.loads(t)
    except SyntaxError as e:
        ls = t.splitlines()
        near = "\n".join(f"{i + 1}: {ls[i]}" for i in range(max(0, (e.lineno or 1) - 3), min(len(ls), (e.lineno or 1) + 2)))
        return f"파이썬 문법 오류: {e.msg} (줄 {e.lineno})\n{near}"
    except ValueError as e:
        return f"JSON 오류: {e}"
    return f"문법 통과 {p.relative_to(ROOT)} ({len(t)}자, {t.count(chr(10)) + 1}줄)"


@mcp.tool()
def new_episode(series: str, episode: str, template: str = "") -> str:
    """새 회차 폴더를 앞 회차 틀로 연다: template 회차(기본 = 그 작품의 가장 최신 회차)의 make_storyboard.py·build_panels.py·trailer.json 을 복사한다.
    그다음 read_file 로 열어 사용자 이야기에 맞게 고쳐 write_file 로 쓰고 prepare_episode 를 돌린다. 이미 있는 회차 폴더는 건드리지 않는다."""
    s = _series(series)
    if not re.fullmatch(r"ep\d{3}(_v\d+)?", episode.strip()):
        return "회차 이름은 ep003, ep003_v2 같은 꼴이다"
    dst = s / "episodes" / episode.strip()
    if dst.exists():
        return f"이미 있다: {dst.relative_to(ROOT)}"
    eps = sorted(d.name for d in (s / "episodes").iterdir() if d.is_dir() and not d.name.endswith("_vast"))
    if not eps and not template.strip():  # 새 작품의 첫 회차: 빈 콘티 틀(제목·크레딧은 series.json 에서 읽음)
        dst.mkdir(parents=True)
        shutil.copy2(ROOT / "skills" / "storyboard" / "make_storyboard_template.py", dst / "make_storyboard.py")
        (dst / "build_panels.py").write_text(SPEC_STUB, encoding="utf-8")
        (dst / "panels_spec.json").write_text(json.dumps({"scenes": {}, "reuse": {}, "panels": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
        return (f"새 회차 {dst.relative_to(ROOT)} (첫 회차라 빈 콘티 틀). read_skill('storyboard') 를 읽고 make_storyboard.py 의 '컷' 부분을 "
                "이야기대로 쓴 뒤(start_file·append_file·check_file) prepare_episode, 컷 설명은 set_panels 로 채운다")
    tpl = template.strip() or eps[-1]
    src = s / "episodes" / tpl
    if not src.is_dir():
        return f"틀 회차가 없다: {tpl}"
    dst.mkdir(parents=True)
    copied = []
    for n in ("make_storyboard.py", "trailer.json"):
        if (src / n).exists():
            t = (src / n).read_text(encoding="utf-8").replace(f"episodes/{tpl}", f"episodes/{episode}").replace(f"episodes\\{tpl}", f"episodes\\{episode}")
            (dst / n).write_text(t, encoding="utf-8")
            copied.append(n)
    # 컷 설명은 코드(build_panels.py)를 쓰지 않고 표(panels_spec.json)를 set_panels 로 채운다(2026-10-16 agent app 시험)
    (dst / "build_panels.py").write_text(SPEC_STUB, encoding="utf-8")
    (dst / "panels_spec.json").write_text(json.dumps({"scenes": {}, "reuse": {}, "panels": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
    return (f"새 회차 {dst.relative_to(ROOT)} (틀 {tpl}): {', '.join(copied)} 복사, 컷 설명 표(panels_spec.json) 준비. "
            "make_storyboard.py 를 새 이야기로 고친 뒤(start_file·append_file·check_file) prepare_episode 로 storyboard.json 을 만들고, "
            "컷 설명은 set_panels 로 몇 컷씩 채운다(틀 회차의 컷 설명 예시는 read_file 로 그 회차 build_panels.py 를 본다)")


@mcp.tool()
def new_series(spec: str) -> str:
    """새 웹툰(작품)을 연다. spec 은 작품 설정 JSON(문자열):
    {"slug": "영어-소문자-하이픈", "title": "제목", "logline": "한 줄 소개", "genre": "장르·수위", "credit": "글: …  그림: …",
     "style": "English art style paragraph (line, coloring, light, faces, proportions)",
     "cast": {"<id>": {"ko": "이름", "en": "English Name (age)", "role": "역할", "who": "English appearance (adult: ethnicity, age, height, build, hair, eyes, face)",
                       "main": "English default outfit", "exprs": "six English expressions, comma separated"}},
     "locations": {"<name>": "English description: place, time of day, light, furniture"},
     "era": "시대(한국어, 예: 2000년대 후반 서울)", "era_rule": "English period rule for every image (phones, cars, signs of that time)",
     "people_rule": "English rule for people (ages, proportions, restrained expressions)",
     "model": "uc" | "anima"(그리는 판, 비우면 uc. anima 는 글만 받아 인물 생김새를 who 글로 맞춘다, 편집은 uc)}
    직함·호칭은 cast 의 role 에 적는다(콘티 관문이 대사의 호칭과 맞춰 본다). era_rule 은 그리기 지시와 그림 검수에 들어간다.
    폴더 series/<slug>-hf 와 설정·프롬프트를 만든다(그림은 아직). 다음: new_episode(slug, "ep001") → 콘티 → set_panels →
    make_episode(에셋 점검에서 인물 시트·장소 시안을 vast 로 그리고 멈춤, 사용자가 고르면 accept_new=True)."""
    try:
        d = json.loads(spec) if isinstance(spec, str) else spec
        made = ns.create(d)
    except SystemExit as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"실패: {type(e).__name__}: {e}"
    return (f"새 작품 {made.relative_to(ROOT)}: 인물 {len(d['cast'])}명 · 장소 {len(d.get('locations') or {})}곳 설정과 프롬프트를 만들었다. "
            f"다음: new_episode('{d['slug']}', 'ep001') 로 첫 회차를 연다")


@mcp.tool()
def add_to_series(series: str, kind: str, key: str, value: str) -> str:
    """작품에 인물이나 장소를 더한다(새 회차에 처음 나오는 사람·곳). kind = cast | location, key = 영어 id.
    cast 면 value 는 JSON {"ko","en","role","who","main","exprs"}(new_series 와 같은 칸), location 이면 영어 설명 한 문장.
    그림은 make_episode 의 에셋 점검이 시안으로 그린다. new_series 로 연 작품만(series.json 이 있는 작품)."""
    try:
        s = _series(series)
        if not (s / "series.json").exists():
            return f"{s.name} 은 작품 설정(series.json)이 없는 옛 작품이다. 그 작품의 make_sheet_prompts.py·make_location_prompts.py 에 한 줄 넣는다"
        if kind not in ("cast", "location"):
            return "kind 는 cast 또는 location"
        files = ns.add(s, kind, key, json.loads(value) if kind == "cast" else value)
    except SystemExit as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"실패: {type(e).__name__}: {e}"
    return f"{s.name} 에 {kind} {key} 를 더했다. 프롬프트 {len(files)}개. 그림은 make_episode 에셋 점검이 시안으로 그린다"


def _draw_models(refresh: bool = False) -> dict:
    sys.path.insert(0, str(VAST))
    import saved_workflow
    return saved_workflow.models(refresh=refresh)


def _check_model(name: str):
    """없는 판이면 JSON 오류(지금 있는 판 목록 포함), 모델 파일이 서버에 모자란 판이면 그 목록, 문제없으면 None."""
    m = _draw_models()
    k = (name or "").strip().lower()
    if k not in m:
        return json.dumps({"error": "unknown_model", "models": sorted(m)}, ensure_ascii=False)
    if m[k]["missing"]:
        return json.dumps({"error": "model_files_missing", "model": k, "missing": m[k]["missing"], "ready_models": sorted(x for x, vv in m.items() if not vv["missing"])},
                          ensure_ascii=False)
    return None


@mcp.tool()
def draw_models(series: str = "") -> str:
    """그릴 때 고를 수 있는 판(모델)의 목록. vast ComfyUI 에 저장된 웹툰 워크플로우(webtoon_vertical_<판>)에서 읽으니, 서버에서 워크플로우를 저장하거나 지우면
    목록이 따라 바뀐다. 판마다: label(설명) · use_for(어디에 맞는지·한계, 판을 고를 때 읽는다) · refs(참고 그림으로 얼굴·장소를 맞추나, 아니면 글로만) ·
    roles(생성·고치기·에셋·시안 역할마다 쓰는 워크플로우, own_roles 밖은 기본 판의 것) · models(쓰는 모델 파일) · ready(모델 파일이 서버에 다 있나) · default.
    series 를 주면 그 작품이 쓰는 판(current)도 알려 준다."""
    m = _draw_models(refresh=True)
    cur = None
    if series:
        sys.path.insert(0, str(VAST))
        import saved_workflow
        cur = saved_workflow.pick(_series(series))[0]
    out = [{"id": k, "label": vv["label"], "use_for": vv["use_for"], "refs": vv["refs"], "workflow": vv["gen"], "roles": vv["roles"], "own_roles": vv.get("own_roles", []),
            "models": vv["models"], "ready": not vv["missing"], "missing": vv["missing"], "default": vv["default"], **({"current": k == cur} if cur else {})}
           for k, vv in sorted(m.items())]
    return json.dumps({"draw_models": out}, ensure_ascii=False)


@mcp.tool()
def set_model(series: str, model: str) -> str:
    """작품의 그리는 판을 바꾼다(series.json model). 쓸 수 있는 판은 vast ComfyUI 에 저장된 웹툰 워크플로우가 정한다: draw_models() 가 지금 목록과 각 판의
    모델·참고 그림을 받는지·모델 파일이 서버에 다 있는지를 준다. 이미 그린 컷은 그대로다: 판을 바꾼 뒤 새로 그리는 컷부터 바뀐다(한 화 안에서 섞으면 그림체가 달라진다)."""
    err = _check_model(model)
    if err:
        return err
    m = model.strip().lower()
    d = _series(series)
    f = d / "series.json"
    spec = _json(f, {}) or {}
    old = spec.get("model") or "uc"
    spec["model"] = m
    f.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    return json.dumps({"series": d.name, "model": m, "was": old}, ensure_ascii=False)


@mcp.tool()
def redo_candidate(series: str, kind: str, name: str) -> str:
    """사용자가 마음에 안 들어 한 새 인물 시트·장소 시안을 다른 시드로 다시 그린다(크레딧 0, vast, 약 2~4분). kind = sheet | location.
    지난 시안은 지우지 않고 assets/_candidates/_rejected/ 로 옮긴다. 생김새·옷을 바꾸려면 먼저 add_to_series 로 설정을 고친다."""
    try:
        s = _series(series)
    except ValueError as e:
        return str(e)
    if kind not in ("sheet", "location"):
        return "kind 는 sheet 또는 location"
    cand = s / "assets" / "_candidates" / (f"locations/{name}.png" if kind == "location" else f"{name}_sheet.png")
    _ensure_idle(s)
    if cand.exists():
        rej = s / "assets" / "_candidates" / "_rejected"
        rej.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cand), str(rej / f"{cand.stem}_{time.strftime('%m%d%H%M%S')}.png"))
    job = _start_job("redo_candidate", s, [{"label": f"시안 다시 그리기({kind} {name})", "cmd": [PY, str(VAST / "new_assets.py"), str(s), kind, name]}])
    return f"작업 시작 {job}: 시안 {cand.relative_to(ROOT)} 를 다시 그린다(약 2~4분). job_wait('{job}')"


SPEC_STUB = '''"""컷 설명은 panels_spec.json(MCP set_panels 로 채움). 이 파일은 공용 변환기 tools/panels_from_spec.py 를 부른다."""
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.argv = ["panels_from_spec.py", str(HERE)]
runpy.run_path(str(HERE.parents[3] / "tools" / "panels_from_spec.py"), run_name="__main__")
'''


@mcp.tool()
def set_panels(series: str, episode: str, panels: str = "", scenes: str = "", reuse: str = "") -> str:
    """컷 설명 표(panels_spec.json)를 채운다. 코드를 쓰지 않고 컷 몇 개씩(5~8컷) 나눠 여러 번 불러도 된다(같은 번호는 덮어씀).
    panels: JSON 객체 {"p003": {"chars": ["jaemin"], "outfit": {"jaemin": "white dress shirt, navy slacks"}, "location": "office",
      "shot": "wide shot", "action": "영어로 이 컷에서 일어나는 일", "acting": "영어로 표정·눈썹·입·몸짓", "camera": "대화 컷이면 어깨 너머 숏 설명"}}
      chars 는 cast.json 의 인물 id, location 은 assets/locations 의 장소 이름. 인물이 있는 컷은 acting 을 꼭 쓴다.
      넓은 컷은 양옆 배치, 대화 컷은 듣는 사람 어깨 너머 숏(camera), 혼자 클로즈업은 한 사람만, 화면 좌우는 장면 내내 같게.
    scenes: JSON {"office": "그 장소의 장면 배치 한 문장(누가 화면 왼쪽·오른쪽에서 어디를 보나)"}. 같은 장소가 한 화 안에서 배치가 바뀐 뒤 또 나오면
      scenes 에 {"office_night": "..."} 를 더하고 그 컷에 "scene": "office_night" 를 적는다. reuse: JSON {"p010": "episodes/ep001/panels_hf/p012.png"}.
    끝나면 prepare_episode 로 panels.json 을 만들고 '빠진 컷'·'연기 없는 인물 컷'이 비었는지 본다."""
    ep = _episode(series, episode)
    sd = _series(series)
    f = ep / "panels_spec.json"
    spec = _json(f, {}) or {}
    spec.setdefault("scenes", {}), spec.setdefault("reuse", {}), spec.setdefault("panels", {})
    notes = []
    try:
        add_p = json.loads(panels) if panels.strip() else {}
        add_s = json.loads(scenes) if scenes.strip() else {}
        add_r = json.loads(reuse) if reuse.strip() else {}
    except ValueError as e:
        return f"JSON 오류: {e}. 큰따옴표 JSON 객체로 다시"
    if isinstance(add_p, list):  # [{"id": "p003", ...}] 도 받는다
        add_p = {x.pop("id"): x for x in add_p if isinstance(x, dict) and x.get("id")}
    cast = _json(sd / "cast.json", {}) or {}
    locs = {p.stem for p in (sd / "assets" / "locations").glob("*.png")}
    for pid, sp in add_p.items():
        if not isinstance(sp, dict):
            return f"{pid}: 컷 설명은 JSON 객체여야 한다"
        pid = _ids(pid)[0]
        if "—" in json.dumps(sp, ensure_ascii=False):
            return "em dash(—) 는 쓰지 않는다. 쉼표·마침표로 바꿔 다시"
        bad = [c for c in sp.get("chars", []) if c not in cast]
        if bad:
            return f"{pid}: cast.json 에 없는 인물 {bad}. 있는 인물: {', '.join(k for k in cast if not k.startswith('_'))}"
        if not sp.get("action"):
            return f"{pid}: action(이 컷에서 일어나는 일)이 비었다"
        if sp.get("location") and sp["location"] not in locs:
            notes.append(f"{pid}: 장소 '{sp['location']}' 그림이 아직 없다(make_episode 가 시안을 만들고 멈춘다. 있는 장소: {', '.join(sorted(locs))})")
        if sp.get("chars") and not sp.get("acting"):
            notes.append(f"{pid}: acting 이 비었다(인물 컷은 연기를 쓴다)")
        spec["panels"][pid] = sp
    spec["scenes"].update(add_s)
    spec["reuse"].update({_ids(k)[0]: v for k, v in add_r.items()})
    f.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    if (ep / "build_panels.py").read_text(encoding="utf-8") != SPEC_STUB:  # 코드로 된 build_panels 가 있으면 백업하고 표를 쓰게 바꾼다
        bk = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / (ep / "build_panels.py").relative_to(ROOT.resolve())
        bk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ep / "build_panels.py", bk)
        (ep / "build_panels.py").write_text(SPEC_STUB, encoding="utf-8")
        notes.append(f"이 회차의 build_panels.py 는 표를 쓰게 바꿨다(원본 백업 {bk.relative_to(ROOT)})")
    board = _json(ep / "storyboard.json", {}) or {}
    need = [p["id"] for p in board.get("panels", []) if not p.get("composite") and p["id"] not in spec["panels"] and p["id"] not in spec["reuse"]]
    return (f"컷 설명 {len(add_p)}개 넣음(지금 표에 {len(spec['panels'])}컷, 장면 배치 {len(spec['scenes'])}, 재사용 {len(spec['reuse'])}). "
            + (f"아직 설명이 없는 콘티 컷: {', '.join(need)}. " if board else "storyboard.json 이 아직 없다(prepare_episode 를 먼저). ")
            + ("\n" + "\n".join(notes) if notes else "")
            + ("\n다음: " + ("남은 컷 설명을 set_panels 로 채운다." if need else "prepare_episode 로 관문 채점을 받는다(통과해야 그린다).")))


# ── 준비·비용 ─────────────────────────────────────────────────
@mcp.tool()
def prepare_episode(series: str, episode: str) -> str:
    """회차의 make_storyboard.py → build_panels.py 를 돌려 storyboard.json·panels.json 을 다시 만들고, 관문(격리 judge)으로 콘티·컷 설명을 채점한다(크레딧 안 씀).
    관문 기준표는 skills/storyboard/judge.json · skills/panel-spec/judge.json(하네스 s08 judge 와 같은 꼴, 합격 0.8, 꼭 지킬 기준은 하나라도 어기면 걸림).
    긴 한 화는 몇 분 걸려서 작업으로 돈다: 40초 안에 끝나면 그 자리에서 job_status 모양의 결과(gates = 관문 통과 여부, log_tail = 걸린 곳)를 주고,
    아니면 state running 과 작업 번호를 주니 job_wait 로 이어 본다. 걸리면 결과의 '고칠 점'대로 make_storyboard.py(edit_file)나 컷 설명(set_panels)을 고치고 다시 부른다."""
    ep = _episode(series, episode)
    steps = []
    for name, label in (("make_storyboard.py", "콘티(make_storyboard.py)"), ("build_panels.py", "컷 설명(build_panels.py)")):
        if (ep / name).exists():
            steps.append({"label": label, "cmd": [PY, str(ep / name)]})
        else:
            return json.dumps({"error": "missing_file", "file": name, "hint": "new_episode 로 앞 회차를 본떠 먼저 만든다"}, ensure_ascii=False)
    steps.append({"label": "관문(콘티·컷 설명 채점)", "cmd": [PY, str(TOOLS / "gates.py"), str(ep), "all"], "allow_fail": True})
    job = _start_job("prepare", ep, steps)
    return _wait_job(job, 40, 80)


@mcp.tool()
def estimate_draw(series: str, episode: str, ids: str = "") -> str:
    """그릴 컷과 예상 크레딧. ids 를 비우면 panels.json 에서 아직 그림이 없는 컷 전부. 그리기 전에 이 결과를 사용자에게 보여 확인받는다."""
    ep = _episode(series, episode)
    pj = {p["id"]: p for p in _panels_json(ep)}
    todo = _pids(ep, ids) or [i for i in pj if not (ep / "panels_hf" / f"{i}.png").exists()]
    c = _credits()
    return (f"그릴 컷 {len(todo)}개: {', '.join(todo) or '없음'}\n예상 크레딧 {len(todo) * COST_PER_PANEL:.2f}(장당 {COST_PER_PANEL}, 검열 오탐은 무료)\n"
            f"남은 크레딧 {c}\n한 번에 {MAX_PANELS}컷까지. 기준 컷(SCENES anchor)이 먼저 그려져 있어야 한다.")


# ── 크레딧을 쓰는 도구 ────────────────────────────────────────
def _draw(series: str, episode: str, ids: list, confirm_credits: float, kind: str, note: str = "") -> str:
    ep = _episode(series, episode)
    pj = {p["id"] for p in _panels_json(ep)}
    todo = [i for i in ids if i in pj]
    if not todo:
        return "그릴 컷이 없다(panels.json 에 있는 번호인지, prepare_episode 를 돌렸는지 확인)"
    if len(todo) > MAX_PANELS:
        return f"한 번에 {MAX_PANELS}컷까지다({len(todo)}컷). 나눠서 부른다"
    cost = len(todo) * COST_PER_PANEL
    if confirm_credits < cost:
        return f"확인 값이 모자라다: 예상 {cost:.2f} 크레딧. 사용자에게 확인받은 뒤 confirm_credits={cost:.2f} 로 다시 부른다"
    c = _credits()
    if c is not None and c < cost:
        return f"크레딧이 모자라다(남은 {c}, 필요 {cost:.2f})"
    _ensure_idle(ep)
    cmd = [PY, str(TOOLS / "hf_panel.py"), str(_series(series)), str(ep / "panels.json"), "--episode", ep.name, "--n", "1", "--only", ",".join(todo)]
    job = _start_job(kind, ep, [{"label": f"{len(todo)}컷 그리기(예상 {cost:.2f} 크레딧)", "cmd": cmd},
                                {"label": "합성용으로 놓기", "cmd": [PY, str(TOOLS / "hf_place.py"), str(ep)]}], note)
    return f"작업 시작 {job}: {len(todo)}컷, 예상 {cost:.2f} 크레딧. 한 컷에 약 1분. job_status('{job}') 로 확인"


@mcp.tool()
def draw_panels(series: str, episode: str, ids: str, confirm_credits: float) -> str:
    """컷을 힉스필드로 그린다(크레딧 사용). ids: 쉼표로 컷 번호(빈 값이면 아직 없는 컷 전부).
    confirm_credits 는 estimate_draw 의 예상 크레딧 이상이어야 돈다(사용자 확인을 받은 값). 끝나면 합성용 폴더에도 놓는다."""
    ep = _episode(series, episode)
    want = _pids(ep, ids) or [p["id"] for p in _panels_json(ep) if not (ep / "panels_hf" / f"{p['id']}.png").exists()]
    return _draw(series, episode, want, confirm_credits, "draw")


@mcp.tool()
def redraw_panels(series: str, episode: str, ids: str, reason: str, confirm_credits: float) -> str:
    """검수에서 걸린 컷을 다시 그린다(크레딧 사용). 지금 그림은 panels_hf/_rejected/ 로 옮기고 새로 그린다.
    먼저 build_panels.py 의 그 컷 설명(행동·연기·카메라)을 고치고 prepare_episode 를 돌린 뒤 부른다. reason 에 걸린 이유를 적는다."""
    ep = _episode(series, episode)
    want = _pids(ep, ids)
    if not want:
        return "다시 그릴 컷 번호를 준다"
    cost = len(want) * COST_PER_PANEL
    if confirm_credits < cost:
        return f"확인 값이 모자라다: 예상 {cost:.2f} 크레딧. 사용자 확인 뒤 confirm_credits={cost:.2f}"
    _ensure_idle(ep)
    rej = ep / "panels_hf" / "_rejected"
    rej.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d%H%M")
    for i in want:
        f = ep / "panels_hf" / f"{i}.png"
        if f.exists():
            shutil.move(str(f), str(rej / f"{i}_{stamp}.png"))
    return _draw(series, episode, want, confirm_credits, "redraw", note=reason)


# ── vast.ai 공용 GPU(크레딧 0) ───────────────────────────────
# 회사 공용 vast.ai 의 ComfyUI 로 그린다(판은 saved_workflow.MODELS). 크레딧은 안 들지만 공용 장비라 한 번에 VAST_MAX 컷까지.
# 인스턴스 만들기·끄기·지우기 도구는 두지 않는다(vast/README.md).
VAST = ROOT / "vast"
VAST_MAX = 80


@mcp.tool()
def vast_status() -> str:
    """vast.ai 공용 GPU 상태: 인스턴스·남은 VRAM·서버 큐(이 작업실 것 포함)·그리는 판마다 모델 파일이 서버에 받아져 있는지. 크레딧 안 씀."""
    return _run([PY, str(VAST / "comfy_client.py"), "status"], timeout=25)


@mcp.tool()
def vast_draw(series: str, episode: str, ids: str = "", turbo: bool = True, expr: bool = True, aspect: str = "", seed: int = -1, fix: bool = True,
              check: bool = True, model: str = "") -> str:
    """컷을 vast GPU 로 그린다(크레딧 0). model: 비우면 작품의 판(set_model), 판 이름(draw_models 목록)을 주면 이 작업만 그 판으로. 같은 panels.json 을 쓰고 결과는 panels_hf_vast/ 에 쌓인다(힉스필드 본편 panels_hf 는 안 건드림).
    ids: 쉼표로 컷 번호(비우면 vast 그림이 아직 없는 컷 전부). turbo=True 면 8스텝(컷당 약 30초), False 면 40스텝(약 2분).
    expr=True 면 연기에 맞는 표정 칸을 얼굴 참고로 넣는다(연기가 세짐). aspect 를 "2:3"·"9:16" 으로 주면 세로 컷.
    fix=True(기본)면 그린 뒤 그림 보는 사내 Qwen 이 남는 사람(중복·낯선 사람)을 찾아 편집으로 지운다(원본은 panels_hf_vast/_stage1/).
    check=True(기본)면 그림 검수로 시점·좌우·시선·표정·배경을 보고 표정·옷은 편집, 구도가 틀리면 시드를 바꿔 한 번 더 그린다(컷당 +20~60초).
    그래도 이상하면 seed 만 바꿔 그 컷을 다시 부른다. 본편으로 쓰려면 사용자가 고른 컷만 vast_adopt."""
    ep = _episode(series, episode)
    sd = _series(series)
    pj = [p["id"] for p in _panels_json(ep)]
    want = _pids(ep, ids) or [i for i in pj if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    if not want:
        return "그릴 컷이 없다(vast 그림이 다 있다. 다시 그리려면 ids 를 준다)"
    if len(want) > VAST_MAX:
        return f"공용 GPU 라 한 번에 {VAST_MAX}컷까지다({len(want)}컷). 나눠서 부른다"
    if aspect and aspect not in ("9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9"):
        return "aspect 는 9:16·2:3·3:4·1:1·4:3·3:2·16:9 중 하나"
    if not (sd / "assets" / "vast_refs").is_dir():
        return "이 작품의 vast 참고 그림이 없다. 먼저 vast_make_assets(series) 를 부른다"
    m = (model or "").strip().lower()
    if m:
        err = _check_model(m)
        if err:
            return err
    _ensure_idle(ep)
    cmd = [PY, str(VAST / "comfy_panel.py"), str(sd), str(ep / "panels.json"), "--episode", ep.name, "--only", ",".join(want), "--variant", "vast"]
    cmd += ["--model", m] if m else []
    cmd += ["--turbo"] if turbo else []
    cmd += ["--expr"] if expr else []
    cmd += ["--aspect", aspect] if aspect else []
    cmd += ["--seed", str(seed)] if seed >= 0 else []
    cmd += ["--fix"] if fix else []
    cmd += ["--check"] if check else []
    job = _start_job("vast_draw", ep, [{"label": f"vast {len(want)}컷 그리기(크레딧 0)", "cmd": cmd}])
    per = (30 if turbo else 120) + (40 if check else 0)
    return (f"작업 시작 {job}: {len(want)}컷, 크레딧 0, 약 {max(1, len(want) * per // 60)}분(공용 큐가 붐비면 더). "
            f"job_status('{job}') → 끝나면 contact_sheet(series, episode, folder='vast')")


@mcp.tool()
def vast_make_assets(series: str, character: str = "all", what: str = "both") -> str:
    """인물 에셋을 vast 로 만든다(크레딧 0): 시트에서 참고 그림 자르기 → 회색 기본복 베이스 몸·센 표정 12가지(오열·고함·경멸 등).
    character: 인물 id(cast.json) 또는 all. what: base · expr · both(센 표정) · subtle(잔잔한 표정 20가지, 차분한 연기용. series.json expressions 로 바꿀 수 있다).
    이미 있는 그림은 건너뛴다. make_episode 가 새 화를 만들 때 필요한 것을 알아서 만드니, 따로 부르는 건 에셋만 다시 만들거나 보탤 때다."""
    sd = _series(series)
    if what not in ("base", "expr", "both", "subtle"):
        return json.dumps({"error": "bad_what", "given": what, "allowed": ["base", "expr", "both", "subtle"]}, ensure_ascii=False)
    if character != "all" and not (sd / "assets" / f"{character}_sheet.png").exists():
        return f"{character}_sheet.png 가 없다(인물 시트를 먼저 만든다). 있는 인물: " + ", ".join(
            sorted(p.name[:-len('_sheet.png')] for p in (sd / "assets").glob("*_sheet.png")))
    _ensure_idle(sd)
    steps = [{"label": "시트에서 참고 그림 자르기", "cmd": [PY, str(VAST / "sheet_refs.py"), str(sd)]},
             {"label": f"에셋 만들기({character}, {what})", "cmd": [PY, str(VAST / "make_assets.py"), str(sd), character, what, "--res", "768"]}]
    job = _start_job("vast_assets", sd, steps)
    return f"작업 시작 {job}: 크레딧 0, 장당 약 3~4초(인물 하나 13장). job_status('{job}')"


@mcp.tool()
def vast_adopt(series: str, episode: str, ids: str, reason: str = "") -> str:
    """vast 로 그린 컷을 본편으로 쓴다: 지금 본편 그림(panels_hf)은 panels_hf/_rejected/ 로 옮기고 vast 그림을 그 자리에 복사한 뒤 합성용으로 놓는다.
    사용자가 고른 컷만 넘긴다(본편이 바뀐다). 지우는 것은 없다. 끝나면 compose 로 다시 합성."""
    ep = _episode(series, episode)
    want = _pids(ep, ids, "any")
    if not want:
        return "채택할 컷 번호를 준다"
    miss = [i for i in want if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    if miss:
        return f"vast 그림이 없는 컷: {', '.join(miss)}(vast_draw 로 먼저 그린다)"
    _ensure_idle(ep)
    rej = ep / "panels_hf" / "_rejected"
    rej.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d%H%M")
    for i in want:
        cur = ep / "panels_hf" / f"{i}.png"
        if cur.exists():
            shutil.move(str(cur), str(rej / f"{i}_{stamp}_before_vast.png"))
        shutil.copy2(ep / "panels_hf_vast" / f"{i}.png", cur)
    job = _start_job("vast_adopt", ep, [{"label": "합성용으로 놓기", "cmd": [PY, str(TOOLS / "hf_place.py"), str(ep)]}], reason)
    return f"{len(want)}컷을 vast 그림으로 바꿨다(원본은 panels_hf/_rejected/*_before_vast.png). 놓기 작업 {job} → 끝나면 compose"


VAST_COPY = ("import shutil,sys; from pathlib import Path; s=Path(sys.argv[1]); d=Path(sys.argv[2]); ids=sys.argv[3].split(',')\n"
             "(d/'panels_hf').mkdir(parents=True,exist_ok=True); n=0\n"
             "for i in ids:\n"
             "    f=s/'panels_hf_vast'/(i+'.png')\n"
             "    if f.exists(): shutil.copy2(f, d/'panels_hf'/(i+'.png')); n+=1\n"
             "    else: print('vast 그림 없음', i)\n"
             "print('vast 컷', n, '/', len(ids), '장을 vast 판 회차로 복사')")


@mcp.tool()
def make_episode(series: str, episode: str, accept_new: bool = False, until: str = "export", check: bool = True, skip_gates: bool = False) -> str:
    """한 화를 끝까지 한 번에(어느 작품이든, 크레딧 0): 콘티 준비 → 에셋 점검 → vast 그리기(표정 칸·남는 사람 지우기·연출 확인) → 합성·점검 → 내보내기.
    콘티(make_storyboard.py·build_panels.py)는 먼저 써 둔다(new_episode 로 틀을 열고 이야기대로 고침).
    이 화에 처음 나오는 장소·인물이 있으면 vast 로 시안을 만들어 assets/_candidates/ 에 두고 멈춘다(job 이 '실패'로 끝나고 로그에 '확인 필요').
    그때 사용자에게 시안 경로를 보여 주고, 고르면 accept_new=True 로 다시 부른다. 본편 그림(panels_hf)이 이미 있는 컷은 건드리지 않는다.
    until: assets · draw · compose · export. 끝나면 job_status 의 review(사람 확인이 남은 컷)를 사용자에게 알린다."""
    ep = _episode(series, episode)
    if until not in ("assets", "draw", "compose", "export"):
        return "until 은 assets · draw · compose · export"
    if not (ep / "make_storyboard.py").exists() or not (ep / "build_panels.py").exists():
        return "콘티 파일이 없다. new_episode 로 틀을 열고 이야기대로 make_storyboard.py·build_panels.py 를 고친 뒤 부른다"
    _ensure_idle(ep)
    # 하네스 관문: 콘티·컷 설명 관문을 지금 파일 그대로 통과했어야 그린다(규칙은 프롬프트가 아니라 관문이 강제). skip_gates 는 사용자가 직접 시킬 때만
    st = gates.status(ep)
    if not skip_gates and not all(st.values()):
        miss = [k for k, v in st.items() if not v]
        return (f"관문을 아직 통과하지 않았다({', '.join('콘티' if m == 'storyboard' else '컷 설명' for m in miss)}). "
                "prepare_episode 를 불러 채점 결과의 고칠 점을 고친 뒤 다시 부른다(콘티나 컷 설명을 고치면 관문을 다시 받아야 한다).")
    cmd = [PY, str(TOOLS / "make_episode.py"), str(_series(series)), ep.name, "--until", until]
    cmd += (["--accept-new"] if accept_new else []) + ([] if check else ["--no-check"])
    job = _start_job("make_episode", ep, [{"label": f"한 화 만들기({ep.name}, ~{until})", "cmd": cmd, "review_codes": [3]}])
    n = len(_panels_json(ep))
    # 2026-10-16 agent app 시험: 이 작업을 같은 대화에서 기다리다 대화가 36분·도구 44번으로 길어져, 사내 모델이 마지막 답을 180초 안에 시작하지 못했다
    return (f"작업 시작 {job}: {ep.name}, 새 컷 약 {n}개, 예상 {max(3, n * 2 + 3)}분 안팎, 크레딧 0. "
            f"기다리지 말고 지금 사용자에게 작업 번호 {job} 와 예상 시간을 알리고 답을 끝낸다. "
            f"사용자가 '다 됐어?' 하면 job_status('{job}', tail_lines=60) 한 번(새 에셋이 필요하면 '확인 필요'로 멈춰 있다).")


@mcp.tool()
def vast_episode(series: str, episode: str, redraw: bool = False, turbo: bool = True, expr: bool = True, fix: bool = True, check: bool = True) -> str:
    """vast 컷으로 회차 하나를 1·2화처럼 엮는다(크레딧 0): 본편은 그대로 두고 <회차>_vast 폴더(콘티·대사·재사용 컷은 본편 그대로)를 만들어
    vast 그림이 없는 컷을 그리고 → 그 회차의 새 컷 자리에 vast 그림을 넣고 → 말풍선 합성·PDF·기계 점검까지. redraw=True 면 새 컷을 전부 다시 그린다.
    끝나면 job_status 로 '막는 경고 0' 을 확인하고, export(series, '<회차>_vast') 로 PDF·epub 을 exports 로(이름 끝 _vast판, 본편을 덮지 않음).
    재사용 컷(앞 화 그림을 그대로 쓰는 컷)은 본편 그림 그대로다."""
    src = _episode(series, episode)
    if src.name.endswith("_vast"):
        return "본편 회차 이름을 준다(예: ep002_v2). _vast 폴더는 이 도구가 만든다"
    sd = _series(series)
    pj = [p["id"] for p in _panels_json(src)]
    if not pj:
        return "panels.json 에 새로 그릴 컷이 없다(prepare_episode 를 먼저)"
    if len(pj) > VAST_MAX * 2:
        return f"새 컷이 {len(pj)}개라 너무 많다. 나눠서 vast_draw 한 뒤 다시 부른다"
    _ensure_idle(src)
    dst = src.parent / f"{src.name}_vast"
    if not dst.exists():  # 콘티·대사·컷 설명·재사용·합성 컷은 본편에서. 본편의 그린 컷·결과물·시험 폴더는 빼고
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("out", "review", "panels_hf", "panels_hf_*", "_rejected", "__pycache__",
                                                                  "render_run.log", "render_status.json"))
    todo = pj if redraw else [i for i in pj if not (src / "panels_hf_vast" / f"{i}.png").exists()]
    steps = []
    if todo:
        cmd = [PY, str(VAST / "comfy_panel.py"), str(sd), str(src / "panels.json"), "--episode", src.name, "--only", ",".join(todo), "--variant", "vast"]
        cmd += (["--turbo"] if turbo else []) + (["--expr"] if expr else []) + (["--fix"] if fix else []) + (["--check"] if check else [])
        steps.append({"label": f"vast {len(todo)}컷 그리기(크레딧 0)", "cmd": cmd, "allow_fail": True})
    steps += [{"label": "vast 컷을 vast 판 회차로", "cmd": [PY, "-c", VAST_COPY, str(src), str(dst), ",".join(pj)]},
              {"label": "컷 놓기", "cmd": [PY, str(TOOLS / "hf_place.py"), str(dst)]},
              {"label": "합성·PDF", "cmd": [PY, str(TOOLS / "run_episode.py"), str(dst)]},
              {"label": "기계 점검", "cmd": [PY, str(TOOLS / "qa_gate.py"), str(dst)], "allow_fail": True},
              {"label": "말풍선 점검", "cmd": [PY, str(TOOLS / "check_bubbles.py"), str(dst)], "allow_fail": True}]
    job = _start_job("vast_episode", src, steps)
    per = (30 if turbo else 120) + (40 if check else 0)
    return (f"작업 시작 {job}: {dst.name} 만들기, 새로 그릴 컷 {len(todo)}/{len(pj)}(약 {len(todo) * per // 60}분) + 합성 1~3분, 크레딧 0. "
            f"job_status('{job}') → 끝나면 export(series, '{dst.name}')")


# ── 합성·내보내기 ─────────────────────────────────────────────
@mcp.tool()
def compose(series: str, episode: str) -> str:
    """합성: 컷 놓기 → 합성 재료 컷(카톡 등) → 말풍선 합성 → PDF → 기계 점검(qa_gate·check_bubbles). 크레딧 안 씀.
    job_status 결과에서 '막는 경고 0' 과 '얼굴을 가리거나 ... 0' 을 확인한다."""
    ep = _episode(series, episode)
    _ensure_idle(ep)
    steps = [{"label": "컷 놓기", "cmd": [PY, str(TOOLS / "hf_place.py"), str(ep)]},
             {"label": "합성·PDF", "cmd": [PY, str(TOOLS / "run_episode.py"), str(ep)]},
             {"label": "기계 점검", "cmd": [PY, str(TOOLS / "qa_gate.py"), str(ep)], "allow_fail": True},
             {"label": "말풍선 점검", "cmd": [PY, str(TOOLS / "check_bubbles.py"), str(ep)], "allow_fail": True}]
    job = _start_job("compose", ep, steps)
    return f"작업 시작 {job}(약 1~3분). job_status('{job}')"


@mcp.tool()
def export(series: str, episode: str) -> str:
    """예고편(trailer.json)·epub 을 만들고 PDF·epub·예고편을 exports/<오늘>/ 로 복사한다. compose 가 끝난 뒤 부른다. 크레딧 안 씀."""
    ep = _episode(series, episode)
    b = _json(ep / "storyboard.json", {}) or {}
    title = re.sub(r"[^\w가-힣]+", "", (b.get("title_card") or ep.name).replace(".", ""))
    name = f"{b.get('title', _series(series).name)}_{title}".replace(" ", "") + ("_vast판" if ep.name.endswith("_vast") else "")  # 본편 내보내기를 덮지 않게
    day = time.strftime("%Y-%m-%d")
    copy = ("import shutil,sys; from pathlib import Path; ep=Path(sys.argv[1]); d=Path(sys.argv[2]); n=sys.argv[3]; d.mkdir(parents=True,exist_ok=True)\n"
            "for src,ext in [(next(ep.glob('out/*.pdf'),None),'.pdf'),(next(ep.glob('out/*.epub'),None),'.epub'),(ep/'out/trailer.mp4','_예고편.mp4'),(ep/'out/trailer.gif','_예고편.gif')]:\n"
            "    if src and Path(src).exists(): shutil.copy2(src, d/(n+ext)); print('복사', d/(n+ext))")
    steps = [{"label": "예고편", "cmd": [PY, str(TOOLS / "trailer2.py"), str(ep)], "allow_fail": True},
             {"label": "epub", "cmd": [PY, str(TOOLS / "to_epub.py"), str(ep)]},
             {"label": "내보내기 복사", "cmd": [PY, "-c", copy, str(ep), str(ROOT / "exports" / day), name]}]
    job = _start_job("export", ep, steps)
    pdf = _link(ROOT / "exports" / day / f"{name}.pdf")
    return f"작업 시작 {job}. 끝나면 exports/{day}/{name}.* . job_status('{job}')" + (f"\n끝난 뒤 볼 링크(PDF): {pdf}" if pdf else "")


def _job_review(spec: dict, started: str) -> list:
    """이 작업이 그린 컷 중 사람 확인이 남은 컷. 검수(vast/refine.py record_review)가 <회차>/review/staging.json 에 남긴 것을 읽는다."""
    cur = _json(Path(spec.get("episode_dir", "")) / "review" / "staging.json", {}) or {}
    ids = set()
    for st in spec.get("steps", []):
        cmd = [str(x) for x in st.get("cmd", [])]
        if "--only" in cmd:
            ids |= set(cmd[cmd.index("--only") + 1].split(","))
    cats = (_json(ROOT / "skills" / "staging-review" / "checks.json", {}) or {}).get("categories", {})  # 항목 이름의 뜻(기준표 데이터)
    return [{"panel": k, **v, "meaning": {i: cats[i] for i in v.get("issues", []) if i in cats}}
            for k, v in sorted(cur.items()) if (k in ids if ids else v.get("at", "") >= started)]


STALLED_S = float(os.environ.get("WEBTOON_STALLED_S", "900"))


def _job_info(job_id: str, tail_lines: int = 40) -> dict:
    if not re.fullmatch(r"[\w-]+", job_id):
        return {"error": "bad_job_id"}
    st = _json(JOBS / f"{job_id}.status.json", None) or {}
    spec = _json(JOBS / f"{job_id}.json", None)
    if spec is None:
        return {"error": "no_such_job", "job": job_id}
    log = JOBS / f"{job_id}.log"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    state = st.get("state", "starting")
    out = {"job": job_id, "kind": spec.get("kind"), "state": state}
    ep = Path(spec.get("episode_dir", ""))
    if ep.parent.name == "episodes":
        out["series"], out["episode"] = ep.parent.parent.name, ep.name
    if st.get("started"):
        fmt = "%Y-%m-%d %H:%M:%S"
        end = time.mktime(time.strptime(st["finished"], fmt)) if st.get("finished") else time.time()
        out["elapsed_s"] = int(end - time.mktime(time.strptime(st["started"], fmt)))
    done = sum(1 for ln in lines if re.match(r"^\s*p\w+: \d+x\d+ ", ln))  # 끝난 컷 줄 "p030: 1664x1248 ..."
    out["panels_done"] = done
    ids = set()
    for stp in spec.get("steps", []):
        cmd = [str(x) for x in stp.get("cmd", [])]
        if "--only" in cmd:
            ids |= set(cmd[cmd.index("--only") + 1].split(","))
    total = len(ids) or next((int(m.group(1)) for ln in lines for m in [re.search(r"그릴 컷 (\d+)개", ln)] if m), None)
    if total:
        out["panels_total"] = total
        if state == "running" and done >= 2 and out.get("elapsed_s"):  # 거친 추정(앞 단계 시간이 섞여 조금 길게 나온다)
            out["eta_s"] = int(out["elapsed_s"] / done * max(total - done, 0))
    if state == "running" and log.exists():  # 로그가 오래 안 늘면 멈춘 것일 수 있다(프로세스가 죽었으면 바로 알린다)
        out["idle_s"] = int(time.time() - log.stat().st_mtime)
        out["stalled"] = out["idle_s"] > STALLED_S or not _alive(st.get("pid"))
    review = _job_review(spec, st.get("started", ""))
    epd = ep
    drawn = {}
    for stp in spec.get("steps", []):
        cmd = [str(x) for x in stp.get("cmd", [])]
        if "--only" in cmd:
            for pid in cmd[cmd.index("--only") + 1].split(","):
                f = next((epd / d / f"{pid}.png" for d in ("panels_hf_vast", "panels_hf") if (epd / d / f"{pid}.png").exists()), None)
                if f is not None and f.stat().st_mtime >= JOBS.joinpath(f"{job_id}.json").stat().st_mtime:
                    drawn[pid] = _link(f) or str(f)
    if drawn:
        out["drawn"] = drawn
    if review:
        out["review"] = review
        if state == "done":
            out["state"] = "needs_review"
    if spec.get("kind") == "prepare" and state in ("done", "failed"):
        out["gates"] = gates.status(epd)
        tail_lines = max(tail_lines, 80)
    cands = [ln.split("확인: ", 1)[1].strip() for ln in lines if "시안 확인: " in ln]
    if cands:  # 승인할 시안: 이 PC 경로와 원격에서 여는 링크(링크는 사용자가 브라우저로 본다)
        out["candidates"] = [{"path": c, "link": _link(c if Path(c).is_absolute() else ROOT / c)} for c in cands]
    links = [x for x in (_link(m.group(1).strip()) for ln in lines for m in [re.match(r"^\s*(?:복사|내보냄)\s+(.+)$", ln)] if m) if x]
    if links:
        out["links"] = links
    out["log_tail"] = "\n".join(lines[-tail_lines:]) if tail_lines else ""
    return out


@mcp.tool()
def job_status(job_id: str, tail_lines: int = 40) -> str:
    """작업(job)의 상태(JSON). state: starting · running · done · failed · needs_review(사람 확인이 필요함, 실패 아님: review 의 컷이나
    candidates 의 시안을 사용자에게 보여 준다). panels_done/panels_total/eta_s = 진행(그리는 작업), stalled = 로그가 오래 안 늘었거나 프로세스가 죽음,
    gates = 준비 작업의 관문 통과 여부, review = 그린 컷 중 검수가 사람 확인으로 남긴 컷(panel·issues·note), drawn · links = 그린 컷·내보낸 파일 링크."""
    return json.dumps(_job_info(job_id, tail_lines), ensure_ascii=False)


def _wait_job(job_id: str, seconds: int = 50, tail_lines: int = 30) -> str:
    """작업이 끝날 때까지(또는 seconds 초) 기다렸다가 job_status 와 같은 결과(agent app 는 MCP 호출을 60초에 끊어 50초가 상한)."""
    end = time.time() + max(1, min(int(seconds), 50))
    while time.time() < end:
        if (_json(JOBS / f"{job_id}.status.json", None) or {}).get("state") not in (None, "starting", "running"):
            break
        time.sleep(2)
    return job_status(job_id, tail_lines)


@mcp.tool()
def job_wait(job_id: str, seconds: int = 50, tail_lines: int = 30) -> str:
    """작업이 끝날 때까지 최대 seconds 초(50초까지, agent app 의 도구 호출 한도 60초 안쪽) 기다렸다가 job_status 와 같은 결과를 준다. job_status 를 연달아 부르지 말고 이걸 쓴다.
    컷 몇 장처럼 짧은 작업에만, 같은 작업으로 최대 4번까지(약 5분). 그래도 running 이면 사용자에게 작업 번호·panels_done/panels_total·eta_s 를 알리고 답을 끝낸다
    (작업은 대화와 상관없이 이어 돈다. 사용자가 '다 됐어?' 하면 list_jobs 나 job_status 한 번). 한 화 만들기처럼 긴 작업은 기다리지 말고 바로 그렇게 알린다.
    sleep 같은 다른 도구로 기다리지 않는다.
    (2026-10-16 agent app 시험: job_status 를 몇 초 간격으로 반복하다 agent app 의 반복 방지에 걸려, 그림은 다 그려졌는데 결과를 보고하지 못했다)"""
    if not re.fullmatch(r"[\w-]+", job_id):
        return json.dumps({"error": "bad_job_id"})
    return _wait_job(job_id, seconds, tail_lines)


@mcp.tool()
def list_jobs(series: str = "", episode: str = "", running_only: bool = False, limit: int = 8) -> str:
    """최근 작업 목록(JSON, 최신순): 작업 번호·종류·작품·회차·state·진행(panels_done/panels_total)·eta_s·stalled. 작업 번호를 잃었거나 새 대화에서 '아까 시킨 거 어디까지
    됐어?' 에 답할 때 쓴다(작업은 대화와 상관없이 이어 돈다). series·episode 로 좁히고, running_only 면 도는 것만. 자세한 건 그 번호로 job_status."""
    want_ep = _episode(series, episode).resolve() if series and episode else None
    want_series = _series(series).name if series and not episode else None
    rows = []
    for f in sorted((x for x in JOBS.glob("*.json") if not x.name.endswith(".status.json")), key=lambda x: x.stat().st_mtime, reverse=True):
        info = _job_info(f.stem, 0)
        if info.get("error"):
            continue
        spec = _json(f, {}) or {}
        if want_ep and Path(spec.get("episode_dir", "")).resolve() != want_ep:
            continue
        if want_series and info.get("series") != want_series:
            continue
        if running_only and info["state"] not in ("starting", "running"):
            continue
        rows.append({k: info[k] for k in ("job", "kind", "series", "episode", "state", "elapsed_s", "panels_done", "panels_total", "eta_s", "stalled")
                     if k in info})
        if len(rows) >= max(1, min(int(limit), 30)):
            break
    return json.dumps({"jobs": rows}, ensure_ascii=False)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="stdio 대신 주소(Streamable HTTP, /mcp)로 띄운다")
    ap.add_argument("--port", type=int, default=7440)
    a = ap.parse_args()
    if not a.http:
        mcp.run()
    else:
        # 127.0.0.1 에만 묶는다(밖에서는 터널로만). 라이브러리의 DNS 리바인딩 방어가 localhost Host 만 받아 터널 주소(Host: webtoon.js-96.com)를
        # 421 로 막으니 WEBTOON_PUBLIC_URL 의 호스트를 더한다
        hosts, origins = ["127.0.0.1:*", "localhost:*", "[::1]:*"], ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        if PUBLIC:
            hosts.append(urllib.parse.urlparse(PUBLIC).netloc)
            origins.append(PUBLIC)
        mcp.settings.host, mcp.settings.port = "127.0.0.1", a.port
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)
        # 세션 없는 HTTP: 서버가 클라이언트 세션을 들고 있지 않아 재시작·오래 쉼·프록시를 거쳐도 끊기지 않는다(작업 상태는 디스크에 있다).
        # 세션을 들고 있던 때는 서버를 다시 띄우자 다른 PC 의 agent app 연결이 끊겼다(2026-10-19)
        mcp.settings.stateless_http = True
        mcp.run(transport="streamable-http")
