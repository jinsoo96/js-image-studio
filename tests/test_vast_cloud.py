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
import time
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


def test_llm_swap_overlays_option_and_keeps_server_share():
    # 같은 서버에서 LLM 만 바꾼다: 고른 후보의 repo·name·args 가 덮이고, GPU 몫(gpu_util)·길이는 이 설정 것 그대로. 고른 게 없으면 기본
    c = V.cfg()
    opts = {k: v for k, v in (c.get("llm_options") or {}).items() if not k.startswith("_")}
    assert opts, "stack 에 llm_options 후보가 있어야 한다"
    pick, opt = next(iter(opts.items()))
    real_state = V.load_state
    try:
        V.load_state = lambda: {"llm_choice": pick}
        L = V.llm_spec(c)
        assert L["repo"] == opt["repo"] and L["name"] == opt["name"] and L["args"] == opt.get("args", c["llm"]["args"])
        assert L["gpu_util"] == c["llm"]["gpu_util"] and L["max_len"] == c["llm"]["max_len"]
        assert not any(k.startswith("_") and k not in c["llm"] for k in L)  # 후보의 설명(_why)은 섞이지 않는다
        V.load_state = lambda: {"llm_choice": "없는이름"}
        assert V.llm_spec(c) == c["llm"]
        V.load_state = lambda: {}
        assert V.llm_spec(c) == c["llm"]
    finally:
        V.load_state = real_state
    # Qwen 전용 인자는 설정에만 있고 띄우는 스크립트에는 없다(다른 모델로 바꿔도 기동이 깨지지 않게)
    sh = V.LLM_SH.read_text(encoding="utf-8")
    assert "--reasoning-parser" not in sh.split("exec ")[-1] and "--reasoning-parser qwen3" in c["llm"]["args"]


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
            assert t.getnames() == ["workspace/bootstrap.sh", "workspace/pod.env", "workspace/tunnel_relay.py"]
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
            {**good, "id": 8, "dph_base": 0.5, "storage_cost": 5.0},                                # 보관비 비쌈(멈춰 둘 때만 빠짐)
            {**good, "id": 9, "dph_base": 0.95, "inet_down_cost": 0.009}]                           # 시간당은 싸지만 첫 설치 내려받기가 비쌈

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
        # 다 쓰면 지우는 판은 켤 때마다 다시 내려받으니 '쓸 시간 x 시간당 + 내려받기' 가 싼 순(시간당만 보면 9 가 먼저)
        hours = float(c.get("plan_hours", 1.5))
        if not c.get("keep_instance"):
            assert got.index(1) < got.index(9)
            assert [o["_hourly"] * hours + o["_setup_usd"] for o in offs] == sorted(o["_hourly"] * hours + o["_setup_usd"] for o in offs)
        V.cfg = lambda: {**real_cfg(), "keep_instance": True, "max_stopped_usd_per_month": 30}
        got_keep = [o["id"] for o in V.offers()]
        assert 1 in got_keep and 8 not in got_keep  # 디스크 x GB당 $5 = 월 수백 달러 > $30
    finally:
        V.urllib.request.urlopen, V.cfg = real_urlopen, real_cfg


def test_ssh_target_prefers_direct():
    inst = {"id": 1, "public_ipaddr": "1.2.3.4 ", "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40022"}]}, "ssh_host": "ssh5.vast.ai", "ssh_port": 1234}
    assert V.ssh_target(inst) == ("1.2.3.4", 40022)
    assert V.ssh_target({**inst, "ports": {}}) == ("ssh5.vast.ai", 1234)


