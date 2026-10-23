"""RunPod 파드에 ComfyUI 를 띄워 쓴다. 일을 시킬 때만 GPU 가 돌고, 끝나면 내린다.

    python tools/runpod_cloud.py run -- <명령...>
        파드를 켜고 ComfyUI 가 준비되면 COMFY_URL 을 넣어 <명령>을 실행한 뒤, 성공하든 실패하든 파드를 내린다.
        예) python tools/runpod_cloud.py run -- python tools/render.py episode series/mudae-dwi/episodes/ep001 --only p003
    python tools/runpod_cloud.py up        파드를 켜고 준비될 때까지 기다려 URL 을 출력한다(다 쓰면 stop 필수)
    python tools/runpod_cloud.py stop      켜져 있는 파드를 내린다(keep_pod 면 멈추고, 아니면 지운다)
    python tools/runpod_cloud.py down      우리 파드(이름이 name_prefix 로 시작)를 전부 지운다(멈춰 둔 것까지)
    python tools/runpod_cloud.py status    우리 파드 상태·시간당 비용·멈춘 동안 보관비
    python tools/runpod_cloud.py llm       켜 둔 파드에 LLM(설정 llm, vLLM 포트 8000)을 띄운다(RUNPOD_PROFILE=runpod_qwen)
    python tools/runpod_cloud.py sync      설정의 custom_nodes·extra_models 중 켜 둔 파드에 없는 것만 받는다(새 노드는 ComfyUI 재시작 뒤)
    python tools/runpod_cloud.py use       모든 도구(MCP·스크립트)가 켜 둔 파드를 쓰게 state/comfy_url.txt·vlm_url.txt 를 적는다(unuse 는 지운다)

"내린다"는 keep_pod(cloud/runpod.json) 에 따라 다르다.
- false(기본): 지운다. 보관비 0, 다음엔 새 파드(2~4.5분). 커뮤니티 GPU 는 멈추면 바로 남이 가져가서 다시 켜기가 자주 실패한다.
- true: 멈춘다. /workspace 볼륨에 설치·체크포인트가 남고 보관비(volume_gb x 월 $0.20/GB)가 든다.
  다시 켤 때 그 기계에 빈 GPU 가 없거나 GPU 없이 붙으면 그 파드를 지우고 새로 만든다.
파드 안에도 안전장치가 있다: 큐가 idle_min 분 비거나 이번 기동 후 max_hours 가 지나면 스스로 내린다.

API 키는 환경변수 RUNPOD_API_KEY. 파드 안 설치·실행은 cloud/runpod_bootstrap.sh, 지금 쓰는 파드 id 는 cloud/pod_state.json.
설정은 RUNPOD_PROFILE(기본 runpod → cloud/runpod.json). runpod_qwen 이면 vast 와 같은 Qwen Image 2.1 UC 구성(cloud/runpod_qwen.json,
2026-10-22 회사 vast 가 꺼져 옮김), 파드 상태는 cloud/pod_state_runpod_qwen.json.
"""
import base64
import secrets
import urllib.parse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = os.environ.get("RUNPOD_PROFILE", "runpod")
CONFIG = ROOT / "cloud" / f"{PROFILE}.json"
BOOTSTRAP = ROOT / "cloud" / "runpod_bootstrap.sh"
STATE = ROOT / "cloud" / ("pod_state.json" if PROFILE == "runpod" else f"pod_state_{PROFILE}.json")
API = "https://rest.runpod.io/v1"
# RunPod API·프록시 앞의 Cloudflare 가 파이썬 기본 User-Agent 를 403(error code 1010)으로 막는다
UA = {"User-Agent": "webtoon-studio/1.0"}


def cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(**kw):
    s = {**load_state(), **kw}
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def api(method, path, body=None):
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        sys.exit("환경변수 RUNPOD_API_KEY 가 없다")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={**UA, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"RunPod {method} {path} -> {e.code}: {e.read().decode('utf-8', 'replace')[:500]}") from e


def proxy_url(pod_id):
    return f"https://{pod_id}-8188.proxy.runpod.net"


def bootstrap_script():
    return BOOTSTRAP.read_bytes().replace(b"\r\n", b"\n")


def setup_sha():
    """파드에 구워 넣는 것(부트스트랩·이미지·체크포인트·볼륨)이 바뀌었는지 보는 값. 바뀌면 멈춘 파드를 버리고 새로 만든다."""
    c = cfg()
    baked = {k: c.get(k) for k in ("image", "container_disk_gb", "volume_gb", "checkpoint", "comfyui_ref", "custom_nodes",
                                   "extra_models", "idle_min", "max_hours", "keep_pod", "patches")}
    return hashlib.sha256(bootstrap_script() + json.dumps(baked, sort_keys=True).encode() + extra_files_b64().encode()).hexdigest()[:12]


