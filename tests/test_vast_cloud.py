"""tools/vast_cloud.py 회귀 테스트(네트워크·도커 없이). python tests/test_vast_cloud.py 또는 pytest.

- 설정 합치기: 설치 내용은 stack_from 것, llm 은 항목별로 덮기
- 부트스트랩 env 파일이 bash 에서 그대로 풀리는지(EXTRA_FILES 는 base64 JSON, 따옴표 섞인 vast env 도)
- watchdog self_stop 이 vast·RunPod 에 맞는 요청을 만드는지
- 기계 고르기: CUDA·신뢰도·나라·내려받기 요금·시간당·(멈춰 둘 때) 보관비 상한
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
os.environ["VAST_PROFILE"] = "vast_qwen"
import vast_cloud as V  # noqa: E402

# Windows 에선 Git bash 를 먼저(System32 의 bash 는 WSL 이라 배포판이 없으면 실패한다)
BASH = next((p for p in (r"C:\Program Files\Git\bin\bash.exe",) if Path(p).exists()), None) or (shutil.which("bash") if os.name != "nt" else None)


def test_stack_merge():
    c = V.cfg()
    base = json.loads((ROOT / "cloud" / c["stack_from"]).with_suffix(".json").read_text(encoding="utf-8"))
    assert c["image"] == base["image"] and c["extra_models"] == base["extra_models"] and c["extra_files"] == base["extra_files"]
    own = json.loads(V.CONFIG.read_text(encoding="utf-8"))["llm"]
    assert c["llm"]["repo"] == base["llm"]["repo"] and c["llm"]["max_len"] == base["llm"]["max_len"]
    assert c["llm"]["gpu_util"] == own["gpu_util"]


def test_comfy_args_and_env():
    c = V.cfg()
    args = V.comfy_args(c)
    assert args.startswith("--listen 127.0.0.1")
    if (c.get("gpu_split") or {}).get("comfy") not in (None, ""):
        assert f"--cuda-device {c['gpu_split']['comfy']}" in args
    env = V.pod_env(c, "tok")
    assert env["ON_IDLE"] == ("stop" if c.get("keep_instance") else "terminate")
    assert env["COMFY_ARGS"] == args and env["AGENT_TOKEN"] == "tok"


def test_bundle_sources_in_bash():
    if not BASH:
        return  # bash 없는 곳에선 건너뜀
    c = V.cfg()
    env = V.pod_env(c, "tok'\"x")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        with tarfile.open(fileobj=io.BytesIO(V.launch_bundle(c, "tok'\"x"))) as t:
            assert t.getnames() == ["workspace/bootstrap.sh", "workspace/pod.env"]
            t.extractall(d)
        assert b"\r\n" not in (d / "workspace" / "bootstrap.sh").read_bytes()
        vast_env, dump = d / "vast_env", d / "dump.json"
        subprocess.run([BASH, "-c", f"export CONTAINER_ID=42 WEIRD=\"a b 'c' \\\"d\\\"\"; export -p > '{vast_env.as_posix()}'"], check=True)
        keys = list(env) + ["CONTAINER_ID", "WEIRD"]
        script = (f"set -a; . '{vast_env.as_posix()}'; . '{(d / 'workspace' / 'pod.env').as_posix()}'; set +a; "
                  f"python -c \"import json,os; json.dump({{k: os.environ.get(k) for k in {keys!r}}}, open(r'{dump}', 'w', encoding='utf-8'))\"")
        subprocess.run([BASH, "-c", script], check=True)
        got = json.loads(dump.read_text(encoding="utf-8"))
    assert all(got[k] == v for k, v in env.items())
    assert got["CONTAINER_ID"] == "42" and got["WEIRD"] == "a b 'c' \"d\""


def test_watchdog_self_stop_per_provider():
    bs = (ROOT / "cloud" / "runpod_bootstrap.sh").read_text(encoding="utf-8")
    defs = re.search(r"cat > /workspace/watchdog.py <<'PY'\n(.*?)\nPY\n", bs, re.S).group(1).split("\nwhile True:")[0]
    cases = (
        ({"CONTAINER_ID": "7", "CONTAINER_API_KEY": "k", "ON_IDLE": "stop"}, ("PUT", "https://console.vast.ai/api/v0/instances/7/", b'{"state": "stopped"}', "Bearer k")),
        ({"CONTAINER_ID": "7", "CONTAINER_API_KEY": "k", "ON_IDLE": "terminate"}, ("DELETE", "https://console.vast.ai/api/v0/instances/7/", None, "Bearer k")),
        ({"RUNPOD_POD_ID": "p", "RUNPOD_API_KEY": "r", "ON_IDLE": "stop"}, ("POST", "https://rest.runpod.io/v1/pods/p/stop", None, "Bearer r")),
        ({"RUNPOD_POD_ID": "p", "RUNPOD_API_KEY": "r", "ON_IDLE": "terminate"}, ("DELETE", "https://rest.runpod.io/v1/pods/p", None, "Bearer r")),
    )
    names = ("CONTAINER_ID", "CONTAINER_API_KEY", "RUNPOD_POD_ID", "RUNPOD_API_KEY", "ON_IDLE")
    for envs, want in cases:
        saved = {k: os.environ.pop(k, None) for k in names}
        os.environ.update(envs)
        seen, real = [], urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: seen.append(req)
        try:
            ns = {}
            exec(defs, ns)
            ns["self_stop"]()
        finally:
            urllib.request.urlopen = real
            for k in names:
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in saved.items() if v is not None})
        r = seen[0]
        assert (r.get_method(), r.full_url, r.data, r.get_header("Authorization")) == want


def test_offer_filters():
    c = V.cfg()
    s = c["search"]
    good = {"id": 1, "machine_id": 10, "cuda_max_good": 13.0, "reliability2": 0.999, "inet_down": 900, "disk_space": c["disk_gb"] + 50,
            "inet_down_cost": 0.001, "geolocation": "Korea, KR", "dph_base": 1.0, "storage_cost": 0.1}
    rows = [good,
            {**good, "id": 2, "cuda_max_good": 12.4},                                              # CUDA 낮음
            {**good, "id": 3, "reliability2": 0.5},                                                 # 신뢰도
            {**good, "id": 4, "geolocation": "Guangdong, " + (s.get("avoid_countries") or ["CN"])[0]},  # 뺄 나라
            {**good, "id": 5, "inet_down_cost": 1.0},                                               # 내려받기 요금
            {**good, "id": 6, "dph_base": 99.0},                                                    # 시간당
            {**good, "id": 7, "disk_space": 10},                                                    # 디스크
            {**good, "id": 8, "dph_base": 0.5, "storage_cost": 5.0}]                                # 보관비 비쌈(멈춰 둘 때만 빠짐)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real_urlopen, real_cfg = V.urllib.request.urlopen, V.cfg
    V.urllib.request.urlopen = lambda req, timeout=None: Resp(json.dumps({"offers": rows}).encode())
    try:
        offs = V.offers()
        got = [o["id"] for o in offs]
        assert 1 in got and not {2, 3, 4, 5, 6, 7} & set(got)
        assert (8 in got) == (not c.get("keep_instance"))
        assert [o["_hourly"] for o in offs] == sorted(o["_hourly"] for o in offs)  # 싼 순
        V.cfg = lambda: {**real_cfg(), "keep_instance": True, "max_stopped_usd_per_month": 30}
        got_keep = [o["id"] for o in V.offers()]
        assert 1 in got_keep and 8 not in got_keep  # 디스크 x GB당 $5 = 월 수백 달러 > $30
    finally:
        V.urllib.request.urlopen, V.cfg = real_urlopen, real_cfg


def test_ssh_target_prefers_direct():
    inst = {"id": 1, "public_ipaddr": "1.2.3.4 ", "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40022"}]}, "ssh_host": "ssh5.vast.ai", "ssh_port": 1234}
    assert V.ssh_target(inst) == ("1.2.3.4", 40022)
    assert V.ssh_target({**inst, "ports": {}}) == ("ssh5.vast.ai", 1234)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e)[:300])
    sys.exit(1 if fails else 0)
