"""ComfyUI 서버 도우미(어느 ComfyUI 든). 어디에 붙을지는 환경변수로 정한다(comfy-workflow-mcp 와 같은 이름):
- COMFY_URL 이 있으면 그 서버(로컬·다른 서버). 인증은 COMFY_USER+COMFY_PASSWORD(기본 인증) · COMFY_TOKEN(Bearer) · COMFY_HEADERS(JSON).
- COMFY_URL 이 없고 state/comfy_url.txt 가 있으면 그 주소(MCP·스크립트를 한 번에 옮기는 스위치. 지우면 vast 로 돌아간다).
  2026-10-22 공용 vast 가 꺼져 RunPod(tools/runpod_cloud.py, RUNPOD_PROFILE=runpod_qwen)로 옮길 때 생김.
- 없으면 vast.ai: 키는 VAST_KEY(또는 VAST_API_KEY) 또는 D:\\.claude\\CREDENTIALS.local.md(vast.ai 절), 출력하지 않는다. VAST_INSTANCE_ID 로 고른다.
  접속 = 기본 인증(vastai / 인스턴스 jupyter_token). 토큰·주소는 vast.ai API 로 매번 읽는다(인스턴스를 다시 만들면 바뀐다).
(2026-10-18 사용자 "vast 의존이 있나, 비하드코딩으로": 전에는 vast 에만 붙었다)

    python vast/comfy_client.py info                 GPU·버전·노드 수
    python vast/comfy_client.py models [폴더]         모델 폴더 목록 / 그 폴더 파일
    python vast/comfy_client.py workflows            저장된 워크플로우 목록
    python vast/comfy_client.py get-workflow <이름> <저장 경로>
    python vast/comfy_client.py nodes <찾을 말>       설치된 노드 이름 찾기
    python vast/comfy_client.py fetch <모델폴더> <HF resolve URL>   Model Manager Neo 로 서버에 받기
    python vast/comfy_client.py downloads            받는 중인 작업
"""
import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CRED = Path(r"D:\.claude\CREDENTIALS.local.md")
TARGET = Path(__file__).resolve().parents[1] / "state" / "comfy_url.txt"


def vast_key() -> str:
    k = os.environ.get("VAST_KEY") or os.environ.get("VAST_API_KEY")
    if k:
        return k
    t = CRED.read_text(encoding="utf-8")
    sec = t[t.find("### vast.ai"):]
    m = re.search(r"API 키[^\n]*`([0-9a-f]{64})`", sec)
    if not m:
        sys.exit("vast.ai API 키를 찾지 못했다(VAST_KEY 또는 CREDENTIALS vast.ai 절)")
    return m.group(1)


INSTANCE_CACHE = Path(__file__).resolve().parents[1] / "state" / "vast_instance.json"  # 깃에 안 올라가는 state/
INSTANCE_TTL = 600