def extra_files_b64() -> str:
    """runpod.json extra_files({ComfyUI 안 경로: 로컬 파일})를 파드 env 로 넘길 JSON(내용은 base64). 우리 노드·상류 패치를 파드에 넣는다."""
    files = {dest: base64.b64encode((ROOT / src).read_bytes().replace(b"\r\n", b"\n")).decode("ascii") for dest, src in cfg().get("extra_files", {}).items()}
    return json.dumps(files, sort_keys=True)


def get_pod(pod_id):
    try:
        return api("GET", f"/pods/{pod_id}")
    except RuntimeError as e:
        if "-> 404" in str(e) or "-> 400" in str(e):
            return None
        raise


TOKENS = {}  # 파드 id -> 에이전트 토큰(이 프로세스가 만든 파드)


def hard_sha():
    """새 파드를 만들어야만 바뀌는 것(이미지·디스크·볼륨 크기). 나머지(부트스트랩·노드·모델·놀면 멈춤 등)는 env 만 바꿔 다시 켜면 된다."""
    c = cfg()
    return hashlib.sha256(json.dumps({k: c.get(k) for k in ("image", "container_disk_gb", "volume_gb", "keep_pod")}, sort_keys=True).encode()).hexdigest()[:12]


def network_volume() -> dict:
    """설정 network_volume {id, data_center}: 모델·설치물을 둔 데이터센터 네트워크 볼륨. 있으면 파드를 그 데이터센터에 만들어 /workspace 에 붙인다.
    멈춘 파드를 다시 켜면 그 기계 GPU 를 남이 빌려 가 있어 거의 매번 새 파드·처음부터 설치였다(2026-10-23 L40S 하루 10번 넘게).
    네트워크 볼륨은 그 데이터센터의 아무 GPU 에나 붙으니 설치를 다시 하지 않는다. 놀면 파드는 지우고(볼륨은 남음) 다음에 새로 만든다."""
    nv = cfg().get("network_volume") or {}
    return nv if nv.get("id") else {}


def pod_env(mode, token):
    """파드 env(부트스트랩이 읽는 값). 만들 때와, 설정이 바뀐 멈춘 파드를 다시 켤 때 같이 쓴다."""
    c = cfg()
    return {
        "MODE": mode,
        "AGENT_TOKEN": token,
        "BOOTSTRAP_B64": base64.b64encode(bootstrap_script()).decode("ascii"),
        "CKPT_URL": c["checkpoint"]["url"],
        "CKPT_NAME": c["checkpoint"]["name"],
        "EXTRA_FILES": extra_files_b64(),
        "PATCHES": json.dumps(c.get("patches", [])),
        "COMFY_REF": c["comfyui_ref"],
        "CUSTOM_NODES": json.dumps(c.get("custom_nodes", [])),
        "EXTRA_MODELS": json.dumps(c.get("extra_models", [])),
        "IDLE_MIN": str(c["idle_min"]),
        "MAX_HOURS": str(c["max_hours"]),
        "ON_IDLE": "stop" if c["keep_pod"] and not network_volume() else "terminate",
        # 이미지가 CUDA 12.8 을 요구(NVIDIA_REQUIRE_CUDA)해 드라이버가 12.4~12.7 인 기계에선 컨테이너가 아예 안 떴다(2026-10-23 L40S 31분 runtime 없음),
        # 12.8 이상만 고르면 L40S 재고가 0. Ada(L40S) 까지는 CUDA 12.x 하위 호환으로 cu128 torch 가 돌아 그 검사만 끈다(설정 nvidia_disable_require)
        **({"NVIDIA_DISABLE_REQUIRE": "1"} if c.get("nvidia_disable_require") else {}),
    }


