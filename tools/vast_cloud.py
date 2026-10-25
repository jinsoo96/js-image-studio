"""vast.ai 인스턴스에 ComfyUI(+그림 보는 LLM)를 띄워 쓴다. runpod_cloud.py 와 같은 동사·같은 설치(cloud/runpod_bootstrap.sh·runpod_llm.sh)라
서버만 바꿔 끼운다: 도구(MCP·미사일 스튜디오)는 state/comfy_url.txt·vlm_url.txt 만 따라가고, 여기선 그 둘이 늘 이 PC 의 SSH 터널 주소(127.0.0.1)다.

    python tools/vast_cloud.py offers    설정 search 에 맞는 빈 기계·시간당 값·첫 설치 내려받기 값(계정 없이 공개 목록, 돈 0)
    python tools/vast_cloud.py up        빌려서(멈춰 둔 게 있으면 다시 켜서) 설치하고 ComfyUI 가 GPU 를 잡을 때까지 기다린다. 다 쓰면 stop
    python tools/vast_cloud.py llm       켜 둔 인스턴스에 LLM(설정 llm, vLLM)을 띄운다. GPU 가 둘이면 gpu_split.llm 장에만
    python tools/vast_cloud.py use       모든 도구가 이 서버를 쓰게 스위치 파일을 적는다(unuse 는 지운다)
    python tools/vast_cloud.py wake      켜기 버튼(미사일 스튜디오, tools/gpu_wake.py 가 부름): up → use → llm → LLM 이 답할 때까지, '##progress' 줄로 진행
    python tools/vast_cloud.py stop      GPU 과금을 끝낸다(keep_instance 면 멈추고 디스크만 남김, 아니면 지움)
    python tools/vast_cloud.py down      우리 인스턴스(label 이 label_prefix 로 시작)를 전부 지운다(멈춘 것까지, 보관비 0)
    python tools/vast_cloud.py status    우리 인스턴스·시간당 값·잔액·터널
    python tools/vast_cloud.py guard     예산 지킴이: 잔액이 min_balance_usd 이하이면 켜진 우리 인스턴스를 멈춘다

키는 환경변수 VAST_API_KEY(없으면 Windows 사용자 환경). 설정은 VAST_PROFILE 또는 --profile(기본 vast_qwen → cloud/vast_qwen.json),
설치 내용(이미지·ComfyUI·모델·노드·패치·LLM)은 그 설정의 stack_from 이 가리키는 설정에서 가져온다(같은 구성을 두 번 적지 않는다).
접속은 SSH 하나만: vast 는 공유 IP 의 아무 포트로 열리고 ComfyUI 에는 인증이 없어서 포트를 밖에 열지 않고 터널(tunnel, up 이 뒤에서 띄우고
끊기면 다시 잇는다)로만 붙는다. 인스턴스 안에서도 놀면(idle_min)·오래 켜지면(max_hours) 스스로 멈춘다(vast 인스턴스 전용 키).
"""
import base64
import contextlib
import gzip
import io
import json
import os
import secrets
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = sys.argv[sys.argv.index("--profile") + 1] if "--profile" in sys.argv[:-1] else os.environ.get("VAST_PROFILE", "vast_qwen")
CONFIG = ROOT / "cloud" / f"{PROFILE}.json"
STATE = ROOT / "state" / f"vast_{PROFILE}.json"
BOOTSTRAP = ROOT / "cloud" / "runpod_bootstrap.sh"
LLM_SH = ROOT / "cloud" / "runpod_llm.sh"
TUNNEL_LOG = ROOT / "state" / "logs" / f"vast_tunnel_{PROFILE}.log"
API = "https://console.vast.ai/api/v0"
UA = {"User-Agent": "webtoon-studio/1.0"}
# stack_from 에서 가져오는 설치 내용. 어디서 빌릴지(search·디스크·GPU 나누기·놀면 멈춤)는 이 설정 것
STACK_KEYS = ("image", "comfyui_ref", "custom_nodes", "extra_models", "extra_files", "patches", "checkpoint", "llm",
              "comfy_start_timeout_min", "download_timeout_min")
# vast 는 SSH 접속에 자기 env(CONTAINER_ID·CONTAINER_API_KEY 등)를 넘기지 않는다: 켜질 때 파일로 남겨 두고 부트스트랩을 띄울 때 읽는다
ONSTART = "export -p > /root/.vast_env; chmod 600 /root/.vast_env; touch /root/.no_auto_tmux"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def cfg() -> dict:
    """설정 + stack_from 의 설치 내용. 같은 키가 있으면 이 설정이 이기고, llm 은 항목별로 덮는다(같은 모델, GPU 몫만 다르게)."""
    c = json.loads(CONFIG.read_text(encoding="utf-8"))
    if c.get("stack_from"):
        base = json.loads((CONFIG.parent / f"{c['stack_from']}.json").read_text(encoding="utf-8"))
        for k in STACK_KEYS:
            if k in base and k not in c:
                c[k] = base[k]
            elif k == "llm" and k in base:
                c[k] = {**base[k], **c[k]}
    return c


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(**kw) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({**load_state(), **kw}, ensure_ascii=False, indent=2), encoding="utf-8")


def api_key() -> str:
    """VAST_API_KEY. 예약작업(지킴이)은 사용자 환경변수를 못 받을 때가 있어 Windows 사용자 환경(레지스트리)도 본다."""
    key = os.environ.get("VAST_API_KEY", "")
    if not key and os.name == "nt":
        with contextlib.suppress(OSError):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "VAST_API_KEY")[0]
    return key


