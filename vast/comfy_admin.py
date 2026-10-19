"""vast ComfyUI 서버 관리: 상태 · 버전 목록 · 버전 전환 · 재시작 · 서버 로그 · 모델 받기. MCP 도구(comfy_server · fetch_model · model_downloads)가 부른다.
(2026-10-19 사용자 "자율 MCP 로, 이 기능 자체를 그렇게": MOSS-Image 때문에 ComfyUI 0.37 → 0.42.1 을 손으로 올리며 한 일을 에이전트가 스스로 하게)

    .venv/Scripts/python vast/comfy_admin.py status | versions | logs [old] [줄수] | restart | switch <버전>

- 버전 전환·재시작은 공용 서버를 잠깐 멈춘다. 큐에 도는 작업이 있으면 하지 않는다(force 로만).
- 버전 전환 전에 서버 워크플로우를 state/workflow_backups/<시각>_before_<버전>/ 에 받아 둔다. 전환은 ComfyUI-Manager(git checkout),
  재시작은 supervisor(실행 스크립트가 requirements 를 다시 깔고 /workspace/.env 의 COMFYUI_ARGS 를 읽는다). Manager 의 reboot 은 같은 인자로
  프로세스만 다시 띄워 .env 를 안 읽었다.
- 서버 로그: 인스턴스 Jupyter 파일 API 로 /var/log/portal/comfyui.log(.old). ComfyUI 의 /internal/logs 는 재시작하면 지워져, 프로세스가 죽은 이유
  (CUDA illegal memory access 등)는 여기에만 남는다.
토큰·비밀번호는 출력하지 않는다.
"""
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy, instance  # noqa: E402

_ctx = ssl._create_unverified_context()


def _jupyter():
    i = instance()
    return f"https://{i['public_ipaddr']}:{i['ports']['8080/tcp'][0]['HostPort']}", i["jupyter_token"]


def read_file(path: str) -> str:
    base, tok = _jupyter()
    req = urllib.request.Request(f"{base}/api/contents/{path.strip('/')}?type=file&format=text", headers={"Authorization": f"token {tok}"})
    with urllib.request.urlopen(req, timeout=60, context=_ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))["content"]


def shell(cmd: str, wait: float = 15) -> str:
    """인스턴스 Jupyter 터미널에 명령 한 줄. 출력 끝 3000자."""
    import websocket
    base, tok = _jupyter()
    req = urllib.request.Request(f"{base}/api/terminals", data=b"{}", headers={"Authorization": f"token {tok}", "Content-Type": "application/json"},
                                 method="POST")
    name = json.loads(urllib.request.urlopen(req, timeout=30, context=_ctx).read())["name"]
    ws = websocket.create_connection(base.replace("https", "wss", 1) + f"/terminals/websocket/{name}", header=[f"Authorization: token {tok}"],
                                     sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=5)
    ws.send(json.dumps(["stdin", cmd + "\r"]))
    out, t0 = [], time.time()
    while time.time() - t0 < wait:
        try:
            m = json.loads(ws.recv())
            if m[0] == "stdout":
                out.append(m[1])
        except Exception:  # noqa: BLE001
            pass
    ws.close()
    urllib.request.urlopen(urllib.request.Request(f"{base}/api/terminals/{name}", headers={"Authorization": f"token {tok}"}, method="DELETE"),
                           timeout=30, context=_ctx)
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", "".join(out))[-3000:]


def status() -> dict:
    c = Comfy()
    s = c.req("/system_stats")
    q = c.req("/queue")
    d = (s.get("devices") or [{}])[0]
    return {"comfyui_version": s["system"].get("comfyui_version"), "argv": s["system"].get("argv"), "gpu": d.get("name"),
            "vram_free_gb": round(d.get("vram_free", 0) / 1e9, 1), "vram_total_gb": round(d.get("vram_total", 0) / 1e9, 1),
            "ram_free_gb": round(s["system"].get("ram_free", 0) / 1e9, 1), "queue_running": len(q.get("queue_running", [])),
            "queue_pending": len(q.get("queue_pending", []))}


def versions() -> dict:
    return Comfy().req("/comfyui_manager/comfyui_versions")


def logs(old: bool = False, lines: int = 80) -> str:
    t = read_file("var/log/portal/comfyui.log" + (".old" if old else ""))
    return "\n".join(t.splitlines()[-lines:])


def _busy(force: bool):
    st = status()
    if (st["queue_running"] or st["queue_pending"]) and not force:
        raise RuntimeError(f"서버 큐에 작업이 있다(도는 것 {st['queue_running']} · 기다리는 것 {st['queue_pending']}). 끝난 뒤 하거나 force")


def _wait_up(timeout: float = 400) -> dict:
    t0 = time.time()
    time.sleep(10)
    while time.time() - t0 < timeout:
        try:
            return {**status(), "up_after_s": int(time.time() - t0)}
        except Exception:  # noqa: BLE001
            time.sleep(6)
    raise RuntimeError(f"{timeout}초 안에 다시 뜨지 않았다. logs(old=True) 로 이유를 본다")


def restart(force: bool = False) -> dict:
    """supervisor 로 ComfyUI 재시작(실행 스크립트·/workspace/.env 를 다시 읽는다)."""
    _busy(force)
    out = shell("supervisorctl restart comfyui", 14)
    return {"supervisor": [l for l in out.splitlines() if l.startswith("comfyui:")], **_wait_up()}


def backup_workflows(tag: str) -> str:
    c = Comfy()
    out = ROOT / "state" / "workflow_backups" / f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}"
    out.mkdir(parents=True, exist_ok=True)
    names = [n for n in c.req("/api/userdata?dir=workflows&recurse=true") if isinstance(n, str)]
    for n in names:
        (out / n.replace("/", "__")).write_bytes(c.req("/api/userdata/" + urllib.parse.quote("workflows/" + n, safe=""), raw=True))
    (out / "_info.json").write_text(json.dumps({"from": status(), "workflows": names}, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(out)


def switch(version: str, force: bool = False) -> dict:
    """워크플로우 백업 → ComfyUI-Manager 로 버전 전환 → supervisor 재시작 → 상태. 버전은 versions() 목록 중 하나."""
    vs = versions()
    if version not in vs.get("versions", []):
        raise RuntimeError(f"없는 버전 {version}: {vs.get('versions')}")
    if version == vs.get("current"):
        return {"already": version, **status()}
    _busy(force)
    bk = backup_workflows(f"before_{version}")
    Comfy().req("/comfyui_manager/comfyui_switch_version", {"ver": version}, raw=True, timeout=600)
    now = versions().get("current")
    if now != version:
        raise RuntimeError(f"전환이 안 됐다(지금 {now}). 백업 {bk}")
    return {"switched": f"{vs.get('current')} → {version}", "backup": bk, **restart(force=True)}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), ensure_ascii=False))
    elif cmd == "versions":
        print(json.dumps(versions(), ensure_ascii=False))
    elif cmd == "logs":
        old = "old" in sys.argv[2:]
        n = next((int(x) for x in sys.argv[2:] if x.isdigit()), 80)
        print(logs(old, n))
    elif cmd == "restart":
        print(json.dumps(restart("force" in sys.argv), ensure_ascii=False))
    elif cmd == "switch":
        print(json.dumps(switch(sys.argv[2], "force" in sys.argv), ensure_ascii=False))


if __name__ == "__main__":
    main()
