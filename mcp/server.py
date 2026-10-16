"""웹툰 작업실 MCP 서버(stdio). agent app(agent platform 데스크톱)의 로컬 MCP 로 붙여 agent platform 에이전트(Qwen 등)가 작업실을 다루게 한다.

    D:\\webtoon-studio\\.venv\\Scripts\\python.exe D:\\webtoon-studio\\mcp\\server.py

원칙
- 한 번 호출은 30초 안에 끝낸다(agent platform 브리지 시간 제한). 작화·합성·내보내기는 작업(job)으로 띄우고 job_status 로 본다.
- 크레딧을 쓰는 도구(draw_panels·redraw_panels)는 confirm_credits 가 예상 비용 이상이어야 돌고, 한 번에 40컷까지.
- vast_* 도구는 회사 공용 vast.ai GPU(Qwen Image 2.1)로 크레딧 없이 그린다. 한 번에 80컷까지, 인스턴스를 켜고 끄는 도구는 없다.
- 지우는 도구는 없다. 다시 그릴 컷의 원본은 panels_hf/_rejected/ 로 옮긴다.
- stdout 은 MCP 전송로다. 출력은 전부 반환값으로만(print 금지). 무거운 import 는 맨 위에서(Windows 워커 스레드 데드락).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # 맨 위에서 미리 읽는다(툴 안 첫 import 는 Windows 에서 멈출 수 있다)
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import vision_review  # noqa: E402  그림 검수(그림을 보는 모델에 컷 그림 + 컷 설명)
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv" / "Scripts" / "python.exe").exists() else sys.executable
TOOLS = ROOT / "tools"
JOBS = ROOT / "state" / "mcp_jobs"
SHEETS = ROOT / "state" / "mcp_sheets"
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
COST_PER_PANEL = 1.25  # Seedream 5.0 Pro 1.5k
MAX_PANELS = 40
SERIES_ALIAS = {"d-plus-18": "d-plus-18-hf", "d+18": "d-plus-18-hf", "dplus18": "d-plus-18-hf",
                "mudae-dwi": "mudae-dwi-hf", "무대뒤": "mudae-dwi-hf", "무대 뒤": "mudae-dwi-hf"}
DOCS = {"CLAUDE": "CLAUDE.md", "README": "README.md", "LESSONS": "LESSONS.md", "OPEN_ISSUES": "worklogs/OPEN-ISSUES.md",
        "ACTING_GUIDE": "worklogs/acting/GUIDE.md"}
SERIES_DOCS = {"README": "README.md", "SERIES": "SERIES.md", "STYLE": "STYLE.md", "ASSETS": "ASSETS.md"}

mcp = FastMCP("webtoon-studio", instructions=(
    "세로 스크롤 웹툰 작업실(D+18·무대 뒤). 처음엔 guide() 로 규칙을 읽고 status() 로 상태를 본다. "
    "힉스필드 작화는 크레딧(돈)을 쓴다: estimate_draw 로 비용을 보고 사용자에게 확인받은 뒤 draw_panels(confirm_credits=비용). "
    "vast_draw 는 회사 공용 GPU 로 크레딧 없이 그린다(결과는 panels_hf_vast, 본편 채택은 vast_adopt). "
    "오래 걸리는 일은 job_id 를 돌려주니 job_status 로 확인한다. 사용자에게는 한국어로 답한다."))


# ── 내부 도우미 ───────────────────────────────────────────────
def _series(name: str) -> Path:
    key = SERIES_ALIAS.get(name.strip().lower(), name.strip())
    p = ROOT / "series" / key
    if not (p.is_dir() and (p / "episodes").is_dir()) or key.startswith("_"):
        known = sorted(d.name for d in (ROOT / "series").iterdir() if d.is_dir() and not d.name.startswith("_"))
        raise ValueError(f"작품을 찾지 못했다: {name}. 있는 작품: {', '.join(known)}")
    return p


def _episode(series: str, episode: str) -> Path:
    if not re.fullmatch(r"ep\d{3}(_v\d+)?(_vast)?", episode.strip()):
        raise ValueError("회차 이름은 ep001, ep002_v2 같은 꼴이다(vast 판 회차는 뒤에 _vast)")
    p = _series(series) / "episodes" / episode.strip()
    if not p.is_dir():
        raise ValueError(f"회차 폴더가 없다: {p.relative_to(ROOT)}")
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


def _running_job(ep: Path):
    for st in JOBS.glob("*.status.json"):
        s = _json(st, {})
        spec = _json(st.with_name(st.name.replace(".status.json", ".json")), {})
        if s.get("state") == "running" and spec.get("episode_dir") == str(ep):
            pid = s.get("pid")
            try:
                os.kill(pid, 0)  # 살아 있는지만 본다
                return st.name.replace(".status.json", "")
            except Exception:
                continue
    return None


def _start_job(kind: str, ep: Path, steps: list, note: str = "") -> str:
    JOBS.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    spec = {"id": job_id, "kind": kind, "episode_dir": str(ep), "note": note, "steps": steps}
    f = JOBS / f"{job_id}.json"
    f.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([PY, str(ROOT / "mcp" / "job_runner.py"), str(f)], cwd=ROOT, env=ENV, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return job_id


def _ids(text: str) -> list:
    return [x for x in re.split(r"[,\s]+", text or "") if x]


# ── 읽기 도구 ─────────────────────────────────────────────────
@mcp.tool()
def guide() -> str:
    """작업실 규칙과 쓰는 순서(CLAUDE.md 전문 + 이 MCP 도구 쓰는 법). 작업을 시작하기 전에 꼭 한 번 읽는다."""
    tips = ("\n\n## 이 MCP 도구로 일하는 순서\n"
            "1) status() 로 작품·회차·크레딧 확인 → 2) read_doc 으로 작품 README·STYLE, LESSONS 해당 장을 읽는다\n"
            "3) 새 회차는 new_episode 로 앞 회차 틀을 복사하고, read_file 로 열어 사용자 이야기에 맞게 고친 뒤 write_file 로 쓴다\n"
            "4) prepare_episode → estimate_draw → 사용자에게 비용 확인 → draw_panels(confirm_credits=비용) → job_status\n"
            "5) review_panels 로 그림 검수(그림을 보는 모델이 판정) + contact_sheet 경로를 사용자에게 → 사용자가 동의한 컷만 build_panels.py 고쳐 redraw_panels → compose → job_status(막는 경고 0 확인) → export\n"
            "크레딧은 개인 결제다. 시안 남발 금지, 재사용 먼저, 끝나면 남은 크레딧을 알린다.\n"
            "vast(크레딧 0, 회사 공용 GPU): vast_status → vast_draw(turbo·expr 기본) → job_status → contact_sheet/review_panels(folder='vast') "
            "→ 걸린 컷은 seed 바꿔 vast_draw → 사용자가 고른 컷만 vast_adopt → compose. 새 인물이면 vast_make_assets.")
    return (ROOT / "CLAUDE.md").read_text(encoding="utf-8") + tips


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


@mcp.tool()
def contact_sheet(series: str, episode: str, ids: str = "", scene: str = "", folder: str = "") -> str:
    """검수용 모아보기 그림(컷 번호 붙임)을 만들고 파일 경로를 돌려준다. ids(쉼표) 또는 scene(장소 이름)으로 고른다.
    비우면 새로 그린 컷 전부. folder="vast" 면 vast 로 그린 컷(panels_hf_vast). 사용자에게 경로를 알려 직접 보게 한다."""
    ep = _episode(series, episode)
    sub = _folder(folder)
    pj = _panels_json(ep)
    want = _ids(ids)
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
    return f"모아보기 {len(tiles)}컷: {out}"


@mcp.tool()
def review_panels(series: str, episode: str, ids: str = "", scene: str = "", folder: str = "") -> str:
    """그림 검수(크레딧 안 씀): 그림을 보는 모델(qwen3.8-27b)이 컷 그림과 그 컷 설명(인물·옷·구도·카메라·연기·대사)을 맞대어
    인원·좌우·시선·옷·표정·배경을 판정하고 '다시' 컷엔 고칠 점을 준다. ids(쉼표) 또는 scene(장소 이름)으로 고르고, 비우면 새로 그린 컷 전부.
    folder="vast" 면 vast 로 그린 컷을 본다(vast 판은 다시 그려도 크레딧이 안 드니 vast_draw 로 시드만 바꿔 다시).
    8컷 이하는 바로 답하고, 더 많으면 작업으로 돌려 job_status 로 본다. '다시' 컷은 사용자에게 보여 주고 동의를 받은 뒤 고칠 점을 build_panels.py 에 반영해 redraw_panels."""
    ep = _episode(series, episode)
    sd = _series(series)
    sub = _folder(folder)
    vision_review.DIRS[:] = [sub] if sub != "panels_hf" else ["panels_hf", "panels"]
    try:
        pick = vision_review.pick_ids(ep, ids, scene)
    except ValueError as e:
        return str(e)
    if not pick:
        return "검수할 컷이 없다(ids·scene 을 확인)"
    if len(pick) <= 8:
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


def _writable(p: Path) -> bool:
    rel = p.relative_to(ROOT.resolve()).as_posix()
    if p.suffix.lower() not in TEXT_EXT:
        return False
    if re.match(r"series/[^/_][^/]*/", rel):  # 작품 폴더의 글 파일(콘티·컷 설명·문서·프롬프트). 그림 폴더는 확장자에서 걸린다
        return not re.search(r"/(panels_hf|panels|parts|out|review)/", rel)
    return rel.startswith("worklogs/") or rel in {"LESSONS.md", "reader/index.html"}


@mcp.tool()
def list_files(path: str = "series") -> str:
    """작업실 안 폴더의 파일 목록(작업실 기준 경로, 예: series/d-plus-18-hf/episodes/ep002_v2). 그림 폴더는 개수만 센다."""
    p = _inside(path)
    if not p.is_dir():
        return f"폴더가 아니다: {path}"
    out = []
    for c in sorted(p.iterdir()):
        if c.is_dir():
            n = sum(1 for _ in c.iterdir())
            out.append(f"[폴더] {c.name}/ ({n}개)")
        else:
            out.append(f"{c.name} ({c.stat().st_size // 1024}KB){' · 쓰기 가능' if _writable(c.resolve()) else ''}")
    return "\n".join(out[:300]) or "(비어 있음)"


@mcp.tool()
def read_file(path: str, offset: int = 0, max_chars: int = 12000) -> str:
    """작업실 안 글 파일 읽기(작업실 기준 경로). 긴 파일은 offset 으로 이어 읽는다. 콘티(make_storyboard.py)·컷 설명(build_panels.py)·문서를 볼 때."""
    p = _inside(path)
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
    eps = sorted(d.name for d in (s / "episodes").iterdir() if d.is_dir())
    tpl = template.strip() or eps[-1]
    src = s / "episodes" / tpl
    if not src.is_dir():
        return f"틀 회차가 없다: {tpl}"
    dst.mkdir(parents=True)
    copied = []
    for n in ("make_storyboard.py", "build_panels.py", "trailer.json"):
        if (src / n).exists():
            t = (src / n).read_text(encoding="utf-8").replace(f"episodes/{tpl}", f"episodes/{episode}").replace(f"episodes\\{tpl}", f"episodes\\{episode}")
            (dst / n).write_text(t, encoding="utf-8")
            copied.append(n)
    return (f"새 회차 {dst.relative_to(ROOT)} (틀 {tpl}): {', '.join(copied)} 복사. 이제 이 파일들을 새 이야기에 맞게 고친다. "
            "틀의 재사용 컷(REUSE)·컷 설명(P)·연기(ACT)는 새 회차에 맞게 지우거나 바꿔야 한다")


# ── 준비·비용 ─────────────────────────────────────────────────
@mcp.tool()
def prepare_episode(series: str, episode: str) -> str:
    """회차의 make_storyboard.py → build_panels.py 를 돌려 storyboard.json·panels.json 을 다시 만든다(크레딧 안 씀).
    재사용 컷 복사도 build_panels 가 한다. 결과의 '빠진 컷'·'연기 없는 인물 컷'이 비어 있어야 한다."""
    ep = _episode(series, episode)
    out = []
    for name in ("make_storyboard.py", "build_panels.py"):
        if (ep / name).exists():
            out.append(f"## {name}\n" + _run([PY, str(ep / name)], timeout=25))
        else:
            out.append(f"## {name} 없음(앞 회차를 본떠 먼저 만든다)")
    return "\n".join(out)


@mcp.tool()
def estimate_draw(series: str, episode: str, ids: str = "") -> str:
    """그릴 컷과 예상 크레딧. ids 를 비우면 panels.json 에서 아직 그림이 없는 컷 전부. 그리기 전에 이 결과를 사용자에게 보여 확인받는다."""
    ep = _episode(series, episode)
    pj = {p["id"]: p for p in _panels_json(ep)}
    want = _ids(ids) or [i for i in pj if not (ep / "panels_hf" / f"{i}.png").exists()]
    bad = [i for i in want if i not in pj]
    todo = [i for i in want if i in pj]
    c = _credits()
    return (f"그릴 컷 {len(todo)}개: {', '.join(todo) or '없음'}\n예상 크레딧 {len(todo) * COST_PER_PANEL:.2f}(장당 {COST_PER_PANEL}, 검열 오탐은 무료)\n"
            f"남은 크레딧 {c}\n" + (f"panels.json 에 없는 번호(그릴 수 없음): {', '.join(bad)}\n" if bad else "") +
            f"한 번에 {MAX_PANELS}컷까지. 기준 컷(SCENES anchor)이 먼저 그려져 있어야 한다.")


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
    busy = _running_job(ep)
    if busy:
        return f"이 회차에 도는 작업이 있다({busy}). 같은 컷을 겹쳐 그리면 안 된다. job_status 로 끝난 뒤 부른다"
    cmd = [PY, str(TOOLS / "hf_panel.py"), str(_series(series)), str(ep / "panels.json"), "--episode", ep.name, "--n", "1", "--only", ",".join(todo)]
    job = _start_job(kind, ep, [{"label": f"{len(todo)}컷 그리기(예상 {cost:.2f} 크레딧)", "cmd": cmd},
                                {"label": "합성용으로 놓기", "cmd": [PY, str(TOOLS / "hf_place.py"), str(ep)]}], note)
    return f"작업 시작 {job}: {len(todo)}컷, 예상 {cost:.2f} 크레딧. 한 컷에 약 1분. job_status('{job}') 로 확인"


@mcp.tool()
def draw_panels(series: str, episode: str, ids: str, confirm_credits: float) -> str:
    """컷을 힉스필드로 그린다(크레딧 사용). ids: 쉼표로 컷 번호(빈 값이면 아직 없는 컷 전부).
    confirm_credits 는 estimate_draw 의 예상 크레딧 이상이어야 돈다(사용자 확인을 받은 값). 끝나면 합성용 폴더에도 놓는다."""
    ep = _episode(series, episode)
    want = _ids(ids) or [p["id"] for p in _panels_json(ep) if not (ep / "panels_hf" / f"{p['id']}.png").exists()]
    return _draw(series, episode, want, confirm_credits, "draw")


@mcp.tool()
def redraw_panels(series: str, episode: str, ids: str, reason: str, confirm_credits: float) -> str:
    """검수에서 걸린 컷을 다시 그린다(크레딧 사용). 지금 그림은 panels_hf/_rejected/ 로 옮기고 새로 그린다.
    먼저 build_panels.py 의 그 컷 설명(행동·연기·카메라)을 고치고 prepare_episode 를 돌린 뒤 부른다. reason 에 걸린 이유를 적는다."""
    ep = _episode(series, episode)
    want = _ids(ids)
    if not want:
        return "다시 그릴 컷 번호를 준다"
    pj = {p["id"] for p in _panels_json(ep)}
    if [i for i in want if i not in pj]:
        return f"panels.json 에 없는 번호: {', '.join(i for i in want if i not in pj)}(재사용·합성 컷은 다시 그리지 않는다)"
    cost = len(want) * COST_PER_PANEL
    if confirm_credits < cost:
        return f"확인 값이 모자라다: 예상 {cost:.2f} 크레딧. 사용자 확인 뒤 confirm_credits={cost:.2f}"
    if _running_job(ep):
        return "이 회차에 도는 작업이 있다. 끝난 뒤 부른다"
    rej = ep / "panels_hf" / "_rejected"
    rej.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d%H%M")
    for i in want:
        f = ep / "panels_hf" / f"{i}.png"
        if f.exists():
            shutil.move(str(f), str(rej / f"{i}_{stamp}.png"))
    return _draw(series, episode, want, confirm_credits, "redraw", note=reason)


# ── vast.ai 공용 GPU(크레딧 0) ───────────────────────────────
# 회사 공용 vast.ai 의 ComfyUI(Qwen Image 2.1)로 그린다. 크레딧은 안 들지만 공용 장비라 한 번에 VAST_MAX 컷까지.
# 인스턴스 만들기·끄기·지우기 도구는 두지 않는다(vast/README.md).
VAST = ROOT / "vast"
VAST_MAX = 80


@mcp.tool()
def vast_status() -> str:
    """vast.ai 공용 GPU 상태: 인스턴스·남은 VRAM·서버 큐(이 작업실 것 포함)·Qwen Image 2.1 모델이 받아져 있는지. 크레딧 안 씀."""
    return _run([PY, str(VAST / "comfy_client.py"), "status"], timeout=25)


@mcp.tool()
def vast_draw(series: str, episode: str, ids: str = "", turbo: bool = True, expr: bool = True, aspect: str = "", seed: int = -1, fix: bool = True, check: bool = True) -> str:
    """컷을 vast GPU 의 Qwen Image 2.1 로 그린다(크레딧 0). 같은 panels.json 을 쓰고 결과는 panels_hf_vast/ 에 쌓인다(힉스필드 본편 panels_hf 는 안 건드림).
    ids: 쉼표로 컷 번호(비우면 vast 그림이 아직 없는 컷 전부). turbo=True 면 8스텝(컷당 약 30초), False 면 40스텝(약 2분).
    expr=True 면 연기에 맞는 표정 칸을 얼굴 참고로 넣는다(연기가 세짐). aspect 를 "2:3"·"9:16" 으로 주면 세로 컷.
    fix=True(기본)면 그린 뒤 그림 보는 사내 Qwen 이 남는 사람(중복·낯선 사람)을 찾아 편집으로 지운다(원본은 panels_hf_vast/_stage1/).
    check=True(기본)면 그림 검수로 시점·좌우·시선·표정·배경을 보고 표정·옷은 편집, 구도가 틀리면 시드를 바꿔 한 번 더 그린다(컷당 +20~60초).
    그래도 이상하면 seed 만 바꿔 그 컷을 다시 부른다. 본편으로 쓰려면 사용자가 고른 컷만 vast_adopt."""
    ep = _episode(series, episode)
    sd = _series(series)
    pj = [p["id"] for p in _panels_json(ep)]
    want = _ids(ids) or [i for i in pj if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    bad = [i for i in want if i not in pj]
    if bad:
        return f"panels.json 에 없는 번호: {', '.join(bad)}(재사용·합성 컷은 그리지 않는다)"
    if not want:
        return "그릴 컷이 없다(vast 그림이 다 있다. 다시 그리려면 ids 를 준다)"
    if len(want) > VAST_MAX:
        return f"공용 GPU 라 한 번에 {VAST_MAX}컷까지다({len(want)}컷). 나눠서 부른다"
    if aspect and aspect not in ("9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9"):
        return "aspect 는 9:16·2:3·3:4·1:1·4:3·3:2·16:9 중 하나"
    if not (sd / "assets" / "vast_refs").is_dir():
        return "이 작품의 vast 참고 그림이 없다. 먼저 vast_make_assets(series) 를 부른다"
    if _running_job(ep):
        return "이 회차에 도는 작업이 있다. job_status 로 끝난 뒤 부른다"
    cmd = [PY, str(VAST / "comfy_panel.py"), str(sd), str(ep / "panels.json"), "--episode", ep.name, "--only", ",".join(want), "--variant", "vast"]
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
    character: 인물 id(cast.json) 또는 all. what: base · expr · both. 이미 있는 그림은 건너뛴다. 새 인물 시트를 만든 뒤 한 번 부른다."""
    sd = _series(series)
    if what not in ("base", "expr", "both"):
        return "what 은 base · expr · both"
    if character != "all" and not (sd / "assets" / f"{character}_sheet.png").exists():
        return f"{character}_sheet.png 가 없다(인물 시트를 먼저 만든다). 있는 인물: " + ", ".join(
            sorted(p.name[:-len('_sheet.png')] for p in (sd / "assets").glob("*_sheet.png")))
    if _running_job(sd):
        return "이 작품에 도는 에셋 작업이 있다. 끝난 뒤 부른다"
    steps = [{"label": "시트에서 참고 그림 자르기", "cmd": [PY, str(VAST / "sheet_refs.py"), str(sd)]},
             {"label": f"에셋 만들기({character}, {what})", "cmd": [PY, str(VAST / "make_assets.py"), str(sd), character, what, "--res", "768"]}]
    job = _start_job("vast_assets", sd, steps)
    return f"작업 시작 {job}: 크레딧 0, 장당 약 3~4초(인물 하나 13장). job_status('{job}')"