def api(method: str, path: str, body=None):
    key = api_key()
    if not key:
        sys.exit("환경변수 VAST_API_KEY 가 없다(vast 계정 → Account → API Keys 에서 만들어 사용자 환경변수로 둔다)")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={**UA, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"})
    for k in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < 2:  # 요청이 잦으면 잠깐 막힌다
                time.sleep(5 * (k + 1))
                continue
            raise RuntimeError(f"vast {method} {path} -> {e.code}: {e.read().decode('utf-8', 'replace')[:400]}") from e
        except urllib.error.URLError as e:
            if k == 2:
                raise RuntimeError(f"vast {method} {path}: {e}") from e
            time.sleep(5 * (k + 1))
    raise RuntimeError(f"vast {method} {path}: 답이 없다")


# ---------- 기계 고르기(공개 목록, 키 없이) ----------

def offers(exclude_machines=()) -> list:
    """설정 search 에 맞는 빈 기계를 시간당 값(GPU + 디스크 보관비) 싼 순으로. 거르는 조건은 받아 온 뒤 여기서 본다(목록 필드를 그대로 읽어 틀릴 일이 적다)."""
    c = cfg()
    s = c["search"]
    q = {"gpu_name": {"eq": s["gpu_name"]}, "num_gpus": {"eq": int(s["num_gpus"])}, "rentable": {"eq": True}, "rented": {"eq": False},
         "type": "on-demand", "order": [["dph_total", "asc"]], "limit": 200}
    if s.get("verified", True):
        q["verified"] = {"eq": True}
    url = API + "/bundles/?" + urllib.parse.urlencode({"q": json.dumps(q)})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        rows = json.loads(r.read()).get("offers", [])
    out = []
    for o in rows:
        if o.get("machine_id") in exclude_machines:
            continue
        if (o.get("cuda_max_good") or 0) < float(s.get("min_cuda", 0)):
            continue
        if (o.get("reliability2") or o.get("reliability") or 0) < float(s.get("min_reliability", 0)):
            continue
        if (o.get("inet_down") or 0) < float(s.get("min_inet_down_mbps", 0)):
            continue
        if (o.get("disk_space") or 0) < float(c["disk_gb"]):
            continue
        if (o.get("inet_down_cost") or 0) > float(s.get("max_inet_down_usd_per_gb", 1e9)):
            continue
        if str(o.get("geolocation") or "")[-2:] in set(s.get("avoid_countries", [])):
            continue
        hourly = (o.get("dph_base") or 0) + (o.get("storage_cost") or 0) * float(c["disk_gb"]) / 730
        if hourly > float(s.get("max_usd_per_hr", 1e9)):
            continue
        stopped_month = (o.get("storage_cost") or 0) * float(c["disk_gb"])
        # 멈춰 둘 거면 보관비가 기계마다 월 $20~73(150GB)까지 차이 나서 상한을 넘는 기계는 빼고, 지울 거면 보관비는 따지지 않는다
        if c.get("keep_instance") and stopped_month > float(c.get("max_stopped_usd_per_month", 1e9)):
            continue
        out.append({**o, "_hourly": hourly, "_setup_usd": (o.get("inet_down_cost") or 0) * float(c.get("setup_download_gb", 0)),
                    "_stopped_month": stopped_month})
    return sorted(out, key=lambda o: o["_hourly"])


def show_offers() -> None:
    c = cfg()
    o = offers()
    s = c["search"]
    print(f"조건: {s['gpu_name']} x{s['num_gpus']} · CUDA≥{s.get('min_cuda')} · 신뢰도≥{s.get('min_reliability')} · 내려받기≥{s.get('min_inet_down_mbps')}Mbps"
          f" · 디스크 {c['disk_gb']}GB · 시간당≤${s.get('max_usd_per_hr')} · 뺄 나라 {s.get('avoid_countries')}"
          f" · 다 쓰면 {'멈춤(보관비 월≤$' + str(c.get('max_stopped_usd_per_month')) + ')' if c.get('keep_instance') else '지움(보관비 0)'}")
    if not o:
        print("맞는 빈 기계 없음(조건을 넓히려면 설정 search)")
        return
    print(f"맞는 기계 {len(o)}대, 시간당 중간 ${statistics.median(x['_hourly'] for x in o):.2f}")
    for x in o[:10]:
        print(f"  offer {x['id']} · 기계 {x.get('machine_id')} · {x.get('geolocation')} · 시간당 ${x['_hourly']:.2f} · 첫 설치 내려받기 약 ${x['_setup_usd']:.2f}"
              f" · 멈춘 동안 월 ${x['_stopped_month']:.1f} · 신뢰도 {(x.get('reliability2') or 0):.3f} · {x.get('inet_down', 0):.0f}Mbps · CUDA {x.get('cuda_max_good')}")


# ---------- 인스턴스 ----------

def get_instance(iid):
    """인스턴스 하나(없거나 지워졌으면 None)."""
    try:
        r = api("GET", f"/instances/{iid}/")
    except RuntimeError as e:
        if "-> 404" in str(e):
            return None
        raise
    inst = r.get("instances", r) if isinstance(r, dict) else None
    return inst if isinstance(inst, dict) and inst.get("id") else None


def list_instances() -> list:
    try:
        r = api("GET", "/instances/?owner=me")
    except RuntimeError as e:
        if "-> 404" not in str(e):
            raise
        req = urllib.request.Request("https://console.vast.ai/api/v1/instances", headers={**UA, "Authorization": f"Bearer {api_key()}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            r = json.loads(resp.read())
    return r.get("instances", []) if isinstance(r, dict) else []


def our_instances() -> list:
    return [i for i in list_instances() if str(i.get("label") or "").startswith(cfg()["label_prefix"])]


def running(inst) -> bool:
    return bool(inst) and inst.get("actual_status") == "running"


def balance() -> dict:
    """{"credit": 쓸 수 있는 돈, "fields": 원래 값}. 문서(2026-10-25)는 balance, 예전 CLI 는 credit(선불 잔액)도 준다: credit 이 있으면 그 값."""
    u = api("GET", "/users/current/")
    for k in ("credit", "balance"):
        if isinstance(u.get(k), (int, float)):
            return {"credit": float(u[k]), "fields": {f: u.get(f) for f in ("credit", "balance", "balance_threshold", "billed_expected") if f in u}}
    raise RuntimeError("vast 잔액 필드를 못 찾음: " + ", ".join(sorted(u)))


def budget_ok(need_hours: float = 0.5) -> bool:
    """잔액이 바닥(min_balance_usd) 위에서 need_hours 를 더 쓸 수 있으면 True. 바닥이 비어 있으면 켜지 않는다(RunPod 와 달리 새 계정이라 먼저 정하게)."""
    floor = cfg().get("min_balance_usd")
    if floor is None:
        raise SystemExit("설정 min_balance_usd(예산 바닥)가 비어 있어 켜지 않는다: 충전한 뒤 '충전액 − 허락한 지출'로 채운다(사용자가 정함)")
    return balance()["credit"] - float(cfg()["search"].get("max_usd_per_hr", 0)) * need_hours > float(floor)


def destroy(iid) -> None:
    try:
        api("DELETE", f"/instances/{iid}/")
        print(f"인스턴스 삭제: {iid}", flush=True)
    except RuntimeError as e:
        print(f"인스턴스 삭제 실패(직접 확인 필요: status): {iid} {e}", flush=True)
        return
    if load_state().get("instance_id") == iid:
        kill_tunnel()
        STATE.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        (ROOT / "state" / "ssh" / f"vast_{iid}.known_hosts").unlink()


def stop_instance(iid) -> None:
    """GPU 과금을 끝낸다(디스크는 남김). 멈추기에 실패하면 과금이 이어지지 않게 지운다."""
    try:
        api("PUT", f"/instances/{iid}/", {"state": "stopped"})
    except RuntimeError as e:
        print(f"멈춤 실패, 대신 지운다: {e}", flush=True)
        destroy(iid)
        return
    kill_tunnel()
    save_state(stopped_at=time.strftime("%Y-%m-%d %H:%M"))
    print(f"인스턴스 멈춤: {iid} (GPU 과금 끝, 디스크 보관비만. 한동안 안 쓸 거면 down)", flush=True)


def release(iid) -> None:
    if cfg().get("keep_instance"):
        stop_instance(iid)
    else:
        destroy(iid)


def create_instance(exclude=()) -> int:
    c = cfg()
    for o in offers(exclude)[:5]:
        body = {"client_id": "me", "image": c["image"], "disk": c["disk_gb"], "label": f"{c['label_prefix']}-{time.strftime('%m%d-%H%M%S')}",
                "runtype": "ssh", "onstart": ONSTART, "cancel_unavail": True}
        try:
            r = api("PUT", f"/asks/{o['id']}/", body)
        except RuntimeError as e:
            print(f"  빌리기 실패(offer {o['id']}): {str(e)[:200]}", flush=True)
            continue
        iid = r.get("new_contract")
        if not iid:
            print(f"  빌리기 답에 인스턴스 번호가 없다(offer {o['id']}): {str(r)[:200]}", flush=True)
            continue
        print(f"인스턴스 생성: {iid} ({o['gpu_name']} x{o['num_gpus']}, {o.get('geolocation')}, 시간당 약 ${o['_hourly']:.2f},"
              f" 첫 설치 내려받기 약 ${o['_setup_usd']:.2f})", flush=True)
        save_state(instance_id=iid, machine_id=o.get("machine_id"), created=time.strftime("%Y-%m-%d %H:%M"),
                   token=secrets.token_hex(16), stopped_at=None, hourly=round(o["_hourly"], 3))
        return iid
    raise RuntimeError("조건에 맞는 빈 기계를 못 빌렸다(offers 로 확인, 조건은 설정 search)")


# ---------- SSH ----------

def key_path() -> Path:
    return Path(os.path.expanduser(cfg()["ssh_key"]))


def ensure_key() -> None:
    """이 일에만 쓰는 SSH 키를 만들고 계정에 올린다(인스턴스를 만들기 전에 올려야 붙는다)."""
    p = key_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([shutil.which("ssh-keygen") or "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "webtoon-studio-vast", "-f", str(p)],
                       check=True, creationflags=NO_WINDOW)
    pub = Path(str(p) + ".pub").read_text(encoding="utf-8").strip()
    if load_state().get("ssh_key_pub") == pub or _account_has_key(pub):
        save_state(ssh_key_pub=pub)
        return
    api("POST", "/ssh/", {"ssh_key": pub})
    save_state(ssh_key_pub=pub)
    print("SSH 키를 계정에 올렸다", flush=True)


def _account_has_key(pub: str) -> bool:
    with contextlib.suppress(Exception):
        r = api("GET", "/ssh/")
        rows = r if isinstance(r, list) else (r.get("keys") or r.get("ssh_keys") or [])
        body = pub.split()[1]
        return any(body in str(x.get("public_key") or x.get("ssh_key") or x) for x in rows)
    return False


def ssh_target(inst) -> tuple:
    """공인 IP 의 22 매핑이 있으면 그쪽(직접, 빠름), 없으면 vast 의 SSH 중계."""
    p = (inst.get("ports") or {}).get("22/tcp") or []
    if inst.get("public_ipaddr") and p:
        return str(inst["public_ipaddr"]).strip(), int(p[0]["HostPort"])
    return inst["ssh_host"], int(inst["ssh_port"])


def ssh_base(inst, extra=()) -> list:
    host, port = ssh_target(inst)
    kh = ROOT / "state" / "ssh" / f"vast_{inst['id']}.known_hosts"
    kh.parent.mkdir(parents=True, exist_ok=True)
    return [shutil.which("ssh") or "ssh", "-T", "-i", str(key_path()), "-p", str(port), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={kh}", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", *extra, f"root@{host}"]


def ssh_run(inst, cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Windows OpenSSH(9.5)는 출력이 파이프면 끝나지 않고, 입력을 파이프·파일로 주면 끝(EOF)을 못 넘겨 멈춘다(2026-10-25 로컬 sshd 시험).
    그래서 입력은 늘 비우고(DEVNULL) 출력은 임시 파일로 받는다. 올릴 것은 ssh_put 이 명령줄에 실어 보낸다."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(ssh_base(inst) + [cmd], stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=NO_WINDOW)
        try:
            rc = p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(p.args, rc, out.read(), err.read())


PUT_CHUNK = 24000  # Windows 명령줄 한도(32,767자) 안에서 한 번에 싣는 base64 글자 수


def ssh_put(inst, data: bytes, path: str) -> None:
    """data 를 인스턴스의 path 에 쓴다: gzip+base64 를 조각내 명령줄로 보내고 거기서 푼다(sftp·scp 가 있는지 따지지 않는다)."""
    b64 = base64.b64encode(gzip.compress(data)).decode("ascii")
    tmp = f"/tmp/.put_{secrets.token_hex(4)}"
    for i in range(0, len(b64), PUT_CHUNK):
        r = ssh_run(inst, f"printf %s {b64[i:i + PUT_CHUNK]} {'>' if i == 0 else '>>'} {tmp}")
        if r.returncode:
            raise RuntimeError(f"올리기 실패(조각 {i // PUT_CHUNK}): {r.stderr.decode('utf-8', 'replace')[-300:]}")
    r = ssh_run(inst, f"base64 -d {tmp} | gunzip > {shlex.quote(path)} && rm -f {tmp}")
    if r.returncode:
        raise RuntimeError(f"올린 것 풀기 실패: {r.stderr.decode('utf-8', 'replace')[-300:]}")


def wait_running(iid, timeout_min: float) -> dict:
    """vast 가 running 이라 하고 SSH 가 받을 때까지. exited·offline·unknown 은 영영 안 뜨는 것(문서)이라 바로 실패."""
    t0, last = time.time(), ""
    while time.time() - t0 < timeout_min * 60:
        inst = get_instance(iid)
        if not inst:
            raise RuntimeError(f"인스턴스가 없어졌다: {iid}")
        st, msg = inst.get("actual_status"), str(inst.get("status_msg") or "")[:160]
        if f"{st} {msg}" != last:
            print(f"  [{(time.time() - t0) / 60:4.1f}분] {st} {msg}", flush=True)
            last = f"{st} {msg}"
        if st in ("exited", "offline", "unknown") and time.time() - t0 > 60:
            raise RuntimeError(f"인스턴스가 {st}: {msg}")
        if st == "running" and inst.get("ssh_host"):
            with contextlib.suppress(Exception):
                if ssh_run(inst, "true", timeout=40).returncode == 0:
                    return inst
        time.sleep(15)
    raise TimeoutError(f"인스턴스가 {timeout_min}분 안에 뜨지 않았다: {iid}")


# ---------- 설치(부트스트랩) ----------

def extra_files_json(c) -> str:
    """{ComfyUI 안 경로: base64} — 우리 노드·상류 패치(runpod_cloud 와 같은 꼴, 부트스트랩이 같은 방식으로 푼다)."""
    files = {dest: base64.b64encode((ROOT / src).read_bytes().replace(b"\r\n", b"\n")).decode("ascii") for dest, src in c.get("extra_files", {}).items()}
    return json.dumps(files, sort_keys=True)


def comfy_args(c) -> str:
    # 터널로만 붙으니 ComfyUI 는 인스턴스 안(127.0.0.1)에서만 듣게 한다. GPU 가 둘이면 그림은 gpu_split.comfy 장에
    args = ["--listen", "127.0.0.1"]
    dev = (c.get("gpu_split") or {}).get("comfy")
    if dev not in (None, ""):
        args += ["--cuda-device", str(dev)]
    return " ".join(args)


def pod_env(c, token: str) -> dict:
    """부트스트랩이 읽는 env(runpod_cloud.pod_env 와 같은 이름)."""
    return {
        "MODE": "comfy",
        "AGENT_TOKEN": token,
        "CKPT_URL": c["checkpoint"]["url"],
        "CKPT_NAME": c["checkpoint"]["name"],
        "EXTRA_FILES": extra_files_json(c),
        "PATCHES": json.dumps(c.get("patches", [])),
        "COMFY_REF": c["comfyui_ref"],
        "CUSTOM_NODES": json.dumps(c.get("custom_nodes", [])),
        "EXTRA_MODELS": json.dumps(c.get("extra_models", [])),
        "IDLE_MIN": str(c["idle_min"]),
        "MAX_HOURS": str(c["max_hours"]),
        "ON_IDLE": "stop" if c.get("keep_instance") else "terminate",
        "COMFY_ARGS": comfy_args(c),
    }


def launch_bundle(c, token: str) -> bytes:
    """올릴 묶음(tar): 부트스트랩과 env 파일. vast 의 onstart 는 4KB 까지라 16KB 부트스트랩·base64 노드 파일을 못 담아 SSH 로 올린다(ssh_put)."""
    env_txt = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in pod_env(c, token).items())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in (("workspace/bootstrap.sh", BOOTSTRAP.read_bytes().replace(b"\r\n", b"\n")), ("workspace/pod.env", env_txt.encode("utf-8"))):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


BUNDLE = "/tmp/.webtoon_bundle.tar"
LAUNCH_CMD = (f"mkdir -p /workspace && tar -xf {BUNDLE} -C / && rm -f {BUNDLE} && "
              "{ [ -s /root/.vast_env ] || echo NO_VAST_ENV; } && "
              "if curl -s -m 5 localhost:8188/system_stats >/dev/null 2>&1; then echo ALREADY; else "
              "nohup setsid bash -c 'set -a; [ -f /root/.vast_env ] && . /root/.vast_env; . /workspace/pod.env; set +a; exec bash /workspace/bootstrap.sh' "
              ">/dev/null 2>&1 </dev/null & echo LAUNCHED; fi")


def launch(inst, c) -> None:
    """부트스트랩을 올려 뒤에서 돌린다. ComfyUI 가 이미 떠 있으면(같은 인스턴스를 다시 부른 때) 건너뛴다."""
    ssh_put(inst, launch_bundle(c, load_state()["token"]), BUNDLE)
    r = ssh_run(inst, LAUNCH_CMD, timeout=180)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        raise RuntimeError(f"부트스트랩 올리기 실패(ssh {r.returncode}): {r.stderr.decode('utf-8', 'replace')[-300:]}")
    if "NO_VAST_ENV" in out:
        print("  주의: 인스턴스 env 파일이 없어(onstart 가 안 돎) 안에서 스스로 멈추지 못한다. 이 PC 의 stop·guard 가 대신 멈춘다", flush=True)
    print("  부트스트랩: " + ("이미 떠 있음" if "ALREADY" in out else "올려서 시작함"), flush=True)


# ---------- 터널 ----------

def port_open(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", int(port)), timeout=2):
        return True
    return False


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_tunnel() -> None:
    pid = load_state().get("tunnel_pid")
    if pid and pid_alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            with contextlib.suppress(OSError):
                os.kill(int(pid), 15)
    if STATE.exists():
        save_state(tunnel_pid=None)


def ensure_tunnel() -> None:
    """터널 프로세스가 살아 있으면 그대로(끊겨도 그 프로세스가 다시 잇는다), 없으면 새로 띄운다(이 PC 를 다시 켰거나 stop 뒤)."""
    p = cfg()["local_ports"]
    if pid_alive(load_state().get("tunnel_pid")):
        return
    kill_tunnel()
    # 우리 터널이 없는데 포트가 열려 있으면 남의 프로그램이다(2026-10-25 18000 을 다른 SSH 터널이 쥐고 있었다). 그대로 띄우면
    # ExitOnForwardFailure 로 터널이 끊기고 다시 잇기만 되풀이하니, 어느 포트인지 알리고 멈춘다
    taken = [f"{name}={port}" for name, port in p.items() if port_open(port)]
    if taken:
        raise RuntimeError(f"이 PC 의 포트를 다른 프로그램이 쓰고 있다({', '.join(taken)}): 설정 local_ports 를 빈 포트로 바꾼다")
    TUNNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(TUNNEL_LOG, "a", encoding="utf-8")
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--profile", PROFILE, "tunnel"]
    flags = (0x00000008 | 0x00000200 | NO_WINDOW) if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:  # 부른 쪽(미사일 스튜디오 켜기 버튼 등)이 끝나도 터널은 남게 작업 묶음(job)에서 빼 본다
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                creationflags=flags | (0x01000000 if os.name == "nt" else 0))  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, creationflags=flags)
    save_state(tunnel_pid=proc.pid)
    for _ in range(30):
        if port_open(p["comfy"]):
            print(f"  터널: 127.0.0.1:{p['comfy']}(ComfyUI) · {p['agent']}(설치 로그) · {p['llm']}(LLM)", flush=True)
            return
        time.sleep(1)
    print(f"  터널이 아직 안 열렸다(로그 {TUNNEL_LOG})", flush=True)


def tunnel_loop() -> None:
    """터널을 붙들고, 끊기면 다시 잇는다. state 에서 인스턴스가 빠지거나 멈추면 끝난다(stop·down·destroy 가 지움)."""
    p = cfg()["local_ports"]
    while True:
        s = load_state()
        if not s.get("instance_id") or s.get("stopped_at"):
            print(f"{time.strftime('%H:%M:%S')} 인스턴스 없음/멈춤: 터널 끝", flush=True)
            return
        try:
            inst = get_instance(s["instance_id"])
        except Exception as e:  # noqa: BLE001  API 가 잠깐 안 되면 기다렸다 다시
            print(f"{time.strftime('%H:%M:%S')} 인스턴스 조회 실패: {str(e)[:120]}", flush=True)
            time.sleep(20)
            continue
        if not inst:
            print(f"{time.strftime('%H:%M:%S')} 인스턴스가 지워졌다: 터널 끝", flush=True)
            return
        if not running(inst):
            time.sleep(20)
            continue
        fwd = [a for local, remote in ((p["comfy"], 8188), (p["agent"], 8189), (p["llm"], 8000))
               for a in ("-L", f"127.0.0.1:{local}:127.0.0.1:{remote}")]
        t0 = time.time()
        print(f"{time.strftime('%H:%M:%S')} 터널 연결 {ssh_target(inst)}", flush=True)
        TUNNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TUNNEL_LOG, "ab") as log:  # 출력은 파일로(파이프면 Windows OpenSSH 가 멈춘다, ssh_run 참고)
            subprocess.run(ssh_base(inst, ["-N", "-o", "ExitOnForwardFailure=yes", *fwd]), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                           creationflags=NO_WINDOW)
        print(f"{time.strftime('%H:%M:%S')} 터널 끊김({time.time() - t0:.0f}초 유지)", flush=True)
        time.sleep(3 if time.time() - t0 > 30 else 20)


# ---------- 준비 기다리기·에이전트 ----------

def local(name: str) -> str:
    return f"http://127.0.0.1:{cfg()['local_ports'][name]}"


def http_ok(url: str, key: str = "") -> bool:
    headers = {**UA, **({"Authorization": f"Bearer {key}"} if key else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def boot_log():
    try:
        with urllib.request.urlopen(urllib.request.Request(local("agent") + "/bootstrap.log", headers=UA), timeout=10) as r:
            return r.read().decode("utf-8", "replace").splitlines()
    except Exception:  # noqa: BLE001
        return None


def wait_ready(timeout_min: float) -> str:
    """ComfyUI 가 응답하고 GPU(cuda)를 잡을 때까지. 설치가 멈추면(로그가 15분 그대로·체크포인트 뒤 comfy_start_timeout_min) 실패."""
    c = cfg()
    url = local("comfy")
    t0 = changed = time.time()
    seen, lines, ckpt_at = 0, [], None
    while time.time() - t0 < timeout_min * 60:
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "/system_stats", headers=UA), timeout=10) as r:
                stats = json.loads(r.read())
        except Exception:  # noqa: BLE001
            ensure_tunnel()
            got = boot_log()
            if got and len(got) != len(lines):
                changed = time.time()
            lines = got or lines
            for line in lines[seen:]:
                if line.startswith(("[boot]", "[watchdog]")) or "Error" in line or "Traceback" in line:
                    print("  " + line[:300], flush=True)
                if "[boot] checkpoint ready" in line and ckpt_at is None:
                    ckpt_at = time.time()
            seen = len(lines)
            if not ckpt_at and time.time() - changed > 15 * 60:
                print(f"  설치가 {(time.time() - changed) / 60:.0f}분째 진행이 없다", flush=True)
                break
            if ckpt_at and time.time() - ckpt_at > c["comfy_start_timeout_min"] * 60:
                break
            time.sleep(15)
            continue
        devices = stats.get("devices", [])
        if not any(d.get("type") == "cuda" for d in devices):
            raise RuntimeError(f"ComfyUI 는 떴는데 GPU 가 없다: {[d.get('name') for d in devices]}")
        print(f"ComfyUI 준비됨: {url} ({devices[0].get('name')}, {(time.time() - t0) / 60:.1f}분)", flush=True)
        return url
    for line in (boot_log() or lines)[-40:]:
        print("  " + line[:300], flush=True)
    raise TimeoutError(f"ComfyUI 가 뜨지 않았다({(time.time() - t0) / 60:.0f}분)")


def agent(method: str, path: str, data: bytes = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(local("agent") + path, data=data, method=method,
                                 headers={**UA, "X-Token": load_state().get("token", ""), "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ensure() -> tuple:
    """쓸 수 있는 인스턴스를 켜서 (인스턴스, ComfyUI 주소). 멈춘 것이 있으면 다시 켜고, 안 되면 지우고 새로 빌린다(기계를 바꿔 세 번까지)."""
    c, s = cfg(), load_state()
    if not budget_ok():
        b = balance()
        raise SystemExit(f"예산 바닥: 잔액 ${b['credit']:.2f} 이 설정 min_balance_usd ${c['min_balance_usd']} 에 가까워 켜지 않는다"
                         " (사용자에게 알리고, 바닥을 바꿀지는 사용자가 정한다)")
    ensure_key()
    inst = get_instance(s["instance_id"]) if s.get("instance_id") else None
    if inst and not running(inst):
        if c.get("keep_instance"):
            try:
                api("PUT", f"/instances/{inst['id']}/", {"state": "running"})
                print(f"인스턴스 다시 켬: {inst['id']}", flush=True)
                save_state(stopped_at=None)
                inst = wait_running(inst["id"], c["resume_timeout_min"])
            except Exception as e:  # noqa: BLE001  멈춘 사이 남이 그 GPU 를 빌려 갔으면 못 켠다
                print(f"다시 켜지 못했다({str(e)[:200]}): 지우고 새로 빌린다", flush=True)
                destroy(inst["id"])
                inst = None
        else:
            destroy(inst["id"])
            inst = None
    tried = set()
    for attempt in (1, 2, 3):
        if not inst:
            iid = create_instance(tried)
            try:
                inst = wait_running(iid, c["loading_timeout_min"])
            except Exception as e:  # noqa: BLE001
                print(f"인스턴스가 뜨지 않아 지우고 다른 기계로: {str(e)[:200]}", flush=True)
                tried.add(load_state().get("machine_id"))
                destroy(iid)
                inst = None
                continue
        try:
            launch(inst, c)
            ensure_tunnel()
            url = wait_ready(c["ready_timeout_min"])
            save_state(stopped_at=None)
            return inst, url
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                destroy(inst["id"])
                raise
            print(f"준비되지 않아 지우고 다른 기계로: {str(e)[:200]}", flush=True)
            tried.add(load_state().get("machine_id"))
            destroy(inst["id"])
            inst = None
    raise RuntimeError("세 번 빌려도 준비되지 않았다")


def start_llm() -> None:
    c = cfg()
    L = c["llm"]
    env = {"LLM_REPO": L["repo"], "LLM_NAME": L["name"], "VLLM_VERSION": L["vllm_version"], "GPU_UTIL": str(L["gpu_util"]),
           "MAX_LEN": str(L["max_len"]), "LLM_ARGS": L.get("args", ""),
           "SCRIPT_B64": base64.b64encode(LLM_SH.read_bytes().replace(b"\r\n", b"\n")).decode("ascii")}
    dev = (c.get("gpu_split") or {}).get("llm")
    if dev not in (None, ""):
        env["LLM_CUDA_DEVICES"] = str(dev)
    body = json.dumps({"name": "llm", "cmd": "echo $SCRIPT_B64 | base64 -d > /workspace/llm.sh && bash /workspace/llm.sh", "env": env}).encode()
    print(json.loads(agent("POST", "/run", body)))
    print(f"로그: {local('agent')}/job_llm.log · 주소: {local('llm')}/v1 (모델 {L['name']}, GPU {dev if dev not in (None, '') else '그림과 같은 장'})")


def write_switch() -> None:
    """comfy_client(state/comfy_url.txt)·judge_ep·미사일 스튜디오(state/vlm_url.txt '주소|모델|키')·켜기 버튼(state/gpu_cloud.json)이 읽는 스위치.
    주소는 늘 터널(127.0.0.1)이라 인스턴스를 새로 빌려도 바뀌지 않는다. 키는 화면에 찍지 않는다."""
    c, s = cfg(), load_state()
    st = ROOT / "state"
    st.mkdir(exist_ok=True)
    (st / "comfy_url.txt").write_text(local("comfy") + "\n", encoding="utf-8")
    print("ComfyUI:", local("comfy"))
    if c.get("llm"):
        (st / "vlm_url.txt").write_text(f"{local('llm')}/v1|{c['llm']['name']}|{s.get('token', '')}\n", encoding="utf-8")
        print(f"LLM: {local('llm')}/v1 ({c['llm']['name']}, 키 {len(s.get('token', ''))}자)")
    (st / "gpu_cloud.json").write_text(json.dumps({"tool": "tools/vast_cloud.py", "profile": PROFILE}, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------- 켜기 버튼(진행 표시) ----------

HIST = ROOT / "state" / f"wake_history_vast_{PROFILE}.json"
DEFAULT_SEC = {"up": 1200.0, "llm": 600.0}


def _expected(step: str) -> float:
    with contextlib.suppress(OSError, ValueError):
        h = json.loads(HIST.read_text(encoding="utf-8")).get(step) or []
        if h:
            return float(statistics.median(h))
    return DEFAULT_SEC[step]


def _remember(step: str, sec: float) -> None:
    h = {}
    with contextlib.suppress(OSError, ValueError):
        h = json.loads(HIST.read_text(encoding="utf-8"))
    h[step] = (h.get(step, []) + [round(sec, 1)])[-5:]
    HIST.parent.mkdir(parents=True, exist_ok=True)
    HIST.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def progress(pct: float, eta: float, stage: str) -> None:
    print(f"##progress {int(max(0, min(100, pct)))} {int(max(0, eta))} {stage}", flush=True)


def wake() -> int:
    """꺼져 있으면 켜고(up) → 스위치(use) → LLM → LLM 이 답할 때까지. 이미 다 떠 있으면 바로 끝난다(사람이 누르는 버튼용:
    에이전트가 과금 GPU 를 스스로 켜지 못하게 하는 원칙은 runpod_wake 와 같다)."""
    e_up, e_llm = _expected("up"), _expected("llm")
    progress(1, e_up + e_llm, "GPU 서버 상태 확인")
    s = load_state()
    inst = get_instance(s["instance_id"]) if s.get("instance_id") else None
    if running(inst):
        ensure_tunnel()
    key = s.get("token", "")
    comfy_ok = running(inst) and http_ok(local("comfy") + "/queue")
    llm_ok = running(inst) and http_ok(local("llm") + "/v1/models", key)
    print(f"인스턴스 {(inst or {}).get('actual_status', '없음')} · ComfyUI {'ok' if comfy_ok else '없음'} · LLM {'ok' if llm_ok else '없음'}", flush=True)
    if comfy_ok and llm_ok:
        write_switch()
        progress(100, 0, "이미 켜져 있음")
        return 0
    total = (0 if comfy_ok else e_up) + e_llm
    if not comfy_ok:
        t, done = time.time(), threading.Event()

        def tick():
            while not done.wait(10):
                frac = min(0.97, (time.time() - t) / e_up)
                progress(100 * e_up * frac / total, max(10, e_up * (1 - frac)) + e_llm, "GPU 서버 켜는 중(빌리기·설치·ComfyUI)")
        threading.Thread(target=tick, daemon=True).start()
        try:
            ensure()
        finally:
            done.set()
        _remember("up", time.time() - t)
    write_switch()
    key = load_state().get("token", "")
    if http_ok(local("llm") + "/v1/models", key):
        progress(100, 0, "준비 완료")
        return 0
    t = time.time()
    jobs = {}
    with contextlib.suppress(Exception):
        jobs = json.loads(agent("GET", "/jobs") or b"{}")
    if not (jobs.get("llm") or {}).get("running"):  # 이미 올리는 중이면 다시 띄우지 않는다(띄우면 처음부터)
        start_llm()
    else:
        print("LLM 은 이미 올라오는 중: 기다린다", flush=True)
    base = total - e_llm
    deadline = time.time() + 40 * 60
    while time.time() < deadline:
        if http_ok(local("llm") + "/v1/models", key):
            _remember("llm", time.time() - t)
            progress(100, 0, "준비 완료")
            return 0
        frac = min(0.97, (time.time() - t) / e_llm)
        progress(100 * (base + e_llm * frac) / total, max(10, e_llm * (1 - frac)), "언어 모델 올리는 중(vLLM, 처음이면 모델 내려받기 포함)")
        time.sleep(10)
    print(f"LLM 이 40분 안에 답하지 않았다: 로그 {local('agent')}/job_llm.log", flush=True)
    return 3


# ---------- 명령 ----------

def main() -> None:
    args = sys.argv[1:]
    if "--profile" in args[:-1]:
        i = args.index("--profile")
        args = args[:i] + args[i + 2:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "offers":
        show_offers()
    elif cmd == "up":
        inst, url = ensure()
        print(url)
        print("다 쓰면: python tools/vast_cloud.py stop")
    elif cmd == "llm":
        start_llm()
    elif cmd == "use":
        write_switch()
    elif cmd == "unuse":
        for f in ("comfy_url.txt", "vlm_url.txt", "gpu_cloud.json"):
            (ROOT / "state" / f).unlink(missing_ok=True)
        print("스위치를 지웠다(도구가 다시 COMFY_URL·사내 게이트웨이를 쓰고, 켜기 버튼은 RunPod 로)")
    elif cmd == "wake":
        sys.exit(wake())
    elif cmd == "tunnel":
        tunnel_loop()
    elif cmd == "stop":
        live = [i for i in our_instances() if running(i)]
        for i in live:
            release(i["id"])
        kill_tunnel()
        if not live:
            print("켜진 인스턴스 없음")
    elif cmd == "down":
        rows = our_instances()
        for i in rows:
            destroy(i["id"])
        kill_tunnel()
        STATE.unlink(missing_ok=True)
        if not rows:
            print("지울 인스턴스 없음")
    elif cmd == "guard":
        c = cfg()
        log = ROOT / "state" / "logs" / "budget_guard.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if c.get("min_balance_usd") is None:
            print("min_balance_usd 설정 없음: 지킬 바닥이 없다(그래서 up 도 거절한다)")
            return
        try:
            b = balance()
        except (RuntimeError, SystemExit) as e:  # 못 읽은 것도 남긴다(조용히 죽으면 지킴이가 도는지 모른다)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M')} vast {e}\n")
            print(e)
            return
        rows = our_instances()
        live = [i for i in rows if running(i)]
        line = (f"{time.strftime('%Y-%m-%d %H:%M')} vast 잔액 ${b['credit']:.2f} 바닥 ${c['min_balance_usd']} 켜진 인스턴스 {len(live)}"
                f" 시간당 ${sum(float(i.get('dph_total') or 0) for i in live):.2f}")
        stopped = [i for i in rows if not running(i)]
        if stopped:
            line += f" · 멈춘 인스턴스 {len(stopped)}개(디스크 보관비)"
        if b["credit"] <= float(c["min_balance_usd"]) and live:
            for i in live:
                stop_instance(i["id"])
            line += " → 바닥이라 멈춤"
        print(line, flush=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    elif cmd == "status":
        s = load_state()
        rows = our_instances()
        for i in rows:
            print(f"{i['id']} {i.get('label')} {i.get('actual_status')} {i.get('gpu_name')} x{i.get('num_gpus')} 시간당 ${float(i.get('dph_total') or 0):.3f}"
                  f" · {i.get('geolocation', '')} · {str(i.get('status_msg') or '')[:80]}")
        if not rows:
            print("인스턴스 없음(보관비도 없음)")
        with contextlib.suppress(Exception):
            b = balance()
            print(f"잔액 ${b['credit']:.2f} {b['fields']} · 바닥 {cfg().get('min_balance_usd')}")
        print(f"터널: {'살아 있음' if pid_alive(s.get('tunnel_pid')) and port_open(cfg()['local_ports']['comfy']) else '없음'}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    main()
