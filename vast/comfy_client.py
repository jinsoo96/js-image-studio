"""ComfyUI 서버 도우미(어느 ComfyUI 든). 어디에 붙을지는 환경변수로 정한다(comfy-workflow-mcp 와 같은 이름):
- COMFY_URL 이 있으면 그 서버(로컬·사내·다른 클라우드). 인증은 COMFY_USER+COMFY_PASSWORD(기본 인증) · COMFY_TOKEN(Bearer) · COMFY_HEADERS(JSON).
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
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CRED = Path(r"D:\.claude\CREDENTIALS.local.md")


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


def instance():
    req = urllib.request.Request("https://console.vast.ai/api/v0/instances/?owner=me",
                                 headers={"Authorization": f"Bearer {vast_key()}", "User-Agent": "webtoon-studio"})
    with urllib.request.urlopen(req, timeout=30) as r:
        inst = json.loads(r.read().decode())["instances"]
    if os.environ.get("VAST_INSTANCE_ID"):
        inst = [i for i in inst if str(i.get("id")) == os.environ["VAST_INSTANCE_ID"]]
    run = [i for i in inst if i.get("actual_status") == "running"] or inst
    if not run:
        sys.exit("인스턴스가 없다")
    return run[0]


class Comfy:
    """self.base(주소) · self.headers(인증 머리) · self.secret(화면·기록에서 가릴 값) · self.info."""

    def __init__(self):
        e = os.environ
        if e.get("COMFY_URL"):
            self.base = e["COMFY_URL"].rstrip("/")
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
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=body, headers=h), timeout=timeout) as r:
            b = r.read()
            return b if raw else json.loads(b.decode("utf-8")) if b else None

    def upload_image(self, path: Path, name: str = "") -> str:
        name = name or path.name
        bnd = uuid.uuid4().hex
        body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n").encode() \
            + path.read_bytes() + f"\r\n--{bnd}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{bnd}--\r\n".encode()
        return self.req("/upload/image", body, ctype=f"multipart/form-data; boundary={bnd}")["name"]

    def queue(self, prompt: dict, workflow: dict | None = None) -> str:
        """workflow(화면용 워크플로우)를 주면 결과 PNG·서버 기록에 같이 남아 ComfyUI 화면에서 그 작업을 캔버스로 연다.
        (2026-10-15 사용자 "comfyui 보니까 워크플로우를 사용한 느낌이 안 난다": API 그래프만 보내면 화면에서 열리지 않았다)"""
        body = {"prompt": prompt, "client_id": "webtoon-studio"}
        if workflow:
            body["extra_data"] = {"extra_pnginfo": {"workflow": workflow}}
        return self.req("/prompt", body)["prompt_id"]

    def wait(self, pid: str, timeout=900) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            h = self.req(f"/history/{pid}")
            if h and pid in h and (h[pid].get("outputs") or h[pid].get("status", {}).get("completed") is not None):
                return h[pid]
            time.sleep(2)
        raise TimeoutError(pid)

    def fetch_model(self, folder: str, url: str) -> str:
        """서버의 Model Manager Neo 로 HuggingFace 파일을 models/<folder>/ 에 받는다(이미 있으면 건너뜀)."""
        name = url.rsplit("/", 1)[-1]
        if name in (self.req(f"/models/{folder}") or []):
            return "있음"
        repo = "/".join(urllib.parse.urlparse(url).path.strip("/").split("/")[:2])
        sub = url.split("/resolve/main/", 1)[1].rsplit("/", 1)[0] if "/" in url.split("/resolve/main/", 1)[1] else ""
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}/tree/main/{sub}", timeout=30) as r:
            size = next(x["size"] for x in json.loads(r.read().decode()) if x["path"].endswith("/" + name) or x["path"] == name)
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
        uc = os.environ.get("WEBTOON_MODEL", "uc").strip().lower() == "uc"  # 그리는 모델 판(saved_workflow.MODELS)
        need = {"diffusion_models": "qwen-image-2.1-UC-fp8.safetensors" if uc else "qwen_image_2.1_bf16.safetensors",
                "text_encoders": "qwen3vl_8b_int8_convrot.safetensors",
                "vae": "qwen_image_2.1_vae_bf16.safetensors", "loras": "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"}
        miss = [n for f, n in need.items() if n not in (c.req(f"/models/{f}") or [])]
        print(f"인스턴스 {c.info['id']} {c.info['gpu_name']} {c.info['actual_status']} · ComfyUI {s['system'].get('comfyui_version')}")
        print(f"VRAM 남음 {d.get('vram_free', 0) / 1e9:.1f} / {d.get('vram_total', 0) / 1e9:.1f}GB")
        print(f"서버 큐: 도는 것 {len(q.get('queue_running', []))} · 기다리는 것 {len(q.get('queue_pending', []))} (그중 이 작업실 것 {mine})")
        print("Qwen Image 2.1 모델: " + ("다 있음" if not miss else "없음 " + ", ".join(miss)))
    elif cmd == "fetch":
        print(sys.argv[3].rsplit("/", 1)[-1], c.fetch_model(sys.argv[2], sys.argv[3]))
    elif cmd == "downloads":
        for t in c.req("/model-manager/download/task").get("data", []):
            print(t.get("fullname"), t.get("status"), "%.0f%%" % (t.get("progress") or 0), "%.1fGB" % ((t.get("totalSize") or 0) / 1e9), t.get("error") or "")
    elif cmd == "nodes":
        oi = c.req("/object_info", timeout=120)
        pat = re.compile(sys.argv[2], re.I)
        for k, v in oi.items():
            if pat.search(k) or pat.search(v.get("display_name", "")) or pat.search(v.get("python_module", "")):
                print(k, "|", v.get("display_name"), "|", v.get("python_module"))