def test_tunnel_relay_closes_when_backend_down_and_relays_when_up():
    # vast 중계 SSH 는 안 뜬 포트로 한 번 가면 터널 전체가 멈춘다(2026-10-25 실측): 중계기는 늘 받고, 뒤가 없으면 조용히 닫는다
    import socket
    import threading
    def free():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    listen, dst = free(), free()
    proc = subprocess.Popen([sys.executable, str(ROOT / "cloud" / "tunnel_relay.py"), f"{listen}:{dst}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert b"relay" in proc.stdout.readline()
        with socket.create_connection(("127.0.0.1", listen), timeout=5) as c:  # 뒤가 없다: 받고 곧 닫는다
            c.settimeout(8)
            assert c.recv(10) == b""
        srv = socket.socket()
        srv.bind(("127.0.0.1", dst))
        srv.listen(1)
        def echo():
            conn, _ = srv.accept()
            with conn:
                conn.sendall(conn.recv(100).upper())
        threading.Thread(target=echo, daemon=True).start()
        with socket.create_connection(("127.0.0.1", listen), timeout=5) as c:
            c.sendall(b"ping")
            c.settimeout(8)
            assert c.recv(10) == b"PING"
        srv.close()
    finally:
        proc.kill()


def test_ssh_exe_prefers_windows_openssh():
    # Git(MSYS) ssh 는 ssh_put 의 긴 인자를 깨뜨린다(2026-10-25 실측): Windows 에선 System32 OpenSSH
    exe = V.ssh_exe()
    if os.name == "nt" and Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe").exists():
        assert exe.lower().endswith(r"system32\openssh\ssh.exe"), exe
    assert V.ssh_base({"id": 1, "ssh_host": "h", "ssh_port": 2})[0] == exe


def test_onstart_revives_sshd_and_image_problem_stops():
    # 호스트 키 없는 이미지(2026-10-25 runpod/pytorch 실측): onstart 가 키를 만들고 sshd 를 띄운다. 4KB 한도 안
    assert "ssh-keygen -A" in V.ONSTART and "/usr/sbin/sshd" in V.ONSTART and len(V.ONSTART) < 4048
    # running 인데 SSH 가 안 받고 로그에 sshd 가 죽은 줄이 있으면 다른 기계로 다시 빌리지 않고 ImageProblem
    clock = iter(range(0, 10**6, 100))
    saved = (V.get_instance, V.ssh_run, V.container_log, V.time.time, V.time.sleep)
    try:
        V.get_instance = lambda iid: {"id": iid, "actual_status": "running", "ssh_host": "ssh1.vast.ai", "ssh_port": 1}
        V.ssh_run = lambda inst, cmd, timeout=0: subprocess.CompletedProcess([], 255, b"", b"reset")
        V.container_log = lambda iid, tail=120: "sshd: no hostkeys available -- exiting.\nconnect_to localhost port 22: failed."
        V.time.time = lambda: next(clock)
        V.time.sleep = lambda s: None
        try:
            V.wait_running(7, 60)
            raise AssertionError("ImageProblem 이 나야 한다")
        except V.ImageProblem as e:
            assert "호스트 키" in str(e)
    finally:
        V.get_instance, V.ssh_run, V.container_log, V.time.time, V.time.sleep = saved


def test_launch_returns_while_relay_keeps_running():
    # 설치 명령(LAUNCH_CMD)은 중계기·부트스트랩을 뒤로 띄우고 바로 끝나야 한다: 뒤로 간 묶음이 출력을 쥐면 SSH 가 안 끝난다(2026-10-26 180초 멈춤)
    if not BASH:
        return
    stub = "sleep 6; true"  # 중계기 대신 오래 사는 것(setsid 는 Git bash 에 없어 뺀다)
    for cmd in (V.RELAY_START, V.LAUNCH_CMD.split(" && ", 3)[3]):  # 3: 묶음 풀기 뒤(중계기·부트스트랩 띄우기)
        cmd = cmd.replace("setsid ", "").replace("python3 /workspace/tunnel_relay.py", stub)
        cmd = cmd.replace("bash /workspace/bootstrap.sh", stub).replace(". /workspace/pod.env;", "")
        t0 = V.time.time()
        p = subprocess.run([BASH, "-c", f"{{ {cmd} }}; echo done"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        assert b"done" in p.stdout and V.time.time() - t0 < 4, (cmd[:80], V.time.time() - t0)


def test_two_boxes_split_roles_ports_switch_and_wake_button():
    # 그림 상자(vast_img, comfy)·LLM 상자(vast_llm, llm): 설치 모드·포트·스위치 파일이 역할대로 나뉘고, 켜기 버튼은 둘을 함께 적는다
    img, llm, both = (V.load_cfg(ROOT / "cloud" / f"{n}.json") for n in ("vast_img", "vast_llm", "vast_qwen"))
    assert V.roles_of(img) == {"comfy"} and V.roles_of(llm) == {"llm"} and V.roles_of(both) == {"comfy", "llm"}
    assert V.pod_env(img, "t")["MODE"] == "comfy" and V.pod_env(llm, "t")["MODE"] == "agent"
    assert set(img["local_ports"]) == {"comfy", "agent"} and set(llm["local_ports"]) == {"llm", "agent"}
    assert not set(img["local_ports"].values()) & set(llm["local_ports"].values())  # 같은 PC 포트를 두 터널이 다투지 않는다
    assert img["label_prefix"] != llm["label_prefix"] and img["min_balance_usd"] == llm["min_balance_usd"]  # 같은 계정, 같은 바닥
    assert llm["llm"]["repo"] == both["llm"]["repo"] and "llm_options" in llm  # 모델·교체 후보는 stack 것
    assert "localhost:8189/jobs" in V.launch_cmd(V.PROBES["agent"]) and "8188/system_stats" in V.LAUNCH_CMD
    # LLM 상자는 vLLM 휠이 요구하는 드라이버(stack llm.min_cuda)까지 맞는 기계만, 그림 상자는 이미지 기준(2026-10-26 CUDA 12.8 A6000 에서 'driver too old')
    assert V.min_cuda(llm) >= 13.0 > V.min_cuda(img) == float(img["search"]["min_cuda"]) and V.min_cuda(both) == V.min_cuda(llm)
    real = (V.ROOT, V.cfg, V.PROFILE, V.load_state, V.boot_log, V.ensure_tunnel, V.time.time, V.time.sleep)
    with tempfile.TemporaryDirectory() as d:
        st = Path(d) / "state"
        st.mkdir()
        try:
            V.ROOT, V.load_state = Path(d), (lambda: {"token": "tok"})
            V.cfg, V.PROFILE = (lambda: llm), "vast_llm"
            V.write_switch()
            assert not (st / "comfy_url.txt").exists() and (st / "vlm_url.txt").read_text(encoding="utf-8").startswith("http://127.0.0.1:28000/v1|qwen3.8-27b|tok")
            V.cfg, V.PROFILE = (lambda: img), "vast_img"
            V.write_switch()
            assert (st / "comfy_url.txt").read_text(encoding="utf-8").strip() == "http://127.0.0.1:28188"
            assert json.loads((st / "gpu_cloud.json").read_text(encoding="utf-8"))["profiles"] == ["vast_llm", "vast_img"]
            V.cfg, V.PROFILE = (lambda: both), "vast_qwen"  # 한 상자(둘 다)로 돌아가면 역할이 겹치는 두 상자를 밀어낸다
            V.write_switch()
            assert json.loads((st / "gpu_cloud.json").read_text(encoding="utf-8"))["profiles"] == ["vast_qwen"]
            assert V.wake_profiles(drop="vast_qwen") == []
            # LLM 상자는 ComfyUI 대신 부트스트랩의 'agent ready' 줄을 기다린다
            V.cfg, V.PROFILE = (lambda: llm), "vast_llm"
            V.boot_log, V.ensure_tunnel = (lambda: ["[boot] driver x", V.AGENT_READY + " 2026"]), (lambda: None)
            clock = iter(range(0, 10**6, 5))
            V.time.time, V.time.sleep = (lambda: next(clock)), (lambda s: None)
            assert V.wait_ready(5) == "http://127.0.0.1:28289"
        finally:
            V.ROOT, V.cfg, V.PROFILE, V.load_state, V.boot_log, V.ensure_tunnel, V.time.time, V.time.sleep = real


def test_watchdog_llm_box_self_destroys_when_vllm_is_quiet():
    # LLM 전용 상자(MODE=agent): vLLM 작업은 늘 '돌고 있음'이라 그것만으로 바쁨이 되면 영영 안 꺼진다. 요청이 없으면 IDLE_MIN 뒤 스스로 지운다
    bs = (ROOT / "cloud" / "runpod_bootstrap.sh").read_text(encoding="utf-8")
    src = re.search(r"cat > /workspace/watchdog.py <<'PY'\n(.*?)\nPY\n", bs, re.S).group(1)

    class Stop(Exception):
        pass

    def run(metrics_running: int, llm_up: bool = True):
        hits, sleeps = [], []

        def fake_open(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if "console.vast.ai" in url:
                hits.append(req.get_method())
                return io.BytesIO(b"{}")
            if url.endswith("/jobs"):
                return io.BytesIO(json.dumps({"llm": {"running": True}}).encode())
            if url.endswith("/activity"):
                return io.BytesIO(json.dumps({"ago": 9999}).encode())
            if url.endswith("/metrics") and llm_up:
                return io.BytesIO(f"vllm:num_requests_running{{m=\"x\"}} {metrics_running}\nvllm:prompt_tokens_total{{m=\"x\"}} 100\n".encode())
            raise OSError(url)  # 8188(ComfyUI)·안 뜬 8000 은 없다

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) > 8:
                raise Stop

        saved = (urllib.request.urlopen, time.sleep, dict(os.environ))
        os.environ.update({"MODE": "agent", "IDLE_MIN": "3", "MAX_HOURS": "99", "CONTAINER_ID": "1", "CONTAINER_API_KEY": "k", "ON_IDLE": "terminate"})
        urllib.request.urlopen, time.sleep = fake_open, fake_sleep
        try:
            exec(compile(src, "watchdog.py", "exec"), {"__name__": "wd"})
        except Stop:
            pass
        finally:
            urllib.request.urlopen, time.sleep = saved[0], saved[1]
            os.environ.clear()
            os.environ.update(saved[2])
        return hits

    assert "DELETE" in run(metrics_running=0)                 # 조용한 vLLM: 3분 뒤 지움
    assert run(metrics_running=1) == []                        # 요청을 처리 중이면 바쁨
    assert run(metrics_running=0, llm_up=False) == []          # 아직 올라오는 중(설치·받기)도 바쁨


def test_loading_stuck_on_same_line_gives_up_before_timeout():
    # 받기가 같은 줄에서 loading_stall_min 넘게 그대로면 전체 한도(20분)를 기다리지 않고 다른 기계로. 줄이 바뀌는 동안은 기다린다
    clock = iter(range(0, 10**6, 60))  # 부를 때마다 1분
    msgs = iter(["a: Pull complete"] * 3 + ["b: Pull complete"] + ["c: Pull complete"] * 100)
    saved = (V.get_instance, V.time.time, V.time.sleep)
    try:
        V.get_instance = lambda iid: {"id": iid, "actual_status": "loading", "status_msg": next(msgs)}
        V.time.time = lambda: next(clock)
        V.time.sleep = lambda s: None
        try:
            V.wait_running(7, 60)
            raise AssertionError("멈춘 받기에서 RuntimeError 가 나야 한다")
        except RuntimeError as e:
            assert "그대로" in str(e) and "c:" in str(e)
    finally:
        V.get_instance, V.time.time, V.time.sleep = saved


def test_watchdog_comfy_box_counts_short_prompts_as_busy():
    # 그림 상자(MODE 없음): 1분마다 큐만 보면 30초짜리 turbo 그림은 샘플 순간마다 빈 큐라, 두 컷을 14분 그리는 동안 '놀았다'로 세어 그리기 직후
    # 스스로 지웠다(2026-10-26 무대 뒤 L40S, 14:17 준비 → 14:32 삭제). /history 의 마지막 프롬프트 번호가 샘플 사이에 바뀌면 바쁨이다
    bs = (ROOT / "cloud" / "runpod_bootstrap.sh").read_text(encoding="utf-8")
    src = re.search(r"cat > /workspace/watchdog.py <<'PY'\n(.*?)\nPY\n", bs, re.S).group(1)

    class Stop(Exception):
        pass

    def run(drawing: bool):
        hits, sleeps, n = [], [], [0]

        def fake_open(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if "console.vast.ai" in url:
                hits.append(req.get_method())
                return io.BytesIO(b"{}")
            if url.endswith("/jobs"):
                return io.BytesIO(b"{}")
            if url.endswith("/activity"):
                return io.BytesIO(json.dumps({"ago": 9999}).encode())
            if url.endswith("/queue"):  # 샘플 순간엔 늘 빈 큐(그림 한 장이 1분 샘플 사이에 끝난다)
                return io.BytesIO(json.dumps({"queue_running": [], "queue_pending": []}).encode())
            if "/history" in url:  # 그리는 중이면 샘플마다 새 프롬프트 번호, 멈추면 같은 번호
                n[0] += 1
                return io.BytesIO(json.dumps({f"p{n[0]}" if drawing else "p0": {"status": "ok"}}).encode())
            raise OSError(url)  # 8000(vLLM)은 그림 상자에 없다

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) > 8:
                raise Stop

        saved = (urllib.request.urlopen, time.sleep, dict(os.environ))
        os.environ.pop("MODE", None)
        os.environ.update({"IDLE_MIN": "3", "MAX_HOURS": "99", "CONTAINER_ID": "1", "CONTAINER_API_KEY": "k", "ON_IDLE": "terminate"})
        urllib.request.urlopen, time.sleep = fake_open, fake_sleep
        try:
            exec(compile(src, "watchdog.py", "exec"), {"__name__": "wd"})
        except Stop:
            pass
        finally:
            urllib.request.urlopen, time.sleep = saved[0], saved[1]
            os.environ.clear()
            os.environ.update(saved[2])
        return hits

    assert run(drawing=True) == []          # 그림이 잇달아 끝나는 동안은(빈 큐여도) 지우지 않는다
    assert "DELETE" in run(drawing=False)   # 그림이 멈추면 IDLE_MIN(3분) 뒤 지움


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
