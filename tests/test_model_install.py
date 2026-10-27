"""모델 하나를 못 받아도 그림 서버 전체가 죽지 않게: 설치는 나머지를 받고, 작업실 사본을 못 올린 까닭은 남고, 판이 없으면 그 까닭을 알린다."""
import io
import json
import re
import subprocess
import sys
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))

import saved_workflow as S  # noqa: E402


def test_bootstrap_keeps_downloading_after_one_model_fails(tmp_path):
    # 2026-10-27: 캐릭터 시트 LoRA 저장소 401 에서 설치가 멈춰 그 뒤 텍스트 인코더를 안 받았다
    src = (ROOT / "cloud" / "runpod_bootstrap.sh").read_text(encoding="utf-8")
    body = re.search(r"\$PY - <<'PY'\n(import hashlib.*?)\nPY\n", src, re.S).group(1)
    fake = tmp_path / "huggingface_hub.py"
    fake.write_text("import os\ndef hf_hub_download(repo, file, local_dir):\n"
                    "    if 'gone' in repo: raise OSError('401 Client Error')\n"
                    "    p = os.path.join(local_dir, os.path.basename(file)); os.makedirs(local_dir, exist_ok=True)\n"
                    "    open(p, 'w').write('x'); return p\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    body = body.replace("/workspace/logs/", str(logs).replace("\\", "/") + "/").replace("/workspace/hf_tmp", str(tmp_path / "hf").replace("\\", "/"))
    models = [{"repo": "a/ok", "file": "x/one.safetensors", "dest": "loras/one.safetensors"},
              {"repo": "b/gone", "file": "two.safetensors", "dest": "loras/two.safetensors"},
              {"repo": "c/ok", "file": "enc.safetensors", "dest": "text_encoders/enc.safetensors"}]
    env = {"COMFY_DIR": str(tmp_path / "comfy"), "EXTRA_MODELS": json.dumps(models), "CUSTOM_NODES": "[]", "PYTHONPATH": str(tmp_path),
           "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    r = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    assert (tmp_path / "comfy" / "models" / "text_encoders" / "enc.safetensors").exists()  # 실패한 것 뒤의 모델도 받았다
    assert "loras/two.safetensors FAILED" in r.stdout
    assert (logs / "models_failed.txt").read_text() == "loras/two.safetensors"


class _FakeComfy:
    def __init__(self, code=None):
        self.code, self.posted = code, []

    def req(self, path, data=None):
        if self.code:
            raise urllib.error.HTTPError(path, self.code, "x", {}, None)
        self.posted.append(path)


def test_push_local_records_why_it_skipped():
    S.PUSH_SKIPPED.clear()
    name = S.MCP_PREFIX + "webtoon_vertical_qwen21_uc.json"
    used = S._used_models(json.loads((S.LOCAL_DIR / name).read_text(encoding="utf-8")))
    have = {k: set(vs) for k, vs in used.items()}
    enc = next(iter(have["text_encoders"]))
    have["text_encoders"] = set()  # 텍스트 인코더만 없는 서버
    S._push_local(_FakeComfy(), {n.name for n in S.LOCAL_DIR.glob("*.json")} - {name}, have)
    assert enc in S.PUSH_SKIPPED[name]
    S.PUSH_SKIPPED.clear()
    have["text_encoders"] = {enc}
    S._push_local(_FakeComfy(code=500), {n.name for n in S.LOCAL_DIR.glob("*.json")} - {name}, have)
    assert S.PUSH_SKIPPED[name] == "올리기 HTTP 500"
    S.PUSH_SKIPPED.clear()
    S._push_local(_FakeComfy(code=409), {n.name for n in S.LOCAL_DIR.glob("*.json")} - {name}, have)
    assert name not in S.PUSH_SKIPPED  # 409 는 다른 작업이 먼저 올린 것


def test_no_board_warns_with_the_reason(monkeypatch):
    # 서버는 읽혔는데 판이 하나도 없으면 기본 이름으로 가되, 왜 없는지(못 올린 사본의 까닭)를 남긴다
    class Empty:
        def req(self, path, data=None):
            return [] if path.startswith("/models/") else None
    monkeypatch.setattr(S, "_c", lambda: Empty())
    monkeypatch.setattr(S, "_list_workflows", lambda c: [])
    monkeypatch.setattr(S, "_models", {})
    monkeypatch.setattr(S, "_NO_BOARD_WARNED", False)
    S.PUSH_SKIPPED.clear()
    err = io.StringIO()
    with redirect_stderr(err):
        m = S.models(refresh=True)
    assert m["uc"]["edit"] == S.OFFLINE["uc"][1]
    assert "판" in err.getvalue() and "서버에 없는 모델" in err.getvalue()
