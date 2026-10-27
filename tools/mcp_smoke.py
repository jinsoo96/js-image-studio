"""웹툰 MCP 읽기 도구를 모든 작품·회차에 불러 오류가 나는지 본다(GPU·대화 모델 안 씀, 아무것도 고치지 않음, 돈 0).
도구를 고칠 때마다 먼저 이걸 돌리고, 돈이 드는 미사일 스튜디오 시험(tools/studio_test.py)은 그 뒤에 가끔.
(2026-10-24 사용자 "괜히 엄한 돈 쓸 거면 보고나 하고, mcp 고도화면 계속": 공짜로 할 수 있는 시험을 따로 둔다)

    .venv/Scripts/python tools/mcp_smoke.py [--url http://127.0.0.1:7440/mcp]

작품마다: read_doc · find(폴더 이름·작품 제목) · 회차마다 list_panels(맨 앞 총계 줄) · 앞 컷 셋 image_info · image_versions,
사람이 부르는 이름(작품 제목 + "1화", 판이 있으면 "1화 v2")으로 list_panels. 작품 이름은 박지 않고 series.json·README 제목에서 읽는다.
끝에 tools/hardcode_scan.py 로 도구 코드·도구 설명에 특정 작품 이름이 박혔는지도 본다(박혔으면 실패),
tools/prompt_dryrun.py 로 작품마다 그리기 지시를 GPU 없이 만들어 본다(약 1~2분).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("COMFY_NO_RETRY", "1")  # 뒤에서 부르는 prompt_dryrun 이 꺼진 서버를 기다리지 않게(vast/comfy_client.req)

import requests

ROOT = Path(__file__).resolve().parents[1]
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class Mcp:
    def __init__(self, url: str):
        self.url, self.n = url, 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcp-smoke", "version": "1"}})

    def rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        r = requests.post(self.url, headers=H, json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}, timeout=300)
        txt = r.content.decode("utf-8")
        if "data:" in txt:
            ev = [e for e in txt.replace("\r\n", "\n").split("\n\n") if "data:" in e][-1]
            txt = "\n".join(ln[5:].lstrip() for ln in ev.split("\n") if ln.startswith("data:"))
        return json.loads(txt)

    def call(self, tool: str, **args) -> tuple[bool, str]:
        """(오류 아님, 결과 글)."""
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        res = r.get("result") or {}
        txt = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text") or json.dumps(r, ensure_ascii=False)
        bad = bool(res.get("isError")) or "error" in r or txt.lstrip().startswith('{"error"')
        return not bad, txt


def title_of(sd: Path) -> str:
    sj = sd / "series.json"
    if sj.exists():
        t = json.loads(sj.read_text(encoding="utf-8")).get("title")
        if t:
            return str(t)
    readme = sd / "README.md"
    if readme.exists():
        m = re.search(r"^#\s*(.+)$", readme.read_text(encoding="utf-8"), re.M)
        if m:
            return re.split(r"\s+[—-]\s+", m.group(1).strip())[0]
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:7440/mcp")
    ap.add_argument("--log", action="store_true", help="결과 한 줄을 state/logs/mcp_smoke.log 에 덧붙인다(예약작업 WEBTOON-mcp-smoke, status 가 마지막 줄을 보인다)")
    a = ap.parse_args()
    try:  # 예약작업(콘솔 없음)에서 한글을 찍다 죽지 않게
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    m = Mcp(a.url)
    fails, calls = [], 0

    def t(tool, **args):
        nonlocal calls
        calls += 1
        ok, txt = m.call(tool, **args)
        if not ok:
            fails.append(f"{tool} {json.dumps(args, ensure_ascii=False)} → {txt[:200]}")
        return ok, txt

    for sd in sorted(p for p in (ROOT / "series").iterdir() if p.is_dir() and not p.name.startswith("_")):
        slug = sd.name[:-3] if sd.name.endswith("-hf") else sd.name
        title = title_of(sd)
        t("read_doc", series=slug)
        t("find", query=slug)
        if title:
            t("find", query=title)
        eps = sorted(p for p in (sd / "episodes").iterdir() if p.is_dir() and (p / "storyboard.json").exists()) if (sd / "episodes").is_dir() else []
        for ep in eps:
            ok, lp = t("list_panels", series=slug, episode=ep.name)
            if ok and not re.match(r"^\d+컷\(", lp):
                fails.append(f"list_panels {slug} {ep.name}: 맨 앞 총계 줄이 없다")
            ids = [ln.split()[0] for ln in lp.splitlines()[1:] if ln[:1] == "p"][:3] if ok else []
            for pid in ids:
                t("image_info", series=slug, episode=ep.name, panel=pid)
            if ids:
                t("image_versions", series=slug, episode=ep.name, panel=ids[-1])
        # 사람이 부르는 이름: '<작품 제목> 1화', 판이 있으면 '1화 v2'
        if title and any(e.name == "ep001" for e in eps):
            t("list_panels", series=title, episode="1화")
            v = next((e.name for e in eps if re.fullmatch(r"ep001_v\d+", e.name)), "")
            if v:
                t("list_panels", series=title, episode="1화 " + v.split("_")[1])
            # '<짧은 작품 이름> 1화 <인물 한글 이름>' 찾기: 회차 낱말까지 컷 글에서 찾아 다섯 작품 모두 0건이던 꼴(2026-10-24)
            board = json.loads((sd / "episodes" / "ep001" / "storyboard.json").read_text(encoding="utf-8"))
            cast = json.loads((sd / "cast.json").read_text(encoding="utf-8")) if (sd / "cast.json").exists() else {}
            cid = next((c for p in board.get("panels", []) for c in p.get("characters", []) if isinstance(cast.get(c), dict) and cast[c].get("ko")), "")
            if cid:
                short = re.sub(r"\s*[\(（].*?[\)）]", "", title).strip()
                ko = re.sub(r"\s*\(.*?\)", "", cast[cid]["ko"])
                ok, r = t("find", query=f"{short} 1화 {ko}")
                if ok and '"total": 0' in r:
                    fails.append(f"find '{short} 1화 {ko}' 이 0건")
        print(f"{sd.name}: 회차 {len(eps)}", flush=True)
    # 도구 코드·모델이 읽는 설명에 특정 작품 이름이 박혔는지(tools/hardcode_scan.py, 같은 공짜 점검)
    sys.path.insert(0, str(ROOT / "tools"))
    import hardcode_scan
    for path, n, sname, what, t, kind in hardcode_scan.scan():
        if kind in ("코드", "모델 글"):
            fails.append(f"작품 이름이 박힘 {path}:{n} [{kind}] {sname} {what} {t!r}")
    # 다섯 작품의 그리기 지시를 GPU 없이 만들어 본다(tools/prompt_dryrun.py: 참고 그림·인물 빠짐·작품 사이 새어 듦·화면 밖 안내)
    import subprocess
    dr = subprocess.run([sys.executable, str(ROOT / "tools" / "prompt_dryrun.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    fails += [ln.strip() for ln in dr.stdout.splitlines() if ln.strip().startswith("문제:")] or ([] if dr.returncode == 0 else [f"prompt_dryrun 실패: {(dr.stderr or dr.stdout)[-300:]}"])
    print(next((ln for ln in dr.stdout.splitlines() if ln.startswith("그리기 지시")), "그리기 지시: 결과 줄 없음"))
    print(f"\n도구 호출 {calls}번, 오류 {len(fails)}")
    for f in fails:
        print("  실패:", f)
    if a.log:
        import time
        log = ROOT / "state" / "logs" / "mcp_smoke.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M')} 도구 {calls}번 오류 {len(fails)}" + ("" if not fails else " · " + " | ".join(fails)[:600]) + "\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
