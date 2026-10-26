"""tools/vast_cloud.py loras(push·pull·status) 회귀 테스트: 가짜 서버(ComfyUI /models/loras + 에이전트 /info·/upload·/download)로, 네트워크·GPU 없이.

- push: PC 보관함에 있고 서버에 없는 것만 올린다(크기 확인), pull: 서버에 있고 PC 에 없는 것만 내려받는다(바이트 같음)
- 대상은 series/*/lora/loras.json 에 적힌 파일 전부(켜짐·꺼짐 무관), 어디에도 없는 것은 missing_everywhere
- gpu_loras.py 는 그림(comfy) 역할 설정만 고른다
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("VAST_PROFILE", "vast_qwen")  # 다른 시험 모듈과 같은 프로세스에서 돌 때 설정을 바꿔치기하지 않는다(cfg 는 아래에서 덮는다)
import gpu_loras  # noqa: E402
import vast_cloud as V  # noqa: E402


class Fake(http.server.BaseHTTPRequestHandler):
    comfy = None  # 가짜 서버의 ComfyUI 폴더(Path)

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/models/loras":  # ComfyUI 는 models/loras 와 학습 노드가 저장하는 output/loras 를 함께 올린다
            names = set()
            for sub in ("models/loras", "output/loras"):
                d = Fake.comfy / sub
                names |= {p.relative_to(d).as_posix() for p in d.rglob("*.safetensors")} if d.exists() else set()
            return self._send(200, sorted(names))
        if u.path == "/info":
            return self._send(200, {"comfy_dir": Fake.comfy.as_posix(), "mode": "comfy"})
        if u.path == "/download":
            if self.headers.get("X-Token") != "tok":
                return self.send_error(403)
            if not Path(q["path"][0]).is_file():  # 진짜 에이전트는 open 이 터져 응답 없이 끊긴다: 어느 쪽이든 '없음'으로 다뤄야 한다
                return self.send_error(500)
            return self._send(200, Path(q["path"][0]).read_bytes(), "application/octet-stream")
        self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("X-Token") != "tok":
            return self.send_error(403)
        if u.path == "/upload":
            p = Path(q["path"][0])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
            return self._send(200, {"ok": True, "bytes": len(body)})
        self.send_error(404)


def test_loras_push_pull_status_against_fake_server():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        Fake.comfy = root / "server_comfy"
        (Fake.comfy / "models" / "loras" / "webtoon").mkdir(parents=True)
        (Fake.comfy / "output" / "loras" / "webtoon").mkdir(parents=True)
        (Fake.comfy / "output" / "loras" / "webtoon" / "b.safetensors").write_bytes(b"B" * 5000)  # 서버에만: 학습 노드가 저장하는 output/loras/
        (root / "models" / "loras" / "webtoon").mkdir(parents=True)
        (root / "models" / "loras" / "webtoon" / "a.safetensors").write_bytes(b"A" * 7000)  # PC 에만(지난 서버에서 학습해 둔 것)
        (root / "series" / "w1" / "lora").mkdir(parents=True)
        (root / "series" / "w1" / "lora" / "loras.json").write_text(json.dumps({
            "_style": {"file": "webtoon/a.safetensors", "on": False}, "hero": {"file": "webtoon/b.safetensors", "on": True},
            "gone": {"file": "webtoon/c.safetensors"}, "_style@base": {"trigger": "x"}}), encoding="utf-8")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        real = (V.ROOT, V.LORA_STORE, V.cfg, V.load_state)
        try:
            V.ROOT, V.LORA_STORE = root, root / "models" / "loras"
            V.cfg = lambda: {**real[2](), "local_ports": {"comfy": port, "agent": port}}
            V.load_state = lambda: {"token": "tok"}
            assert V.lora_files() == {"webtoon/a.safetensors", "webtoon/b.safetensors", "webtoon/c.safetensors"}  # 꺼진 것·trigger 만인 칸
            st = V.loras_sync("status")
            assert st["local_only"] == ["webtoon/a.safetensors"] and st["server_only"] == ["webtoon/b.safetensors"] and st["missing_everywhere"] == ["webtoon/c.safetensors"]
            up = V.loras_sync("push")
            assert up["pushed"] == ["webtoon/a.safetensors"]
            assert (Fake.comfy / "models" / "loras" / "webtoon" / "a.safetensors").read_bytes() == b"A" * 7000
            down = V.loras_sync("pull")
            assert down["pulled"] == ["webtoon/b.safetensors"]
            assert (root / "models" / "loras" / "webtoon" / "b.safetensors").read_bytes() == b"B" * 5000
            again = V.loras_sync("push")
            assert not again["pushed"] and set(again["both"]) == {"webtoon/a.safetensors", "webtoon/b.safetensors"}  # 두 번째는 할 일 없음
        finally:
            V.ROOT, V.LORA_STORE, V.cfg, V.load_state = real
            srv.shutdown()
    # 켜기 버튼 설정 중 그림 역할만 고른다(LLM 상자엔 ComfyUI 가 없다)
    assert gpu_loras.comfy_profiles("vast_cloud.py", ["vast_img", "vast_llm", "vast_qwen", "없는설정"]) == ["vast_img", "vast_qwen"]


if __name__ == "__main__":
    test_loras_push_pull_status_against_fake_server()
    print("OK")