def instance():
    """돌고 있는 vast 인스턴스. 10분 동안은 저장해 둔 것을 쓴다: 도구가 프로세스마다 vast API 를 물어 429(요청이 너무 많음)로 막혔다(2026-10-19).
    API 가 막히면 오래된 저장본이라도 쓴다."""
    cached = None
    try:
        cached = json.loads(INSTANCE_CACHE.read_text(encoding="utf-8"))
        if time.time() - cached.get("_at", 0) < INSTANCE_TTL and not os.environ.get("VAST_INSTANCE_ID"):
            return cached
    except Exception:  # noqa: BLE001
        cached = None
    req = urllib.request.Request("https://console.vast.ai/api/v0/instances/?owner=me",
                                 headers={"Authorization": f"Bearer {vast_key()}", "User-Agent": "webtoon-studio"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            inst = json.loads(r.read().decode())["instances"]
    except urllib.error.HTTPError as e:
        if e.code == 429 and cached:
            return cached
        raise
    if os.environ.get("VAST_INSTANCE_ID"):
        inst = [i for i in inst if str(i.get("id")) == os.environ["VAST_INSTANCE_ID"]]
    run = [i for i in inst if i.get("actual_status") == "running"] or inst
    if not run:
        sys.exit("인스턴스가 없다")
    keep = {k: run[0].get(k) for k in ("id", "public_ipaddr", "ports", "jupyter_token", "gpu_name", "gpu_ram", "dph_total", "actual_status")}
    try:
        INSTANCE_CACHE.parent.mkdir(exist_ok=True)
        INSTANCE_CACHE.write_text(json.dumps({**keep, "_at": time.time()}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return keep


class Comfy:
    """self.base(주소) · self.headers(인증 머리) · self.secret(화면·기록에서 가릴 값) · self.info."""

    def __init__(self):
        e = os.environ
        url = e.get("COMFY_URL") or (TARGET.read_text(encoding="utf-8").strip() if TARGET.exists() else "")
        if url:
            self.base = url.rstrip("/")
            self.headers = json.loads(e["COMFY_HEADERS"]) if e.get("COMFY_HEADERS") else {}
            if e.get("COMFY_TOKEN"):
                self.headers["Authorization"], self.secret = f"Bearer {e['COMFY_TOKEN']}", e["COMFY_TOKEN"]
            elif e.get("COMFY_USER"):
                self.headers["Authorization"] = "Basic " + base64.b64encode(f"{e['COMFY_USER']}:{e.get('COMFY_PASSWORD', '')}".encode()).decode()
                self.secret = e.get("COMFY_PASSWORD", "")
            else:
                self.secret = ""
            self.info = {"server": self.base}
            return
        i = instance()
        port = e.get("VAST_COMFY_PORT", "8188")
        self.base = f"http://{i['public_ipaddr']}:{i['ports'][f'{port}/tcp'][0]['HostPort']}"
        self.secret = i["jupyter_token"]
        self.headers = {"Authorization": "Basic " + base64.b64encode(f"vastai:{self.secret}".encode()).decode()}
        self.info = {k: i.get(k) for k in ("id", "gpu_name", "gpu_ram", "dph_total", "actual_status")}

    def req(self, path, data=None, ctype="application/json", timeout=60, raw=False):
        h = {**self.headers, "User-Agent": "webtoon-studio"}
        body = None
        if data is not None:
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            h["Content-Type"] = ctype
        # vast 프록시가 가끔 한 번 끊기거나 늦는다: 서버에서 다 그린 컷이 결과 묻기(/history) 한 번이 시간 초과라 통째로 실패했다(2026-10-21 외출 p101).
        # 다시 보내도 같은 요청(읽기, 덮어쓰는 그림 올리기)만 세 번까지. 큐에 넣기(/prompt)는 두 번 들어갈 수 있어 다시 보내지 않는다
        tries = 3 if data is None or path.startswith("/upload/") else 1
        # 돈 0 시험(prompt_dryrun·mcp_smoke)은 서버가 꺼져 있으면 바로 실패해야 한다: 재시도 3번 + 3·6초 대기로 한 번에 15초, 컷마다 여러 번이라
        # 20분 한도를 넘겼다(2026-10-26). 실제 그리기는 그대로 끈기 있게(터널이 몇 초 끊겼다 돌아오는 일이 있다)
        if os.environ.get("COMFY_NO_RETRY"):
            tries = 1
        for k in range(tries):
            try:
                with urllib.request.urlopen(urllib.request.Request(self.base + path, data=body, headers=h), timeout=timeout) as r:
                    b = r.read()
                    return b if raw else json.loads(b.decode("utf-8")) if b else None
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and path.startswith("/prompt"):  # 서버가 그래프를 거절한 까닭(node_errors)을 남긴다: 400 만 보여 원인을 못 봤다
                    try:
                        print(f"[ComfyUI 거절 {e.code}] {e.read().decode('utf-8', 'replace')[:1500]}", file=sys.stderr, flush=True)
                    except Exception:  # noqa: BLE001
                        pass
                if e.code not in (502, 503, 504) or k == tries - 1:
                    raise
            except (TimeoutError, ConnectionError, urllib.error.URLError, http.client.HTTPException):
                if k == tries - 1:
                    raise
            time.sleep(3 * (k + 1))

    def upload_image(self, path: Path, name: str = "", subfolder: str = "", ctype: str = "image/png") -> str:
        """서버 input/(subfolder/) 에 올린다. 학습 데이터 폴더는 그림과 같은 이름의 캡션 .txt 도 같이 올린다(lora.py)."""
        name = name or path.name
        bnd = uuid.uuid4().hex
        sub = f"\r\n--{bnd}\r\nContent-Disposition: form-data; name=\"subfolder\"\r\n\r\n{subfolder}" if subfolder else ""
        body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() \
            + path.read_bytes() + f"\r\n--{bnd}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue{sub}\r\n--{bnd}--\r\n".encode()
        return self.req("/upload/image", body, ctype=f"multipart/form-data; boundary={bnd}")["name"]

    def queue(self, prompt: dict, workflow: dict | None = None) -> str:
        """workflow(화면용 워크플로우)를 주면 결과 PNG·서버 기록에 같이 남아 ComfyUI 화면에서 그 작업을 캔버스로 연다.
        (2026-10-15 사용자 "comfyui 보니까 워크플로우를 사용한 느낌이 안 난다": API 그래프만 보내면 화면에서 열리지 않았다)"""
        # 작업 번호를 이쪽에서 정해 보낸다(서버가 받아 준다): 응답이 끊겨도 그 번호가 큐·기록에 있으면 들어간 것이라 두 번 넣지 않고,
        # 없을 때만 다시 보낸다. 모델을 새로 올리는 첫 컷에서 응답 대기 시간 초과로 컷이 통째로 실패했다(2026-10-21 외출 p101 두 번)
        pid = str(uuid.uuid4())
        body = {"prompt": prompt, "client_id": "webtoon-studio", "prompt_id": pid}
        if os.environ.get("WEBTOON_QUEUE_FRONT") == "1":  # 사람이 기다리는 작업(대화에서 시킨 것)은 큐 앞으로(mcp/server.py ENV)
            body["front"] = True
        if workflow:
            body["extra_data"] = {"extra_pnginfo": {"workflow": workflow}}
        for k in range(3):
            try:
                return self.req("/prompt", body, timeout=120)["prompt_id"]
            except urllib.error.HTTPError as e:
                if e.code not in (502, 503, 504) or k == 2:
                    raise
            except (TimeoutError, ConnectionError, urllib.error.URLError, http.client.HTTPException):
                if k == 2:
                    raise
            time.sleep(5 * (k + 1))
            if self._queued(pid):
                return pid
        return pid

    def _queued(self, pid: str) -> bool:
        """이 번호의 작업이 서버 큐(돌고 있거나 기다림)나 기록에 있나."""
        try:
            q = self.req("/queue") or {}
            if any(len(x) > 1 and x[1] == pid for x in (q.get("queue_running") or []) + (q.get("queue_pending") or [])):
                return True
            return bool(self.req(f"/history/{pid}"))
        except Exception:  # noqa: BLE001
            return False

    def wait(self, pid: str, timeout=900) -> dict:
        t0, last_check, missing = time.time(), time.time(), 0
        while time.time() - t0 < timeout:
            h = self.req(f"/history/{pid}")
            if h and pid in h and (h[pid].get("outputs") or h[pid].get("status", {}).get("completed") is not None):
                return h[pid]
            # 30초마다 큐에도 기록에도 없는지 본다: 서버가 죽었다 다시 뜨면 작업이 사라지는데 6시간을 기다렸다(2026-10-21 무대 뒤 Restyle 학습)
            if time.time() - last_check > 30:
                last_check = time.time()
                missing = 0 if self._queued(pid) else missing + 1
                if missing >= 2:
                    raise RuntimeError(f"작업 {pid[:8]} 이 서버 큐·기록에서 사라졌다(ComfyUI 가 죽었다 다시 떴을 수 있다: comfy_admin.py logs 의 old 로그)")
            time.sleep(2)
        raise TimeoutError(pid)

    def fetch_model(self, folder: str, url: str, name: str = "") -> str:
        """서버의 Model Manager Neo 로 HuggingFace 파일을 models/<folder>/ 에 받는다(이미 있으면 건너뜀). name 을 주면 그 이름으로 저장한다
        (로라 저장소들이 lora.safetensors · pytorch_lora_weights.safetensors 처럼 같은 이름을 써서 겹친다)."""
        name = name or url.rsplit("/", 1)[-1]
        if name in (self.req(f"/models/{folder}") or []):
            return "있음"
        repo = "/".join(urllib.parse.urlparse(url).path.strip("/").split("/")[:2])
        sub = url.split("/resolve/main/", 1)[1].rsplit("/", 1)[0] if "/" in url.split("/resolve/main/", 1)[1] else ""
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}/tree/main/{sub}", timeout=30) as r:
            src = url.rsplit("/", 1)[-1]
            size = next(x["size"] for x in json.loads(r.read().decode()) if x["path"].endswith("/" + src) or x["path"] == src)
        fields = {"type": folder, "pathIndex": "0", "fullname": name, "description": "", "downloadPlatform": "huggingface",
                  "downloadUrl": url, "sizeBytes": str(size)}
        bnd = uuid.uuid4().hex
        body = b"".join(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items()) + f"--{bnd}--\r\n".encode()
        r = self.req("/model-manager/model", body, ctype=f"multipart/form-data; boundary={bnd}")
        if not r.get("success"):
            raise RuntimeError(r.get("error"))
        return r["data"]["taskId"]

    def download(self, img: dict, out: Path) -> Path:
        q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
        out.write_bytes(self.req("/view?" + q, raw=True, timeout=120))
        return out


if __name__ == "__main__":
    c = Comfy()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "info":
        s = c.req("/system_stats")
        print("인스턴스", c.info)
        print("ComfyUI", s["system"].get("comfyui_version"), "파이썬", s["system"].get("python_version", "")[:6], "torch", s["system"].get("pytorch_version"))
        for d in s.get("devices", []):
            print("GPU", d.get("name"), "VRAM %.1fGB" % (d.get("vram_total", 0) / 1e9), "남음 %.1fGB" % (d.get("vram_free", 0) / 1e9))
        print("노드 수", len(c.req("/object_info", timeout=120)))
    elif cmd == "models":
        if len(sys.argv) > 2:
            print("\n".join(c.req(f"/models/{sys.argv[2]}")))
        else:
            for f in c.req("/models"):
                try:
                    n = c.req(f"/models/{f}")
                    print(f, len(n), (n[:6] if n else ""))
                except Exception as e:
                    print(f, "?", str(e)[:40])
    elif cmd == "workflows":
        print("\n".join(c.req("/api/userdata?dir=workflows&recurse=true")))
    elif cmd == "get-workflow":
        data = c.req("/api/userdata/" + urllib.parse.quote("workflows/" + sys.argv[2], safe=""), raw=True)
        Path(sys.argv[3]).write_bytes(data)
        print("받음", sys.argv[3], len(data))
    elif cmd == "status":  # 가벼운 상태(MCP vast_status 가 부른다): GPU·큐·Qwen 모델
        s = c.req("/system_stats")
        d = (s.get("devices") or [{}])[0]
        q = c.req("/queue")
        mine = sum(1 for e in q.get("queue_running", []) + q.get("queue_pending", []) if len(e) > 3 and (e[3] or {}).get("client_id") == "webtoon-studio")
        from saved_workflow import models as draw_models  # 판 = 서버에 저장된 webtoon_vertical_<판> 워크플로우(그 워크플로우가 쓰는 모델 파일이 서버에 있나)
        miss = {k: v["missing"] for k, v in draw_models(refresh=True).items()}
        print(f"인스턴스 {c.info['id']} {c.info['gpu_name']} {c.info['actual_status']} · ComfyUI {s['system'].get('comfyui_version')}")
        print(f"VRAM 남음 {d.get('vram_free', 0) / 1e9:.1f} / {d.get('vram_total', 0) / 1e9:.1f}GB")
        print(f"서버 큐: 도는 것 {len(q.get('queue_running', []))} · 기다리는 것 {len(q.get('queue_pending', []))} (그중 이 작업실 것 {mine})")
        print("그리는 판: " + (" · ".join(f"{k} " + ("다 있음" if not m else "없음 " + ", ".join(m)) for k, m in miss.items()) or "서버에 webtoon_vertical_* 워크플로우가 없다"))
    elif cmd == "fetch":  # fetch <폴더> <HF 주소> [저장 이름]
        print(sys.argv[3].rsplit("/", 1)[-1], c.fetch_model(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""))
    elif cmd == "downloads":
        for t in c.req("/model-manager/download/task").get("data", []):
            print(t.get("fullname"), t.get("status"), "%.0f%%" % (t.get("progress") or 0), "%.1fGB" % ((t.get("totalSize") or 0) / 1e9), t.get("error") or "")
    elif cmd == "nodes":
        oi = c.req("/object_info", timeout=120)
        pat = re.compile(sys.argv[2], re.I)
        for k, v in oi.items():
            if pat.search(k) or pat.search(v.get("display_name", "")) or pat.search(v.get("python_module", "")):
                print(k, "|", v.get("display_name"), "|", v.get("python_module"))