def create_pod(mode="comfy", image=None):
    """mode: comfy(ComfyUI 를 띄움) | agent(ComfyUI 없이 에이전트만, 학습 등). image 를 주면 runpod.json 의 image 대신 쓴다."""
    c = cfg()
    token = secrets.token_hex(16)
    env = pod_env(mode, token)
    errors = []
    # 싼 커뮤니티 클라우드부터, GPU 는 목록 순서대로 빈 곳을 찾는다.
    # RunPod 가 잠깐씩 500("Something went wrong", "machine does not have the resources")을 내서 30초 간격으로 4바퀴 돈다(2026-10-07)
    # RUNPOD_CLOUD_TYPES=SECURE 로 한 번만 바꿔 켤 수 있다(2026-10-12 커뮤니티 파드 두 대가 연달아 ComfyUI 를 못 띄웠다, 드라이버 580)
    types = [t for t in os.environ.get("RUNPOD_CLOUD_TYPES", "").split(",") if t] or c["cloud_types"]
    for cloud_type in [t for _ in range(4) for t in types]:
        if errors and cloud_type == types[0]:
            time.sleep(30)
        body = {
            "name": f"{c['name_prefix']}-{time.strftime('%m%d-%H%M%S')}",
            "imageName": image or c["image"],
            "gpuTypeIds": c["gpu_types"],
            "gpuCount": 1,
            "cloudType": cloud_type,
            "containerDiskInGb": c["container_disk_gb"],
            "volumeInGb": c["volume_gb"] if c["keep_pod"] else 0,
            "volumeMountPath": "/workspace",
            "ports": c.get("ports", ["8188/http", "8189/http"]),
            "env": env,
            # RunPod 는 이 배열을 공백으로 이었다 다시 쪼개는 듯해서(2026-10-06 첫 시험: "bash -c '...'" 가 갈라져 빈 스크립트로 즉시 종료·재시작 반복)
            # 공백 없이 ${IFS} 로 쓴다. 그대로 넘어가도, 쪼개져도 같은 한 덩어리다
            "dockerStartCmd": ["bash", "-c", "echo${IFS}$BOOTSTRAP_B64|base64${IFS}-d>/bootstrap.sh&&bash${IFS}/bootstrap.sh"],
        }
        # 이미지가 CUDA 12.8 이라 드라이버가 낮은 기계에 붙으면 ComfyUI 가 뜨지 못한다(2026-10-07 30분 대기 후 실패).
        # 비우면 거르지 않는다: 2026-10-22 RTX PRO 6000 재고가 많은데도 ["12.8","12.9","13.0"] 이면 늘 "no instances currently available"
        if c.get("allowed_cuda"):
            body["allowedCudaVersions"] = c["allowed_cuda"]
        # 데이터센터를 고를 수 있다(설정 data_centers). 2026-10-23 L40S 다섯 대 중 정상으로 뜬 두 대가 US-NC-1, 나머지는 컨테이너가 안 뜨거나 pip 가 멈춤.
        # 첫 바퀴만 그곳에서 찾고, 없으면 다음 바퀴부터는 어디서든 찾는다
        if c.get("data_centers") and len(errors) < len(types):
            body["dataCenterIds"] = c["data_centers"]
        # 네트워크 볼륨: 앞 두 바퀴는 그 데이터센터에서 볼륨을 붙여 찾고, 그래도 없으면 볼륨 없이(처음부터 설치) 어디서든
        nv = network_volume()
        if nv and len(errors) < 2 * len(types):
            body.update({"networkVolumeId": nv["id"], "dataCenterIds": [nv["data_center"]], "volumeInGb": 0})
        elif nv and len(errors) == 2 * len(types):
            print(f"  네트워크 볼륨 데이터센터({nv['data_center']})에 빈 GPU 가 없어 볼륨 없이 만든다(처음부터 설치)", flush=True)
        try:
            pod = api("POST", "/pods", body)
        except RuntimeError as e:
            errors.append(str(e))
            continue
        print(f"파드 생성: {pod['id']} ({cloud_type}, {mode}, 시간당 ${pod.get('costPerHr', '?')})", flush=True)
        TOKENS[pod["id"]] = token
        if mode == "comfy":
            save_state(pod_id=pod["id"], setup_sha=setup_sha(), hard_sha=hard_sha(), created=time.strftime("%Y-%m-%d %H:%M"), stopped_at=None)
        return pod["id"]
    raise RuntimeError("빈 GPU 를 못 찾았다:\n" + "\n".join(errors))


