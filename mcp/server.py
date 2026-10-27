"""이미지 스튜디오 작업실 MCP 서버(id 는 webtoon-studio 그대로). agent app(agent platform 데스크톱)의 MCP 로 붙여 agent platform 에이전트(Qwen 등)가 작업실을 다루게 한다.

    D:\\webtoon-studio\\.venv\\Scripts\\python.exe D:\\webtoon-studio\\mcp\\server.py                    stdio(이 PC 의 agent app 가 직접 띄움)
    D:\\webtoon-studio\\.venv\\Scripts\\python.exe D:\\webtoon-studio\\mcp\\server.py --http [--port 7440]  주소(http://127.0.0.1:7440/mcp)

주소 방식(2026-10-19 사용자 "어느 PC 에서든 김진수 agent app 계정으로"): agent app 의 MCP 목록은 PC 마다 따로라 다른 PC 의 agent app 는 이 PC 의 stdio 서버를 못 띄운다.
이 PC(24시간)가 유일한 작업실로 남고, 예약작업 WEBTOON-MCP-http(mcp/serve_http.cmd)가 --http 로 띄운 것을 Cloudflare 터널
https://webtoon.js-96.com/mcp + Access(서비스 토큰 헤더) 로 연다. WEBTOON_PUBLIC_URL 이 있으면 결과 파일을 /files/ 링크로 준다.

원칙
- 한 번 호출은 30초 안에 끝낸다(agent platform 브리지 시간 제한). 작화·합성·내보내기는 작업(job)으로 띄우고 job_status 로 본다.
- 그리기는 GPU 서버(ComfyUI; state/comfy_url.txt 가 가리키는 곳, 지금은 vast 그림 상자) 하나로 한다. 힉스필드(유료)는 쓰지 않는다(2026-10-22 사용자,
  도구는 2026-10-27 지움). 그리는 판은 그 서버에 저장된 웹툰 워크플로우가 정한다(draw_models 로 목록·모델 확인), 작품마다는 series.json model,
  한 번만 바꿀 땐 vast_draw 의 model. 한 번에 80컷까지, 인스턴스를 켜고 끄는 도구는 없다.
- 지우는 도구는 없다. 다시 그릴 컷의 원본은 panels_hf/_rejected/ 로 옮긴다.
- stdout 은 MCP 전송로다. 출력은 전부 반환값으로만(print 금지). 무거운 import 는 맨 위에서(Windows 워커 스레드 데드락).
"""
import contextlib
import ctypes
import difflib
import functools
import importlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # 맨 위에서 미리 읽는다(툴 안 첫 import 는 Windows 에서 멈출 수 있다)
import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import FileResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import vision_review  # noqa: E402  그림 검수(그림을 보는 모델에 컷 그림 + 컷 설명)
sys.path.insert(0, str(ROOT / "tools"))
import new_series as ns  # noqa: E402  새 작품 열기(설정 한 장 → 작품 폴더)
import gates  # noqa: E402  하네스 관문(콘티·컷 설명 격리 judge, 통과해야 그린다)
import make_episode as make_episode_mod  # noqa: E402  내보내기 이름(export_name)을 같이 쓴다
import clean_old  # noqa: E402  _tmp·_old 로 옮긴 파일의 처음 본 시각(_tmp 만 10일 뒤 지운다)
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv" / "Scripts" / "python.exe").exists() else sys.executable
TOOLS = ROOT / "tools"
JOBS = ROOT / "state" / "mcp_jobs"
SHEETS = ROOT / "exports" / "_sheets"  # 링크(/files/)가 나가는 곳. state 아래에 두면 경로만 줄 수 있어 에이전트가 agent app ShareFile 로 옮기려다 실패했다(2026-10-19)
# WEBTOON_QUEUE_FRONT: agent app 에서 시킨 작업은 공용 ComfyUI 큐의 앞에 넣는다(comfy_client.queue). 작업실의 큰 배치(장소 시안·한 화 그리기) 뒤에 서서
# 한 컷이 10분 걸렸고, 에이전트가 기다리다 멈췄다(2026-10-19 agent app)
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "WEBTOON_QUEUE_FRONT": "1"}
DOCS = {"LESSONS": "LESSONS.md", "ACTING_GUIDE": "worklogs/acting/GUIDE.md"}
# 작업실 사람·Claude Code 세션용 문서. 에이전트가 CLAUDE.md 를 읽고 그 안의 'README → LESSONS → OPEN-ISSUES 차례로 읽기'를 따라
# 도구 한도(25번)를 문서 읽기에 다 썼다(2026-10-23 미사일 스튜디오 새 작품 시험) → read_doc 에선 빼고 guide·스킬로 돌린다
INTERNAL_DOCS = {"CLAUDE", "README", "OPEN_ISSUES"}
SERIES_DOCS = {"README": "README.md", "SERIES": "SERIES.md", "STYLE": "STYLE.md", "ASSETS": "ASSETS.md"}
# 원격 주소(예: https://webtoon.js-96.com). 있으면 결과 파일을 링크로 준다: 다른 PC 의 agent app 에선 D:\ 경로를 못 연다
PUBLIC = os.environ.get("WEBTOON_PUBLIC_URL", "").rstrip("/")
FILE_DIRS = ("exports", "series")  # /files/ 로 내주는 곳(그 밖은 404)
FILE_TYPES = {".pdf", ".epub", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4"}

mcp = FastMCP("webtoon-studio", instructions=(
    "웹툰 하네스(이미지 스튜디오의 웹툰 템플릿). 붙이면 끝: 별도 시스템 프롬프트 없이 이 MCP 만으로 새 작품·다음 화를 만든다. "
    "일을 시작하기 전에 guide() 를 한 번 부른다(단계·관문·규칙). 문서는 guide 와 지금 단계의 스킬(read_doc('skill:<이름>'))만 읽고 바로 도구로 일한다"
    "(다른 작품 파일을 두루 읽지 않는다). "
    "차례: 새 작품 = new_series(설정 객체) → new_episode(작품, 'ep001') → set_storyboard(컷 목록) → set_panels(컷 설명) → prepare_episode(관문) → "
    "사용자가 그리자고 하면 make_episode. 다음 화 = new_episode → set_storyboard(replace=True) → set_panels → prepare_episode. "
    "컷 고치기 = panel_info → preview_panel 또는 edit_panel → compare_panel → adopt_panel. 그림체 고르기 = style_compare. "
    "작업 결과는 JSON(state·review·drawn·links)이다. 사용자가 말한 작품·회차·판 이름('1화 v3' 등)은 바꾸거나 빼지 말고 그대로 episode 에 넘긴다"
    "(도구가 풀어 주고, 없으면 있는 판을 알려 준다: 그때는 없다고 사용자에게 말한다). "
    "콘티·컷 설명은 prepare_episode 의 관문(격리 judge)을 통과해야 make_episode 가 그린다. 그린 그림은 검수가 스스로 다시 그린다. "
    "오래 걸리는 일은 job_id 를 돌려주니 기다리지 말고 알린 뒤 답을 끝낸다. 사용자에게는 한국어로 답한다."))

# 도구를 워커 스레드에서 돌린다. 이 SDK(mcp 1.30)의 FastMCP 는 동기 도구를 이벤트 루프에서 바로 불러, 한 호출이 도는 동안 서버 전체가 멈췄다
# (2026-10-23: job_status(wait=50) 하나가 50초 동안 다른 클라이언트의 initialize 까지 막아 미사일 스튜디오가 TimeoutError 로 이 MCP 를 못 붙였다).
# 등록은 비동기 껍데기로 하고, 코드 안에서 부르는 도구(job_status 등)는 원래 함수 그대로 쓰게 원래 함수를 돌려준다.
# 도구 안에서 처음 불러오는 작업실 모듈은 아래에서 미리 불러 둔다(Windows 워커 스레드의 첫 무거운 import 교착, 메모리 fastmcp-windows-thread-import-deadlock).
_register_tool = mcp.tool
# 도구 성질을 MCP 표준 annotations 로 알린다: 붙는 쪽(미사일 스튜디오·agent app)이 읽기 도구는 묻지 않고, 덮어쓰기·바깥 게시는 묻게 스스로 정한다.
# 없을 땐 모든 도구가 '물어봄'이라 이름을 하나씩 허용 목록에 적어야 했고, 새 도구(style_compare)는 비대화 실행에서 막혔다(2026-10-23).
READ_ONLY = {"guide", "read_doc", "status", "list_panels", "find", "panel_info", "contact_sheet", "file_link", "list_files", "read_file",
             "draw_models", "panel_versions", "compare_panel", "job_status", "tool_help", "ailism_works",
             "style_check"}  # 작품을 바꾸지 않고 점수 보고서·캐시만 남긴다(contact_sheet 가 모아보기 그림을 남기는 것과 같은 결). 빠졌을 땐 새 도구라 묻다가 막혔다(2026-10-27)
DESTRUCTIVE = {"write_file", "edit_file", "move_file", "copy_file", "adopt_panel", "publish_ailism", "ailism_work", "comfy_server", "loras"}
OPEN_WORLD = {"publish_ailism", "ailism_work"}


def _job_started(result):
    """작업을 띄운 도구의 글('작업 시작 <번호>: …')을 state=running JSON 으로. 글로만 주자 27B 모델이 시작만 한 작업을
    '다 됐어요, PDF 여기' 라며 없는 링크까지 지어 답했다(2026-10-24 미사일 스튜디오 시험). 결과가 없다는 것을 칸으로 못 박는다."""
    if not (isinstance(result, str) and result.startswith("작업 시작 ")):
        return result
    m = re.search(r"\b(\d{4}-\d{6}-[0-9a-f]{4})\b", result)
    return json.dumps({"state": "running", "job": m.group(1) if m else "", "result_ready": False,
                       "rule": "아직 끝나지 않았다. 그림·PDF·링크는 job_status 의 state 가 done 이나 needs_review 일 때만 있다. "
                               "지금은 작업 번호와 예상 시간만 알린다(끝났다고 말하지 않는다).",
                       "message": result}, ensure_ascii=False)


def _threaded_tool(*args, **kwargs):
    def deco(fn):
        name = kwargs.get("name") or fn.__name__
        ann = kwargs.get("annotations") or ToolAnnotations(readOnlyHint=name in READ_ONLY, destructiveHint=name in DESTRUCTIVE,
                                                           idempotentHint=name in READ_ONLY, openWorldHint=name in OPEN_WORLD)
        return _register_tool(*args, **{**kwargs, "annotations": ann})(fn)

    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            return deco(fn)

        @functools.wraps(fn)
        async def run(**kw):
            return _job_started(await anyio.to_thread.run_sync(functools.partial(fn, **kw)))

        deco(run)
        return fn
    return wrap


mcp.tool = _threaded_tool
sys.path.insert(0, str(ROOT / "vast"))
for _m in ("saved_workflow", "lora", "restyle", "comfy_admin"):
    with contextlib.suppress(Exception):
        importlib.import_module(_m)


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


def _norm_name(s: str) -> str:
    """작품 이름 비교용: 소문자, +→plus, 한자 괄호·공백·기호 뺌('D+18'·'d-plus-18'·'무대 뒤'·'개항(開肛)' → dplus18·무대뒤·개항)."""
    s = re.sub(r"\(.*?\)|（.*?）", "", s.strip().lower()).replace("+", "plus")
    return re.sub(r"[\s\-_·.,'\"「」]+", "", s)


def _series_names() -> dict:
    """정규화한 이름 → 작품 폴더. 폴더 이름(-hf 뺀 것 포함) · series.json 의 slug·title·aliases · README/SERIES.md 첫 제목줄.
    작품마다 별명을 코드에 적던 표(SERIES_ALIAS: D+18·무대 뒤만)를 없앴다(2026-10-23 사용자: 특정 웹툰만 되게 박지 말 것)."""
    out = {}
    for d in sorted((ROOT / "series").iterdir()):
        if not d.is_dir() or d.name.startswith("_") or not (d / "episodes").is_dir():
            continue
        names = [d.name, re.sub(r"-hf$", "", d.name)]
        sj = _json(d / "series.json", {}) if (d / "series.json").exists() else {}
        names += [str(sj.get("slug") or ""), str(sj.get("title") or "")] + [str(a) for a in (sj.get("aliases") or [])]
        for doc in ("SERIES.md", "README.md"):
            if (d / doc).exists():
                first = (d / doc).read_text(encoding="utf-8", errors="replace").lstrip().splitlines()[:1]
                if first and first[0].startswith("# "):
                    names.append(first[0][2:])
        names += [m for n in names for m in re.findall(r"\(([一-鿿]+)\)", n)]  # 괄호 속 한자 제목('開肛')도('힉스필드 판' 같은 설명은 아님)
        for n in names:
            if _norm_name(n):
                out.setdefault(_norm_name(n), d.name)
    return out


def _series(name: str) -> Path:
    key = name.strip()
    if not (ROOT / "series" / key).is_dir() or key.startswith("_"):
        key = _series_names().get(_norm_name(name), key)
    p = ROOT / "series" / key
    if not (p.is_dir() and (p / "episodes").is_dir()) or key.startswith("_"):
        known = sorted(d.name for d in (ROOT / "series").iterdir() if d.is_dir() and not d.name.startswith("_"))
        raise ValueError(_fail("unknown_series", given=name, candidates=_near(name, known) or known, series=known))
    return p


def _episode(series: str, episode: str) -> Path:
    sd = _series(series)
    # "1화"·"ep2"·"2" 도 받는다(agent app 에이전트가 이렇게 불렀다). 판 번호도: "1화 v3"·"ep1_v3"·"ep001v3", 판만("v3")은 그 판이 하나뿐일 때
    # (2026-10-23 미사일 스튜디오 시험: "개항 1화 v3" 를 episode="v3" 로 불러 한 번 헛돌았다)
    m = re.fullmatch(r"\s*(?:ep)?\s*0*(\d+)\s*(?:화)?\s*[_\s-]*(v\d+)?\s*(?:판)?\s*", episode or "", re.I)
    if m:
        episode = f"ep{int(m.group(1)):03d}" + (f"_{m.group(2).lower()}" if m.group(2) else "")
    else:
        v = re.fullmatch(r"\s*(v\d+)\s*(?:판)?\s*", episode or "", re.I)
        hits = [d.name for d in (sd / "episodes").iterdir() if v and d.is_dir() and d.name.endswith("_" + v.group(1).lower())] if v else []
        episode = hits[0] if len(hits) == 1 else (episode or "").strip()
    p = sd / "episodes" / episode
    if not re.fullmatch(r"ep\d{3}(_v\d+)?(_vast)?", episode) or not p.is_dir():
        have = sorted(d.name for d in (sd / "episodes").iterdir() if d.is_dir())
        # 같은 화의 없는 판(1화 v8)이면 그 화의 판들과 가장 최근 판을 알린다: 후보로 ep001 하나만 주니 에이전트가 v8 을 찾아 서른 번 헤매다
        # v3 을 통째로 복사해 v8 을 만들었다(2026-10-24 스튜디오 시험)
        base = re.match(r"ep\d{3}", episode)
        ver_only = re.fullmatch(r"v\d+", episode, re.I)  # 판만('v8')인데 그런 판이 없다
        same = sorted((d for d in have if (base and d.startswith(base.group(0))) or ver_only),
                      key=lambda d: (d[:5], int((re.search(r"_v(\d+)", d) or [0, 1])[1])))
        extra = {"versions": same, "latest": same[-1],
                 "hint": f"그 판은 없다. 이 화의 판은 {', '.join(same)}(가장 최근 {same[-1]}). 사용자에게 어느 판인지 묻거나, 가장 최근 판으로 한다고 "
                         "알리고 진행한다. 새 판이 필요하면 new_episode(파일 복사로 만들지 않는다)"} if same and episode not in same else {}
        raise ValueError(_fail("unknown_episode", given=episode, series=sd.name, candidates=_near(episode, have) or have, episodes=have, **extra))
    return p


def _json(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _run(cmd, timeout=25) -> str:
    # stdin 은 꼭 막는다: 자식이 MCP 전송로(stdin)를 물려받으면 기다리며 멈추거나 MCP 메시지를 가로챈다(힉스필드 CLI 가 20초 멈췄다)
    r = subprocess.run(cmd, cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    return out[-4000:] if out else f"(출력 없음, 종료 코드 {r.returncode})"


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
    """그 회차(작품)에 아직 도는 작업이 있음. 메시지는 JSON(도는 작업 번호·상태): 에이전트가 그 번호로 job_status(wait=50) 한다."""


QUICK_KINDS = {"vast_adopt", "edit_panel"}  # 몇 초~1분이면 끝나는 작업(그림 옮겨 놓기·편집 한 장): 다음 호출이 잠깐 기다린다
# (2026-10-24 개항 v4: 편집을 잇달아 부르다 앞 편집이 안 끝나 busy 로 두 번 걸렸다)
QUICK_WAIT_S = 60


def _already(ep: Path, kind: str) -> str:
    """같은 회차에서 같은 종류 작업이 이미 돌면 그 작업을 기다리라는 답(오류가 아니다). 없으면 ''.
    합성이 도는 중에 compose 를 다섯 번 다시 불러 busy 오류만 쌓였다(2026-10-24 개항 v4 미사일 스튜디오 시험)."""
    busy = _running_job(ep)
    if busy and (_json(JOBS / f"{busy}.json", {}) or {}).get("kind") == kind:
        return f"이미 같은 작업이 돌고 있다(작업 {busy}). 다시 부르지 말고 job_status('{busy}', wait=50) 로 끝날 때까지 기다린다"
    return ""


def _ensure_idle(ep: Path):
    """도는 작업이 있으면 JobBusy. 작업을 띄우는 도구가 파일을 옮기기 전에 부르고, _start_job 도 마지막에 한 번 더 본다.
    도구마다 다른 문장('도는 작업이 있다. 끝난 뒤 부른다')만 돌려줬더니 에이전트가 기다릴 번호를 몰라 같은 호출을 몇 초 간격으로 되풀이했다(2026-10-19 agent app)."""
    busy = _running_job(ep)
    # 몇 초면 끝나는 작업(컷 놓기)은 잠깐 기다린다: adopt_panel 바로 뒤 compose 가 busy 로 걸려 에이전트가 헛호출을 했다(2026-10-24 스튜디오 시험)
    t0 = time.time()
    while busy and (_json(JOBS / f"{busy}.json", {}) or {}).get("kind") in QUICK_KINDS and time.time() - t0 < QUICK_WAIT_S:
        time.sleep(2)
        busy = _running_job(ep)
    if busy:
        raise JobBusy(json.dumps({"error": "busy", "running_job": busy, "running": json.loads(job_status(busy, tail_lines=5))}, ensure_ascii=False))


class RepeatedJob(Exception):
    """같은 입력으로 같은 작업을 짧은 간격에 다시 부름. 메시지는 JSON(이전 작업 번호·상태) — MCP 가 도구 오류로 돌려준다."""


REPEAT_MIN = float(os.environ.get("WEBTOON_REPEAT_MIN", "30"))


IMAGE_INPUT_KINDS = {"compose", "export", "publish_ailism", "review", "vast_adopt"}  # 그린 그림·합성본을 읽어 만드는 작업


def _fingerprint(kind: str, ep: Path, steps: list) -> str:
    """작업의 지문 = 종류 + 명령 + 그 작업이 읽는 입력 파일(콘티·컷 설명) 내용. 입력이 바뀌면(컷 설명 수정·시드 변경) 다른 작업이다."""
    import hashlib
    h = hashlib.sha1(json.dumps([kind, [s.get("cmd") for s in steps]], ensure_ascii=False, default=str).encode())
    for name in ("make_storyboard.py", "storyboard_spec.json", "build_panels.py", "storyboard.json", "panels.json", "panels_spec.json", "series.json"):
        if (ep / name).exists():
            h.update((ep / name).read_bytes())
    # 작품 단위 작업(시안 다시 그리기 등)은 그림체·인물 글(prompts/*.txt)이 입력이다. 글을 고쳐 다시 그리게 했는데 명령이 같다고
    # repeated_job 으로 막혔다(2026-10-22 개항 그림체를 씹덕풍으로 바꾼 직후 시트 3장)
    if (ep / "prompts").is_dir():
        for f in sorted((ep / "prompts").glob("*.txt")):
            h.update(f.read_bytes())
    # 합성·내보내기·게시는 그림·합성본도 입력이다(이름·크기·고친 시각만): 컷 하나를 편집·교체한 뒤 부른 compose 가 repeated_job 으로 막혀
    # export 가 옛 PDF 를 그대로 복사했다(2026-10-24 개항 v3 p031a). 그리기 작업에는 넣지 않는다(그림이 결과라 매번 달라져 되풀이를 못 막는다)
    for d in ("panels_hf", "panels_hf_vast", "out") if kind in IMAGE_INPUT_KINDS else ():
        if (ep / d).is_dir():
            for f in sorted((ep / d).iterdir()):
                if f.is_file():
                    st = f.stat()
                    h.update(f"{d}/{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def _start_job(kind: str, ep: Path, steps: list, note: str = "") -> str:
    """작업을 띄운다(모든 작업 도구가 여기를 거친다). 같은 지문의 작업을 REPEAT_MIN 분 안에 다시 부르면 띄우지 않고 RepeatedJob:
    에이전트가 '사람 눈으로 확인'을 실패로 읽고 같은 컷만 열 번 다시 그렸다(2026-10-19 agent app, D+18 2화 p022). 도구마다 막지 않고 여기 한 곳에서."""
    JOBS.mkdir(parents=True, exist_ok=True)
    _ensure_idle(ep)
    if kind in GPU_KINDS:
        why = _gpu_down()
        if why:
            raise GpuDown(json.dumps({"error": "gpu_server_down", "server": _gpu_host(), "detail": why,
                                      "how": "그리기 서버(ComfyUI)가 꺼졌거나 주소가 바뀌었다. 사용자에게 알린다: GPU 서버를 깨우거나(스튜디오 설정 → MCP 의 "
                                             "webtoon-studio 'Restart (wake)', 또는 tools/gpu_wake.py: 마지막으로 쓴 서버를 깨움) "
                                             "다른 서버 주소를 state/comfy_url.txt 에 적은 뒤 다시"},
                                     ensure_ascii=False))
    fp = _fingerprint(kind, ep, steps)
    since = time.time() - REPEAT_MIN * 60
    # 회차 폴더가 그 작업 뒤에 새로 생겼으면(지우고 같은 이름으로 다시 뜸) 이전 작업의 결과는 없다: 같은 판을 지우고 v3 에서 다시 뜬
    # ep001_v9 의 compose 가 지워진 폴더의 옛 작업 때문에 repeated_job 으로 막혀 PDF 를 못 만들었다(2026-10-24 개항 사본 시험)
    born = getattr(ep.stat(), "st_birthtime", None) if ep.is_dir() else None
    if born:
        since = max(since, born)
    for f in sorted(JOBS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.name.endswith(".status.json") or f.stat().st_mtime < since:
            continue
        old = _json(f, {})
        if not isinstance(old, dict):  # 작업 기록이 아닌 JSON(손으로 둔 명령 목록 등)이 섞여도 모든 작업 도구가 멈추지 않게(2026-10-22 'list' has no attribute 'get')
            continue
        # 실패한 작업은 다시 돌린다: 같은 입력이라도 GPU 서버가 꺼져 있던 탓(404)이면 결과가 달라진다
        # (2026-10-23 파드가 멈춘 사이 시안 13장이 실패하자, 서버를 켜고 다시 불러도 이 검사가 실패 결과를 그대로 돌려줬다)
        if old.get("fingerprint") == fp and (_json(f.with_name(f.name.replace(".json", ".status.json")), {}) or {}).get("state") != "failed":
            raise RepeatedJob(json.dumps({"error": "repeated_job", "previous_job": old.get("id"),
                                          "previous": json.loads(job_status(old.get("id"), tail_lines=5))}, ensure_ascii=False))
    job_id = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    if kind in ("lora_train", "restyle", "lora_compare"):  # 학습은 로그가 드문드문 나온다: 멈춤 지킴이(job_runner) 한도를 넉넉히
        steps = [{**s, "stall_min": s.get("stall_min", 120)} for s in steps]
    spec = {"id": job_id, "kind": kind, "episode_dir": str(ep), "note": note, "steps": steps, "fingerprint": fp}
    if kind in ("lora_train", "restyle", "lora_compare"):
        # 학습한 서버를 적어 둔다: 학습 결과(LoRA)는 그 서버 볼륨에 있어, 서버를 갈아끼울 때 이 서버만 남기고 나머지 멈춘 파드는 지운다
        # (tools/runpod_cloud.py trained_on, 2026-10-24 멈춘 파드가 넷까지 쌓여 보관비 월 약 $80)
        spec["gpu_host"] = _gpu_url()
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
    spec = _json(ep / "panels_spec.json", {}) or {}
    reuse = spec.get("reuse", {})
    # set_panels 로 넣고 아직 prepare_episode 를 안 돌린 컷도 설명이 있는 컷이다(panels.json 만 보면 '설명 없음'으로 나왔다)
    for i, sp in (spec.get("panels") or {}).items():
        draw.setdefault(i, {"id": i, **sp})
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

def _gpu_url() -> str:
    """그리기 서버(ComfyUI) 주소. vast/comfy_client.Comfy 와 같은 차례: 환경변수 COMFY_URL → state/comfy_url.txt.
    (예전 이 함수는 파일을 먼저 읽어 안내에 보이는 서버와 실제로 그리는 서버가 다를 수 있었다)"""
    sw = ROOT / "state" / "comfy_url.txt"
    txt = sw.read_text(encoding="utf-8").strip() if sw.exists() else ""
    return os.environ.get("COMFY_URL", "") or (txt.splitlines()[0] if txt else "")


GPU_KINDS = {"vast_draw", "make_episode", "style_compare", "redo_candidate", "vast_assets", "edit_panel", "preview_panel", "lora_train",
             "restyle", "lora_compare"}
_gpu_seen = {"at": 0.0, "why": ""}


def _gpu_down() -> str:
    """그리기 서버에 닿지 않으면 그 까닭(30초 동안 다시 묻지 않음), 닿으면 ''. 꺼진 서버에 작업을 띄우면 몇 분 뒤 날것의 오류로
    실패해 에이전트가 무엇이 문제인지 몰랐다(2026-10-24) → 띄우기 전에 묻는다."""
    if time.time() - _gpu_seen["at"] < 30:
        return _gpu_seen["why"]
    why = ""
    try:
        from comfy_client import Comfy
        c = Comfy()
        with urllib.request.urlopen(urllib.request.Request(c.base + "/system_stats", headers={**c.headers, "User-Agent": "webtoon-studio"}),
                                    timeout=10) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        why = f"{type(e).__name__}: {str(e)[:150]}"
    _gpu_seen.update(at=time.time(), why=why)
    return why


class GpuDown(Exception):
    """그리기 서버에 닿지 않음. 메시지는 JSON(서버·까닭·하는 법)."""


def _gpu_host() -> str:
    """지금 그리기에 쓰는 ComfyUI 서버의 호스트. 도구 설명은 서버 이름을 박지 않고 여기서 읽는다."""
    import urllib.parse as _up
    url = _gpu_url()
    if not url:
        return "주소 없음(state/comfy_url.txt 비어 있음)"
    host = _up.urlparse(url).netloc or url
    kind = "RunPod 파드" if "runpod" in host else ("vast.ai" if "vast" in host else "ComfyUI")
    return f"{host} ({kind})"

@mcp.tool()
def guide(topic: str = "") -> str:
    """웹툰 하네스 안내: 단계(작품 열기 → 콘티 → 컷 설명 → 그리기 → 이어짐 → 합성·출판), 단계마다 통과해야 하는 관문, 긴 작업·오류 다루기, 돈·장비·내용 규칙.
    어떤 에이전트든 일을 시작하기 전에 꼭 한 번 부른다(이 MCP 는 따로 시스템 프롬프트가 필요 없다). topic='storage' 면 저장소 둘러보기·버전·그림체 바꾸기 예,
    'all' 이면 전부."""
    t = (ROOT / "mcp" / "HARNESS.md").read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", t)
    storage = [x for x in parts if x.startswith("## 4.")]
    # 처음엔 핵심만: 전부(16KB)를 주니 32k 창의 작은 모델이 그것만으로 문맥을 많이 썼다(2026-10-24 스튜디오 시험). 저장소 예시는 필요할 때
    if topic.strip().lower() in ("storage", "저장소", "4"):
        body = "".join(storage)
    elif topic.strip().lower() in ("all", "전부"):
        body = t
    else:
        body = "".join(x for x in parts if not x.startswith("## 4.")) + ("\n(저장소 둘러보기·회차 버전·그림체 바꾸기 예는 guide('storage'))" if storage else "")
    return f"지금 그리는 GPU 서버: {_gpu_host()} (COMFY_URL 또는 state/comfy_url.txt)\n\n" + body


@mcp.tool()
def read_doc(doc: str = "", series: str = "", offset: int = 0, max_chars: int = 12000) -> str:
    """그 단계의 스킬·작품 문서를 읽는다. 'skill:<이름>' 이 기본(storyboard 콘티 · panel-spec 컷 설명 · continuity 이어 그리기 · asset 에셋 ·
    staging-review 연출 검수 · lettering-publish 식자·출판), 'skill' 이면 스킬 목록. series 를 주면 README · SERIES · STYLE · ASSETS(그 작품 설정).
    LESSONS(사용자가 짚은 문제 모음, 길다)·ACTING_GUIDE(연기). 지금 하는 단계의 스킬 하나만 읽는다: 순서는 guide(), 규칙은 관문이 강제한다.
    긴 문서는 offset 으로 이어 읽는다."""
    d = (doc.strip() or ("SERIES" if series else "SKILL")).upper()  # series 만 주면 그 작품 설정, 아무것도 없으면 스킬 목록
    if d == "SKILL" or d.startswith("SKILL:"):
        t = _skill_text(doc.strip()[6:])
        part = t[offset:offset + max_chars]
        return part + (f"\n\n(… 전체 {len(t)}자 중 {offset}~{offset + len(part)}. 이어 읽으려면 offset={offset + len(part)})"
                       if offset + len(part) < len(t) else "")
    if series:
        sd = _series(series)
        have = [k for k, f in SERIES_DOCS.items() if (sd / f).exists()]
        if d not in SERIES_DOCS or not (sd / SERIES_DOCS[d]).exists():
            # 없는 문서를 열면 날것의 FileNotFoundError 였다(새 작품엔 ASSETS.md 가 없다, 2026-10-24) → 있는 문서를 알린다
            return f"{sd.name} 의 작품 문서는 {', '.join(have) or '없음'}(설정 원본은 read_file('series/{sd.name}/series.json'))"
        p = sd / SERIES_DOCS[d]
    else:
        if d in INTERNAL_DOCS:
            return ("그 문서는 작업실 사람용 내부 문서라 에이전트 일에 쓰지 않는다. 일하는 순서는 guide(), 단계별 규칙은 read_doc('skill:<이름>'), "
                    "작품 설정은 read_doc('SERIES', series=작품), 지금 상태는 status()")
        if d not in DOCS:
            return f"문서는 {', '.join(DOCS)} 중 하나(작품 문서는 series 를 같이 준다, 스킬은 'skill:<이름>')"
        p = ROOT / DOCS[d]
    t = p.read_text(encoding="utf-8")
    part = t[offset:offset + max_chars]
    more = f"\n\n(… 전체 {len(t)}자 중 {offset}~{offset + len(part)}. 이어 읽으려면 offset={offset + len(part)})" if offset + len(part) < len(t) else ""
    return part + more


def _skill_text(name: str = "") -> str:
    """스킬(작업 노하우 설명서 skills/<이름>/SKILL.md). 비우면 스킬 목록과 웹툰 템플릿. read_doc('skill:<이름>') 이 부른다
    (도구를 줄이려고 read_doc('skill:…') 을 read_doc 에 합침, 2026-10-22)."""
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


def _stack_lines() -> list:
    """어디서 무슨 모델로 도는지(키는 안 적음). 사용자가 '어디서 무슨 모델로 하고 있는지 알아야 판단한다'고 했는데 설정 파일을 뒤져야
    답할 수 있었다(2026-10-24). 서버에 묻는 건 켜짐 여부 하나뿐(짧게)."""
    out = []
    down = None
    with contextlib.suppress(Exception):
        down = _gpu_down()
    out.append(f"- 그리는 서버: {_gpu_host()} — {'꺼짐·응답 없음(' + str(down)[:60] + ')' if down else '켜짐'}")
    models = sorted({m.group(1) for f in VAST.glob("mcp_use_webtoon_vertical_*.json") for m in [re.match(r"mcp_use_webtoon_vertical_(.+)\.json$", f.name)] if m})
    out.append(f"- 그리는 판(작업실에 저장된 그리기 워크플로우): {', '.join(models) or '없음'} — 서버에 그 모델 파일이 다 있는지는 draw_models()의 ready")
    vf = ROOT / "state" / "vlm_url.txt"
    if vf.exists():
        url, _, rest = vf.read_text(encoding="utf-8").strip().partition("|")
        out.append(f"- 판정 모델(관문·그림 검수): {rest.split('|')[0] or '?'} @ {re.sub(r'^https?://', '', url).split('/')[0]}")
    else:
        out.append(f"- 판정 모델(관문·그림 검수): {os.environ.get('VLM_MODEL') or '기본값'} @ {os.environ.get('VLM_URL') or '사내 게이트웨이(기본)'}")
    out.append("- 대화 모델: 이 MCP 를 부르는 쪽(미사일 스튜디오 등)의 설정")
    sl = ROOT / "state" / "logs" / "mcp_smoke.log"  # 공짜 시험(tools/mcp_smoke.py, 예약작업 WEBTOON-mcp-smoke)의 마지막 결과
    if sl.exists():
        last = sl.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
        if last:
            out.append(f"- 공짜 시험(모든 작품 읽기 도구, 6시간마다): {last[0][:200]}")
    bl = ROOT / "state" / "logs" / "budget_guard.log"
    if bl.exists():
        last = bl.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
        if last:
            out.append(f"- 예산(10분마다): {last[0]}")
    return out


@mcp.tool()
def status() -> str:
    """지금 구성(그리는 서버·그리는 판·판정 모델·예산)과 작품별 회차 목록(제목·컷 수·그린 컷·합성 여부), 진행 중인 작업."""
    lines = ["# 지금 구성"] + _stack_lines() + [""]
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
    # 프로세스가 죽은 채 running 으로 남은 작업은 도는 것이 아니다(job_status 가 lost 로 알리는 것과 같은 셈, 2026-10-26)
    running = [st.name.replace('.status.json', '') for st in JOBS.glob('*.status.json')
               if (_json(st, {}) or {}).get('state') == 'running' and _alive((_json(st, {}) or {}).get('pid'))]
    lines.append(f"\n진행 중인 작업: {', '.join(running) or '없음'}")
    return "\n".join(lines)



@mcp.tool()
def list_panels(series: str, episode: str, only_missing: bool = False) -> str:
    """회차의 컷 목록: 번호·장면·인물·첫 대사, 그 컷 그림이 있는지(새로 그리는 컷은 panels_hf, 합성·재사용 컷은 panels)."""
    ep = _episode(series, episode)
    b = _json(ep / "storyboard.json", {}) or {}
    cat = _catalog(ep)  # 상태는 panel_info·find 와 같은 것으로(컷 설명이 아직 없는 컷을 '재사용'이라 적어 에이전트가 헷갈렸다, 2026-10-24)
    label = {"new": "새로", "composite": "합성", "reuse": "재사용", "no_description": "설명 없음"}
    out, count = [], {}
    for p in b.get("panels", []):
        pid = p["id"]
        st = cat.get(pid, {}).get("status", "no_description")
        kind = label[st]
        count[kind] = count.get(kind, 0) + 1
        has = (ep / "panels_hf" / f"{pid}.png").exists() if st == "new" else (ep / "panels" / f"{pid}.png").exists()
        if only_missing and has:
            continue
        line = next((d.get("text", "") for d in p.get("dialogue", [])), "")
        out.append(f"{pid} [{kind}{'' if has else ', 없음'}] {p.get('scene', '')} {','.join(p.get('characters', []))} | {line[:40]}")
    if not out:
        return "빠진 컷 없음(전부 그림이 있다)" if only_missing and b.get("panels") else "컷이 없다(storyboard.json 확인)"
    # 맨 앞에 총계: 긴 목록은 대화 쪽에서 잘려 보이는데, 에이전트가 보이는 줄만 세어 113컷 회차를 '90컷'이라 답했다(2026-10-24 개항 v3)
    total = sum(count.values())
    head = f"{total}컷(" + " · ".join(f"{k} {v}" for k, v in count.items()) + ")" + (f", 그림이 빠진 컷 {len(out)}" if only_missing else "")
    return head + "\n" + "\n".join(out)


def _folder(folder: str) -> str:
    """그림 폴더: 비우면 본편 그림(panels_hf), "vast" 면 GPU 서버로 그린 판(panels_hf_vast)."""
    f = (folder or "").strip().lower()
    # "out"·"main"·"본편" 도 본편: 작은 모델이 list_files 에서 본 out/ 폴더 이름을 그대로 넣어 오류 한 번을 썼다(2026-10-26 무대 뒤 GPU 시나리오)
    if f in ("", "hf", "panels_hf", "out", "main", "본편", "final"):
        return "panels_hf"
    if f in ("vast", "panels_hf_vast"):
        return "panels_hf_vast"
    if f in ("vast_restyle", "restyle", "panels_hf_vast_restyle"):
        return "panels_hf_vast_restyle"
    # 모델이 review/… 같은 다른 폴더를 folder 에 넣었다(2026-10-26 무대 뒤 GPU 시나리오): 그런 그림은 paths 로 본다고 바로 알린다
    raise ValueError('folder 는 비우거나(본편; "out"·"main" 도 같음) "vast" · "vast_restyle" 만. 다른 폴더의 그림(review/…, assets/…)은 '
                     "contact_sheet(paths='series/<작품>/<폴더>/*.jpg') 로 본다")


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
def find(query: str, series: str = "", limit: int = 12, episode: str = "") -> str:
    """찾기: 작품 이름(제목·별명), 컷 번호('p22'·'22p'·'p022'), 인물·장소 이름, 대사·행동 글의 낱말로 작품·회차·컷을 찾아 정확한 식별자와 상태를 준다. 파일을 읽어 짐작하지 말고 이걸로 먼저 찾는다.
    컷 번호를 주면 그 번호가 있는 모든 회차(vast 판 복사본 포함)와 컷마다 status(new = 컷 설명이 있어 그린다 · reuse = 앞 화·다른 판 그림을 그대로 · composite = 합성 재료 ·
    no_description)와 그림이 어디 있는지(images)를 준다. 글을 주면 낱말이 모두 든 컷(대사·행동·연기·장소·인물 이름)을 준다. series·episode 로 좁힌다
    (query 를 '<작품> <회차> <찾을 말>' 로 줘도 된다: '<작품> 1화 v2 <인물 이름>').
    인물·낱말이 든 컷을 세거나 모아 볼 땐 결과의 total·by_episode·ids_by_episode(걸린 컷 번호 전부, 쉼표)를 그대로 쓴다: list_panels 목록을 눈으로 골라 세지 않는다."""
    q = (query or "").strip()
    if not q:
        return _fail("empty_query")
    only_ep = None
    ep_only = r"\s*(?:(?:ep)?\s*\d+\s*(?:화)?\s*[_\s-]*(?:v\d+)?|v\d+)\s*(?:판)?\s*"  # 회차·판만인 글
    # '작품 이름 + 나머지'(D+18 3화 · 개항 1화 v3 p023f · 무대 뒤 리허설): 앞 낱말들이 작품 이름이면 그 작품으로 좁히고 나머지로 찾는다.
    # 나머지가 회차면 그 회차 요약을 준다(2026-10-24 스튜디오 시험: 'D+18 3화' 가 nothing_found)
    if not series:
        toks = q.split()
        names = _series_names()
        for i in range(len(toks) - 1, 0, -1):
            hit = names.get(_norm_name(" ".join(toks[:i])))
            if hit:
                series, q = hit, " ".join(toks[i:])
                break
    # '<회차> <찾을 말>'(1화 김금수 · 1화 v4 도경 · ep002 리허설): 앞의 회차를 떼어 그 회차로 좁힌다. 회차 낱말까지 컷 글에서 찾아
    # '개항 1화 김금수' 가 다섯 작품 모두 nothing_found 였다(2026-10-24 작품별 시나리오 준비)
    if series and episode.strip():
        try:
            only_ep = _episode(series, episode)
        except ValueError as e:
            return str(e)
    elif series and not re.fullmatch(ep_only, q, re.I):  # 회차만이면 아래에서 그 회차 요약
        m = re.match(r"^((?:ep\s*)?\d+\s*화?(?:\s*[_-]?\s*v\d+)?(?:\s*판)?)\s+(\S.*)$", q, re.I)
        if m:
            try:
                only_ep, q = _episode(series, m.group(1)), m.group(2)
            except ValueError as e:
                if re.search(r"화|ep|v\d", m.group(1), re.I):  # 회차 꼴이 분명한데 없다: 있는 판을 알린다(숫자로 시작하는 보통 글은 그대로 찾는다)
                    return str(e)
    if series and only_ep is None:
        try:
            ep = _episode(series, q)
        except ValueError as e:
            ep = None
            if re.fullmatch(ep_only, q, re.I):  # 회차·판만 찾았는데 없다: 있는 판과 할 일
                return str(e)
        if ep is not None:
            board = _json(ep / "storyboard.json", {}) or {}
            panels = board.get("panels", [])
            flow = []
            for p_ in panels:
                sc = p_.get("scene") or "none"
                if flow and flow[-1]["scene"] == sc:
                    flow[-1]["to"] = p_["id"]
                    flow[-1]["cuts"] += 1
                else:
                    flow.append({"scene": sc, "from": p_["id"], "to": p_["id"], "cuts": 1})
            chars = sorted({c for p_ in panels for c in p_.get("characters", [])})
            first = {c: next(p_["id"] for p_ in panels if c in p_.get("characters", [])) for c in chars}
            return json.dumps({"query": query, "series": _series(series).name, "episode": ep.name, "title": board.get("title_card", ""),
                               "cuts": len(panels), "scenes": flow, "characters": chars, "first_appearance": first,
                               # 요약 질문에 작은 모델이 panel_info·status·read_doc·list_panels 를 더 뒤졌다(2026-10-26 외출 286컷, 도구 11번): 이 결과로 족하다고 말한다
                               "next": "회차 요약·컷 수·장면·인물 첫 등장은 이 결과로 충분하다(list_panels 는 컷마다 볼 때). 컷 하나는 panel_info, 그림은 contact_sheet(series, episode, ids=…)"},
                              ensure_ascii=False)
    key = _pid_key(q) if len(q) <= 6 else None
    toks = [x for x in re.split(r"\s+", q.lower()) if x]
    hits, cast_hits = [], []
    for s, e in _ep_dirs(series):
        if only_ep is not None and e.resolve() != only_ep.resolve():
            continue
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
    # 작품 이름(제목·별명·slug)도 찾는다: '개항' 을 찾으면 컷 글에 그 낱말이 없어 nothing_found 였다(2026-10-23 스튜디오 시험, Claude)
    series_hits = []
    if key is None and not series:
        sname = _series_names().get(_norm_name(q))
        if sname:
            sd = ROOT / "series" / sname
            sj = _json(sd / "series.json", {}) if (sd / "series.json").exists() else {}
            heads = [(sd / d).read_text(encoding="utf-8", errors="replace").lstrip().splitlines()[:1] for d in ("SERIES.md", "README.md") if (sd / d).exists()]
            head = next((h[0][2:].strip() for h in heads if h and h[0].startswith("# ")), "")
            series_hits.append({"series": sname, "title": sj.get("title") or head or sname,
                                "episodes": sorted(d.name for d in (sd / "episodes").iterdir() if d.is_dir()),
                                # 인물은 id 와 이름을 같이: id 만 주니 모델이 한자 이름을 지어 붙였다(2026-10-24 스튜디오 시험)
                                "characters": [{"id": c, "name": v.get("ko", ""), "role": v.get("role", "")}
                                               for c, v in (_json(sd / "cast.json", {}) or {}).items() if not c.startswith("_") and isinstance(v, dict)],
                                "next": (f"시트 모아 보기 = contact_sheet(paths='series/{sname}/assets/*_sheet.png'), 파일 목록 = list_files(path='series/{sname}/assets'), "
                                         f"회차 요약 = find('{sname} 1화')(이 MCP 의 도구로 본다)")})
    n = max(1, min(int(limit), 40))
    out = {"query": q, "total": len(hits), "hits": hits[:n]}
    # 걸린 컷 번호는 전부(hits 는 limit 까지만 자세히): 세기·모아보기(contact_sheet ids)에 그대로 쓰게. limit 에 잘린 번호를 채우려고 list_panels 목록을
    # 눈으로 골라 세다 틀린 수를 답했다(2026-10-26 외출 1화 '이상수': find total 180 인데 답 244). 회차·판 여럿에 걸치면 회차마다 나눠 준다
    ids_by_ep = {}
    for h in hits:
        ids_by_ep.setdefault(f"{h['series']}/{h['episode']}", []).append(h["panel"])
    if hits:
        out["by_episode"] = {k: len(v) for k, v in ids_by_ep.items()}
        out["ids_by_episode"] = {k: ",".join(v) for k, v in ids_by_ep.items()}
        notes = []
        if len(hits) > n:
            notes.append(f"hits 는 앞 {n}개만, 컷 번호 전체는 ids_by_episode(세기는 by_episode)")
        if len(ids_by_ep) > 1:
            notes.append("total 은 모든 회차·판(사본 포함)의 합, 한 회차의 수는 by_episode")
        if len(hits) > 8:  # 인물 컷 모아보기: 회차 전체를 한 번 모아 보고 다시 인물 컷을 모으는 헛호출이 있었다(2026-10-26 외출 이상수 180컷)
            notes.append("모아보기는 contact_sheet(series, episode, ids=ids_by_episode 의 값) 한 번으로(24개를 넘으면 도구가 여러 장으로 나눈다)")
        if notes:
            out["note"] = ". ".join(notes)
    if series_hits:
        out["series"] = series_hits
    if cast_hits:
        out["characters"] = cast_hits[:n]
    if not hits and not cast_hits and not series_hits:
        out["error"] = "nothing_found"
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def panel_info(series: str, episode: str, panel: str | int) -> str:
    """컷 하나의 모든 것(JSON): status(new · reuse · composite · no_description), 콘티(장면·구도·인물·대사), 컷 설명(장소·옷·행동·연기·카메라), 그림이 있는 곳(images 링크),
    재사용이면 출처, 검수에서 사람 확인으로 남은 것(review). 컷 설명 파일(build_panels.py, 22KB)을 읽어 그 컷을 찾는 대신 이걸 쓴다. 번호는 p22·22 처럼 줘도 풀어 준다.
    여러 컷은 쉼표로('p001,p005', 관문에 걸린 컷을 한 번에 볼 때) → 목록."""
    panel = str(panel)  # 작은 모델이 컷 번호를 숫자(4)로 넘겨 형식 오류가 났다(2026-10-24 개항 v4)
    ep = _episode(series, episode)
    cat = _catalog(ep)
    review = _json(ep / "review" / "staging.json", {}) or {}
    # 같은 화의 다른 판을 알린다: 사용자가 말한 판(v8)을 빼고 원본(ep001)을 읽어 답한 적이 있다(2026-10-24 스튜디오 시험)
    sib = sorted(d.name for d in ep.parent.iterdir() if d.is_dir() and d.name[:5] == ep.name[:5] and d.name != ep.name and not d.name.endswith("_vast"))
    out = [{"series": _series(series).name, "episode": ep.name, **({"other_versions": sib} if sib else {}), "panel": pid, "status": cat[pid]["status"], "storyboard": cat[pid]["board"],
            "description": cat[pid]["spec"], "reuse_source": cat[pid]["reuse"], "images": _images_of(ep, pid), "review": review.get(pid),
            # 같은 번호에 글자 붙은 컷이 있으면 알린다: 'p023i' 를 '23' 으로 줄여 불러 p023 을 답했다(2026-10-24 스튜디오 시험)
            **({"same_number": sib} if (sib := sorted(x for x in cat if re.fullmatch(re.escape(pid) + r"[a-z]", x))) else {})}
           for pid in _pids(ep, panel, "any")]
    # 고치는 길을 결정하는 자리에 적는다: 파이썬 콘티(make_storyboard.py)인 회차에서 대사 한 줄을 바꾸려고 에이전트가 그 파일을 읽고
    # edit_file 로 고쳤다(2026-10-24 개항 사본 시험, Claude). set_storyboard 는 코드 콘티도 표로 옮겨 그 컷만 덮는다
    how = {"대사·구도·인물 목록": "set_storyboard(cuts=[{그 컷 id·바꿀 칸}]) — 같은 id 만 덮는다. make_storyboard.py·build_panels.py 를 edit_file 로 고치지 않는다",
           "장소·옷·행동·연기·카메라": "set_panels(panels={id: {...}}) → prepare_episode",
           "그림": "일부만 틀리면 edit_panel(먼저 보려면 preview_panel), 구도가 틀리면 vast_draw(ids) → compare_panel → adopt_panel",
           "그 뒤": "compose(대사만 바꿨으면 다시 그리지 않고 합성만)"}
    for o in out:
        o["how_to_change"] = how
    return json.dumps(out[0] if len(out) == 1 else out, ensure_ascii=False)


@mcp.tool()
def contact_sheet(series: str = "", episode: str = "", ids: str = "", scene: str = "", folder: str = "", paths: str = "") -> list:
    """그림을 모아 한 장으로 보여 준다(그림 자체 + JSON: sheet 경로·link 원격 링크). 회차 컷이면 series·episode 와 ids(쉼표)·scene(장소 이름),
    아무 그림이면 paths(파일·폴더·* 패턴을 쉼표로): 인물 시트·시안·장소·참고 그림·내보낸 쪽 그림을 볼 때.

    회차 컷: 비우면 새로 그린 컷 전부, folder="vast" 면 GPU 서버로 그린 컷(panels_hf_vast). folder 는 그림 폴더를 고르는 칸(비움·vast·vast_restyle)이라
    인물·장면 이름을 넣는 곳이 아니다: 인물의 컷만 모으려면 find 로 받은 컷 번호를 ids 에, 장소는 scene 에.
    paths 예: series/<작품>/assets/*_sheet.png · series/<작품>/assets/_candidates · series/<작품>/assets/vast_refs/<인물 id>_*.png (이름을 하나씩 적기보다 * 패턴이 오타가 없다).
    한 장에 24개, 넘으면 도구가 여러 장으로 나눠 전부 만든다(JSON pages 에 장마다 sheet·link, 그림은 첫 장만 실린다): 번호를 손으로 쪼개 여러 번
    부르지 않는다. 사용자에게는 link(여러 장이면 pages 의 link 전부)를 준다."""
    if paths.strip():
        # 회차 컷이 아닌 그림(시트·시안·참고 그림)을 볼 도구가 없어 에이전트가 그림체·생김새를 확인하지 못했다(2026-10-23 사용자:
        # "저장소를 둘러볼 수 있는 쓸모 있는 도구를 골라 쓰게")
        found = []
        for one in [x.strip() for x in re.split(r"[,\n]+", paths) if x.strip()]:
            try:
                for p in _matches(one, series, fuzzy=True):
                    found += sorted(c for c in p.iterdir() if c.suffix.lower() in IMG_EXT) if p.is_dir() else [p] if p.suffix.lower() in IMG_EXT else []
            except (NotFound, ValueError) as e:
                return [str(e)]
        if not found:
            return ["그 경로에 그림(.png .jpg .webp)이 없다. list_files 로 확인"]
        name = re.sub(r"[^\w.-]+", "_", Path(found[0].parent).relative_to(ROOT.resolve()).as_posix())[-60:]
        return _sheets([(f.name, f) for f in found], name)
    ep = _episode(series, episode)
    sub = _folder(folder)
    pj = _panels_json(ep)
    want = _pids(ep, ids, "any")
    sel = [p for p in pj if (not want or p["id"] in want) and (not scene or p.get("location") == scene)]
    if want:  # 재사용·합성 컷도 번호로 고를 수 있게
        sel_ids = [p["id"] for p in sel] + [i for i in want if i not in {p["id"] for p in sel}]
    else:
        sel_ids = [p["id"] for p in sel]
    items = []
    for pid in sel_ids:
        f = ep / sub / f"{pid}.png"
        if not f.exists() and sub == "panels_hf":  # 재사용·합성 컷은 panels 에만 있다
            f = ep / "panels" / f"{pid}.png"
        if f.exists():
            items.append((pid, f))
    if not items:
        return "고른 컷 중 그림이 있는 컷이 없다"
    return _sheets(items, f"{_series(series).name}_{ep.name}")


SHEET_PER = 24  # 모아보기 한 장의 컷 수(4열 6줄, 1280px 로 줄여도 얼굴이 보이는 크기)


def _sheets(items: list, name: str) -> list:
    """(이름, 그림 경로) 목록을 SHEET_PER 개씩 여러 장으로. 결과 = [JSON(pages 에 장마다 sheet·link·shown, 전체 수), 첫 장 그림].
    예전엔 앞 24개만 만들고 전체 수를 알렸는데, 180컷짜리 인물(외출 1화 이상수)에서 작은 모델이 번호를 손으로 7번 쪼개 부르며(겹치는 묶음까지)
    도구 17번·입력 63만 토큰을 썼다(2026-10-26 미사일 스튜디오 시험). 장은 도구가 나누고 모델은 링크만 전한다."""
    if len(items) <= SHEET_PER:
        return _sheet(items, name)
    pages = [items[i:i + SHEET_PER] for i in range(0, len(items), SHEET_PER)]
    first, made = None, []
    for k, page in enumerate(pages, 1):
        r = _sheet(page, f"{name}_{k}of{len(pages)}")
        info = json.loads(r[0])
        made.append({k2: info[k2] for k2 in ("sheet", "link", "panels", "shown")})
        first = first or r[1]
    out = {"panels": len(items), "sheets": len(pages), "per_sheet": SHEET_PER, "pages": made,
           "note": f"{len(items)}컷을 {len(pages)}장으로 나눠 모두 만들었다. 그림은 첫 장만 실었고, 사용자에게는 pages 의 link 를 전부 준다"}
    return [json.dumps(out, ensure_ascii=False), first]


def _sheet(items: list, name: str, total: int = 0) -> list:
    """(이름, 그림 경로) 목록을 4열 모아보기로. 결과 = [JSON, 그림]."""
    tiles = []
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\malgunbd.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
    cell = 1280 if len(items) == 1 else 420  # 한 장(화풍 비교표 등)은 크게: 420 으로 줄이면 칸 글자·얼굴이 안 보인다
    for label, f in items:
        im = Image.open(f).convert("RGB")
        im.thumbnail((cell, cell))
        if len(items) > 1:  # 한 장이면 파일 이름표가 비교표 첫 줄 이름을 가린다
            ImageDraw.Draw(im).text((6, 4), label, fill="yellow", font=font, stroke_width=3, stroke_fill="black")
        tiles.append(im)
    cols = min(4, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    h = max(t.height for t in tiles)
    cw = max(t.width for t in tiles) + 4
    sheet = Image.new("RGB", (cw * cols, (h + 4) * rows), "gray")
    for k, t in enumerate(tiles):
        sheet.paste(t, ((k % cols) * cw, (k // cols) * (h + 4)))
    SHEETS.mkdir(parents=True, exist_ok=True)
    out = SHEETS / f"{name}_{time.strftime('%H%M%S')}.jpg"
    sheet.save(out, quality=85)
    # 그림을 결과에 같이 싣는다: 링크만 주면 링크가 잠겨 있어(Cloudflare Access) 에이전트가 그림을 못 보고 모아보기를 되풀이하다
    # agent app 반복 방지에 끊겼다(2026-10-19). agent app 는 MCP 결과의 그림을 그대로 모델에 넘긴다(agent-workflow connector_mcp injector)
    view = sheet.copy()
    view.thumbnail((1280, 1280))
    buf = io.BytesIO()
    view.save(buf, "JPEG", quality=80)
    info = {"sheet": str(out), "link": _link(out), "panels": len(items), "shown": [lb for lb, _ in items]}
    if total > len(items):
        info["total"] = total
    return [json.dumps(info, ensure_ascii=False), McpImage(data=buf.getvalue(), format="jpeg")]


@mcp.tool()
def file_link(paths: str, series: str = "") -> str:
    """작업실 안 그림·PDF 의 원격 링크(사용자가 브라우저로 여는 주소). paths 는 쉼표로 여러 개. 이 PC 의 파일이라 agent app 작업 공간의 도구(ShareFile·cp)로는 못 옮긴다."""
    out, busy = {}, {}
    for one in [x.strip() for x in re.split(r"[,\n]+", paths or "") if x.strip()]:
        try:
            f = _existing(one, series, fuzzy=True)
            out[one] = _link(f) or "(link unavailable: not under exports/ or series/, or no public URL)"
            # 그 회차에 아직 도는 작업(합성·그리기)이 있으면 알린다: 작은 모델이 마지막 job_status 가 running 인데 PDF 링크를 주며
            # '모든 작업이 끝났습니다' 라고 답했다(2026-10-26 추몽 v9 합성). 링크의 파일이 바뀌는 중일 수 있다
            ep = next((p for p in f.resolve().parents if p.parent.name == "episodes"), None)
            job = _running_job(ep) if ep else None
            if job:
                busy[job] = ep.name
        except (NotFound, ValueError) as e:
            out[one] = json.loads(str(e)) if str(e).startswith("{") else str(e)
    if busy:
        out["running_jobs"] = busy
        out["note"] = "이 회차에 아직 도는 작업이 있다: 끝났다고 답하기 전에 job_status 로 done 을 본다(링크의 파일이 아직 바뀌는 중일 수 있다)"
    return json.dumps(out, ensure_ascii=False)


SYNC_REVIEW_MAX = 3


@mcp.tool()
def review_panels(series: str, episode: str, ids: str = "", scene: str = "", folder: str = "", deep: bool = False) -> str:
    """그림 검수(크레딧 안 씀): 그림을 보는 판정 모델(지금 쓰는 판정 서버의 모델, state/vlm_url.txt)이 컷 그림과 그 컷 설명(인물·옷·구도·카메라·연기·대사)을 맞대어
    인원·좌우·시선·옷·표정·배경을 판정하고 '다시' 컷엔 고칠 점을 준다. ids(쉼표) 또는 scene(장소 이름)으로 고르고, 비우면 새로 그린 컷 전부.
    folder="vast" 면 GPU 서버로 그린 컷을 본다(다시 그려도 크레딧이 안 드니 vast_draw 로 시드만 바꿔 다시).
    3컷 이하는 바로 답하고, 더 많으면 작업으로 돌려 job_status 로 본다. '다시' 컷은 먼저 edit_panel 로 그 부분만 고치고, 구도가 통째로 틀렸을 때만 컷 설명을 고쳐 vast_draw.
    deep=True: 이미 그린 회차를 그리기 때와 같은 검사로 다시 본다(인물이 시트와 같은 모습인지·기준표 항목·팔손·사라진 사람 + 같은 장면 앞뒤 컷
    이어짐, 독자 눈 거르기까지). "인물·상황이 안 지켜진 컷을 찾아 줘" 같은 회차 전체 점검에 쓴다(컷당 약 15초, 늘 작업으로).
    ids·scene 을 비우면 인물이 나오는 컷 전부. 걸린 컷은 review/staging.json 에 남고 job_status 의 review 로 나온다."""
    ep = _episode(series, episode)
    sd = _series(series)
    sub = _folder(folder)
    if deep:
        only = ",".join(vision_review.pick_ids(ep, ",".join(_pids(ep, ids, "any")), scene)) if (ids or scene) else ""
        n = len(only.split(",")) if only else sum(1 for p in (_json(ep / "panels.json", []) or []) if p.get("chars"))
        _ensure_idle(sd)
        steps = [{"label": f"연출·생김새 검사 {n}컷", "cmd": [PY, str(VAST / "qa_fix.py"), str(sd), ep.name, "--folder", sub, "--record"]
                  + (["--only", only] if only else [])},
                 {"label": "장면 이어짐 점검", "cmd": [PY, str(TOOLS / "continuity_check.py"), str(ep), "--record"] + (["--only", only] if only else []),
                  "allow_fail": True}]
        job = _start_job("review", ep, steps)
        return (f"작업 시작 {job}: {n}컷 깊은 검수(약 {max(1, n * 15 // 60)}분). job_status('{job}') 의 review 에 사람 확인이 남은 컷이 나온다"
                "(끝났다고 말하지 말고 작업 번호와 시간만 알린다)")
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


def _existing(rel: str, series: str = "", want_dir: bool = False, fuzzy: bool = False) -> Path:
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
    # 이름 오타(jaeheon → jaehon): 같은 폴더에서 비슷한 이름을 후보로. 후보가 비어 에이전트가 같은 오타로 네 번 다시 불렀다
    # (2026-10-23 미사일 스튜디오 시험). 보기만 하는 도구(fuzzy=True)는 뚜렷이 하나면 그 파일을 쓴다
    if not found:
        try:
            parent = _inside("/".join(parts[:-1])) if len(parts) > 1 else ROOT
        except ValueError:
            parent = None
        if parent is not None and parent.is_dir():
            sib = [c.name for c in parent.iterdir() if c.is_dir() == want_dir and not c.name.startswith(".")]
            close = difflib.get_close_matches(name, sib, n=5, cutoff=0.7)
            ratio = [difflib.SequenceMatcher(None, name.lower(), c.lower()).ratio() for c in close]
            if fuzzy and close and ratio[0] >= 0.85 and (len(close) == 1 or ratio[1] < ratio[0] - 0.05):
                return parent / close[0]
            found = [parent / c for c in close]
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


# ── 저장소 다루기: 작품 폴더·exports·worklogs 안에서는 그림·폴더까지 복사하고 옮긴다(지우는 도구는 두지 않는다) ──
# 다른 버전·그림체 바꾸기처럼 일마다 도구를 만들지 않고, 저장소를 둘러보고(list_files·contact_sheet) 옮기고(move_file) 복사하는(copy_file)
# 일반 도구를 에이전트가 골라 쓴다(2026-10-23 사용자). 순서 예시는 HARNESS.md '저장소' 절.
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
STORE_RE = re.compile(r"(series/[^/_][^/]*/.+|exports/.+|worklogs/.+)")


def _storable(p: Path) -> bool:
    """그림·폴더까지 복사해 넣거나 옮길 수 있는 곳: 작품 폴더 안 · exports/ · worklogs/ (도구 코드·state·.git 은 안 된다)."""
    return bool(STORE_RE.fullmatch(p.resolve().relative_to(ROOT.resolve()).as_posix()))


TOP_DIRS = ("series/", "exports/", "worklogs/", "state/")


def _workspace_based(path: str) -> bool:
    """작업실 기준 경로인가(series/… · exports/… 로 시작). series 를 같이 줘도 이런 경로는 작품 폴더 기준으로 다시 붙이지 않는다:
    copy_file(dst='series/x/episodes/ep001_v2', series='x') 가 series/x/series/x/episodes/ep001_v2 를 만들었다(2026-10-24 스튜디오 시험)."""
    return path.replace("\\", "/").strip().lstrip("/").startswith(TOP_DIRS)


def _matches(src: str, series: str = "", fuzzy: bool = False) -> list:
    """src 에 * ? [ 가 있으면 작업실(series 를 주면 그 작품 폴더) 기준 패턴, 아니면 있는 파일 또는 폴더 하나(fuzzy: 뚜렷한 오타는 고쳐 찾음, 보기 도구만)."""
    norm = src.replace("\\", "/").strip().lstrip("/")
    if any(c in norm for c in "*?["):
        base = _series(series) if series and not _workspace_based(norm) else ROOT
        out = []
        for p in sorted(base.glob(norm)):
            try:
                out.append(_inside(p.resolve().relative_to(ROOT.resolve()).as_posix()))
            except ValueError:  # .git·.venv·models
                pass
        return out
    try:
        return [_existing(norm, series, fuzzy=fuzzy)]
    except NotFound as e:
        try:
            return [_existing(norm, series, want_dir=True, fuzzy=fuzzy)]
        except NotFound:
            raise e


def _busy_under(paths: list):
    """옮기려는 파일이 든 작품에 도는 작업이 있으면 그 작업 번호(그리는 중에 시트·참고 그림이 옮겨지면 그림이 틀어진다)."""
    roots = {q for p in paths for q in [p, *p.parents] if q.parent == (ROOT / "series").resolve()}
    for st in JOBS.glob("*.status.json"):
        s = _json(st, {})
        if s.get("state") != "running" or not _alive(s.get("pid")):
            continue
        d = Path((_json(st.with_name(st.name.replace(".status.json", ".json")), {}) or {}).get("episode_dir") or ".").resolve()
        if any(r == d or r in d.parents for r in roots):
            return st.name.replace(".status.json", "")
    return None


def _targets(items: list, dst: str, series: str, pattern: bool = False):
    """옮기거나 복사할 (원본, 대상) 짝. 패턴이거나 여러 개거나 dst 가 '/' 로 끝나거나 있는 폴더면 그 안에 같은 이름으로."""
    base = f"series/{_series(series).name}/" if series and not _workspace_based(dst) else ""
    d = _inside(base + dst.replace("\\", "/").strip().lstrip("/"))
    into = pattern or len(items) > 1 or dst.strip().endswith("/") or d.is_dir()
    return [(s, d / s.name if into else d) for s in items]


@mcp.tool()
def list_files(path: str = "series", series: str = "", pattern: str = "") -> str:
    """작업실 저장소 둘러보기: 폴더 안 파일·폴더 목록(크기·고친 시각). path 는 작업실 기준(예: series/<작품>/assets, exports/2026-10-23),
    series 를 주면 그 작품 폴더 기준. pattern 을 주면 그 폴더와 아래 폴더에서 맞는 것만(예: '*.pdf' 는 out/ 안까지, '<인물 id>_*'. 맨 위만 보려면 './*.png').

    작업실 구조: series/<작품>-hf/ (series.json 설정 · assets/ 인물 시트·locations/ 장소·vast_refs/ 참고 그림·표정 은행·_candidates/ 시안 ·
    episodes/<회차>/ 콘티·컷 설명·panels_hf/ 본편 그림·panels_hf_vast/ 다시 그린 그림·out/ 합성본) · exports/<날짜>/ 내보낸 PDF·epub · worklogs/ 작업 기록."""
    p = _existing(path, series, want_dir=True)
    pat = pattern.strip()
    # '/' 없는 패턴('*.pdf')은 아래 폴더까지 찾는다(_old·_rejected 같은 보관 폴더는 뺌): 맨 위만 보다가 out/ep001.pdf 를 못 찾고
    # '원본 PDF 가 아직 없다'고 답했다(2026-10-24 스튜디오 시험)
    if pat and "/" not in pat:
        items = sorted(c for c in p.rglob(pat) if not any(x.startswith("_") for x in c.relative_to(p).parts[:-1]))  # _candidates·_old 같은 곁 폴더는 뺌(들어가려면 path 로)
    else:
        items = sorted(p.glob(pat)) if pat else sorted(p.iterdir())
    out = []
    for c in items[:300]:
        name = c.relative_to(p).as_posix()
        if c.is_dir():
            out.append(f"[폴더] {name}/ ({sum(1 for _ in c.iterdir())}개)")
        else:
            st = c.stat()
            out.append(f"{name} ({max(1, st.st_size // 1024)}KB, {time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))})"
                       f"{' · 쓰기 가능' if _writable(c.resolve()) else ''}")
    more = f"\n(… {len(items)}개 중 300개. pattern 으로 좁힌다)" if len(items) > 300 else ""
    return ("\n".join(out) or "(비어 있음)") + more


@mcp.tool()
def read_file(path: str, offset: int = 0, max_chars: int = 12000, series: str = "") -> str:
    """작업실 안 글 파일 읽기(작업실 기준 경로, series 를 주면 그 작품 폴더 기준). 긴 파일은 offset 으로 이어 읽는다. 콘티(make_storyboard.py)·컷 설명(build_panels.py)·문서를 볼 때."""
    p = _existing(path, series, fuzzy=True)
    if p.suffix.lower() not in TEXT_EXT:
        return "글 파일(.py .json .md .txt .html)만 읽는다. 그림은 contact_sheet(paths=…) 로 본다"
    t = p.read_text(encoding="utf-8")
    part = t[offset:offset + max_chars]
    more = f"\n\n(… 전체 {len(t)}자 중 {offset}~{offset + len(part)}. 이어 읽으려면 offset={offset + len(part)})" if offset + len(part) < len(t) else ""
    return part + more


# 한 번에 약 8천 토큰까지만 쓰는 모델(agent app 의 Qwen)도 한 화 콘티(약 16KB)·컷 설명(약 22KB)을 쓸 수 있게 나눠 쓰기를 둔다(2026-10-16).
# 예전엔 start_file → append_file → check_file 세 도구였는데, 도구 수를 줄이려고 write_file 의 part 로 합쳤다(2026-10-22).
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


def _syntax_error(p: Path, t: str) -> str:
    """.py 는 파이썬 문법, .json 은 JSON. 통과면 빈 글, 아니면 줄 번호와 그 근처."""
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
    return ""


@mcp.tool()
def write_file(path: str, content: str, part: str = "whole") -> str:
    """작업실 글 파일 쓰기. 쓸 수 있는 곳: 작품 폴더(series/<작품>-hf/...)의 .py .json .md .txt, worklogs/, LESSONS.md, reader/index.html.
    도구(tools/)·그림·에셋·.git 은 쓸 수 없다. 덮어쓰기 전 원본은 state/mcp_backups/ 에 남긴다.
    part: whole(통째로 덮어쓰기, 문법이 틀리면 안 씀) · 한 번에 다 못 쓰는 긴 파일(한 화 콘티·컷 설명)은 first(첫 조각으로 새로 씀) → more(조각 붙이기, 여러 번)
    → last(마지막 조각을 붙이고 .py·.json 문법 검사). 조각은 4천 자 안팎, 줄바꿈은 content 에 넣는다. 일부만 고칠 땐 edit_file."""
    try:
        p = _guard(path, content)
    except ValueError as e:
        return str(e)
    if part not in ("whole", "first", "more", "last"):
        return "part 는 whole · first · more · last"
    if part == "whole":
        err = _syntax_error(p, content)
        if err:
            return err + "\n(쓰지 않았다)"
    if part in ("whole", "first"):
        note = _backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"썼다 {p.relative_to(ROOT)} ({len(content)}자){note}" + (". 이어서 part='more' 로 붙이고 마지막 조각은 part='last'" if part == "first" else "")
    if not p.exists():
        return "파일이 없다. part='first' 로 먼저 시작한다"
    with p.open("a", encoding="utf-8") as f:
        f.write(content)
    if part == "more":
        return f"붙였다 {p.relative_to(ROOT)} (지금 {p.stat().st_size}바이트)"
    t = p.read_text(encoding="utf-8")
    err = _syntax_error(p, t)
    return (err + "\n(edit_file 로 고친다)") if err else f"다 썼다, 문법 통과 {p.relative_to(ROOT)} ({len(t)}자, {t.count(chr(10)) + 1}줄)"


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
def copy_file(src: str, dst: str, overwrite: bool = False, series: str = "") -> str:
    """작업실 안 파일·폴더를 내용을 읽지 않고 그대로 복사한다: 긴 콘티·컷 설명, 그림, 참고 그림 폴더. src 는 파일·폴더·* 패턴,
    dst 는 작품 폴더 안·exports/·worklogs/. 여러 개면 dst 는 폴더. 회차를 다른 판으로 여는 건 new_episode(같은 화 번호면 콘티·컷 설명·그림을 옮긴다).

    series 를 주면 두 경로 모두 그 작품 폴더 기준. 이미 있는 파일은 overwrite=True 일 때만 덮고 원본은 state/mcp_backups/ 에 남긴다. 이미 있는 폴더는 덮지 않는다.
    read_file → write_file 로 옮기지 않는다(수만 자가 컨텍스트에 들어와 끊긴다)."""
    # 모델이 3만 자를 읽어 들여 그대로 다시 치면 컨텍스트가 무거워지고 한 번에 다 못 써 끊겼다(2026-10-22 미사일 스튜디오)
    try:
        pairs = _targets(_matches(src, series), dst, series, pattern=any(c in src for c in "*?["))
    except (NotFound, ValueError) as e:
        return str(e)
    if not pairs:
        return json.dumps({"error": "no_match", "given": src}, ensure_ascii=False)
    # 회차 폴더 통째 복사(1.1GB, 작업 기록·지난 판까지)로 새 판을 만들어 판 규칙(_v 번호·콘티 연결)을 건너뛰었다(2026-10-24 스튜디오 시험)
    whole = [s for s, _ in pairs if s.is_dir() and s.parent.name == "episodes"]
    if whole:
        return _fail("use_new_episode", given=src, reason="회차 폴더는 복사하지 않는다",
                     hint=f"새 판은 new_episode(series, '{whole[0].name.split('_')[0]}_v<번호>', template='{whole[0].name}')")
    rel = lambda p: p.resolve().relative_to(ROOT.resolve()).as_posix()  # noqa: E731
    bad = [rel(d) for _, d in pairs if not (_writable(d) or _storable(d))]
    if bad:
        return json.dumps({"error": "not_writable", "dst": bad[:5], "reason": "작품 폴더 안·exports/·worklogs/ 에만 복사한다"}, ensure_ascii=False)
    for s, d in pairs:
        if d.resolve() == s.resolve() or s in d.parents:
            return f"src 와 dst 가 같거나 dst 가 src 안이다: {rel(d)}"
        if d.exists() and (s.is_dir() or not overwrite):
            return f"이미 있다: {rel(d)}" + ("" if s.is_dir() else " (덮어쓰려면 overwrite=True)")
    notes, size = [], 0
    for s, d in pairs:
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d)
            size += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        else:
            notes.append(_backup(d))
            shutil.copy2(s, d)
            size += d.stat().st_size
    shown = ", ".join(f"{rel(s)} → {rel(d)}" for s, d in pairs[:3]) + (f" 외 {len(pairs) - 3}개" if len(pairs) > 3 else "")
    return f"복사했다 {len(pairs)}개({size // 1024}KB): {shown}{''.join(n for n in notes if n)[:300]}"


@mcp.tool()
def move_file(src: str, dst: str, series: str = "") -> str:
    """작업실 안 파일·폴더를 옮기거나 이름을 바꾼다. 지우는 도구는 없다: 안 쓰게 된 옛 판은 _old/(자동으로 안 지움), 시험용 결과물은 _tmp/(10일 뒤 저절로 지움)로 옮긴다. src 는 파일·폴더·* 패턴,
    dst 는 작품 폴더 안·exports/·worklogs/. 여러 개면 dst 는 폴더.

    series 를 주면 두 경로 모두 그 작품 폴더 기준. dst 에 같은 이름이 이미 있으면 하나도 옮기지 않는다(먼저 그것을 다른 곳으로 옮긴다).
    그 작품에 도는 작업이 있으면 옮기지 않는다(그리는 중에 시트·참고 그림이 바뀌면 그림이 틀어진다).
    예: 시안으로 인물 시트 바꾸기 → move_file('assets/<id>_sheet.png', 'assets/_old/', series='<작품>') 뒤
    move_file('assets/_candidates/<id>_sheet.png', 'assets/', series='<작품>'). 옛 표정 은행 치우기 → move_file('assets/vast_refs/<id>_*', 'assets/_old/refs/', series=…)."""
    try:
        pairs = _targets(_matches(src, series), dst, series, pattern=any(c in src for c in "*?["))
    except (NotFound, ValueError) as e:
        return str(e)
    if not pairs:
        return json.dumps({"error": "no_match", "given": src}, ensure_ascii=False)
    rel = lambda p: p.resolve().relative_to(ROOT.resolve()).as_posix()  # noqa: E731
    bad = [rel(p) for s, d in pairs for p in (s, d) if not _storable(p)]
    if bad:
        return json.dumps({"error": "not_movable", "paths": sorted(set(bad))[:5],
                           "reason": "작품 폴더 안·exports/·worklogs/ 안에서만 옮긴다(작품 폴더 자체·도구 코드·state 는 안 된다)"}, ensure_ascii=False)
    clash = [rel(d) for s, d in pairs if d.exists() or s in d.parents or d == s]
    if clash:
        return json.dumps({"error": "exists", "dst": clash[:5], "reason": "이미 있거나 자기 안으로 옮긴다. 있는 것을 먼저 다른 곳(_old/ 등)으로 옮긴다"},
                          ensure_ascii=False)
    busy = _busy_under([s for s, _ in pairs] + [d for _, d in pairs])
    if busy:
        return json.dumps({"error": "busy", "running_job": busy, "reason": "그 작품에 도는 작업이 있다. 끝난 뒤 옮긴다"}, ensure_ascii=False)
    shown = ", ".join(f"{rel(s)} → {rel(d)}" for s, d in pairs[:3]) + (f" 외 {len(pairs) - 3}개" if len(pairs) > 3 else "")
    for s, d in pairs:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
    parts = [d.resolve().relative_to(ROOT.resolve()).parts for _, d in pairs]
    binned = [d for (_, d), pt in zip(pairs, parts) if "_old" in pt or "_tmp" in pt]
    if binned:
        clean_old.note(binned)
    tmp = any("_tmp" in pt for pt in parts)
    return f"옮겼다 {len(pairs)}개: {shown}" + (" (_tmp 안의 것은 10일 뒤 저절로 지운다: tools/clean_old.py)" if tmp else "")


@mcp.tool()
def new_episode(series: str, episode: str, template: str = "") -> str:
    """새 회차 폴더를 연다. 다음 화(ep003)면 template 회차(기본 = 그 작품의 가장 최신 회차)의 틀만 옮기고 콘티 표는 비워 둔다 → set_storyboard·set_panels.
    같은 화의 다른 판(ep001_v4, template='ep001_v3' 처럼 바탕 판)이면 콘티·컷 설명·그림을 통째로 옮긴다 → 바꿀 컷만 고친다.
    이미 있는 회차 폴더는 건드리지 않는다."""
    s = _series(series)
    if not re.fullmatch(r"ep\d{3}(_v\d+)?", episode.strip()):
        return "회차 이름은 ep003, ep003_v2 같은 꼴이다"
    dst = s / "episodes" / episode.strip()
    if dst.exists():
        return f"이미 있다: {dst.relative_to(ROOT)}"
    eps = sorted(d.name for d in (s / "episodes").iterdir() if d.is_dir() and not d.name.endswith("_vast"))
    if not eps and not template.strip():  # 새 작품의 첫 회차: 빈 콘티 틀(제목·크레딧은 series.json 에서 읽음)
        dst.mkdir(parents=True)
        (dst / "make_storyboard.py").write_text(STORYBOARD_STUB, encoding="utf-8")
        (dst / "storyboard_spec.json").write_text(json.dumps({"cuts": []}, ensure_ascii=False, indent=1), encoding="utf-8")
        (dst / "build_panels.py").write_text(SPEC_STUB, encoding="utf-8")
        (dst / "panels_spec.json").write_text(json.dumps({"scenes": {}, "reuse": {}, "panels": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
        return (f"새 회차 {dst.relative_to(ROOT)} (첫 회차). read_doc('skill:storyboard') 를 읽고 set_storyboard 로 컷 목록을 넣은 뒤(몇 컷씩 나눠도 된다) "
                "set_panels 로 컷 설명 → prepare_episode")
    tpl = template.strip() or eps[-1]
    src = s / "episodes" / tpl
    if not src.is_dir():
        return f"틀 회차가 없다: {tpl}"
    # 같은 화의 다른 판(ep001 → ep001_v2): 콘티 표·컷 설명·그림까지 옮겨 바꿀 컷만 고치게 한다. 다음 화를 여는 것처럼 비워 두니
    # 판을 만들려던 에이전트가 콘티 표가 없다는 오류에 걸려 v2 를 끝내지 못했다(2026-10-24 스튜디오 시험 '다른 버전 만들기')
    same = re.match(r"ep\d{3}", tpl) and re.match(r"ep\d{3}", episode.strip()) and tpl[:5] == episode.strip()[:5]
    if same:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("_old", "_rejected", "out", "review", "_stage1", "*.log"))
        return (f"새 판 {dst.relative_to(ROOT)} ({tpl} 를 통째로 옮김: 콘티·컷 설명·그림). 바꿀 컷만 set_storyboard·set_panels 로 고치고(같은 id 는 덮음) "
                "prepare_episode → 그 컷만 vast_draw(ids) → compose → export(파일 이름 끝에 판 번호가 붙어 원본을 덮지 않는다)")
    dst.mkdir(parents=True)
    copied = []
    for n in ("make_storyboard.py", "trailer.json"):
        if (src / n).exists():
            t = (src / n).read_text(encoding="utf-8").replace(f"episodes/{tpl}", f"episodes/{episode}").replace(f"episodes\\{tpl}", f"episodes\\{episode}")
            (dst / n).write_text(t, encoding="utf-8")
            copied.append(n)
    if (src / "storyboard_spec.json").exists():  # 표 콘티 틀: 다음 화는 빈 표에서(옛 컷이 섞이지 않게)
        (dst / "storyboard_spec.json").write_text(json.dumps({"cuts": []}, ensure_ascii=False, indent=1), encoding="utf-8")
    # 컷 설명은 코드(build_panels.py)를 쓰지 않고 표(panels_spec.json)를 set_panels 로 채운다(2026-10-16 agent app 시험)
    (dst / "build_panels.py").write_text(SPEC_STUB, encoding="utf-8")
    (dst / "panels_spec.json").write_text(json.dumps({"scenes": {}, "reuse": {}, "panels": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
    return (f"새 회차 {dst.relative_to(ROOT)} (틀 {tpl}): {', '.join(copied)} 복사, 컷 설명 표(panels_spec.json) 준비. "
            "콘티는 set_storyboard(replace=True) 로 새 이야기의 컷 목록을 넣는다(파이썬을 쓰지 않는다. 틀 회차의 대사·컷 흐름은 read_file 로 본다). "
            "컷 설명은 set_panels 로 몇 컷씩 채운 뒤 prepare_episode")


@mcp.tool()
def new_series(spec: dict | str) -> str:
    """새 웹툰(작품)을 연다. spec 은 작품 설정 JSON(문자열):
    {"slug": "영어-소문자-하이픈", "title": "제목", "logline": "한 줄 소개", "genre": "장르·수위", "credit": "글: …  그림: …",
     "style": "English art style paragraph (line, coloring, light, faces, proportions)",
     "cast": {"<id>": {"ko": "이름", "en": "English Name (age)", "role": "역할", "who": "English appearance. 첫 마디는 무엇인지(사람이면 'Korean man, 30s', 사람이 아니면 'an elderly grey cat beastman' 처럼 종족부터), 이어서 age, height, build, hair or fur, eyes, face",
                       "main": "English default outfit(기본 옷차림 글. 주인공 여부가 아니다)", "exprs": "six English expressions, comma separated"}},
     그림 모델에 들어가는 칸(who·main·exprs·locations·style·people_rule·era_rule)은 영어로 쓴다(한국어면 되돌려 보낸다).
     "locations": {"<name>": "English description: place, time of day, light, furniture"},
     "era": "시대(한국어, 예: 2000년대 후반 서울)", "era_rule": "English period rule for every image (phones, cars, signs of that time)",
     "people_rule": "English rule for people (ages, proportions, restrained expressions)",
     "model": "uc" | "anima"(그리는 판, 비우면 uc. anima 는 글만 받아 인물 생김새를 who 글로 맞춘다, 편집은 uc)}
    직함·호칭은 cast 의 role 에 적는다(콘티 관문이 대사의 호칭과 맞춰 본다). era_rule 은 그리기 지시와 그림 검수에 들어간다.
    폴더 series/<slug>-hf 와 설정·프롬프트를 만든다(그림은 아직). 시트·장소 그림을 먼저 보려면 draw_candidates(slug).
    다음: new_episode(slug, "ep001") → 콘티 → set_panels → make_episode(에셋 점검에서 없는 인물 시트·장소 시안을 GPU 서버로 그리고 멈춤,
    사용자가 고르면 accept_new=True)."""
    try:
        d = json.loads(spec) if isinstance(spec, str) else spec
        made = ns.create(d)
    except SystemExit as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"실패: {type(e).__name__}: {e}"
    return (f"새 작품 {made.relative_to(ROOT)}: 인물 {len(d['cast'])}명 · 장소 {len(d.get('locations') or {})}곳 설정과 프롬프트를 만들었다. "
            f"다음: 인물 시트·장소 그림을 먼저 보려면 draw_candidates('{d['slug']}'), 바로 이야기로 가면 new_episode('{d['slug']}', 'ep001') "
            "(첫 make_episode 가 없는 시안을 그리고 멈춘다)")


@mcp.tool()
def add_to_series(series: str, kind: str, key: str, value: str) -> str:
    """작품에 인물이나 장소를 더하거나 바꾼다(새 회차에 처음 나오는 사람·곳, 생김새·옷 고치기). kind = cast | location | style | people_rule, key = 영어 id.
    cast 면 value 는 JSON {"ko","en","role","who","main","exprs"}(new_series 와 같은 칸, 있는 id 면 통째로 바뀜), location 이면 영어 설명 한 문장,
    style 이면 영어 그림체 문단(style_compare 로 고른 것), people_rule 이면 사람 규칙 영어 문장(누가 미남·미녀이고 나머지는 평범하게 등). style·people_rule 은 key 를 안 쓴다.
    그림은 make_episode 의 에셋 점검이 시안으로 그린다(이미 있는 시트·장소를 바꾸려면 redo_candidate). new_series 로 연 작품만(series.json 이 있는 작품)."""
    try:
        s = _series(series)
        if not (s / "series.json").exists():
            return f"{s.name} 은 작품 설정(series.json)이 없는 옛 작품이다. 그 작품의 make_sheet_prompts.py·make_location_prompts.py 에 한 줄 넣는다"
        if kind not in ("cast", "location", "style", "people_rule"):
            return "kind 는 cast · location · style · people_rule"
        if kind in ("style", "people_rule") and len(value.strip()) < 20:
            return f"{kind} 는 영어 문장으로(20자 넘게)"
        files = ns.add(s, kind, key, json.loads(value) if kind == "cast" else value)
    except SystemExit as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"실패: {type(e).__name__}: {e}"
    if kind in ("style", "people_rule"):
        return (f"{s.name} 의 {kind} 를 바꿨다(series.json · prompts {len(files)}개). 이미 그린 시트·장소는 그대로다: "
                "redo_candidate 로 새 시안 → contact_sheet 로 고르기 → move_file 로 바꿔 넣기 → vast_make_assets")
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
    """그릴 때 고를 수 있는 판(모델)의 목록. GPU 서버 ComfyUI 에 저장된 웹툰 워크플로우(mcp_use_webtoon_vertical_<판>)에서 읽으니, 서버에서 워크플로우를 저장하거나 지우면
    목록이 따라 바뀐다. 판마다: label(설명) · use_for(어디에 맞는지·한계, 판을 고를 때 읽는다) · refs(참고 그림으로 얼굴·장소를 맞추나, 아니면 글로만) ·
    roles(생성·고치기·에셋·시안 역할마다 쓰는 워크플로우, own_roles 밖은 기본 판의 것) · refine(2차 다듬기를 지원하나) · models(쓰는 모델 파일) · ready(모델 파일이 서버에 다 있나) · default.
    series 를 주면 그 작품이 쓰는 판(current)도 알려 준다."""
    m = _draw_models(refresh=True)
    cur = None
    if series:
        sys.path.insert(0, str(VAST))
        import saved_workflow
        cur = saved_workflow.pick(_series(series))[0]
    out = [{"id": k, "label": vv["label"], "use_for": vv["use_for"], "refs": vv["refs"], "workflow": vv["gen"], "roles": vv["roles"], "own_roles": vv.get("own_roles", []),
            "models": vv["models"], "ready": not vv["missing"], "missing": vv["missing"], "default": vv["default"],
            "refine": bool((vv.get("meta") or {}).get("refine")), **({"current": k == cur} if cur else {})}
           for k, vv in sorted(m.items())]
    return json.dumps({"draw_models": out}, ensure_ascii=False)


@mcp.tool()
def draw_candidates(series: str, names: str = "") -> str:
    """새 작품의 인물 시트·장소 시안을 처음 그린다(크레딧 0, GPU 서버, 한 장에 약 1~3분). 콘티 전에 "시트랑 장소 그림 보여 줘" 할 때 쓴다.
    names: 인물 id·장소 이름을 쉼표로(비우면 assets 에도 시안에도 없는 것 전부). 결과는 assets/_candidates/(<id>_sheet.png · locations/<이름>.png).
    끝나면 contact_sheet(paths='series/<작품>-hf/assets/_candidates/*') 로 보여 주고, 사용자가 고르면 move_file 로 assets/ 에 넣거나
    make_episode(accept_new=True) 가 넣는다. 마음에 안 드는 것은 redo_candidate. 생김새를 바꾸려면 먼저 add_to_series."""
    try:
        s = _series(series)
    except ValueError as e:
        return str(e)
    sj = _json(s / "series.json", {}) or {}
    cast_ids = list((sj.get("cast") or {}).keys()) or [k for k in (_json(s / "cast.json", {}) or {}) if not k.startswith("_")]
    locs = list((sj.get("locations") or {}).keys())
    want = [x.strip() for x in names.split(",") if x.strip()]
    unknown = [x for x in want if x not in cast_ids and x not in locs]
    if unknown:
        return _fail("unknown_name", unknown=unknown, cast=cast_ids, locations=locs)
    todo = []
    for kind, ids in (("sheet", cast_ids), ("location", locs)):
        for n in ids:
            if want and n not in want:
                continue
            have = s / "assets" / (f"locations/{n}.png" if kind == "location" else f"{n}_sheet.png")
            cand = s / "assets" / "_candidates" / (f"locations/{n}.png" if kind == "location" else f"{n}_sheet.png")
            if not want and (have.exists() or cand.exists()):
                continue
            if not (s / "prompts" / (f"loc_{n}.txt" if kind == "location" else f"sheet_{n}.txt")).exists():
                return _fail("no_prompt", name=n, hint="add_to_series 로 설정을 넣으면 프롬프트가 생긴다")
            todo.append((kind, n))
    if not todo:
        return "그릴 시안이 없다(모두 assets 나 _candidates 에 있다). 다시 그리려면 redo_candidate, 이름을 콕 집으려면 names"
    _ensure_idle(s)
    steps = [{"label": f"시안 그리기({k} {n})", "cmd": [PY, str(VAST / "new_assets.py"), str(s), k, n]} for k, n in todo]
    job = _start_job("draw_candidates", s, steps)
    return (f"작업 시작 {job}: 시안 {len(todo)}장({', '.join(n for _, n in todo)}), 약 {len(todo) * 2}분. job_status('{job}', wait=50) → "
            f"끝나면 contact_sheet(paths='{(s / 'assets' / '_candidates').relative_to(ROOT).as_posix()}/*')")


@mcp.tool()
def redo_candidate(series: str, kind: str, name: str) -> str:
    """사용자가 마음에 안 들어 한 새 인물 시트·장소 시안을 다른 시드로 다시 그린다(크레딧 0, GPU 서버, 약 2~4분). kind = sheet | location.
    지난 시안은 지우지 않고 assets/_candidates/_rejected/ 로 옮긴다. 생김새·옷을 바꾸려면 먼저 add_to_series 로 설정을 고친다."""
    try:
        s = _series(series)
    except ValueError as e:
        return str(e)
    if kind not in ("sheet", "location"):
        return "kind 는 sheet 또는 location"
    cand = s / "assets" / "_candidates" / (f"locations/{name}.png" if kind == "location" else f"{name}_sheet.png")
    _ensure_idle(s)
    # 작업이 실제로 시작된 뒤에 지난 시안을 옮긴다(먼저 옮기면 repeated_job 으로 막혔을 때 시안만 사라졌다, 2026-10-22).
    # 새 시안은 그리는 데 1분 넘게 걸려 마지막에 저장되니, 시작 직후 옮겨도 겹치지 않는다
    job = _start_job("redo_candidate", s, [{"label": f"시안 다시 그리기({kind} {name})", "cmd": [PY, str(VAST / "new_assets.py"), str(s), kind, name]}])
    if cand.exists():
        rej = s / "assets" / "_candidates" / "_rejected"
        rej.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cand), str(rej / f"{cand.stem}_{time.strftime('%m%d%H%M%S')}.png"))
    return f"작업 시작 {job}: 시안 {cand.relative_to(ROOT)} 를 다시 그린다(약 2~4분). job_status('{job}', wait=50)"


ASPECTS = ("9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9")  # vast/comfy_panel.SIZES


def _named_items(value, text_keys=("text", "prompt", "description", "style"), name_keys=("name", "label", "title", "id")) -> dict:
    """{이름: 값} 으로 맞춘다. JSON 글·객체·목록([{label, prompt}] · ["글", …]) 어느 꼴로 와도 받는다: 작은 모델이 JSON 안의 JSON 글을
    이스케이프하다 깨뜨리거나 다른 키 이름(label·prompt)을 썼다(2026-10-23 미사일 스튜디오 시험, style_compare 를 세 번 못 부름)."""
    if isinstance(value, str):
        value = json.loads(value) if value.strip()[:1] in "[{" else {"1": value}
    if isinstance(value, list):
        out = {}
        for i, x in enumerate(value, 1):
            if isinstance(x, dict):
                k = next((str(x[n]) for n in name_keys if x.get(n)), str(i))
                out[k] = x
            else:
                out[str(i)] = x
        value = out
    if not isinstance(value, dict):
        raise ValueError("객체나 목록이 아니다")
    return {str(k): (next((v[n] for n in text_keys if v.get(n)), v) if isinstance(v, dict) and "aspect" not in v else v) for k, v in value.items()}


@mcp.tool()
def style_compare(name: str, styles: dict | list | str, scenes: dict | list | str, series: str = "", people_rule: str = "", seed: int = -1,
                  full: bool = False) -> str:
    """화풍 후보 비교표: 그림체 여러 개 x 장면 몇 개를 같은 시드로 그려, 줄마다 그림체 이름표를 붙인 한 장(compare.jpg)을 만든다(크레딧 0, GPU 서버, 장당 약 10~20초).
    새 작품의 그림체를 정할 때, 사용자가 "그림체를 바꾸자·후보를 골라 보고해" 할 때 쓴다. 결과는 exports/<오늘>/화풍시안_<name>/ 에 남는다.
    styles: {"이름": "영어 그림체 문단" 또는 "series:<작품>"(그 작품 그림체 그대로, 지금 그림체를 후보에 넣을 때)}, 2~8개. 객체·목록·JSON 글 다 된다.
    scenes: {"이름": "영어 장면 글"} 또는 {"이름": {"text": "...", "aspect": "3:2"}} 또는 [{"label": "...", "prompt": "..."}], 1~4개(인물 얼굴이 보이는 장면 + 배경 장면 하나가 고르기 좋다).
    series 를 주면 그 작품의 사람 규칙(people_rule)을 쓴다. full=True 면 터보 없이 40스텝(고른 그림체를 품질로 확인할 때, 장당 약 1분).
    끝나면 job_status 의 links 에 compare.jpg, contact_sheet(paths='<그 폴더>/compare.jpg') 로 크게 본다. 고른 그림체는 add_to_series(작품, 'style', 'style', 문단)."""
    if not re.fullmatch(r"[\w가-힣 ·-]{1,40}", name.strip()):
        return _fail("bad_name", given=name, hint="이름은 40자 안의 글자·숫자·공백·-(폴더 이름이 된다)")
    try:
        st = _named_items(styles)
        sc = _named_items(scenes, text_keys=("text", "prompt", "description", "scene"))
    except ValueError as e:
        return _fail("bad_json", detail=str(e), hint='styles = {"이름": "English style paragraph"}, scenes = {"이름": {"text": "...", "aspect": "3:2"}}')
    if not isinstance(st, dict) or not 2 <= len(st) <= 8:
        # 쉼표로 이은 이름 한 줄("watercolor, anime-soft")은 그림체 하나로 읽힌다(그림체 문단에도 쉼표가 있어 나누지 않는다, 2026-10-24 등불 상점 시험)
        return _fail("bad_styles", got=len(st) if isinstance(st, dict) else 0,
                     hint='styles 는 그림체 2~8개: {"수채": "soft watercolor ... (영어 문단)", "애니": "clean anime ..."}. 쉼표로 이은 이름 한 줄은 하나로 읽힌다')
    if not isinstance(sc, dict) or not 1 <= len(sc) <= 4:
        return _fail("bad_scenes", hint="scenes 는 장면 1~4개의 JSON 객체")
    probe_styles = {}
    for k, v in st.items():
        v = str(v).strip()
        if k.isdigit():  # 이름 없이 목록으로 받은 후보: 비교표 줄 이름이 '1·2·3' 이라 무슨 그림체인지 몰랐다 → 글 앞 몇 낱말
            k = f"{k} " + (v[7:].strip() if v.startswith("series:") else re.sub(r"[^\w가-힣 -]", "", " ".join(v.split()[:4])))[:32]
        if v.startswith("series:"):
            try:
                sd = _series(v[7:].strip())
            except ValueError as e:
                return _fail("unknown_series", given=v, detail=str(e))
            sp = next((sd / "prompts" / f for f in ("style.txt", "style_v2.txt") if (sd / "prompts" / f).exists()), None)
            if sp is None:
                return _fail("no_style", given=v, hint=f"{sd.name}/prompts/style.txt 가 없다")
            v = re.sub(r"^ART STYLE[^:]*:\s*", "", sp.read_text(encoding="utf-8").strip())
        if len(v) < 20:
            return _fail("short_style", given=k, hint="그림체는 영어 문단으로(선·색·빛·얼굴·비율)")
        probe_styles[k] = v
    probe_scenes = {}
    for k, v in sc.items():
        v = {"text": v} if isinstance(v, str) else dict(v)
        v.setdefault("text", next((v[n] for n in ("prompt", "description", "scene") if v.get(n)), ""))
        if len(str(v.get("text", "")).strip()) < 20:
            return _fail("short_scene", given=k, hint="장면은 영어로: 누가·어디서·무엇을·빛")
        if v.setdefault("aspect", "3:2") not in ASPECTS:
            return _fail("bad_aspect", given=v["aspect"], allowed=list(ASPECTS))
        probe_scenes[k] = {"text": str(v["text"]).strip(), "aspect": v["aspect"]}
    rule = people_rule.strip()
    if not rule and series:
        try:
            rule = str(_json(_series(series) / "series.json", {}).get("people_rule") or "")
        except ValueError as e:
            return _fail("unknown_series", given=series, detail=str(e))
    probe = {"styles": probe_styles, "scenes": probe_scenes, "seed": seed if seed >= 0 else 20261018, "full": bool(full)}
    if rule:
        probe["people_rule"] = rule
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / f"화풍시안_{name.strip().replace(' ', '_')}"
    out.mkdir(parents=True, exist_ok=True)
    body = json.dumps(probe, ensure_ascii=False, indent=1)
    import hashlib
    pf = out / f"probe_{hashlib.sha1(body.encode()).hexdigest()[:8]}.json"  # 내용이 바뀌면 다른 작업(같은 이름으로 후보를 바꿔 다시 그릴 때)
    pf.write_text(body, encoding="utf-8")
    n = len(probe_styles) * len(probe_scenes)
    job = _start_job("style_compare", out, [{"label": f"화풍 비교 {len(probe_styles)}x{len(probe_scenes)}", "cmd": [PY, str(TOOLS / "style_probe.py"), str(pf), str(out)]}])
    rel = out.relative_to(ROOT).as_posix()
    return (f"작업 시작 {job}: 그림체 {len(probe_styles)} x 장면 {len(probe_scenes)} = {n}장(약 {max(1, n * (60 if full else 15) // 60)}분). "
            f"job_status('{job}', wait=50) → links 의 compare.jpg, contact_sheet(paths='{rel}/compare.jpg')")


STORYBOARD_STUB = '''"""콘티는 storyboard_spec.json(MCP set_storyboard 로 채움). 이 파일은 공용 변환기 tools/storyboard_from_spec.py 를 부른다."""
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.argv = ["storyboard_from_spec.py", str(HERE)]
runpy.run_path(str(HERE.parents[3] / "tools" / "storyboard_from_spec.py"), run_name="__main__")
'''

SPEC_STUB = '''"""컷 설명은 panels_spec.json(MCP set_panels 로 채움). 이 파일은 공용 변환기 tools/panels_from_spec.py 를 부른다."""
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.argv = ["panels_from_spec.py", str(HERE)]
runpy.run_path(str(HERE.parents[3] / "tools" / "panels_from_spec.py"), run_name="__main__")
'''


@mcp.tool()
def set_storyboard(series: str, episode: str, cuts: list | dict | str, title_card: str = "", replace: bool = False) -> str:
    """콘티(컷 목록)를 넣는다. 파이썬을 쓰지 않는다: 이 표(storyboard_spec.json)에서 storyboard.json 을 만든다. 몇 컷씩 나눠 불러도 된다(같은 id 는 그 자리에서 덮고,
    새 id 는 뒤에 붙는다. 처음부터 다시 쓰려면 replace=True). 표지(제목 카드)는 없으면 맨 앞에 저절로 붙는다.
    cuts: [{"id": "p001", "shot": "wide|medium|close|full|insert", "scene": "<장소 이름 또는 none>", "chars": ["<인물 id>"],
            "dialogue": [{"type": "speech|thought|narration|monologue|sfx", "text": "대사", "who": "<말하는 인물 id>"}], "gap_after": 400(장면 끝)}]
      대사 한 줄은 ["speech", "대사", "top-left", "<인물 id>"] 처럼 목록이어도 된다. 장면마다 넓은 컷 하나, 두 사람 대화는 who 를 꼭 쓴다.
    규칙·노하우는 read_doc("skill:storyboard"). 다 넣으면 set_panels 로 컷 설명 → prepare_episode(관문)."""
    try:
        ep = _episode(series, episode)
    except ValueError as e:
        return str(e)
    sd = _series(series)
    cast = [k for k in (_json(sd / "cast.json", {}) or {}) if not k.startswith("_")]
    locs = {p.stem for p in (sd / "assets" / "locations").glob("*.png")} | set((_json(sd / "series.json", {}) or {}).get("locations") or {})
    try:
        add = json.loads(cuts) if isinstance(cuts, str) else cuts
    except ValueError as e:  # 예시는 그 작품의 실제 장소·인물 키로(작품을 박지 않는다)
        ex = {"id": "p001", "shot": "wide", "scene": min(locs) if locs else "none", "chars": cast[:1], "dialogue": []}
        return _fail("bad_json", detail=str(e), hint="cuts = " + json.dumps([ex], ensure_ascii=False))
    if isinstance(add, dict):  # {"p001": {...}} 꼴
        add = [{"id": k, **v} for k, v in add.items() if isinstance(v, dict)]
    if not isinstance(add, list) or not all(isinstance(c, dict) for c in add):
        return _fail("bad_cuts", hint="cuts 는 컷 객체의 목록")
    notes, clean = [], []
    for k, c in enumerate(add, 1):
        cid = str(c.get("id") or "").strip()
        if not cid:
            return _fail("missing_id", index=k, hint="컷마다 id(p001 같은)")
        c = {**c, "id": (_ids(cid) or [cid])[0]}
        bad = [x for x in (c.get("chars") or c.get("characters") or []) if x not in cast]
        if bad:
            return _fail("unknown_character", panel=c["id"], given=bad, candidates=sorted(cast), hint="새 인물은 add_to_series(작품, 'cast', id, …) 먼저")
        sc = c.get("scene") or c.get("location") or "none"
        if sc != "none" and sc not in locs:
            notes.append(f"{c['id']}: 장소 '{sc}' 가 작품 설정에 없다(add_to_series(작품, 'location', '{sc}', 영어 설명) 하거나 있는 장소를 쓴다: "
                         f"{', '.join(sorted(locs)) or '없음'})")
        if "—" in json.dumps(c, ensure_ascii=False):
            return "em dash(—) 는 쓰지 않는다. 쉼표·마침표로 바꿔 다시"
        clean.append(c)
    f = ep / "storyboard_spec.json"
    spec = {} if replace else (_json(f, {}) or {})
    if not replace and not f.exists() and (ep / "storyboard.json").exists():
        # 코드로 된 콘티(make_storyboard.py)를 처음 표로 바꿀 때: 지금 콘티 컷을 표에 먼저 옮겨 둔다. 안 그러면 넘겨준 몇 컷만 남고
        # 나머지 컷이 콘티에서 사라진다(옛 회차·copy_file 로 연 버전에서 컷 몇 개만 고칠 때)
        old_board = _json(ep / "storyboard.json", {}) or {}
        spec = {"cuts": [{**p_, "chars": p_.get("characters", [])} for p_ in old_board.get("panels", [])],
                **({"title_card": old_board["title_card"]} if old_board.get("title_card") else {})}
    have = spec.get("cuts") or []
    pos = {c["id"]: i for i, c in enumerate(have)}
    for c in clean:
        if c["id"] in pos:
            have[pos[c["id"]]] = c
        else:
            pos[c["id"]] = len(have)
            have.append(c)
    spec["cuts"] = have
    if title_card.strip():
        spec["title_card"] = title_card.strip()
    f.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    mk = ep / "make_storyboard.py"
    if not mk.exists() or mk.read_text(encoding="utf-8") != STORYBOARD_STUB:  # 코드로 된 콘티가 있으면 백업하고 표를 쓰게 바꾼다
        if mk.exists():
            bk = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / mk.relative_to(ROOT.resolve())
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mk, bk)
            notes.append(f"이 회차의 make_storyboard.py 는 표를 쓰게 바꿨다(원본 백업 {bk.relative_to(ROOT)})")
        mk.write_text(STORYBOARD_STUB, encoding="utf-8")
    out = _run([PY, str(TOOLS / "storyboard_from_spec.py"), str(ep)], timeout=25)
    board = _json(ep / "storyboard.json", {}) or {}
    spec_p = (_json(ep / "panels_spec.json", {}) or {}).get("panels") or {}
    need = [p["id"] for p in board.get("panels", []) if not p.get("composite") and p["id"] not in spec_p]
    nl = "\n"
    return (f"콘티 {len(clean)}컷 넣음(지금 {len(have)}컷). {out.strip()[-200:]}"
            + (nl + nl.join(notes) if notes else "")
            + (f"{nl}다음: set_panels 로 컷 설명(아직 없는 컷: {', '.join(need[:30])}{' …' if len(need) > 30 else ''}) → prepare_episode." if need
               else f"{nl}다음: prepare_episode 로 관문 채점을 받는다."))


@mcp.tool()
def set_panels(series: str, episode: str, panels: dict | list | str = "", scenes: dict | str = "", reuse: dict | str = "") -> str:
    """컷 설명 표(panels_spec.json)를 채운다. 코드를 쓰지 않고 컷 몇 개씩(5~8컷) 나눠 여러 번 불러도 된다(같은 번호는 덮어씀).
    panels: 객체(또는 JSON 글) {"p003": {"chars": ["<인물 id>"], "outfit": {"<인물 id>": "white dress shirt, navy slacks"}, "location": "<장소 이름>",
      "shot": "wide shot", "action": "영어로 이 컷에서 일어나는 일", "acting": "영어로 표정·눈썹·입·몸짓", "camera": "대화 컷이면 어깨 너머 숏 설명"}}
      chars 는 cast.json 의 인물 id, location 은 assets/locations 의 장소 이름. 인물이 있는 컷은 acting 을 꼭 쓴다.
      넓은 컷은 양옆 배치, 대화 컷은 듣는 사람 어깨 너머 숏(camera), 혼자 클로즈업은 한 사람만, 화면 좌우는 장면 내내 같게.
    scenes: JSON {"<장소 이름>": "그 장소의 장면 배치 한 문장(누가 화면 왼쪽·오른쪽에서 어디를 보나)"}. 같은 장소가 한 화 안에서 배치가 바뀐 뒤 또 나오면
      scenes 에 {"<장소 이름>_2": "..."} 를 더하고 그 컷에 "scene": "<장소 이름>_2" 를 적는다. reuse: JSON {"p010": "episodes/ep001/panels_hf/p012.png"}.
    끝나면 prepare_episode 로 panels.json 을 만들고 '빠진 컷'·'연기 없는 인물 컷'이 비었는지 본다."""
    ep = _episode(series, episode)
    sd = _series(series)
    f = ep / "panels_spec.json"
    spec = _json(f, {}) or {}
    spec.setdefault("scenes", {}), spec.setdefault("reuse", {}), spec.setdefault("panels", {})
    notes = []
    try:  # JSON 글이든 객체든 받는다(작은 모델은 JSON 안의 JSON 글 이스케이프에서 자주 깨진다)
        add_p, add_s, add_r = ((json.loads(v) if v.strip() else {}) if isinstance(v, str) else (v or {}) for v in (panels, scenes, reuse))
    except ValueError as e:
        # 큰 컷 설명을 JSON 글로 넣다 이스케이프가 깨져 같은 오류를 네 번 되풀이했다(2026-10-24 스튜디오 시험) → 객체로 넘기는 길을 알린다
        return (f"JSON 오류: {e}. panels 는 JSON 글 대신 객체 그대로 넘기면 이스케이프가 필요 없다(예: panels={{\"p001\": {{\"chars\": [...], …}}}}). "
                "한 번에 2~3컷씩 나눠 넣어도 된다")
    if isinstance(add_p, list):  # [{"id": "p003", ...}] 도 받는다
        no_id = [i for i, x in enumerate(add_p, 1) if not (isinstance(x, dict) and x.get("id"))]
        if no_id:  # 예전엔 id 없는 항목을 말없이 버려 '0개 넣음'만 돌려줬다
            return _fail("missing_id", items=no_id, hint='목록이면 항목마다 "id"(p001 같은)를 넣거나, {"p001": {...}, "p002": {...}} 객체로 넘긴다')
        add_p = {x.pop("id"): x for x in add_p}
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


# ── 준비 ─────────────────────────────────────────────────────
@mcp.tool()
def prepare_episode(series: str, episode: str) -> str:
    """회차의 make_storyboard.py → build_panels.py 를 돌려 storyboard.json·panels.json 을 다시 만들고, 관문(격리 judge)으로 콘티·컷 설명을 채점한다(크레딧 안 씀).
    관문 기준표는 skills/storyboard/judge.json · skills/panel-spec/judge.json(하네스 s08 judge 와 같은 꼴, 합격 0.8, 꼭 지킬 기준은 하나라도 어기면 걸림).
    긴 한 화는 몇 분 걸려서 작업으로 돈다: 40초 안에 끝나면 그 자리에서 job_status 모양의 결과(gates = 관문 통과 여부, log_tail = 걸린 곳)를 주고,
    아니면 state running 과 작업 번호를 주니 job_status(wait=50) 로 이어 본다. 걸리면 결과의 '고칠 점'대로 make_storyboard.py(edit_file)나 컷 설명(set_panels)을 고치고 다시 부른다."""
    ep = _episode(series, episode)
    steps = []
    for name, label in (("make_storyboard.py", "콘티(make_storyboard.py)"), ("build_panels.py", "컷 설명(build_panels.py)")):
        if (ep / name).exists():
            steps.append({"label": label, "cmd": [PY, str(ep / name)]})
        else:
            return json.dumps({"error": "missing_file", "file": name, "hint": "new_episode 로 앞 회차를 본떠 먼저 만든다"}, ensure_ascii=False)
    steps.append({"label": "관문(콘티·컷 설명 채점)", "cmd": [PY, str(TOOLS / "gates.py"), str(ep), "all"], "allow_fail": True})
    job = _start_job("prepare", ep, steps)
    return job_status(job, wait=40, tail_lines=80)  # 옛 _wait_job(job_wait 와 함께 job_status 로 합침, 2026-10-22)


# ── vast.ai 공용 GPU(크레딧 0) ───────────────────────────────
# 회사 공용 vast.ai 의 ComfyUI 로 그린다(판은 saved_workflow.MODELS). 크레딧은 안 들지만 공용 장비라 한 번에 VAST_MAX 컷까지.
# 인스턴스 만들기·끄기·지우기 도구는 두지 않는다(vast/README.md).
VAST = ROOT / "vast"
VAST_MAX = 80



@mcp.tool()
def vast_draw(series: str, episode: str, ids: str = "", turbo: bool = True, expr: bool = True, aspect: str = "", seed: int = -1, fix: bool = True,
              check: bool = True, model: str = "", refine: bool = False, continuity: bool = True, continue_closeups: bool = False,
              restyle: bool = False) -> str:
    """컷을 GPU 서버(ComfyUI)로 그린다(크레딧 0, 기본 작화 도구). 결과는 panels_hf_vast/ 에 쌓이고 본편(panels_hf)은 안 바뀐다: 한 컷을 다시 그려
    보여 줄 때도 이것(ids='p022', seed 를 바꾸면 다른 그림) → compare_panel(what='redraw') 로 본편과 나란히 보여 주고, 사용자가 고르면 adopt_panel.
    model: 비우면 컷마다 판을 자동으로 고른다(vast/router.py: 작품 그림체와 같은 판, 인물 컷은 참고 그림을 받는 판,
    그림체 LoRA 를 학습한 판. 컷마다 고른 판과 이유가 작업 로그에 남는다). 사용자가 판을 말했을 때만 판 이름(draw_models 목록)을 준다. 같은 panels.json 을 쓴다.
    refine=True 면 2차 다듬기(그림을 1.3배로 키워 낮은 강도로 한 번 더 그린다): 얼굴 선·머리카락·옷·배경 질감이 또렷해지고 구도·표정은 그대로. 컷당 시간이 약 2배라 기본은 끄고, 클로즈업·표지·중요한 컷에 쓴다.
    ids: 쉼표로 컷 번호(비우면 vast 그림이 아직 없는 컷 전부). turbo=True 면 8스텝(컷당 약 30초), False 면 40스텝(약 2분).
    expr=True 면 연기에 맞는 표정 칸을 얼굴 참고로 넣는다(연기가 세짐). aspect 를 "2:3"·"9:16" 으로 주면 세로 컷.
    fix=True(기본)면 그린 뒤 그림 보는 Qwen 이 남는 사람(중복·낯선 사람)을 찾아 편집으로 지운다(원본은 panels_hf_vast/_stage1/).
    check=True(기본)면 그림 검수로 시점·좌우·시선·표정·배경을 보고 표정·옷은 편집, 구도가 틀리면 시드를 바꿔 한 번 더 그린다(컷당 +20~60초).
    fix·check 는 끄지 않는다: 끄면 남는 사람·글에 없는 사람·인물이 딴 모습으로 나온 컷을 아무도 못 잡는다(둘 다 끄고 다시 그렸더니
    글에 없는 사람이 나왔다). 사용자가 "검수 없이 빨리 초안만"이라고 했을 때만 끈다.
    continuity=True(기본)면 같은 장면의 앞 컷을 쓴다: 같은 인물 묶음의 넓은·미디엄 컷은 앞 컷의 선으로 사람 자리·테이블 배치를 고정한다
    (2026-10-20 시험: 고정 없이 두 사람 좌우가 바뀌었다). 장소가 바뀌거나 시간 자막('수요일')·옷이 바뀐 컷에서는 쓰지 않는다. 어느 앞 컷을 썼는지 작업 로그에 남는다.
    continue_closeups=True 면 같은 사람 연속 클로즈업을 앞 컷에서 잇기도 쓴다(얼굴·배경은 잘 이어지나 얼굴 얼룩·옷 바뀜이 섞여 기본은 끔).
    restyle=True 면 그린 뒤 그 작품 Restyle LoRA(restyle 도구로 학습한 것)로 그림체를 마무리해 panels_hf_vast_restyle/ 에 따로 둔다(컷당 +약 40초).
    견줘 볼 땐 contact_sheet(folder='vast') 와 contact_sheet(folder='vast_restyle').
    그래도 이상하면 seed 만 바꿔 그 컷을 다시 부른다. 본편으로 쓰려면 사용자가 고른 컷만 adopt_panel."""
    ep = _episode(series, episode)
    sd = _series(series)
    pj = [p["id"] for p in _panels_json(ep)]
    want = _pids(ep, ids) or [i for i in pj if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    if not want:
        return "그릴 컷이 없다(GPU 서버 그림이 다 있다. 다시 그리려면 ids 를 준다)"
    if len(want) > VAST_MAX:
        return f"한 번에 {VAST_MAX}컷까지다({len(want)}컷). 나눠서 부른다"
    if aspect and aspect not in ("9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9"):
        return "aspect 는 9:16·2:3·3:4·1:1·4:3·3:2·16:9 중 하나"
    if not (sd / "assets" / "vast_refs").is_dir():
        return "이 작품의 참고 그림(assets/vast_refs)이 없다. 먼저 vast_make_assets(series) 를 부른다"
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
    cmd += ["--refine"] if refine else []
    cmd += ([] if continuity else ["--no-continuity"]) + (["--continue-closeups"] if continue_closeups else [])
    # review_codes 2: 일부 컷만 실패(끝난 컷은 쓸 수 있다) → failed 가 아니라 needs_review(comfy_panel 끝 참고)
    steps = [{"label": f"GPU 서버 {len(want)}컷 그리기(크레딧 0)", "cmd": cmd, "review_codes": [2]}]
    if restyle:
        sys.path.insert(0, str(VAST))
        import lora as L
        if not ((L.registry(sd).get("_restyle") or L.registry(sd).get("_restyle@base") or {}).get("file")):
            return _fail("no_restyle_lora", given=sd.name, reason="이 작품의 Restyle LoRA 가 없다: restyle(series, action='pairs'→'dataset'→'train') 먼저")
        steps.append({"label": "Restyle 마무리", "cmd": [PY, str(VAST / "restyle.py"), "apply", str(sd), ep.name, ",".join(want),
                                                       "--src", "panels_hf_vast", "--variant", "vast_restyle"]})
    # 그린 뒤 그림체 닮음(이 PC CPU, GPU 시간 아님): 걸린 컷이 있으면 needs_review. 점수를 못 내도(가중치 없는 PC) 그리기 결과는 그대로 쓴다.
    # 그동안 시험은 도구 호출·숫자만 재고 그림이 원작 그림체를 닮았는지는 아무도 재지 않았다(2026-10-27)
    drawn = ep / ("panels_hf_vast_restyle" if restyle else "panels_hf_vast")
    steps.append({"label": "그림체 닮음 점수", "allow_fail": True, "review_codes": [2],
                  "cmd": [PY, str(TOOLS / "style_fidelity.py"), "score", str(sd), *[str(drawn / f"{p}.png") for p in want],
                          "--json-out", str(ep / "review" / f"style_{time.strftime('%m%d-%H%M%S')}.json"), "--review-exit"]})
    job = _start_job("vast_draw", ep, steps)
    per = (30 if turbo else 120) + (40 if check else 0) + ((30 if turbo else 60) if refine else 0) + (40 if restyle else 0)
    return (f"작업 시작 {job}: {len(want)}컷, 크레딧 0, 약 {max(1, len(want) * per // 60)}분(공용 큐가 붐비면 더). "
            f"job_status('{job}') → 끝나면 contact_sheet(series, episode, folder='vast'). 그린 뒤 그림체 닮음 점수가 붙는다(job_status 의 style). "
            f"그림체 LoRA: {_style_lora_state(sd)}")


def _style_lora_state(sd: Path) -> str:
    """이 작품의 그림체 LoRA(loras.json '_style')가 이번 그림에 붙는지 한 줄로. 꺼져 있거나 서버에 없으면 참고 그림만으로 그려 그림체가 밋밋해지는데
    아무도 몰랐다(2026-10-26 무대 뒤: 10-21 학습 노드 버그로 꺼 둔 뒤 닷새 동안 LoRA 없이 그림)."""
    try:
        import lora as L
        e = L.registry(sd).get(L.STYLE) or {}
        if not e.get("file"):
            return "없음(학습한 적 없음: train_lora)"
        if not e.get("on", True):
            return f"꺼짐({e['file']}: loras.json on=false, 다시 학습했으면 켠다)"
        have = L.on_server()
        if have is not None and e["file"] not in have:
            return f"서버에 없음({e['file']}: 이 서버에 올리거나 다시 학습)"
        return f"켜짐({e['file']}, 강도 {e.get('strength', 0.8)})"
    except Exception as ex:  # noqa: BLE001
        return f"확인 못 함({str(ex)[:60]})"


@mcp.tool()
def vast_make_assets(series: str, character: str = "all", what: str = "both") -> str:
    """인물 에셋을 GPU 서버로 만든다(크레딧 0): 시트에서 참고 그림 자르기 → 회색 기본복 베이스 몸·센 표정 12가지(오열·고함·경멸 등).
    character: 인물 id(cast.json) 또는 all. what: base · expr · both(센 표정) · subtle(잔잔한 표정 20가지, 차분한 연기용. series.json expressions 로 바꿀 수 있다)
    · turn(캐릭터 시트: 얼굴 + 앞·뒤·옆·3/4 전신. 칸마다 잘라 뒷모습·옆모습 컷의 참고 그림으로 그리기 도구가 알아서 쓴다, 인물당 약 20초).
    이미 있는 그림은 건너뛴다. make_episode 가 새 화를 만들 때 필요한 것을 알아서 만드니, 따로 부르는 건 에셋만 다시 만들거나 보탤 때다."""
    sd = _series(series)
    if what not in ("base", "expr", "both", "subtle", "turn"):
        return json.dumps({"error": "bad_what", "given": what, "allowed": ["base", "expr", "both", "subtle", "turn"]}, ensure_ascii=False)
    sheets = sorted(p.name[:-len('_sheet.png')] for p in (sd / "assets").glob("*_sheet.png"))
    # 시트가 하나도 없는 작품에 all 로 부르면 '할 일 0장'으로 조용히 끝나 에이전트가 빈 회차를 만들며 헤맸다(2026-10-24 등불 상점 시험)
    if (character != "all" and not (sd / "assets" / f"{character}_sheet.png").exists() and not (sd / "assets" / "vast_refs" / f"{character}_front.png").exists()) \
            or (character == "all" and not sheets and not any((sd / "assets" / "vast_refs").glob("*_front.png"))):
        return _fail("no_sheet", character=character, sheets=sheets,
                     hint="인물 시트가 먼저 있어야 한다: draw_candidates(작품) 로 시안을 그리고, 사용자가 고르면 move_file 로 assets/ 에 넣거나 "
                          "make_episode(accept_new=True) 가 넣는다")
    _ensure_idle(sd)
    steps = [{"label": "시트에서 참고 그림 자르기", "cmd": [PY, str(VAST / "sheet_refs.py"), str(sd)]},
             {"label": f"에셋 만들기({character}, {what})", "cmd": [PY, str(VAST / "make_assets.py"), str(sd), character, what, "--res", "768"]}]
    job = _start_job("vast_assets", sd, steps)
    return f"작업 시작 {job}: 크레딧 0, 장당 약 3~4초(인물 하나 13장). job_status('{job}')"


@mcp.tool()
def loras(series: str = "", character: str = "", strength: float = -1, on: str = "", file: str = "") -> str:
    """LoRA 상태를 보고, character 를 주면 그 LoRA 를 고친다. server: GPU 서버 ComfyUI 에 있는 LoRA 파일(webtoon/… 는 여기서 학습한 것).
    series 를 주면 registered: 그 작품에 등록된 LoRA {file, trigger, model(학습한 판, 그 판으로 그릴 때만 붙는다), strength, on, steps, images, train_s},
    can_train: 학습할 수 있는 인물(cast 에 있고 표정 은행이 있는 사람), train_workflow: 학습에 쓰는 서버 워크플로우(역할 train_lora).
    training_disabled 가 있으면 지금 이 서버에선 학습하지 않는다(이유·대안).
    고치기(series + character, 그림체는 style): strength(0~1.5, 얼굴이 너무 굳거나 그림체가 무너지면 내린다, -1 은 그대로) · on('true' 붙임 · 'false' 안 붙임 · 비우면 그대로)
    · file(서버 LoRA 파일 이름으로 바꾸기, server 목록). 다음에 그리는 컷부터 바뀐다(예전 loras(series, character, …) 를 여기에 합침, 2026-10-22)."""
    sys.path.insert(0, str(VAST))
    import lora as L
    if character:
        if not series:
            return _fail("series_required", given={"character": character})
        sd = _series(series)
        who = L.STYLE if character in ("style", "그림체") else character
        if who not in L.registry(sd) and not file:
            return _fail("no_lora", given=character, registered=sorted(L.registry(sd)), reason="학습한 LoRA 가 없다: lora_train 으로 학습하거나 file 로 지정")
        if file and file not in L.server_loras():
            return _fail("unknown_lora_file", given=file, candidates=_near(file, L.server_loras()))
        flag = {"true": True, "false": False}.get(on.strip().lower()) if on else None
        cur = L.set_entry(sd, who, {"strength": strength if strength >= 0 else None, "on": flag, "file": file or None})
        return json.dumps({"series": sd.name, "character": who, "lora": cur}, ensure_ascii=False)
    out = {"server": L.server_loras()}
    m = _draw_models()
    out["train_workflow"] = {k: v["roles"].get("train_lora") for k, v in m.items() if "train_lora" in v.get("own_roles", [])}
    if series:
        sd = _series(series)
        blocked = L.training_block(sd)
        if blocked:
            out["training_disabled"] = blocked
        out["series"] = sd.name
        out["registered"] = L.registry(sd)
        out["can_train"] = L.people(sd)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def lora_train(series: str, character: str, steps: int = 0, rank: int = 0, learning_rate: float = 0.0, panels: int = 0, chunk: int = 500) -> str:
    """LoRA 를 GPU 서버에서 학습한다(크레딧 0, GPU 시간만, 작업으로 돈다). character 는 인물 id, 쉼표로 여럿, all 이면 학습할 수 있는 인물 전부(차례로),
    style 이면 그 작품의 그림체 LoRA(본편 컷으로 학습, 사람 없는 컷까지 모든 컷에 붙는다). 학습 데이터는 도구가 만든다:
    인물은 표정 은행(정면 상반신) + 기본 전신 + 그 인물 혼자 나온 잘 된 컷 panels 장(기본 12), 그림체는 본편 컷 panels 장(기본 100, 샷 종류를 고루, 종류마다 둘은 평가용으로 남김).
    서버 워크플로우(역할 train_lora, WebtoonTrainLora: 묶음 잠재 정규화 · alpha=rank + PEFT 초기값 · 블록 안 층만 · 속도 손실 · LoRA fp32, 화면에서 값을 고칠 수 있다)로 학습한다.
    steps 는 전체 스텝(0 이면 인물 1500 · 그림체 3000), rank·learning_rate 가 0 이면 워크플로우 값(16 · 1e-4).
    chunk 스텝마다 저장하고 이어 학습해 중간 파일이 남는다: 이때는 꺼 둔 채 등록되니 lora_compare 로 견줘 고른 뒤 loras(series, character, file=…, on='true').
    chunk=0 이면 한 번에 학습하고 바로 켠다. 시간은 loras() 의 train_s 참고(그림체 100장 3000스텝은 약 2시간). 학습 중엔 같은 GPU 로 그리기가 기다린다."""
    sys.path.insert(0, str(VAST))
    import lora as L
    sd = _series(series)
    who = [c.strip() for c in character.split(",") if c.strip()]
    who = L.people(sd) if who == ["all"] else [L.STYLE if c in ("style", "그림체") else c for c in who]
    bad = [c for c in who if c != L.STYLE and c not in L.people(sd)]
    if bad or not who:
        return _fail("unknown_character", given=character, candidates=L.people(sd) + ["all", "style"],
                     reason="설정에 있고 표정 은행(vast_make_assets)이 있는 인물, 또는 style(그림체)만 학습한다. 여럿은 쉼표로, 전부는 all")
    tw = L.training_block(sd)
    if tw:  # 학습 워크플로우 데이터에 막아 둔 이유(extra.webtoon.disabled)가 있으면 작업을 띄우지 않는다
        return _fail("training_disabled", given=character, reason=tw,
                     alternative="다른 GPU 에서 학습한 LoRA 파일을 서버 loras/webtoon/ 에 두고 loras(series, character, file=…) 로 등록")
    _ensure_idle(sd)
    steps_list = []
    for c in who:  # 여럿이면 한 작업 안에서 차례로(공용 GPU 큐를 한 번에 하나만 쓴다)
        style = c == L.STYLE
        cmd = [PY, str(VAST / "lora.py"), "train", str(sd), c, "--panels", str(panels or (100 if style else 12)),
               "--steps", str(steps or (3000 if style else 1500)), "--chunk", str(chunk)]
        for flag, v in (("--rank", rank), ("--lr", learning_rate)):
            if v:
                cmd += [flag, str(v)]
        steps_list.append({"label": f"{'그림체' if style else '인물'} LoRA 학습({c})", "cmd": cmd})
    # 학습 결과를 이 PC 보관함(models/loras)으로: 서버 디스크에만 두면 서버를 옮기며 사라진다(2026-10-26 무대 뒤·D+18). 실패해도 학습은 성공
    steps_list.append({"label": "학습한 LoRA 를 PC 보관함으로", "cmd": [PY, str(ROOT / "tools" / "gpu_loras.py"), "pull"], "allow_fail": True})
    job = _start_job("lora_train", sd, steps_list)
    nxt = f"job_status('{job}', wait=50) 를 되풀이(학습은 오래 걸린다), 끝나면 " + (f"lora_compare('{sd.name}', …) 로 중간 파일을 견줘 고르고 loras(series, character, …)" if chunk else f"loras('{sd.name}')")
    return json.dumps({"job": job, "next": nxt}, ensure_ascii=False)


@mcp.tool()
def restyle(series: str, action: str = "status", episode: str = "", panels: str = "", source: str = "panels_hf_vast", steps: int = 1500) -> str:
    """짝 데이터 Restyle(UC 만): 'Qwen UC 가 그린 결 → 그 작품 정본 결' 을 배우는 편집 LoRA 로 컷을 그 작품 그림체로 마무리한다.
    서버 워크플로우를 쓴다: 짝 = 생성(mcp_use_webtoon_vertical_qwen21_uc), 데이터 = mcp_use_webtoon_restyle_dataset_uc, 학습 = mcp_use_webtoon_train_lora_uc_base,
    마무리 = mcp_use_webtoon_restyle_uc(편집 + Restyle LoRA). action=status 는 바로 답한다(짝 수·학습한 파일·중간 파일·평가 그림), 나머지는 작업으로 돈다.
    action: pairs(정본 컷을 같은 구도로 Qwen UC 가 다시 그린 짝 만들기) → dataset(짝을 편집 조건 학습 데이터로) → train(공식 bf16 + UC 위 학습, steps)
    → eval(학습에 안 쓴 정본 컷: 정답 | 1단 Qwen UC | 2단 Restyle, exports/<날짜>/비교/<작품>_Restyle_평가.jpg)
    → apply(episode 의 panels(쉼표) 를 source 폴더 그림에서 마무리, 결과 panels_hf_restyle/). 학습한 것은 loras.json '_restyle'(UC fp8 위)."""
    sd = _series(series)
    if action not in ("status", "pairs", "dataset", "train", "eval", "apply"):
        return _fail("unknown_action", given=action, candidates=["status", "pairs", "dataset", "train", "eval", "apply"])
    if action == "status":
        sys.path.insert(0, str(VAST))
        import restyle as RS
        st = RS.status(sd)
        if st.get("eval_sheet"):
            st["eval_link"] = _link(Path(st["eval_sheet"]))
        return json.dumps(st, ensure_ascii=False)
    cmd = [PY, str(VAST / "restyle.py"), action, str(sd)]
    if action == "apply":
        if not episode or not panels:
            return _fail("episode_and_panels_required", given={"episode": episode, "panels": panels})
        cmd += [episode, panels, "--src", source]
    if action == "train":
        cmd += ["--steps", str(steps)]
    _ensure_idle(sd)
    steps_list = [{"label": f"Restyle {action}", "cmd": cmd}]
    if action == "train":  # 학습 결과를 PC 보관함으로(lora_train 과 같은 이유)
        steps_list.append({"label": "학습한 LoRA 를 PC 보관함으로", "cmd": [PY, str(ROOT / "tools" / "gpu_loras.py"), "pull"], "allow_fail": True})
    job = _start_job("restyle", sd, steps_list)
    return json.dumps({"job": job, "next": f"job_status('{job}', wait=50)"}, ensure_ascii=False)


@mcp.tool()
def lora_compare(series: str, character: str = "style", strengths: str = "1.0", panels: int = 4, files: str = "") -> str:
    """학습한 LoRA 의 중간 파일들(lora_train 의 chunk 마다 저장된 것, loras() 의 checkpoints)을 같은 시드로 그려 한 장에 놓는다(작업으로 돈다).
    학습에 쓰지 않은 본편 컷을 골라 정답(원본) | LoRA 끔 | 중간 파일 × strengths(쉼표, 예 0.6,0.8,1.0) 순서. 그림체(style)는 클로즈업·둘 이상·배경·와이드를 고루.
    files 를 주면(쉼표) 그 파일들만. 결과는 exports/<날짜>/비교/ 의 jpg: 사람이 보고 고른 파일을 loras(series, character, file=…, on='true') 로 켠다.
    너무 배운 파일은 데이터 컷을 베끼거나 얼룩이 지고, 덜 배운 파일은 LoRA 끔과 같아 보인다. 그리는 동안 loras.json 을 잠깐 바꿨다가 되돌린다."""
    sys.path.insert(0, str(VAST))
    import lora as L
    sd = _series(series)
    who = L.STYLE if character in ("style", "그림체") else character
    if who not in L.registry(sd):
        return _fail("no_lora", given=character, registered=sorted(L.registry(sd)), reason="학습한 LoRA 가 없다: lora_train 으로 학습")
    _ensure_idle(sd)
    cmd = [PY, str(VAST / "lora_eval.py"), str(sd), character, "--strengths", strengths, "--panels", str(panels)] + (["--files", files] if files else [])
    job = _start_job("lora_compare", sd, [{"label": f"LoRA 중간 파일 견주기({character})", "cmd": cmd}])
    return json.dumps({"job": job, "next": f"job_status('{job}', wait=50), 끝나면 로그 마지막 줄의 jpg 를 보고 loras(…, file=…, on='true')"}, ensure_ascii=False)


@mcp.tool()
def comfy_server(action: str = "status", lines: int = 60, old: bool = False, force: bool = False) -> str:
    """GPU 서버의 ComfyUI 관리(주소는 state/comfy_url.txt). action:
    status(버전·실행 인자·VRAM·큐, GPU 인스턴스, 판마다 모델 파일이 있나) · logs(서버 프로세스 로그 끝 lines 줄, old=True 면 직전 실행의 로그: 서버가 재시작됐을 때 죽은 이유를 본다)
    · restart(supervisor 로 재시작) · patch(덧댄 상류 수정 vast/server_patches 가 붙어 있나 보고 붙인다, 예: Qwen 2.1 학습 수정 PR #16654)
    · nodes(우리 노드 vast/server_nodes 를 custom_nodes 에 올린다, 예: WebtoonTrainLora. 바뀌었으면 재시작).
    restart 는 큐에 작업이 있으면 하지 않는다(force=True 로만). ComfyUI 버전과 모델 파일은 RunPod 설정(cloud/runpod_qwen.json)이 정하고
    tools/runpod_cloud.py sync 로 받는다(이 도구로 버전을 바꾸거나 모델을 받지 않는다, 2026-10-22)."""
    sys.path.insert(0, str(VAST))
    import comfy_admin as A
    try:
        if action == "status":  # 서버(버전·인자) + vast 인스턴스(남은 VRAM·큐·판마다 모델 파일이 서버에 있나)
            return json.dumps({"server": A.status(), "vast": _run([PY, str(VAST / "comfy_client.py"), "status"], timeout=25)}, ensure_ascii=False)
        if action == "logs":
            return json.dumps({"log": A.logs(old, max(10, min(lines, 400)))}, ensure_ascii=False)
        if action == "restart":
            return json.dumps(A.restart(force), ensure_ascii=False)
        if action == "nodes":  # 우리 노드(vast/server_nodes, 예 WebtoonTrainLora)를 custom_nodes 에 올린다. 새로 올렸으면 재시작해야 뜬다
            r = A.install_nodes()
            if r["changed"]:
                r.update(A.restart(force))
            return json.dumps(r, ensure_ascii=False)
        if action == "patch":  # 상류에 아직 안 들어간 수정(vast/server_patches)을 보고 붙인다. 새로 붙이면 재시작
            r = A.patches(apply=True)
            if r["changed"]:
                r.update(A.restart(force))
            return json.dumps(r, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _fail("comfy_server_failed", given={"action": action}, reason=f"{type(e).__name__}: {str(e)[:300]}")
    return _fail("unknown_action", given=action, candidates=["status", "logs", "restart", "patch", "nodes"])


@mcp.tool()
def adopt_panel(series: str, episode: str, ids: str, source: str = "vast", name: str = "", reason: str = "") -> str:
    """다른 판을 본편으로 쓴다(사용자가 고른 것만, 본편이 바뀐다). source: vast(vast_draw 로 다시 그린 판, ids 에 여러 컷) · preview(preview_panel 미리 보기,
    한 컷, name 비우면 마지막 것) · version(panel_versions 의 예전 판 name 으로 되돌리기, 한 컷) · current(지금 본편을 그대로 쓴다: 사람이 보고 괜찮다고 한
    컷의 검수 기록을 닫는다, 여러 컷, reason 에 까닭). 지금 본편은 panels_hf/_rejected/ 로 옮긴다. 끝나면 compose.

    예전 vast_adopt · adopt_preview · revert_panel 을 합쳤다(셋 다 '다른 판을 본편 자리에 넣고 지금 본편은 _rejected 로', 2026-10-23 도구 정리)."""
    ep = _episode(series, episode)
    want = _pids(ep, ids, "any")
    if not want:
        return "본편으로 쓸 컷 번호를 준다"
    if source not in ("vast", "preview", "version", "current"):
        return _fail("bad_source", given=source, candidates=["vast", "preview", "version", "current"])
    if source == "current":
        # 사람이 보고 넘긴 컷도 검수 기록이 그대로 남아 합성·내보내기 때마다 '사람 확인 27컷'으로 떴다(2026-10-24 개항 v3).
        # 닫은 기록은 review/accepted.json 에 까닭과 함께 옮긴다(지우지 않는다)
        f, acc_f = ep / "review" / "staging.json", ep / "review" / "accepted.json"
        cur, acc = _json(f, {}) or {}, _json(acc_f, {}) or {}
        closed = [i for i in want if i in cur]
        for i in closed:
            acc[i] = {**cur.pop(i), "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason or "사람이 보고 그대로 씀"}
        if closed:
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
            acc_f.write_text(json.dumps(acc, ensure_ascii=False, indent=1), encoding="utf-8")
        rest = [i for i in want if i not in closed]
        return (f"검수 기록을 닫음 {len(closed)}컷({', '.join(closed) or '없음'}), 그림은 그대로." + (f" 기록이 없던 컷: {', '.join(rest)}." if rest else "")
                + f" 남은 사람 확인 {len(cur)}컷")
    if source != "vast":
        if len(want) != 1:
            return f"source={source} 는 한 컷씩 준다"
        pid = want[0]
        if source == "version":
            if not name:
                return "name 에 panel_versions 의 예전 판 이름을 준다"
            job = _start_job("revert_panel", ep, [{"label": f"{pid} 되돌리기", "cmd": [PY, str(VAST / "panel_edit.py"), "revert", str(_series(series)), ep.name, pid, name]}])
            return f"작업 시작 {job}. job_status('{job}') → compare_panel"
        d = ep / "review" / "previews"
        pick = name or next((f.stem for f in sorted(d.glob(f"{pid}_*.png"), key=lambda f: f.stat().st_mtime, reverse=True)), "") if d.exists() else name
        if not pick:
            return "이 컷의 미리 보기가 없다(preview_panel 먼저)"
        r = subprocess.run([PY, str(VAST / "panel_edit.py"), "adopt", str(_series(series)), ep.name, pid, pick], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=ENV)
        return (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else f"종료 {r.returncode}"
    miss = [i for i in want if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    if miss:
        return f"GPU 서버 그림이 없는 컷: {', '.join(miss)}(vast_draw 로 먼저 그린다)"
    _ensure_idle(ep)
    rej = ep / "panels_hf" / "_rejected"
    rej.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d%H%M")
    for i in want:
        cur = ep / "panels_hf" / f"{i}.png"
        if cur.exists():
            shutil.move(str(cur), str(rej / f"{i}_{stamp}_before_vast.png"))
        shutil.copy2(ep / "panels_hf_vast" / f"{i}.png", cur)
    job = _start_job("vast_adopt", ep, [{"label": "합성용으로 놓기", "cmd": [PY, str(TOOLS / "place_panels.py"), str(ep)]}], reason)
    return f"{len(want)}컷을 vast 그림으로 바꿨다(원본은 panels_hf/_rejected/*_before_vast.png). 놓기 작업 {job} → 끝나면 compose"


# ── 부분 고치기(vast/panel_edit.py, 그 판의 '편집' 워크플로우). 다시 그리지 않고 본편 그림의 일부만 바꾼다(2026-10-20 사용자
#    "부분부분만 고치는 것도 가능하냐, MCP 도 알아서 컨피 워크플로우를 활용해야"). 고친 판은 본편에 들어가고 옛 판은 panel_versions 로 되돌린다.
FIX_LADDER = ("고치는 순서: ① 예전 판이 더 나았으면 panel_versions → adopt_panel(source='version') ② 일부만 틀림(소품·사람 하나 지우기·입·손·크기)은 "
              "edit_panel ③ 얼굴이 다른 사람 같으면 edit_panel(refs=그 인물 id, 'redraw only the face to match <image2>') ④ 자리·테이블·구도·카메라가 "
              "틀리면 컷 설명을 고치고 vast_draw(같은 장면 앞 컷으로 자리를 맞춘다) → compare_panel(what='redraw') → adopt_panel. "
              "고친 뒤엔 compare_panel 로 전/후를 꼭 눈으로 본다. 다 고친 뒤 compose 는 한 번만.")


@mcp.tool()
def edit_panel(series: str, episode: str, panel: str | int, instruction: str, refs: str | list = "", seed: int = -1) -> str:
    """그린 컷의 일부만 고친다(다시 그리지 않음, 크레딧 0, 컷당 약 30초): 그 판의 편집 워크플로우에 지금 본편 그림(<image1>)과 지시를 준다.
    instruction 은 영어로 '무엇을 어떻게 바꾸고 나머지는 그대로': 예 "In <image1>, remove the man in the grey polo shirt; fill the space with the lobby.
    Keep everyone else, the place, the light and the framing exactly the same." refs: 쉼표로 인물 id(그 인물 상반신 참고 그림이 <image2>부터)나 작품 안 파일.
    얼굴이 다른 사람 같으면 refs 에 그 인물 id 를 주고 얼굴만 <image2> 처럼 다시 그리게 한다. 사람 자리 옮기기·카메라 바꾸기는 편집으로 잘 안 된다(vast_draw).
    다른 사람이 가진 것(모자·안경 등)을 한 사람에게 더할 땐 '원래 가진 사람도 그대로 쓴다'를 같이 적는다: 안 적으면 그 사람에게서 빼앗아 옮겨 그렸다.
    얼굴이 똑같은 사람들(복제)을 다르게 할 땐 '서로 다르게'만 적지 말고 자리마다 머리 모양·얼굴형·장신구를 구체적으로 적는다(막연한 지시는 거의 안 바뀌었다).
    결과는 바로 본편에 들어간다(adopt_panel 이 필요 없다, 옛 본편은 panels_hf/_rejected/, 되돌리려면 adopt_panel(source='version')).
    본편을 건드리지 않고 먼저 보고 싶으면 preview_panel. 끝나면 compare_panel 로 전/후를 보여 주고, 합성본에 넣으려면 compose."""
    panel = str(panel)  # 작은 모델이 컷 번호를 숫자(4)로 넘겨 형식 오류가 났다(2026-10-24 개항 v4)
    ep = _episode(series, episode)
    pid = _pids(ep, panel, "any")[0]
    refs = ",".join(map(str, refs)) if isinstance(refs, list) else refs  # 목록으로 줘도 받는다(refs=[] 로 불러 한 번 헛돌았다, 2026-10-24)
    if not (instruction or "").strip():
        return _fail("empty_instruction")
    cmd = [PY, str(VAST / "panel_edit.py"), "edit", str(_series(series)), ep.name, pid, instruction, "--refs", refs]
    cmd += ["--seed", str(seed)] if seed >= 0 else []
    job = _start_job("edit_panel", ep, [{"label": f"{pid} 부분 고치기", "cmd": cmd}], instruction[:120])
    return f"작업 시작 {job}(약 30초~1분). job_status('{job}') → 끝나면 compare_panel('{series}', '{episode}', '{pid}')"


# derive_panel(잘 된 컷에서 잘라 다시)·fix_faces(얼굴 거리 재며 얼굴만 다시)는 2026-10-23 도구 정리에서 MCP 에서 뺐다:
# 10-16 이후 한 번도 안 불렸고, 자리 맞추기는 vast_draw 의 같은 장면 이어 그리기, 얼굴은 edit_panel(refs=인물 id)이 한다. 손으로는 vast/panel_edit.py derive·faces.


@mcp.tool()
def panel_versions(series: str, episode: str, panel: str | int) -> str:
    """이 컷의 예전 판들(새 것부터, name·time): 본편에서 밀려난 판(_rejected), 고치기 전(_qa_before), 장면 기준판 전(_preplate), 1단 판(_stage1).
    예전 판이 더 나으면 adopt_panel(source='version', name=…) 로 되돌린다. 그림은 contact_sheet(paths=series/<작품>/episodes/<회차>/<폴더>/<name>) 로."""
    panel = str(panel)  # 작은 모델이 컷 번호를 숫자(4)로 넘겨 형식 오류가 났다(2026-10-24 개항 v4)
    ep = _episode(series, episode)
    pid = _pids(ep, panel, "any")[0]
    # panel_edit.versions 와 같은 자리(툴 안에서 무거운 모듈을 import 하지 않는다: Windows 워커 스레드 데드락)
    spots = [(ep / "panels_hf" / "_rejected", f"{pid}_*.png"), (ep / "panels_hf" / "_rejected", f"{pid}.png"),
             (ep / "panels_hf_vast" / "_qa_before", f"{pid}.png"), (ep / "panels_hf_vast" / "_preplate", f"{pid}.png"),
             (ep / "panels_hf_vast" / "_stage1", f"{pid}*.png")]
    found = sorted(((f.stat().st_mtime, f"{d.name}/{f.name}") for d, pat in spots if d.exists() for f in d.glob(pat)), reverse=True)
    return json.dumps({"panel": pid, "versions": [{"name": n, "time": time.strftime("%m-%d %H:%M", time.localtime(t))} for t, n in found[:20]]},
                      ensure_ascii=False)


@mcp.tool()
def compare_panel(series: str, episode: str, panel: str | int, what: str = "fix") -> list:
    """이 컷의 전/후 나란히 그림을 그림 자체로 돌려준다(사용자에게 보여 줄 때도 이것). what:
    fix(기본, 마지막 고치기 전/후: edit_panel·되돌리기가 남긴 것) · preview(마지막 미리 보기: preview_panel, 이름 목록도 준다)
    · redraw(본편 | GPU 로 새로 그린 판 panels_hf_vast: 한 컷을 vast_draw 로 다시 그려 본 뒤). 고친 뒤엔 반드시 본다: 지시한 곳이 고쳐졌는지,
    다른 곳(얼굴·옷·배경·사람 수)이 바뀌지 않았는지. 나빠졌으면 panel_versions → adopt_panel(source='version'), 미리 보기·다시 그린 판은 adopt_panel(source='preview'·'vast') 로 본편에."""
    panel = str(panel)  # 작은 모델이 컷 번호를 숫자(4)로 넘겨 형식 오류가 났다(2026-10-24 개항 v4)
    ep = _episode(series, episode)
    pid = _pids(ep, panel, "any")[0]
    if what not in ("fix", "preview", "redraw"):
        return "what 은 fix · preview · redraw"
    if what == "redraw":
        new = ep / "panels_hf_vast" / f"{pid}.png"
        if not new.exists():
            return "GPU 로 새로 그린 판이 없다(vast_draw 로 이 컷을 먼저 그린다)"
        tiles = [Image.open(f).convert("RGB") for f in (ep / "panels_hf" / f"{pid}.png", new) if f.exists()]
        h = 700
        tiles = [t.resize((round(t.width * h / t.height), h)) for t in tiles]
        im = Image.new("RGB", (sum(t.width for t in tiles) + 12 * (len(tiles) - 1), h), "white")
        x = 0
        for t in tiles:
            im.paste(t, (x, 0))
            x += t.width + 12
        out = ep / "review" / "redraws" / f"{pid}_{time.strftime('%m%d%H%M%S')}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, quality=88)
        info = {"panel": pid, "left": "본편" if len(tiles) == 2 else "(본편 없음)", "right": "새로 그린 판", "compare": str(out), "link": _link(out)}
    else:
        d = ep / "review" / ("edits" if what == "fix" else "previews")
        cands = sorted(d.glob(f"{pid}_*.jpg"), key=lambda f: f.stat().st_mtime) if d.exists() else []
        if not cands:
            return "이 컷의 고치기 기록이 없다" if what == "fix" else "이 컷의 미리 보기가 없다(preview_panel 먼저)"
        im = Image.open(cands[-1]).convert("RGB")
        info = {"panel": pid, "compare": str(cands[-1]), "link": _link(cands[-1])}
        info.update({"edits": len(cands)} if what == "fix" else {"latest": cands[-1].stem, "all": [f.stem for f in cands]})
    im.thumbnail((1400, 1400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return [json.dumps(info, ensure_ascii=False), McpImage(data=buf.getvalue(), format="jpeg")]


@mcp.tool()
def style_check(series: str, episode: str = "", ids: str = "", folder: str = "vast", paths: str = "", floor: float = 10.0) -> str:
    """그린 그림이 그 작품 원작 그림체를 얼마나 닮았나(작업, GPU 없음: 이 PC 의 CPU 로 장당 약 2초, 처음엔 원작 특징을 뽑느라 몇 분).
    결과는 job_status 로그: 그림마다 '원작끼리 닮음 분포의 몇 백분위'와 통과(floor 백분위 이상)·걸림. 회차 컷이면 series·episode·ids(쉼표, 비우면 그 폴더 전부)·
    folder(기본 "vast" = GPU 서버로 새로 그린 판, 비우면 본편), 아무 그림이면 paths(파일·폴더·* 패턴, 쉼표).
    진단에서 그 작품을 다른 작품과 구별하지 못한 지표면 판정하지 않는다고 로그에 적는다. vast_draw 는 그린 뒤 이 점수를 스스로 붙이니
    따로 부르지 않는다: 예전 그림·다른 판·LoRA 를 켠 그림을 견줄 때 쓴다. 걸린 그림은 compare_panel 로 원작과 나란히 보여 준다."""
    sd = _series(series)
    target = sd
    if paths.strip():
        files = []
        for one in [x.strip() for x in re.split(r"[,\n]+", paths) if x.strip()]:
            for p in _matches(one, series, fuzzy=True):
                files += sorted(c for c in p.iterdir() if c.suffix.lower() in IMG_EXT) if p.is_dir() else [p] if p.suffix.lower() in IMG_EXT else []
    else:
        target = _episode(series, episode)
        sub = target / _folder(folder)
        want = _pids(target, ids, "any") if ids.strip() else sorted(f.stem for f in sub.glob("*.png"))
        files = [sub / f"{pid}.png" for pid in want if (sub / f"{pid}.png").exists()]
    if not files:
        return _fail("no_images", series=sd.name, folder=folder, ids=ids, paths=paths, how="그림이 있는 폴더·컷 번호를 list_files 로 확인")
    out = target / "review" / f"style_{time.strftime('%m%d-%H%M%S')}.json"
    job = _start_job("style_check", target, [{"label": f"그림체 닮음 {len(files)}장", "review_codes": [2],
                                              "cmd": [PY, str(TOOLS / "style_fidelity.py"), "score", str(sd), *map(str, files),
                                                      "--floor", f"{floor:g}", "--json-out", str(out), "--review-exit"]}])
    return (f"작업 시작 {job}: {len(files)}장(GPU 없음). job_status('{job}', wait=50) → 로그에 그림마다 백분위·통과/걸림 "
            f"(needs_review = 걸린 그림이 있음, 실패 아님). 전체 결과 {out.relative_to(ROOT).as_posix()}")


@mcp.tool()
def preview_panel(series: str, episode: str, panel: str | int, instruction: str = "", refs: str | list = "", character: str = "", design: str = "", seed: int = -1) -> str:
    """본편은 그대로 두고 한 컷을 바꾼 모습만 미리 본다(크레딧 0, 약 30초): '이 컷 이렇게 바꾸면 어때, 보여줘'. instruction(영어, edit_panel 과 같은 지시)
    또는 인물 디자인 바꾸기 character(인물 id) + design(영어, 예 "short silver-white hair instead of black hair"). 끝나면 compare_panel(what='preview').

    결과는 <회차>/review/previews/ 에만 쌓인다. 사용자가 고르면 adopt_panel(source='preview') 로 본편에 넣는다.
    디자인이 마음에 들어 작품 전체에 쓰려면 add_to_series 로 그 인물 설명을 바꾸고 시안·참고 그림을 다시 만드는 일이 따로 필요하다(사용자에게 묻는다).
    (예전 preview_design 을 합쳤다, 2026-10-23)"""
    panel = str(panel)  # 작은 모델이 컷 번호를 숫자(4)로 넘겨 형식 오류가 났다(2026-10-24 개항 v4)
    ep = _episode(series, episode)
    pid = _pids(ep, panel, "any")[0]
    refs = ",".join(map(str, refs)) if isinstance(refs, list) else refs
    if (design or "").strip():
        if not (character or "").strip():
            return _fail("character_required", reason="design 을 주면 character(인물 id)도 준다")
        cmd = [PY, str(VAST / "panel_edit.py"), "design", str(_series(series)), ep.name, pid, design, "--who", character]
        label, note = f"{pid} {character} 디자인 미리 보기", design
    elif (instruction or "").strip():
        cmd = [PY, str(VAST / "panel_edit.py"), "preview", str(_series(series)), ep.name, pid, instruction, "--refs", refs]
        label, note = f"{pid} 미리 보기", instruction
    else:
        return _fail("empty_instruction", reason="instruction 이나 character+design 을 준다")
    cmd += ["--seed", str(seed)] if seed >= 0 else []
    job = _start_job("preview_panel", ep, [{"label": label, "cmd": cmd}], note[:120])
    return f"작업 시작 {job}(약 30초~1분, 본편은 그대로). job_status('{job}') → 끝나면 compare_panel('{series}', '{episode}', '{pid}', what='preview')"





@mcp.tool()
def make_episode(series: str, episode: str, accept_new: bool = False, until: str = "export", check: bool = True, skip_gates: bool = False) -> str:
    """한 화를 끝까지 한 번에(어느 작품이든, 크레딧 0): 콘티 준비 → 에셋 점검 → GPU 서버 그리기(표정 칸·남는 사람 지우기·연출 확인) → 합성·점검 → 내보내기.
    콘티(make_storyboard.py·build_panels.py)는 먼저 써 둔다(new_episode 로 틀을 열고 이야기대로 고침).
    이 화에 처음 나오는 장소·인물이 있으면 vast 로 시안을 만들어 assets/_candidates/ 에 두고 멈춘다(job 이 '실패'로 끝나고 로그에 '확인 필요').
    그때 사용자에게 시안 경로를 보여 주고, 고르면 accept_new=True 로 다시 부른다(accept_new 는 이미 그려 둔 시안만 넣는다: 처음 그리는 시안은 accept_new 여도 확인을 받으러 멈춘다). 본편 그림(panels_hf)이 이미 있는 컷은 건드리지 않는다.
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



# ── 합성·내보내기 ─────────────────────────────────────────────
@mcp.tool()
def compose(series: str, episode: str) -> str:
    """합성: 컷 놓기 → 합성 재료 컷(카톡 등) → 말풍선 합성 → PDF → 기계 점검(qa_gate·check_bubbles). 크레딧 안 씀.
    job_status 결과에서 '막는 경고 0' 과 '얼굴을 가리거나 ... 0' 을 확인한다."""
    ep = _episode(series, episode)
    if _already(ep, "compose"):
        return _already(ep, "compose")
    _ensure_idle(ep)
    steps = [{"label": "컷 놓기", "cmd": [PY, str(TOOLS / "place_panels.py"), str(ep)]},
             {"label": "합성·PDF", "cmd": [PY, str(TOOLS / "run_episode.py"), str(ep)]},
             {"label": "기계 점검", "cmd": [PY, str(TOOLS / "qa_gate.py"), str(ep)], "allow_fail": True},
             {"label": "말풍선 점검", "cmd": [PY, str(TOOLS / "check_bubbles.py"), str(ep)], "allow_fail": True}]
    job = _start_job("compose", ep, steps)
    return f"작업 시작 {job}(약 1~3분). job_status('{job}')" + _review_left(ep)


def _review_left(ep: Path) -> str:
    """검수가 사람 확인으로 남긴 컷(review/staging.json)을 합성·내보내기 답에 붙인다. 걸린 컷(몸통이 둘인 어깨 너머 컷)이 기록만 되고
    아무 말 없이 PDF 로 나갔다(2026-10-24 야근의 온도 3화 p002). 기록 뒤에 그림이 바뀐 컷은 따로 표시한다(고쳤을 수 있다)."""
    cur = _json(ep / "review" / "staging.json", {}) or {}
    if not cur:
        return ""
    structural = set((_json(ROOT / "skills" / "staging-review" / "checks.json", {}) or {}).get("structural", []))
    groups = {}
    for pid, v in sorted(cur.items(), key=lambda x: _pid_key(x[0])):
        # 그린·고친 판(panels_hf_vast)만 본다: panels_hf 는 합성 때 컷 놓기(place_panels)가 늘 다시 써서 매번 '바뀜'이 된다
        f = ep / "panels_hf_vast" / f"{pid}.png"
        newer = f.exists() and time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime)) > v.get("at", "")
        groups.setdefault(_review_level(v.get("issues", []), structural), []).append(
            f"{pid}({'·'.join(v.get('issues', []))}{', 기록 뒤 그림 바뀜' if newer else ''})")
    text = " · ".join(f"{lv} {len(groups[lv])}개: {', '.join(groups[lv])}" for lv in ("짜임", "이어짐", "글과 다름") if lv in groups)
    return (f"\n검수에서 사람 확인이 남은 컷 {len(cur)}개(짜임부터 본다) — {text}. panel_info 로 review 를 보고 고칠 컷은 "
            "set_panels(글이 틀렸으면)·edit_panel·vast_draw(ids) 뒤 다시 compose, 그대로 둘 컷은 사용자에게 알리고 괜찮다고 하면 "
            "adopt_panel(source='current', reason=…)로 기록을 닫는다.")


@mcp.tool()
def export(series: str, episode: str) -> str:
    """예고편(trailer.json)·epub 을 만들고 PDF·epub·예고편을 exports/<오늘>/ 로 복사한다. compose 가 끝난 뒤 부른다. 크레딧 안 씀."""
    ep = _episode(series, episode)
    if _already(ep, "export"):
        return _already(ep, "export")
    name = make_episode_mod.export_name(_series(series), ep)  # make_episode 의 내보내기와 같은 이름(판 번호 _v2 등)
    day = time.strftime("%Y-%m-%d")
    copy = ("import shutil,sys; from pathlib import Path; ep=Path(sys.argv[1]); d=Path(sys.argv[2]); n=sys.argv[3]; d.mkdir(parents=True,exist_ok=True)\n"
            "for src,ext in [(next(ep.glob('out/*.pdf'),None),'.pdf'),(next(ep.glob('out/*.epub'),None),'.epub'),(ep/'out/trailer.mp4','_예고편.mp4'),(ep/'out/trailer.gif','_예고편.gif')]:\n"
            "    if src and Path(src).exists(): shutil.copy2(src, d/(n+ext)); print('복사', d/(n+ext))")
    steps = [{"label": "예고편", "cmd": [PY, str(TOOLS / "trailer2.py"), str(ep)], "allow_fail": True},
             {"label": "epub", "cmd": [PY, str(TOOLS / "to_epub.py"), str(ep)]},
             {"label": "내보내기 복사", "cmd": [PY, "-c", copy, str(ep), str(ROOT / "exports" / day), name]}]
    job = _start_job("export", ep, steps)
    pdf = _link(ROOT / "exports" / day / f"{name}.pdf")
    return f"작업 시작 {job}. 끝나면 exports/{day}/{name}.* . job_status('{job}')" + (f"\n끝난 뒤 볼 링크(PDF): {pdf}" if pdf else "") + _review_left(ep)


# ── Ailism Original 에 게시(tools/ailism_publish.py, API 키로) ─────────────────
@mcp.tool()
def ailism_works() -> str:
    """Ailism Original(play.ailism.team)에 올린 내 작품 목록: 주소 이름(slug)·제목·작가·회차 수·공개 상태·링크.
    publish_ailism·ailism_work 에 넣을 slug 를 여기서 찾는다."""
    return _run([PY, str(TOOLS / "ailism_publish.py"), "works"], timeout=60)


@mcp.tool()
def ailism_work(slug: str, title: str = "", summary: str = "", genre: str = "", tags: str = "", age: str = "", status: str = "",
                days: str = "-", cover: str = "", author: str = "", visible: str = "") -> str:
    """Ailism Original 작품을 만들거나(없는 slug + title) 고친다. 주지 않은 칸은 지금 값 그대로, 작가 이름은 늘 넣는다(기본 '미사일 발사대').
    사용자가 Ailism 에 새 작품을 올리라거나 작품 정보를 바꾸라고 할 때만 부른다(바로 사이트에 반영된다).
    slug: 영문 소문자·숫자·- (예: night-shift-2). genre: 로맨스·드라마·판타지·일상·개그·스릴러 등. tags: 쉼표로 8개까지. age: all · 15.
    status: ongoing · completed · hiatus. days: mon,thu 처럼(빈 문자열이면 비정기, '-' 면 그대로). cover: 세로 3:4 그림 경로(작업실 기준).
    visible: '' 그대로 · 'true' 공개 · 'false' 비공개. 스튜디오 화면에서 작품 정보를 저장하면 작가 이름이 계정 이름으로 돌아가니 그때 이 도구를 한 번 부른다."""
    cmd = [PY, str(TOOLS / "ailism_publish.py"), "work", slug]
    if title:
        cmd += ["--title", title]
    if summary:
        f = JOBS / f"ailism_summary_{slug}.txt"
        JOBS.mkdir(parents=True, exist_ok=True)
        f.write_text(summary, encoding="utf-8")
        cmd += ["--summary-file", str(f)]
    for flag, v in (("--genre", genre), ("--tags", tags), ("--age", age), ("--status", status), ("--author", author)):
        if v:
            cmd += [flag, v]
    if days != "-":
        cmd += ["--days", days]
    if cover:
        cmd += ["--cover", str(_inside(cover))]
    if visible.lower() in ("true", "false"):
        cmd += ["--visible" if visible.lower() == "true" else "--hidden"]
    return _run(cmd, timeout=180)


@mcp.tool()
def publish_ailism(series: str, episode: str, slug: str, title: str = "", thumb: str = "", note: str = "", publish: str = "now",
                   publish_at: str = "", replace: int = 0) -> str:
    """다 만든 회차(compose·export 뒤)를 Ailism Original 에 올린다. 사용자가 Ailism 에 올리라고 했을 때만 부른다: publish=now 면 바로 공개된다.
    합성본(out/<회차>_full.png)을 PDF 와 같은 자리(컷 사이 여백)에서 잘라 올리고, 제목을 비우면 콘티 title_card 에서 'N화.' 를 뗀 것.
    slug: Ailism 작품 주소 이름(모르면 ailism_works, 작품이 없으면 ailism_work 로 먼저 만든다). thumb: 목록 그림(컷 번호 p010 이나 그림 경로, 비우면 첫 그림 윗부분).
    publish: now · schedule(publish_at 에 ISO 시각, 예 2026-10-25T18:00:00+09:00) · draft(저장만). 같은 회차를 다시 올리면 회차가 하나 더 생긴다
    → 이미 올린 회차를 고칠 땐 replace=회차 번호. 업로드는 분당 20장이라 60장이면 3분 남짓(작업으로 돈다)."""
    ep = _episode(series, episode)
    if not slug.strip():
        return "slug 가 비었다. 지금 올린 작품:\n" + ailism_works()
    if publish not in ("now", "schedule", "draft"):
        return "publish 는 now · schedule · draft"
    if not (ep / "out" / f"{ep.name}_full.png").exists():
        return f"{ep.name} 합성본이 없다. compose 를 먼저 부른다"
    cmd = [PY, str(TOOLS / "ailism_publish.py"), "episode", slug.strip(), str(ep), "--publish", publish]
    if title:
        cmd += ["--title", title]
    if thumb:
        pid = re.fullmatch(r"p\d+\w*", thumb.strip())
        f = next((ep / d / f"{thumb.strip()}.png" for d in ("panels_hf", "panels") if pid and (ep / d / f"{thumb.strip()}.png").exists()), None)
        cmd += ["--thumb", str(f or _inside(thumb))]
    if note:
        cmd += ["--note", note]
    if publish_at:
        cmd += ["--at", publish_at]
    if replace:
        cmd += ["--replace", str(int(replace))]
    job = _start_job("publish_ailism", ep, [{"label": f"Ailism 게시({slug} {ep.name}, {publish})", "cmd": cmd}])
    return (f"작업 시작 {job}: {ep.name} → play.ailism.team/ko/webtoon/{slug}. 3~4분 안팎. "
            f"기다리지 말고 사용자에게 작업 번호를 알리고 답을 끝낸다. 끝나면 job_status('{job}') 로그 끝에 회차 링크가 나온다.")


def _job_review(spec: dict, started: str) -> list:
    """이 작업이 그린 컷 중 사람 확인이 남은 컷. 검수(vast/refine.py record_review)가 <회차>/review/staging.json 에 남긴 것을 읽는다."""
    cur = _json(Path(spec.get("episode_dir", "")) / "review" / "staging.json", {}) or {}
    ids = set()
    for st in spec.get("steps", []):
        cmd = [str(x) for x in st.get("cmd", [])]
        if "--only" in cmd:
            ids |= set(cmd[cmd.index("--only") + 1].split(","))
    ck = _json(ROOT / "skills" / "staging-review" / "checks.json", {}) or {}
    cats, structural = ck.get("categories", {}), set(ck.get("structural", []))  # 항목 이름의 뜻·짜임 결함(기준표 데이터)
    items = [{"panel": k, **v, "level": _review_level(v.get("issues", []), structural), "meaning": {i: cats[i] for i in v.get("issues", []) if i in cats}}
             for k, v in sorted(cur.items()) if (k in ids if ids else v.get("at", "") >= started)]
    # 짜임 결함(사람이 남거나 사라짐·몸·생김새·좌우) → 이어짐 → 글과 다름 순: 회차 전체 검수에서 74컷이 한 덩어리로 나와 무엇부터
    # 볼지 몰랐다(2026-10-24 개항 1화 v3 깊은 검수)
    order = {"짜임": 0, "이어짐": 1, "글과 다름": 2}
    return sorted(items, key=lambda x: (order[x["level"]], _pid_key(x["panel"])))


def _review_level(issues: list, structural: set) -> str:
    return "짜임" if set(issues) & structural else "이어짐" if "이어짐" in issues else "글과 다름"


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
        if not _alive(st.get("pid")):
            # 끊긴 작업(학습 마무리 스크립트가 죽어 running 으로 남음, 2026-10-26)을 '도는 중'으로 알리면 모델이 기다리고 하네스도 기다린다
            out["state"] = "lost"
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
    # 하위 도구가 RESULT 줄에 남긴 경고(편집 뒤 시트와 달라진 인물 등)는 로그 꼬리에 묻히지 않게 따로 올린다
    for ln in reversed(lines):
        if ln.startswith("RESULT "):
            with contextlib.suppress(ValueError, AttributeError):
                w = json.loads(ln[7:]).get("warning")
                if w:
                    out["warning"] = w
            break
    if review:
        out["review"] = review
        levels = {}
        for r in review:
            levels.setdefault(r["level"], []).append(r["panel"])
        out["review_summary"] = {k: {"count": len(v), "panels": v} for k, v in levels.items()}
        out["review_rule"] = ("짜임(사람이 남거나 사라짐·몸·생김새·좌우)부터 사용자에게 보여 주고 고친다. 이어짐은 같은 장면 앞뒤 컷과 나란히 보고, "
                              "글과 다름(시선·논리·구도 등)은 독자가 알아챌 것만 고른다. 걸린 수만 말하지 말고 등급별로 나눠 말한다")
        if state == "done":
            out["state"] = "needs_review"
    if spec.get("kind") == "prepare" and state in ("done", "failed"):
        out["gates"] = gates.status(epd)
        tail_lines = max(tail_lines, 80)
        # 걸린 컷을 고치는 길을 결과에 적는다: 에이전트가 panels_spec.json 을 read_file 로 몇 조각씩 읽어 edit_file 로 고치려다
        # 도구 한도를 다 썼다(2026-10-23 미사일 스튜디오 새 작품 시험)
        bad = sorted({m.group(1) for ln in lines for m in [re.match(r"^\s*-\s*(p\w+)\s*\[", ln)] if m}, key=_pid_key)
        g = out["gates"] or {}
        if bad and g.get("storyboard") is False:
            out["next"] = (f"콘티 관문에 걸린 컷 {', '.join(bad)}: 그 컷만 고친 컷 객체로 set_storyboard(cuts=[…]) 를 다시 부른 뒤(같은 id 는 덮는다) "
                           "prepare_episode. 파일을 read_file·edit_file 로 고치지 않는다")
        elif bad and g.get("panels") is False:
            out["next"] = (f"컷 설명 관문에 걸린 컷 {', '.join(bad)}: panel_info 로 그 컷 설명을 보고, 걸린 점만 고친 그 컷 객체를 set_panels(panels={{…}}) 로 다시 넣은 뒤 "
                           "prepare_episode(장면 배치 문장이 원인이면 scenes 도). 파일을 read_file·edit_file 로 고치지 않는다")
        elif g and all(g.values()):
            out["next"] = "관문 통과. 사용자가 그리자고 하면 make_episode"
    cands = [ln.split("확인: ", 1)[1].strip() for ln in lines if "시안 확인: " in ln]
    if cands:  # 승인할 시안: 이 PC 경로와 원격에서 여는 링크(링크는 사용자가 브라우저로 본다)
        out["candidates"] = [{"path": c, "link": _link(c if Path(c).is_absolute() else ROOT / c)} for c in cands]
    links = [x for x in (_link(m.group(1).strip()) for ln in lines for m in [re.match(r"^\s*(?:복사|내보냄)\s+(.+)$", ln)] if m) if x]
    # 끝난 합성·내보내기·한 화 작업엔 그 회차의 PDF 링크를 바로 준다: 모델이 find("ep001_v9 PDF")·list_files 로 PDF 를 찾아 헤맸다(2026-10-26 합성 시나리오)
    if out["state"] in ("done", "needs_review") and any(k in str(spec.get("kind") or "") for k in ("compose", "export", "episode")):
        pdf = ep / "out" / f"{ep.name}.pdf"
        lk = _link(pdf) if pdf.exists() else None
        if lk and lk not in links:
            links.append(lk)
    if links:
        out["links"] = links
    # 끝난 작업만 결과가 있다(시작만 한 작업을 '다 됐다'고 답한 일이 있어 칸으로 알린다, 2026-10-24)
    out["result_ready"] = out["state"] in ("done", "needs_review")
    if out["state"] == "failed":
        out["next"] = out.get("next") or "실패: log_tail 의 마지막 오류를 사용자에게 알리고, 고칠 수 있는 입력(컷 설명·콘티)이면 고친 뒤 같은 도구를 다시 부른다"
    partial = next((ln for ln in reversed(lines) if ln.startswith("끝난 컷:")), None)
    if partial and out["state"] == "needs_review":  # 일부 컷만 실패(comfy_panel 종료 2): 끝난 컷으로 이어가고 실패한 컷만 다시
        failed_line = next((ln for ln in reversed(lines) if ln.startswith("실패한 컷:")), "")
        out["partial"] = {"done": partial.split(":", 1)[1].split("(")[0].strip(), "failed": failed_line.split(":", 1)[1].strip() if failed_line else ""}
        out["next"] = (f"일부만 실패. 끝난 컷({out['partial']['done']})은 지금 compare_panel·adopt_panel 로 이어가고, "
                       f"실패한 컷({out['partial']['failed']})만 같은 도구로 다시 그린다. 사용자가 시킨 다음 단계(채택·고치기·합성)를 끝난 컷으로 먼저 진행한다")
    # 그림체 닮음 점수 줄(tools/style_fidelity.py: "  p043.png: 62백분위 통과  (경로)")을 칸으로: 긴 로그 끝에서 잘려 모델이 못 보는 일을 막는다
    style = [m.groups() for ln in lines for m in [re.match(r"^\s+(\S+?)\.\w+: (\d+)백분위 (통과|걸림)", ln)] if m]
    if style:
        out["style"] = {"pct": {pid: int(p) for pid, p, _ in style}, "failed": [pid for pid, _, v in style if v == "걸림"]}
        if out["state"] == "needs_review" and out["style"]["failed"] and not partial:
            out["next"] = (f"그림은 다 그렸지만 원작 그림체와 덜 닮은 컷이 있다({', '.join(out['style']['failed'])}): compare_panel 로 원작과 나란히 보여 주고 "
                           "다시 그릴지 사용자에게 묻는다. 나머지 컷은 사용자가 시킨 다음 단계로 이어간다")
    out["log_tail"] = "\n".join(lines[-tail_lines:]) if tail_lines else ""
    return out


@mcp.tool()
def job_status(job_id: str = "", wait: int = 0, tail_lines: int = 40, series: str = "", episode: str = "", running_only: bool = False, limit: int = 8) -> str:
    """작업(job)의 상태(JSON). job_id 를 비우면 최근 작업 목록(최신순, series·episode 로 좁히고 running_only 면 도는 것만): 작업 번호를 잃었거나 새 대화에서
    '아까 시킨 거 어디까지 됐어?' 에 답할 때. wait 를 주면 끝날 때까지 최대 wait 초(50까지) 기다렸다가 답한다: 짧은 작업(컷 몇 장)은 wait=50 으로 부르고,
    같은 작업으로 최대 4번(약 5분)까지. 그래도 running 이면 사용자에게 작업 번호·panels_done/panels_total·eta_s 를 알리고 답을 끝낸다(작업은 대화와 상관없이
    이어 돈다). 한 화 만들기처럼 긴 작업은 기다리지 말고 바로 알린다. wait 없이 몇 초 간격으로 되풀이하지 않는다.
    state: starting · running · done · failed · needs_review(사람 확인이 필요함, 실패 아님: review 의 컷이나 candidates 의 시안을 사용자에게 보여 준다)
    · lost(프로세스가 죽어 끊김: 돌고 있지 않다. 로그 끝을 보고 다시 시킨다).
    panels_done/panels_total/eta_s = 진행(그리는 작업), stalled = 로그가 오래 안 늘었거나 프로세스가 죽음, gates = 준비 작업의 관문 통과 여부,
    review = 그린 컷 중 검수가 사람 확인으로 남긴 컷(panel·issues·note), drawn · links = 그린 컷·내보낸 파일 링크.
    (예전 job_status(wait=50) 를 여기에 합침, 2026-10-22. 2026-10-16 agent app 시험: 몇 초 간격 되풀이가 반복 방지에 걸려 다 그린 결과를 보고하지 못했다.
    예전 list_jobs 도 job_id 를 비운 꼴로 합침, 2026-10-23)"""
    if not job_id.strip():
        return _list_jobs(series, episode, running_only, limit)
    if not re.fullmatch(r"[\w-]+", job_id):
        return json.dumps({"error": "bad_job_id"})
    end = time.time() + max(0, min(int(wait or 0), 50))
    while time.time() < end:
        st = _json(JOBS / f"{job_id}.status.json", None) or {}
        if st.get("state") not in (None, "starting", "running") or (st.get("state") == "running" and not _alive(st.get("pid"))):
            break  # 죽은 프로세스가 running 으로 남긴 작업은 기다리지 않는다(_job_info 가 lost 로 알린다)
        time.sleep(2)
    return json.dumps(_job_info(job_id, tail_lines), ensure_ascii=False)


def _list_jobs(series: str = "", episode: str = "", running_only: bool = False, limit: int = 8) -> str:
    """최근 작업 목록(JSON, 최신순): 작업 번호·종류·작품·회차·state·진행(panels_done/panels_total)·eta_s·stalled. job_status(job_id='') 가 부른다."""
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


# ── 모델에게 보내는 도구 설명은 첫 문단(360자 안쪽)만. 긴 설명·규칙은 tool_help 로 읽는다(2026-10-22 사용자: 설명이 길고 규칙이
# 박혀 있어 도구를 몇 번 부르면 LLM 컨텍스트가 넘치고, 지시가 범용적으로 안 먹는다). 전체 글은 _FULL_DOCS 에 남긴다.
_FULL_DOCS: dict = {}


def _first_para(text: str, n: int = 360) -> str:
    t = (text or "").strip()
    para = " ".join(re.split(r"\n\s*\n", t, 1)[0].split())
    if len(para) > n:
        cut = para[:n]
        m = max(cut.rfind(". "), cut.rfind("다. "), cut.rfind(") "))
        para = (cut[: m + 1] if m > n // 3 else cut).rstrip() + "…"
    return para


for _t in mcp._tool_manager._tools.values():
    # 고치기 도구들은 예전엔 docstring 뒤에 FIX_LADDER 를 '+' 로 붙였는데, 그러면 docstring 이 아니게 되어 모델에게 설명이 비어 갔다
    # (2026-10-22: 옷 바꾸기 같은 부분 수정을 시켜도 edit_panel 을 모르던 원인). 전체 글에만 순서를 붙인다.
    _FULL_DOCS[_t.name] = (_t.description or "") + ("\n\n" + FIX_LADDER if _t.name in ("edit_panel",) else "")
    _short_desc = _first_para(_t.description or "")
    _t.description = _short_desc + (" (전체 설명·규칙: tool_help)" if _short_desc != " ".join((_t.description or "").split()) else "")


@mcp.tool()
def tool_help(name: str = "") -> str:
    """도구의 전체 설명(인자 뜻·규칙·주의)을 읽는다. name 을 비우면 도구 이름 목록."""
    if not name.strip():
        return "\n".join(sorted(_FULL_DOCS))
    k = name.strip().split("__")[-1]
    return _FULL_DOCS.get(k) or f"{k}: 그런 도구가 없다"


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