@mcp.tool()
def vast_adopt(series: str, episode: str, ids: str, reason: str = "") -> str:
    """vast 로 그린 컷을 본편으로 쓴다: 지금 본편 그림(panels_hf)은 panels_hf/_rejected/ 로 옮기고 vast 그림을 그 자리에 복사한 뒤 합성용으로 놓는다.
    사용자가 고른 컷만 넘긴다(본편이 바뀐다). 지우는 것은 없다. 끝나면 compose 로 다시 합성."""
    ep = _episode(series, episode)
    want = _ids(ids)
    if not want:
        return "채택할 컷 번호를 준다"
    miss = [i for i in want if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    if miss:
        return f"vast 그림이 없는 컷: {', '.join(miss)}(vast_draw 로 먼저 그린다)"
    if _running_job(ep):
        return "이 회차에 도는 작업이 있다. 끝난 뒤 부른다"
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
    if _running_job(src):
        return "이 회차에 도는 작업이 있다. 끝난 뒤 부른다"
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
    if _running_job(ep):
        return "이 회차에 도는 작업이 있다. 끝난 뒤 부른다"
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
    return f"작업 시작 {job}. 끝나면 exports/{day}/{name}.* . job_status('{job}')"


@mcp.tool()
def job_status(job_id: str, tail_lines: int = 40) -> str:
    """작업(job) 상태와 로그 끝부분. state 가 running 이면 잠시 뒤 다시 본다(작화는 한 컷에 약 1분)."""
    if not re.fullmatch(r"[\w-]+", job_id):
        return "job_id 꼴이 아니다"
    st = _json(JOBS / f"{job_id}.status.json", None)
    spec = _json(JOBS / f"{job_id}.json", None)
    if spec is None:
        return f"작업이 없다: {job_id}"
    log = JOBS / f"{job_id}.log"
    tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]) if log.exists() else "(로그 없음)"
    return f"{job_id} {spec.get('kind')} | {json.dumps(st or {'state': 'starting'}, ensure_ascii=False)}\n--- 로그 끝 ---\n{tail}"


if __name__ == "__main__":
    mcp.run()