def boot_log(pod_id):
    """파드 설치·ComfyUI 로그(8189 로 내보낸 /workspace/logs/bootstrap.log). 아직 안 열렸으면 None."""
    try:
        req = urllib.request.Request(f"https://{pod_id}-8189.proxy.runpod.net/bootstrap.log", headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return None


def wait_ready(pod_id, timeout_min, mode="comfy"):
    """ComfyUI 가 응답하고 GPU(cuda) 를 잡았는지까지 본다. 멈췄다 켠 파드는 GPU 없이 붙을 수 있다.
    체크포인트까지 받았는데 comfy_start_timeout_min 안에 안 뜨면 ComfyUI 가 죽은 것으로 보고 로그 끝을 찍고 실패한다."""
    url = proxy_url(pod_id)
    t0, seen, ckpt_at, req_at = time.time(), 0, None, None
    lines = []
    # 멈춘 기계는 일찍 버린다(2026-10-23 L40S: 컨테이너가 31분 안 뜸 · /workspace 가 네트워크 저장소라 pip 가 27분에 500MB, CPU 1%).
    # 로그가 아예 없이 25분(이미지 받기, 캐시 없는 기계는 10GB 이미지에 12분 넘게 걸렸다), 또는 체크포인트 전까지 로그가 15분 그대로면
    # 실패로 보고 ensure_pod 가 새 파드를 만든다
    changed_at = t0
    while time.time() - t0 < timeout_min * 60:
        try:
            if mode == "agent":
                if not any("[boot] agent ready" in ln for ln in (boot_log(pod_id) or [])):
                    raise RuntimeError("not yet")
                print(f"에이전트 준비됨: {agent_url(pod_id)} ({(time.time() - t0) / 60:.1f}분)", flush=True)
                return agent_url(pod_id)
            with urllib.request.urlopen(urllib.request.Request(url + "/system_stats", headers=UA), timeout=10) as r:
                stats = json.loads(r.read())
        except Exception:
            got = boot_log(pod_id)
            if got and len(got) != len(lines):
                changed_at = time.time()
            lines = got or lines
            if not ckpt_at and ((not lines and time.time() - t0 > 25 * 60) or (lines and time.time() - changed_at > 15 * 60)):
                print(f"  파드가 {(time.time() - changed_at) / 60:.0f}분째 진행이 없다(컨테이너가 안 뜨거나 설치가 멈춤): 버리고 새로 만든다", flush=True)
                break
            for line in lines[seen:]:
                if line.startswith("[boot]") or line.startswith("[watchdog]") or "Error" in line or "Traceback" in line:
                    print("  " + line[:300], flush=True)
                if "[boot] checkpoint ready" in line and ckpt_at is None:
                    ckpt_at = time.time()
                if "[boot] requirements" in line and req_at is None:
                    req_at = time.time()
            seen = len(lines)
            if ckpt_at and time.time() - ckpt_at > cfg()["comfy_start_timeout_min"] * 60:
                break
            # 체크포인트(7GB) 받기는 보통 3분. 네트워크가 느린 기계에 걸리면 버리고 새로 만든다(2026-10-07 16분째 안 끝남)
            if req_at and not ckpt_at and time.time() - req_at > cfg()["download_timeout_min"] * 60:
                break
            time.sleep(15)
            continue
        devices = stats.get("devices", [])
        if not any(d.get("type") == "cuda" for d in devices):
            raise RuntimeError(f"ComfyUI 는 떴는데 GPU 가 없다: {[d.get('name') for d in devices]}")
        print(f"ComfyUI 준비됨: {url} ({devices[0].get('name')}, {(time.time() - t0) / 60:.1f}분)", flush=True)
        return url
    print("  --- 파드 로그 끝 ---", flush=True)
    for line in (boot_log(pod_id) or lines)[-40:]:
        print("  " + line[:300], flush=True)
    raise TimeoutError(f"ComfyUI 가 뜨지 않았다({(time.time() - t0) / 60:.0f}분): {pod_id}")



def agent_url(pod_id):
    return f"https://{pod_id}-8189.proxy.runpod.net"


def _agent(pod_id, method, path, data=None, timeout=300):
    if pod_id not in TOKENS:  # 다른 프로세스가 만든 파드(up 으로 켜 둔 것): 토큰은 파드 env 에 있다
        TOKENS[pod_id] = (get_pod(pod_id) or {}).get("env", {}).get("AGENT_TOKEN", "")
    req = urllib.request.Request(agent_url(pod_id) + path, data=data, method=method,
                                 headers={**UA, "X-Token": TOKENS[pod_id], "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def agent_upload(pod_id, local, remote):
    """로컬 파일을 파드의 remote(절대 경로)로 올린다."""
    for attempt in range(3):
        try:
            return json.loads(_agent(pod_id, "POST", "/upload?" + urllib.parse.urlencode({"path": remote}), Path(local).read_bytes()))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)


def agent_upload_dir(pod_id, local_dir, remote_dir, chunk_mb=150):
    """폴더를 tar 묶음(최대 chunk_mb)으로 나눠 올려 파드에서 푼다. 파일 1천 개를 하나씩 올리면 20분 넘게 걸렸다(2026-10-11)."""
    import io
    import tarfile
    files = sorted(f for f in Path(local_dir).rglob("*") if f.is_file())
    batch, size, sent = [], 0, 0

    def flush():
        nonlocal batch, size, sent
        if not batch:
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for f in batch:
                t.add(f, arcname=str(f.relative_to(local_dir)).replace("\\", "/"))
        for attempt in range(3):
            try:
                _agent(pod_id, "POST", "/untar?" + urllib.parse.urlencode({"dest": remote_dir}), buf.getvalue(), timeout=600)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5)
        sent += len(batch)
        batch, size = [], 0

    for f in files:
        batch.append(f)
        size += f.stat().st_size
        if size >= chunk_mb * 2**20:
            flush()
    flush()
    return sent


def agent_run(pod_id, name, cmd, env=None):
    """파드에서 bash 명령을 뒤에서 돌린다. 출력은 /workspace/logs/job_<name>.log"""
    return json.loads(_agent(pod_id, "POST", "/run", json.dumps({"name": name, "cmd": cmd, "env": env or {}}).encode()))


def agent_wait(pod_id, name, poll=30, show=r"steps:|Error|error|Traceback|saving|epoch"):
    """작업이 끝날 때까지 기다리며 로그 중 show 에 맞는 새 줄을 찍는다. 종료 코드를 돌려준다."""
    import re
    seen = 0
    while True:
        time.sleep(poll)
        try:
            jobs = json.loads(urllib.request.urlopen(urllib.request.Request(agent_url(pod_id) + "/jobs", headers=UA), timeout=20).read())
            log = urllib.request.urlopen(urllib.request.Request(f"{agent_url(pod_id)}/job_{name}.log", headers=UA), timeout=30).read()
        except Exception as e:
            print(f"  (에이전트 응답 없음: {str(e)[:80]})", flush=True)
            continue
        text = log.decode("utf-8", "replace").replace(chr(13), chr(10)).splitlines()
        hits = [ln for ln in text[seen:] if re.search(show, ln)]
        if hits:
            print("  " + hits[-1][-200:], flush=True)
        seen = len(text)
        j = jobs.get(name)
        if j and not j["running"]:
            return j["rc"]


def agent_download(pod_id, remote, local):
    data = _agent(pod_id, "GET", "/download?" + urllib.parse.urlencode({"path": remote}), timeout=900)
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    Path(local).write_bytes(data)
    return len(data)


def upload_models(pod_id, image):
    """runpod.json 의 upload_models(우리가 학습한 LoRA 등 로컬 파일)를 ComfyUI models 폴더로 올린다."""
    items = cfg().get("upload_models", [])
    if not items:
        return
    base = "/comfyui/models" if "worker-comfyui" in image else "/workspace/ComfyUI/models"
    for m in items:
        src = ROOT / m["src"]
        if not src.exists():
            print(f"  올릴 모델이 없다: {src}", flush=True)
            continue
        size = src.stat().st_size
        for attempt in range(3):  # 받은 바이트가 원본과 다르면 다시(2026-10-12 깨진 LoRA 로 D+18 104컷이 전부 회색으로 나왔다)
            got = agent_upload(pod_id, src, f"{base}/{m['dest']}").get("bytes")
            if got == size:
                break
            print(f"  모델 올림 크기 불일치({got} != {size}), 다시 올림", flush=True)
        else:
            raise RuntimeError(f"모델 올리기 실패: {m['dest']}")
        print(f"  모델 올림: {m['dest']} ({size / 2**20:.0f}MB, 확인)", flush=True)

def terminate(pod_id):
    try:
        api("DELETE", f"/pods/{pod_id}")
        print(f"파드 삭제: {pod_id}", flush=True)
    except RuntimeError as e:
        print(f"파드 삭제 실패(직접 확인 필요: status): {pod_id} {e}", flush=True)
        return
    if load_state().get("pod_id") == pod_id:
        STATE.unlink(missing_ok=True)


def stop(pod_id):
    """GPU 과금을 끝낸다. 멈추기에 실패하면 과금이 이어지지 않게 지운다."""
    try:
        api("POST", f"/pods/{pod_id}/stop")
    except RuntimeError as e:
        print(f"파드 멈춤 실패, 대신 지운다: {e}", flush=True)
        terminate(pod_id)
        return
    save_state(stopped_at=time.strftime("%Y-%m-%d %H:%M"))
    c = cfg()
    print(f"파드 멈춤: {pod_id} (GPU 과금 끝. 멈춘 동안 보관비 월 약 ${c['volume_gb'] * c['stopped_storage_usd_per_gb_month']:.1f}, "
          f"한동안 안 쓸 거면 down)", flush=True)


def ensure_pod(mode="comfy", image=None):
    """쓸 수 있는 파드를 하나 켜서 (pod_id, url) 을 돌려준다. 멈춘 파드가 있으면 다시 켜고, 안 되면 새로 만든다.
    mode=agent 면 늘 새로 만들고 에이전트 주소를 돌려준다."""
    c, s = cfg(), load_state()
    if mode == "agent":
        s = {}
    # 지우는 모드(keep_pod false)에선 남의 파드를 건드리지 않는다. 켜져 있는 파드는 다른 작업이 쓰는 중일 수 있다
    # (2026-10-07 두 묶음을 동시에 돌리자 두 번째가 첫 번째 파드를 지워 첫 번째가 502 로 끊겼다)
    pod = get_pod(s["pod_id"]) if c["keep_pod"] and s.get("pod_id") else None
    if network_volume() and pod and pod.get("desiredStatus") != "RUNNING":
        pod = None  # 네트워크 볼륨이면 멈춘 파드를 다시 켜지 않고 새로 만든다(설치·모델은 볼륨에 있어 몇 분이면 뜬다)
    if pod and s.get("setup_sha") != setup_sha():
        # 예전엔 무엇이 바뀌든 파드를 지우고 새로 만들어, 볼륨에 받아 둔 모델(약 190GB)·학습 자료까지 날아갔다
        # (2026-10-22 놀면 멈춤 60→30분만 바꿨는데 켜져 있던 파드가 지워짐). 이미지·디스크가 그대로면 지우지 않는다:
        # 켜져 있으면 다른 작업이 쓰는 중일 수 있어 그대로 쓰고, 멈춰 있으면 env(부트스트랩 등)만 새 값으로 바꿔 다시 켠다.
        if s.get("hard_sha", hard_sha()) != hard_sha():
            print("파드 이미지·디스크 설정이 바뀌어 기존 파드를 지우고 새로 만든다", flush=True)
            terminate(pod["id"])
            pod = None
        elif pod.get("desiredStatus") == "RUNNING":
            print("파드 설정이 바뀌었지만 켜져 있는 파드라 그대로 쓴다(멈춘 뒤 다음에 켤 때 새 설정을 넣는다)", flush=True)
        else:
            token = (pod.get("env") or {}).get("AGENT_TOKEN") or secrets.token_hex(16)
            try:
                # RunPod 가 넣은 값(PUBLIC_KEY 등)은 남기고 우리 값만 새로
                api("PATCH", f"/pods/{pod['id']}", {"env": {**(pod.get("env") or {}), **pod_env("comfy", token)}})
                save_state(setup_sha=setup_sha(), hard_sha=hard_sha())
                print(f"파드 설정만 새로 넣었다(볼륨 그대로): {pod['id']}", flush=True)
                pod = get_pod(pod["id"]) or pod
            except Exception as e:  # noqa: BLE001  못 바꾸면 옛 설정 그대로 켠다(지우지 않는다)
                print(f"파드 설정을 못 바꿨다({str(e)[:200]}). 옛 설정 그대로 켠다", flush=True)
    if pod:
        try:
            if pod.get("desiredStatus") != "RUNNING":
                api("POST", f"/pods/{pod['id']}/start")
                print(f"파드 다시 켬: {pod['id']} (시간당 ${pod.get('costPerHr', '?')})", flush=True)
            save_state(stopped_at=None)
            return pod["id"], wait_ready(pod["id"], c["resume_timeout_min"])
        except Exception as e:
            # 지우지 않고 멈춘 채 남긴다: 볼륨에 받은 모델·학습한 LoRA 가 있을 수 있다(2026-10-23 그 기계에 빈 GPU 가 없어 못 켜자
            # 볼륨째 지우고 새로 만들어, 안을 확인하지 못한 채 잃었다). 남긴 파드는 state 의 left_pods 에 적고, 필요 없으면 down 으로 지운다
            print(f"다시 켜지 못했다({str(e)[:200]}). 이 파드는 멈춘 채 남기고(볼륨 보관) 새로 만든다: {pod['id']}", flush=True)
            with contextlib.suppress(Exception):
                if (get_pod(pod["id"]) or {}).get("desiredStatus") == "RUNNING":
                    api("POST", f"/pods/{pod['id']}/stop")
            save_state(left_pods=sorted(set(load_state().get("left_pods", [])) | {pod["id"]}))
    # 기계를 잘못 만나면(드라이버·디스크 문제) 그 파드를 지우고 한 번 더 새로 만든다
    for attempt in (1, 2):
        pod_id = create_pod(mode, image)
        try:
            url = wait_ready(pod_id, c["ready_timeout_min"], mode)
            if mode == "comfy":
                upload_models(pod_id, image or c["image"])
            return pod_id, url
        except Exception as e:
            terminate(pod_id)
            if attempt == 2:
                raise
            print(f"파드가 준비되지 않아 새로 만든다: {str(e)[:200]}", flush=True)


def release(pod_id):
    if cfg()["keep_pod"] and not network_volume():
        stop(pod_id)
    else:
        terminate(pod_id)


@contextlib.contextmanager
def session():
    """파드를 켜서 ComfyUI URL 을 넘겨주고, 블록이 끝나면(예외여도) 멈추거나 지운다."""
    pod_id, url = ensure_pod()
    try:
        yield url
    finally:
        release(pod_id)


def our_pods():
    pods = api("GET", "/pods")
    pods = pods if isinstance(pods, list) else pods.get("pods", pods.get("data", []))
    return [p for p in pods if str(p.get("name", "")).startswith(cfg()["name_prefix"])]


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "run":
        rest = args[args.index("--") + 1:] if "--" in args else args[1:]
        if not rest:
            sys.exit("run 뒤에 -- <명령> 을 준다")
        t0 = time.time()
        with session() as url:
            r = subprocess.run(rest, env={**os.environ, "COMFY_URL": url, "PYTHONUTF8": "1"})
        print(f"총 {(time.time() - t0) / 60:.1f}분", flush=True)
        sys.exit(r.returncode)
    elif cmd == "up":
        pod_id, url = ensure_pod()
        print(url)
        print("다 쓰면: python tools/runpod_cloud.py stop")
    elif cmd == "llm":
        # 켜 둔 파드에 LLM(vLLM, 포트 8000)을 띄운다. 설정은 runpod_*.json 의 llm. 파드를 다시 켜면 다시 부른다
        s, llm = load_state(), cfg()["llm"]
        script = (ROOT / "cloud" / "runpod_llm.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
        env = {"LLM_REPO": llm["repo"], "LLM_NAME": llm["name"], "VLLM_VERSION": llm["vllm_version"],
               "GPU_UTIL": str(llm["gpu_util"]), "MAX_LEN": str(llm["max_len"]), "LLM_ARGS": llm.get("args", ""),
               "SCRIPT_B64": base64.b64encode(script.encode()).decode("ascii")}
        print(agent_run(s["pod_id"], "llm", "echo $SCRIPT_B64 | base64 -d > /workspace/llm.sh && bash /workspace/llm.sh", env))
        print(f"로그: {agent_url(s['pod_id'])}/job_llm.log · 주소: https://{s['pod_id']}-8000.proxy.runpod.net/v1 (모델 {llm['name']}, 키는 파드 env AGENT_TOKEN)")
    elif cmd == "sync":
        # 설정의 custom_nodes·extra_models 중 켜 둔 파드에 없는 것만 받는다(부트스트랩과 같은 방식). 다 받으면 setup_sha 를 맞춰
        # 다음 up 이 '설정이 바뀌었다'며 파드를 지우고 새로 만들지 않게 한다. 새 노드는 ComfyUI 를 다시 켜야 붙는다
        s, c = load_state(), cfg()
        code = (
            "import hashlib, json, os, shutil, subprocess, urllib.request\n"
            "from huggingface_hub import hf_hub_download\n"
            "base = '/workspace/ComfyUI'\n"
            "def fetch(m, dest):  # url 항목(Civitai 등): 받아서 sha256 이 맞을 때만 둔다\n"
            "    tmp = dest + '.part'\n"
            "    req = urllib.request.Request(m['url'], headers={'User-Agent': 'webtoon-studio/1.0'})\n"
            "    h = hashlib.sha256()\n"
            "    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, 'wb') as f:\n"
            "        for chunk in iter(lambda: r.read(1 << 22), b''):\n"
            "            f.write(chunk); h.update(chunk)\n"
            "    if m.get('sha256') and h.hexdigest().lower() != m['sha256'].lower():\n"
            "        os.remove(tmp); raise RuntimeError(f\"sha256 mismatch {m['dest']}\")\n"
            "    os.replace(tmp, dest)\n"
            "for n in json.loads(os.environ['CUSTOM_NODES']):\n"
            "    d = f\"{base}/custom_nodes/{n['dir']}\"\n"
            "    if not os.path.isdir(d):\n"
            "        subprocess.run(['git', 'clone', '-q', n['url'], d], check=True)\n"
            "        subprocess.run(['git', '-C', d, 'checkout', '-q', n['ref']], check=True)\n"
            "    print(f\"[sync] node {n['dir']} ok\", flush=True)\n"
            "for m in json.loads(os.environ['EXTRA_MODELS']):\n"
            "    dest = f\"{base}/models/{m['dest']}\"\n"
            "    if not (os.path.exists(dest) and os.path.getsize(dest) > 0):\n"
            "        os.makedirs(os.path.dirname(dest), exist_ok=True)\n"
            "        if m.get('url'):\n"
            "            fetch(m, dest)\n"
            "        else:\n"
            "            shutil.move(hf_hub_download(m['repo'], m['file'], local_dir='/workspace/hf_tmp'), dest)\n"
            "    print(f\"[sync] model {m['dest']} {os.path.getsize(dest) >> 20}MB\", flush=True)\n"
            "print('[sync] done', flush=True)\n")
        env = {"CUSTOM_NODES": json.dumps(c.get("custom_nodes", [])), "EXTRA_MODELS": json.dumps(c.get("extra_models", [])),
               "HF_HUB_ENABLE_HF_TRANSFER": "1", "SYNC_B64": base64.b64encode(code.encode()).decode("ascii")}
        print(agent_run(s["pod_id"], "sync", "echo $SYNC_B64 | base64 -d > /workspace/sync.py && /workspace/venv/bin/python /workspace/sync.py", env))
        print(f"로그: {agent_url(s['pod_id'])}/job_sync.log")
        save_state(setup_sha=setup_sha())
    elif cmd in ("use", "unuse"):
        # comfy_client(state/comfy_url.txt)·judge_ep(state/vlm_url.txt '주소|모델|키')가 읽는 스위치. 키는 화면에 찍지 않는다
        st = ROOT / "state"
        st.mkdir(exist_ok=True)
        if cmd == "unuse":
            for f in ("comfy_url.txt", "vlm_url.txt"):
                (st / f).unlink(missing_ok=True)
            print("스위치를 지웠다(도구가 다시 COMFY_URL·vast·사내 게이트웨이를 쓴다)")
        else:
            pod_id = load_state()["pod_id"]
            (st / "comfy_url.txt").write_text(proxy_url(pod_id) + "\n", encoding="utf-8")
            print("ComfyUI:", proxy_url(pod_id))
            llm = cfg().get("llm")
            if llm:
                key = (get_pod(pod_id) or {}).get("env", {}).get("AGENT_TOKEN", "")
                (st / "vlm_url.txt").write_text(f"https://{pod_id}-8000.proxy.runpod.net/v1|{llm['name']}|{key}\n", encoding="utf-8")
                print(f"LLM: https://{pod_id}-8000.proxy.runpod.net/v1 ({llm['name']}, 키 {len(key)}자)")
    elif cmd == "stop":
        pods = [p for p in our_pods() if p.get("desiredStatus") == "RUNNING"]
        for p in pods:
            release(p["id"])
        if not pods:
            print("켜진 파드 없음")
    elif cmd == "down":
        pods = our_pods()
        for p in pods:
            terminate(p["id"])
        STATE.unlink(missing_ok=True)
        if not pods:
            print("지울 파드 없음")
    elif cmd == "status":
        c, s = cfg(), load_state()
        pods = our_pods()
        for p in pods:
            line = f"{p['id']} {p.get('name')} {p.get('desiredStatus')} 켜져 있으면 ${p.get('costPerHr', '?')}/h"
            if p.get("desiredStatus") != "RUNNING":
                line += f", 멈춘 동안 보관비 월 약 ${c['volume_gb'] * c['stopped_storage_usd_per_gb_month']:.1f}"
                if s.get("pod_id") == p["id"] and s.get("stopped_at"):
                    days = (time.time() - time.mktime(time.strptime(s["stopped_at"], "%Y-%m-%d %H:%M"))) / 86400
                    line += f", {days:.0f}일째 멈춤"
                    if days >= c["stale_stopped_days"]:
                        line += " (오래 안 썼다. 계속 안 쓸 거면 down)"
            print(line)
        if not pods:
            print("파드 없음(보관비도 없음)")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
