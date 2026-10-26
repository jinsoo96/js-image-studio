"""vast.ai 인스턴스에 ComfyUI(+그림 보는 LLM)를 띄워 쓴다. runpod_cloud.py 와 같은 동사·같은 설치(cloud/runpod_bootstrap.sh·runpod_llm.sh)라
서버만 바꿔 끼운다: 도구(MCP·미사일 스튜디오)는 state/comfy_url.txt·vlm_url.txt 만 따라가고, 여기선 그 둘이 늘 이 PC 의 SSH 터널 주소(127.0.0.1)다.

    python tools/vast_cloud.py offers    설정 search 에 맞는 빈 기계·시간당 값·첫 설치 내려받기 값(계정 없이 공개 목록, 돈 0)
    python tools/vast_cloud.py up        빌려서(멈춰 둔 게 있으면 다시 켜서) 설치하고 ComfyUI 가 GPU 를 잡을 때까지 기다린다. 다 쓰면 stop
    python tools/vast_cloud.py llm       켜 둔 인스턴스에 LLM(설정 llm, vLLM)을 띄운다. GPU 가 둘이면 gpu_split.llm 장에만
    python tools/vast_cloud.py use       모든 도구가 이 서버를 쓰게 스위치 파일을 적는다(unuse 는 지운다)
    python tools/vast_cloud.py hold [분]  켜진 상자를 N분(기본 20) 동안 '바쁨'으로 붙든다(다른 상자가 뜨길 기다리는 동안 놀았다고 지워지지 않게)
    python tools/vast_cloud.py loras [push|pull|status]  학습한 LoRA 를 PC 보관함(models/loras)과 서버 사이에서 맞춘다(켤 때 push 자동, 학습 뒤 pull)
    python tools/vast_cloud.py wake      켜기 버튼(미사일 스튜디오, tools/gpu_wake.py 가 부름): up → use → llm → LLM 이 답할 때까지, '##progress' 줄로 진행
    python tools/vast_cloud.py stop      GPU 과금을 끝낸다(keep_instance 면 멈추고 디스크만 남김, 아니면 지움)
    python tools/vast_cloud.py down      우리 인스턴스(label 이 label_prefix 로 시작)를 전부 지운다(멈춘 것까지, 보관비 0)
    python tools/vast_cloud.py status    우리 인스턴스·시간당 값·잔액·터널
    python tools/vast_cloud.py guard     예산 지킴이: 잔액이 min_balance_usd 이하이면 켜진 우리 인스턴스를 멈춘다

키는 환경변수 VAST_API_KEY(없으면 Windows 사용자 환경). 설정은 VAST_PROFILE 또는 --profile(기본 vast_qwen → cloud/vast_qwen.json),
설치 내용(이미지·ComfyUI·모델·노드·패치·LLM)은 그 설정의 stack_from 이 가리키는 설정에서 가져온다(같은 구성을 두 번 적지 않는다).
설정의 roles 로 상자를 나눈다: ["comfy"] 그림 상자(vast_img), ["llm"] LLM 상자(vast_llm, ComfyUI 없이 vLLM 만), 없으면 한 상자에 둘 다(vast_qwen·vast_48g).
상자마다 state·터널·스위치(자기 역할 것만)가 따로고, 켜기 버튼(tools/gpu_wake.py)은 state/gpu_cloud.json 의 profiles 를 함께 깨운다.
LLM 만 바꿔 끼우기: `llm --model <llm_options 이름>`(되돌리기 default). `guard --all` 은 cloud/vast_*.json 전부를 각자 바닥으로 지킨다.
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
CLOUD = ROOT / "cloud"
CONFIG = CLOUD / f"{PROFILE}.json"
STATE = ROOT / "state" / f"vast_{PROFILE}.json"
BOOTSTRAP = ROOT / "cloud" / "runpod_bootstrap.sh"
LLM_SH = ROOT / "cloud" / "runpod_llm.sh"
RELAY = ROOT / "cloud" / "tunnel_relay.py"
# 인스턴스 안 서비스 포트와, 터널이 실제로 잇는 중계기 포트(+10000). 이유는 cloud/tunnel_relay.py 머리말
REMOTE_PORTS = {"comfy": 8188, "agent": 8189, "llm": 8000}
RELAY_OFFSET = 10000
# 떠 있는지는 포트로 본다(pgrep -f 는 이 명령을 실은 셸 자신에 걸려 늘 '떠 있다'가 된다).
# 뒤로 보낸 묶음 전체의 입출력을 돌린다: 'A || B >log &' 는 A||B 를 도는 서브셸이 SSH 출력을 쥐고 있어 SSH 가 끝나지 않는다(2026-10-26 설치 명령 180초 멈춤)
RELAY_START = (f"{{ (exec 3<>/dev/tcp/127.0.0.1/{REMOTE_PORTS['agent'] + RELAY_OFFSET}) 2>/dev/null || exec nohup setsid python3 /workspace/tunnel_relay.py "
               + ",".join(f"{p + RELAY_OFFSET}:{p}" for p in REMOTE_PORTS.values()) + "; } >/tmp/tunnel_relay.log 2>&1 </dev/null &")
TUNNEL_LOG = ROOT / "state" / "logs" / f"vast_tunnel_{PROFILE}.log"
API = "https://console.vast.ai/api/v0"
UA = {"User-Agent": "webtoon-studio/1.0"}
# stack_from 에서 가져오는 설치 내용. 어디서 빌릴지(search·디스크·GPU 나누기·놀면 멈춤)는 이 설정 것
STACK_KEYS = ("image", "comfyui_ref", "custom_nodes", "extra_models", "extra_files", "patches", "checkpoint", "llm", "llm_options",
              "comfy_start_timeout_min", "download_timeout_min")
# vast 는 SSH 접속에 자기 env(CONTAINER_ID·CONTAINER_API_KEY 등)를 넘기지 않는다: 켜질 때 파일로 남겨 두고 부트스트랩을 띄울 때 읽는다.
# 호스트 키를 빼고 배포하는 이미지(runpod/pytorch 는 자기 start.sh 가 만든다)는 vast 의 sshd 가 'no hostkeys available' 로 바로 꺼져
# 영영 접속이 안 된다(2026-10-25 실측): 키가 없으면 만들고, sshd 가 없으면 띄운다
ONSTART = ("export -p > /root/.vast_env; chmod 600 /root/.vast_env; touch /root/.no_auto_tmux; "
           "ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || ssh-keygen -A; "
           "pgrep -x sshd >/dev/null || { mkdir -p /run/sshd; /usr/sbin/sshd; }")
# 컨테이너 로그에 이런 줄이 있으면 기계가 아니라 이미지·설정 문제라 다른 기계로 다시 빌려도 같다
IMAGE_PROBLEMS = {"no hostkeys available": "이미지에 SSH 호스트 키가 없다(onstart 의 ssh-keygen 이 돌지 않았다)",
                  "connect_to localhost port 22: failed": "컨테이너 안에 SSH 서버가 떠 있지 않다"}


class ImageProblem(RuntimeError):
    """다른 기계로 다시 빌려도 같은 실패(이미지·onstart 문제): 돈만 나가니 바로 멈춘다."""
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def load_cfg(path: Path) -> dict:
    """설정 + stack_from 의 설치 내용. 같은 키가 있으면 이 설정이 이기고, llm 은 항목별로 덮는다(같은 모델, GPU 몫만 다르게)."""
    c = json.loads(path.read_text(encoding="utf-8"))
    if c.get("stack_from"):
        base = json.loads((path.parent / f"{c['stack_from']}.json").read_text(encoding="utf-8"))
        for k in STACK_KEYS:
            if k in base and k not in c:
                c[k] = base[k]
            elif k == "llm" and k in base:
                c[k] = {**base[k], **c[k]}
    return c


def cfg() -> dict:
    return load_cfg(CONFIG)


ROLES = ("comfy", "llm")


def roles_of(c: dict | None = None) -> set:
    """이 설정의 인스턴스가 맡는 역할: comfy(그림, ComfyUI)·llm(글·검수 모델, vLLM). 없으면 둘 다(한 상자에 같이).
    따로 두면(그림 상자 ["comfy"], LLM 상자 ["llm"]) 각자 켜고 끄고, 스위치 파일도 자기 역할 것만 쓴다(2026-10-26 사용자 '인스턴스 두 개')."""
    return set((c if c is not None else cfg()).get("roles") or ROLES)


def profile_roles(name: str) -> set:
    try:
        return roles_of(load_cfg(CLOUD / f"{name}.json"))
    except (OSError, ValueError):
        return set()


def min_cuda(c: dict) -> float:
    """기계 드라이버의 CUDA 하한: 이미지가 요구하는 search.min_cuda 와, LLM 역할이면 vLLM 휠이 요구하는 llm.min_cuda 중 큰 쪽.
    (2026-10-26 CUDA 12.8 드라이버 기계에서 vLLM 0.31 이 'NVIDIA driver too old' 로 죽었다. 그림만 올리는 상자는 이미지 기준으로 충분하다)"""
    need = float(c["search"].get("min_cuda", 0))
    if "llm" in roles_of(c):
        need = max(need, float((c.get("llm") or {}).get("min_cuda", 0)))
    return need


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(**kw) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({**load_state(), **kw}, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state() -> None:
    """인스턴스 기록만 지운다. 못 뜬 기계 목록(bad_machines)·올린 SSH 공개키는 다음 빌리기에도 쓴다."""
    keep = {k: v for k, v in load_state().items() if k in ("bad_machines", "ssh_key_pub")}
    STATE.unlink(missing_ok=True)
    if keep:
        save_state(**keep)


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
    # gpu_name 은 하나 또는 목록(같은 메모리급 카드 여럿 중 가장 싼 기계). 목록이면 이름마다 물어 합친다
    rows = []
    for name in (s["gpu_name"] if isinstance(s["gpu_name"], list) else [s["gpu_name"]]):
        q = {"gpu_name": {"eq": name}, "num_gpus": {"eq": int(s["num_gpus"])}, "rentable": {"eq": True}, "rented": {"eq": False},
             "type": "bid" if s.get("interruptible") else "on-demand", "order": [["dph_total", "asc"]], "limit": 200}
        if s.get("verified", True):
            q["verified"] = {"eq": True}
        url = API + "/bundles/?" + urllib.parse.urlencode({"q": json.dumps(q)})
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            rows += json.loads(r.read()).get("offers", [])
    out = []
    for o in rows:
        if o.get("machine_id") in exclude_machines:
            continue
        if (o.get("cuda_max_good") or 0) < min_cuda(c):
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
        # 끊길 수 있는(interruptible) 판은 최저 입찰가에 bid_margin 을 얹어 산다: 보통 값의 절반쯤, 더 높게 부른 사람이 오면 멈춘다
        gpu = round((o.get("min_bid") or 0) * float(s.get("bid_margin", 1.15)), 4) if s.get("interruptible") else (o.get("dph_base") or 0)
        if s.get("interruptible") and not gpu:
            continue
        hourly = gpu + (o.get("storage_cost") or 0) * float(c["disk_gb"]) / 730
        if hourly > float(s.get("max_usd_per_hr", 1e9)):
            continue
        stopped_month = (o.get("storage_cost") or 0) * float(c["disk_gb"])
        # 멈춰 둘 거면 보관비가 기계마다 월 $20~73(150GB)까지 차이 나서 상한을 넘는 기계는 빼고, 지울 거면 보관비는 따지지 않는다
        if c.get("keep_instance") and stopped_month > float(c.get("max_stopped_usd_per_month", 1e9)):
            continue
        out.append({**o, "_hourly": hourly, "_bid": gpu if s.get("interruptible") else None, "_setup_usd": (o.get("inet_down_cost") or 0) * float(c.get("setup_download_gb", 0)),
                    "_stopped_month": stopped_month})
    # 다 쓰면 지우는 판은 켤 때마다 첫 설치를 다시 내려받는다: 시간당 값만 보면 내려받기값이 큰 기계를 고른다(1.23+0.55 > 1.31+0.16)
    hours = float(c.get("plan_hours", 1.5))
    setup_each_time = not c.get("keep_instance")
    return sorted(out, key=lambda o: o["_hourly"] * hours + (o["_setup_usd"] if setup_each_time else 0))


def show_offers() -> None:
    c = cfg()
    o = offers()
    s = c["search"]
    print(f"조건: {s['gpu_name']} x{s['num_gpus']} · CUDA≥{min_cuda(c):g} · 신뢰도≥{s.get('min_reliability')} · 내려받기≥{s.get('min_inet_down_mbps')}Mbps"
          f" · 디스크 {c['disk_gb']}GB · 시간당≤${s.get('max_usd_per_hr')} · 뺄 나라 {s.get('avoid_countries')}"
          f" · 다 쓰면 {'멈춤(보관비 월≤$' + str(c.get('max_stopped_usd_per_month')) + ')' if c.get('keep_instance') else '지움(보관비 0)'}")
    if not o:
        print("맞는 빈 기계 없음(조건을 넓히려면 설정 search)")
        return
    print(f"맞는 기계 {len(o)}대, 시간당 중간 ${statistics.median(x['_hourly'] for x in o):.2f}")
    bad = bad_machines()
    for x in o[:10]:
        print(f"  offer {x['id']} · 기계 {x.get('machine_id')}{f' (최근 {BAD_MACHINE_HOURS}시간 안에 못 떠서 건너뜀)' if x.get('machine_id') in bad else ''}"
              f" · {x.get('geolocation')} · 시간당 ${x['_hourly']:.2f} · 첫 설치 내려받기 약 ${x['_setup_usd']:.2f}"
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
    # 목록은 v1(끝 슬래시 필수, 없으면 301). v0 /instances/ 목록은 2026-10-25 실측 410 deprecated_endpoint. 하나 조회(v0 /instances/{id}/)는 아직 된다
    req = urllib.request.Request("https://console.vast.ai/api/v1/instances/", headers={**UA, "Authorization": f"Bearer {api_key()}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        r = json.loads(resp.read())
    return (r.get("instances") or []) if isinstance(r, dict) else []


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


def container_log(iid, tail: int = 120) -> str:
    """컨테이너 출력 끝부분(vast 가 잠깐 뒤 받을 수 있는 주소를 준다). 못 받으면 빈 글."""
    with contextlib.suppress(Exception):
        r = api("PUT", f"/instances/request_logs/{iid}/", {"tail": str(tail)})
        url = r.get("temp_download_url") or r.get("result_url")
        for _ in range(8):
            time.sleep(3)
            with contextlib.suppress(Exception), urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
    return ""


def destroy(iid) -> None:
    try:
        api("DELETE", f"/instances/{iid}/")
        print(f"인스턴스 삭제: {iid}", flush=True)
    except RuntimeError as e:
        print(f"인스턴스 삭제 실패(직접 확인 필요: status): {iid} {e}", flush=True)
        return
    if load_state().get("instance_id") == iid:
        kill_tunnel()
        clear_state()
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


BAD_MACHINE_HOURS = 6  # 못 뜬 기계는 이만큼 다시 고르지 않는다(가장 싼 기계라 다음 up 이 같은 기계를 또 집었다, 2026-10-25)


def bad_machines() -> set:
    now = time.time()
    return {int(m) for m, t in (load_state().get("bad_machines") or {}).items() if now - t < BAD_MACHINE_HOURS * 3600}


def mark_bad(machine_id) -> None:
    if machine_id is None:
        return
    rows = {m: t for m, t in (load_state().get("bad_machines") or {}).items() if time.time() - t < BAD_MACHINE_HOURS * 3600}
    save_state(bad_machines={**rows, str(machine_id): time.time()})


def create_instance(exclude=()) -> int:
    c = cfg()
    for o in offers(set(exclude) | bad_machines())[:5]:
        body = {"client_id": "me", "image": c["image"], "disk": c["disk_gb"], "label": f"{c['label_prefix']}-{time.strftime('%m%d-%H%M%S')}",
                "runtype": "ssh", "onstart": ONSTART, "cancel_unavail": True}
        if o.get("_bid"):
            body["price"] = o["_bid"]  # 입찰가가 있으면 끊길 수 있는 인스턴스(vast CLI 의 --bid_price 와 같은 필드)
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


def ssh_exe() -> str:
    """Windows 는 기본 OpenSSH. Git 에 딸린 MSYS ssh 는 긴 명령줄 인자를 바꿔 ssh_put 이 깨진다(2026-10-25 실측: 같은 200KB 가
    MSYS ssh 로는 'not in gzip format', Windows OpenSSH 로는 sha256 일치). Git bash 에서 띄우면 PATH 에 MSYS ssh 가 먼저 잡힌다."""
    if os.name == "nt":
        win = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if win.exists():
            return str(win)
    return shutil.which("ssh") or "ssh"


def ssh_base(inst, extra=()) -> list:
    host, port = ssh_target(inst)
    kh = ROOT / "state" / "ssh" / f"vast_{inst['id']}.known_hosts"
    kh.parent.mkdir(parents=True, exist_ok=True)
    return [ssh_exe(), "-T", "-i", str(key_path()), "-p", str(port), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
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
    if r.returncode:  # 올린 글이 깨진 것은 이 PC 쪽(ssh 클라이언트) 문제라 다른 기계로 가도 같다
        raise ImageProblem(f"올린 것 풀기 실패(이 PC 의 ssh {ssh_exe()}): {r.stderr.decode('utf-8', 'replace')[-300:]}")


def wait_running(iid, timeout_min: float) -> dict:
    """vast 가 running 이라 하고 SSH 가 받을 때까지. exited·offline·unknown 은 영영 안 뜨는 것(문서)이라 바로 실패."""
    t0, last, up_at, looked, stuck = time.time(), "", None, False, None
    # 이미지 받기가 같은 줄에서 멈춘 기계는 끝내 안 뜬다(2026-10-26 5090 기계 셋이 마지막 큰 층 뒤 17~20분 그대로, 뜬 기계는 그 자리 4.5분)
    stall_min, changed = float(cfg().get("loading_stall_min", 10)), time.time()
    while time.time() - t0 < timeout_min * 60:
        inst = get_instance(iid)
        if not inst:
            raise RuntimeError(f"인스턴스가 없어졌다: {iid}")
        st, msg = inst.get("actual_status"), str(inst.get("status_msg") or "")[:160]
        if f"{st} {msg}" != last:
            print(f"  [{(time.time() - t0) / 60:4.1f}분] {st} {msg}", flush=True)
            last, changed = f"{st} {msg}", time.time()
        if st == "loading" and time.time() - changed > stall_min * 60:
            raise RuntimeError(f"이미지 받기가 {stall_min:g}분째 그대로: {msg}")
        if st in ("exited", "offline", "unknown") and time.time() - t0 > 60:
            raise RuntimeError(f"인스턴스가 {st}: {msg}")
        # 우리가 멈추지 않았는데 vast 쪽 의도가 stopped 로 바뀌고 컨테이너가 안 켜지면(created 에서 15분 멈춤, 2026-10-25) 그 기계는 안 뜬다
        if inst.get("intended_status") == "stopped" and st != "running":
            stuck = stuck or time.time()
            if time.time() - stuck > 120:
                raise RuntimeError(f"기계가 컨테이너를 켜지 않는다(intended stopped, {st}): {msg}")
        else:
            stuck = None
        if st == "running" and inst.get("ssh_host"):
            up_at = up_at or time.time()
            with contextlib.suppress(Exception):
                if ssh_run(inst, "true", timeout=40).returncode == 0:
                    return inst
            # running 인데 3분 넘게 SSH 가 안 받으면 컨테이너 로그를 한 번 본다(sshd 가 죽었으면 기다려도 안 뜬다)
            if not looked and time.time() - up_at > 180:
                looked = True
                text = container_log(iid)
                for pat, why in IMAGE_PROBLEMS.items():
                    if pat in text:
                        raise ImageProblem(f"{why}. 컨테이너 로그 끝: {text[-600:]}")
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
        "MODE": "comfy" if "comfy" in roles_of(c) else "agent",  # agent: ComfyUI·그림 모델 없이 에이전트·watchdog 만(LLM 전용 상자)
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
        for name, data in (("workspace/bootstrap.sh", BOOTSTRAP.read_bytes().replace(b"\r\n", b"\n")), ("workspace/pod.env", env_txt.encode("utf-8")),
                           ("workspace/tunnel_relay.py", RELAY.read_bytes().replace(b"\r\n", b"\n"))):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


BUNDLE = "/tmp/.webtoon_bundle.tar"


def launch_cmd(probe: str) -> str:
    """묶음을 풀고 중계기·부트스트랩을 뒤로 띄운다. probe(이미 떠 있는지 볼 주소)가 답하면 다시 띄우지 않는다."""
    return (f"mkdir -p /workspace && tar -xf {BUNDLE} -C / && rm -f {BUNDLE} && {{ {RELAY_START} }} && "
            "{ [ -s /root/.vast_env ] || echo NO_VAST_ENV; } && "
            f"if curl -s -m 5 {probe} >/dev/null 2>&1; then echo ALREADY; else "
            "nohup setsid bash -c 'set -a; [ -f /root/.vast_env ] && . /root/.vast_env; . /workspace/pod.env; set +a; exec bash /workspace/bootstrap.sh' "
            ">/dev/null 2>&1 </dev/null & echo LAUNCHED; fi")


# 떠 있는지 볼 곳: 그림 상자는 ComfyUI, LLM 전용 상자(MODE=agent)는 에이전트(토큰 없어도 403 으로 답하니 curl 은 성공)
PROBES = {"comfy": f"localhost:{REMOTE_PORTS['comfy']}/system_stats", "agent": f"localhost:{REMOTE_PORTS['agent']}/jobs"}
LAUNCH_CMD = launch_cmd(PROBES["comfy"])


def launch(inst, c) -> None:
    """부트스트랩을 올려 뒤에서 돌린다. ComfyUI(LLM 상자는 에이전트)가 이미 떠 있으면(같은 인스턴스를 다시 부른 때) 건너뛴다."""
    ssh_put(inst, launch_bundle(c, load_state()["token"]), BUNDLE)
    r = ssh_run(inst, launch_cmd(PROBES["comfy" if "comfy" in roles_of(c) else "agent"]), timeout=180)
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
    s = load_state()
    # 살아 있어도 다른 인스턴스용이면 끈다: 지워진 인스턴스의 중계 SSH 가 끊기지 않고 붙어 있어 새 인스턴스로 못 옮겨 갔다
    # (2026-10-25: 새 인스턴스의 ComfyUI 는 2분 만에 떴는데 터널이 옛 것을 가리켜 '안 뜬다'로 보였고, 아무도 안 쓰니 watchdog 이 지웠다)
    if pid_alive(s.get("tunnel_pid")) and s.get("tunnel_for") == s.get("instance_id"):
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
    save_state(tunnel_pid=proc.pid, tunnel_for=s.get("instance_id"))
    names = {"comfy": "ComfyUI", "agent": "설치 로그", "llm": "LLM"}
    for _ in range(30):
        if port_open(p["agent"]):  # 에이전트 포트는 어느 역할에도 있다
            print("  터널: " + " · ".join(f"127.0.0.1:{v}({names.get(k, k)})" for k, v in p.items()), flush=True)
            return
        time.sleep(1)
    print(f"  터널이 아직 안 열렸다(로그 {TUNNEL_LOG})", flush=True)


def tunnel_loop() -> None:
    """터널을 붙들고, 끊기면 다시 잇는다. state 에서 인스턴스가 빠지거나 멈추면 끝난다(stop·down·destroy 가 지움).
    붙어 있는 동안에도 15초마다 state 를 본다: 인스턴스가 바뀌거나 지워졌는데 중계 SSH 가 안 끊기면 직접 끊고 새 인스턴스로 다시 잇는다."""
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
        # 터널은 늘 떠 있는 중계기 포트로만 잇는다(안 뜬 포트로 한 번 가면 터널 전체가 멈춘다, cloud/tunnel_relay.py).
        # 다시 이을 때마다 중계기가 살아 있는지 보고 없으면 올려서 띄운다(인스턴스를 다시 켰거나 launch 전에 터널을 연 때)
        try:
            if ssh_run(inst, "test -f /workspace/tunnel_relay.py", timeout=60).returncode != 0:
                ssh_put(inst, RELAY.read_bytes().replace(b"\r\n", b"\n"), "/workspace/tunnel_relay.py")
            ssh_run(inst, RELAY_START, timeout=60)
        except Exception as e:  # noqa: BLE001  잇기는 해 본다(중계기가 이미 있을 수 있다)
            print(f"{time.strftime('%H:%M:%S')} 중계기 확인 실패: {str(e)[:160]}", flush=True)
        fwd = [a for name, lp in p.items() for a in ("-L", f"127.0.0.1:{lp}:127.0.0.1:{REMOTE_PORTS[name] + RELAY_OFFSET}")]  # 이 역할의 포트만
        t0, iid = time.time(), inst["id"]
        print(f"{time.strftime('%H:%M:%S')} 터널 연결 {iid} {ssh_target(inst)}", flush=True)
        # 출력은 이 프로세스의 출력(=터널 로그 파일)으로. 파이프면 Windows OpenSSH 가 멈춘다(ssh_run 참고).
        # -N(세션 없이 포워딩만)은 vast 중계(sshN.vast.ai)가 컨테이너로 넘기지 않아 포워딩이 중계 쪽에서 거절된다(2026-10-25 실측:
        # -N 은 연결 거부, 'sleep' 세션을 열면 5초 만에 ComfyUI 200). 그래서 끝나지 않는 세션 명령을 붙인다
        proc = subprocess.Popen(ssh_base(inst, ["-o", "ExitOnForwardFailure=yes", *fwd]) + ["sleep infinity"], stdin=subprocess.DEVNULL,
                                stdout=sys.stdout, stderr=sys.stdout, creationflags=NO_WINDOW)
        while proc.poll() is None:
            time.sleep(15)
            cur = load_state()
            if cur.get("instance_id") != iid or cur.get("stopped_at"):
                print(f"{time.strftime('%H:%M:%S')} 인스턴스가 바뀌었거나 멈춤: 옛 연결을 끊는다", flush=True)
                proc.kill()
                break
        proc.wait()
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


AGENT_READY = "[boot] agent ready"  # 부트스트랩이 agent 모드 끝에 찍는 줄


def comfy_stats(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/system_stats", headers=UA), timeout=10) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def wait_ready(timeout_min: float) -> str:
    """그림 상자: ComfyUI 가 응답하고 GPU(cuda)를 잡을 때까지. LLM 전용 상자(agent 모드): 부트스트랩이 '[boot] agent ready' 를 찍을 때까지.
    설치가 멈추면(로그가 15분 그대로·체크포인트 뒤 comfy_start_timeout_min) 실패."""
    c = cfg()
    want_comfy = "comfy" in roles_of(c)
    url = local("comfy" if want_comfy else "agent")
    t0 = changed = time.time()
    seen, lines, ckpt_at = 0, [], None
    while time.time() - t0 < timeout_min * 60:
        stats = comfy_stats(url) if want_comfy else None
        if stats is not None:
            devices = stats.get("devices", [])
            if not any(d.get("type") == "cuda" for d in devices):
                raise RuntimeError(f"ComfyUI 는 떴는데 GPU 가 없다: {[d.get('name') for d in devices]}")
            print(f"ComfyUI 준비됨: {url} ({devices[0].get('name')}, {(time.time() - t0) / 60:.1f}분)", flush=True)
            return url
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
            if not want_comfy and line.startswith(AGENT_READY):
                print(f"에이전트 준비됨(LLM 전용 상자): {url} ({(time.time() - t0) / 60:.1f}분)", flush=True)
                return url
        seen = len(lines)
        if not ckpt_at and time.time() - changed > 15 * 60:
            print(f"  설치가 {(time.time() - changed) / 60:.0f}분째 진행이 없다", flush=True)
            break
        if want_comfy and ckpt_at and time.time() - ckpt_at > c["comfy_start_timeout_min"] * 60:
            break
        time.sleep(15)
    for line in (boot_log() or lines)[-40:]:
        print("  " + line[:300], flush=True)
    raise TimeoutError(f"{'ComfyUI' if want_comfy else '에이전트'} 가 뜨지 않았다({(time.time() - t0) / 60:.0f}분)")


def agent_ready() -> bool:
    return any(line.startswith(AGENT_READY) for line in boot_log() or [])


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
            except ImageProblem:
                destroy(iid)
                raise
            except Exception as e:  # noqa: BLE001
                print(f"인스턴스가 뜨지 않아 지우고 다른 기계로: {str(e)[:200]}", flush=True)
                tried.add(load_state().get("machine_id"))
                mark_bad(load_state().get("machine_id"))
                destroy(iid)
                inst = None
                continue
        try:
            launch(inst, c)
            ensure_tunnel()
            url = wait_ready(c["ready_timeout_min"])
            save_state(stopped_at=None)
            if "comfy" in roles_of(c):
                loras_after_ready()
            return inst, url
        except ImageProblem:
            destroy(inst["id"])
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                destroy(inst["id"])
                raise
            print(f"준비되지 않아 지우고 다른 기계로: {str(e)[:200]}", flush=True)
            tried.add(load_state().get("machine_id"))
            mark_bad(load_state().get("machine_id"))
            destroy(inst["id"])
            inst = None
    raise RuntimeError("세 번 빌려도 준비되지 않았다")


# ---------- 학습한 LoRA 동기화(서버를 바꿔도 그림체가 따라가게) ----------

LORA_STORE = ROOT / "models" / "loras"  # 이 PC 의 LoRA 보관함(ComfyUI models/loras 와 같은 상대 경로, 깃 밖)
# 서버에서 LoRA 가 있을 수 있는 자리(ComfyUI 폴더 기준): 올릴 땐 첫째, 내려받을 땐 차례로 찾는다. 학습 노드(SaveLoRA)는 output/loras/ 에 저장하고
# ComfyUI 는 둘 다 LoRA 목록에 올린다(2026-10-26 무대 뒤 500스텝 파일이 output/loras/webtoon/ 에 있어 models/loras/ 만 보던 내려받기가 못 찾을 뻔)
LORA_SERVER_DIRS = ("models/loras", "output/loras")


def lora_files() -> set:
    """어느 작품이든 series/*/lora/loras.json 에 적힌 LoRA 파일(models/loras 기준 상대 경로). 켜짐·꺼짐 무관(꺼진 것도 보관·복원한다).
    학습한 LoRA 가 서버 디스크에만 남아 서버를 옮기며 사라졌다(2026-10-26: 무대 뒤·D+18 가중치가 PC 에 없고 옛 서버 디스크에만)."""
    out = set()
    for f in (ROOT / "series").glob("*/lora/loras.json"):
        with contextlib.suppress(OSError, ValueError):
            for v in json.loads(f.read_text(encoding="utf-8")).values():
                if isinstance(v, dict) and v.get("file"):
                    out.add(str(v["file"]).replace("\\", "/"))
    return out


def server_loras() -> set:
    with urllib.request.urlopen(urllib.request.Request(local("comfy") + "/models/loras", headers=UA), timeout=20) as r:
        return {str(x).replace("\\", "/") for x in json.loads(r.read())}


def comfy_dir() -> str:
    """서버의 ComfyUI 폴더(에이전트 /info, 설치가 적어 둔 것). 못 들으면 runpod/pytorch 이미지의 기본 자리."""
    with contextlib.suppress(Exception):
        d = json.loads(agent("GET", "/info") or b"{}").get("comfy_dir")
        if d:
            return d
    return "/workspace/ComfyUI"


def loras_sync(direction: str = "status") -> dict:
    """push: PC 보관함에 있고 서버에 없는 LoRA 를 올린다(켤 때마다). pull: 서버에 있고 PC 에 없는 것을 내려받는다(학습 뒤).
    status: 어디에 있는지만 센다. 대상은 lora_files()(작품 무관). 올린 크기가 다르면 다시 올린다(깨진 LoRA 로 104컷이 회색으로 나온 일, runpod_cloud)."""
    want, have = lora_files(), server_loras()
    cdir = comfy_dir().rstrip("/")
    bases = [f"{cdir}/{d}/" for d in LORA_SERVER_DIRS]
    res = {"pushed": [], "pulled": [], "both": [], "server_only": [], "local_only": [], "missing_everywhere": []}
    for f in sorted(want):
        local_f = LORA_STORE / f
        on_server, on_pc = f in have, local_f.exists()
        if direction == "push" and on_pc and not on_server:
            data = local_f.read_bytes()
            for _ in range(3):
                got = json.loads(agent("POST", f"/upload?path={urllib.parse.quote(bases[0] + f)}", data, timeout=900)).get("bytes")
                if got == len(data):
                    break
                print(f"  LoRA 올린 크기 불일치({got} != {len(data)}), 다시 올림", flush=True)
            else:
                raise RuntimeError(f"LoRA 올리기 실패: {f}")
            res["pushed"].append(f)
            print(f"  LoRA 올림: {f} ({len(data) / 2**20:.0f}MB)", flush=True)
        elif direction == "pull" and on_server and not on_pc:
            data = None
            for b in bases:  # 목록엔 있지만 어느 폴더인지는 모른다: 차례로
                with contextlib.suppress(urllib.error.URLError, OSError, ValueError):
                    data = agent("GET", f"/download?path={urllib.parse.quote(b + f)}", timeout=900)
                    if data:
                        break
            if not data:
                raise RuntimeError(f"서버 목록엔 있는데 파일을 못 찾음: {f} ({', '.join(LORA_SERVER_DIRS)} 아래 없음)")
            local_f.parent.mkdir(parents=True, exist_ok=True)
            local_f.write_bytes(data)
            res["pulled"].append(f)
            print(f"  LoRA 내려받음: {f} ({len(data) / 2**20:.0f}MB) → {local_f.relative_to(ROOT)}", flush=True)
        elif on_server and on_pc:
            res["both"].append(f)
        elif on_server:
            res["server_only"].append(f)
        elif on_pc:
            res["local_only"].append(f)
        else:
            res["missing_everywhere"].append(f)
    return res


def loras_after_ready() -> None:
    """ComfyUI 가 뜬 뒤(up·wake): PC 보관함의 LoRA 를 올리고, 어디에도 없는(다시 학습해야 하는) 것은 알린다. 실패해도 켜기는 막지 않는다."""
    try:
        r = loras_sync("push")
    except Exception as e:  # noqa: BLE001
        print(f"  LoRA 맞추기 실패(그림은 그릴 수 있다, 그림체 LoRA 없이): {str(e)[:160]}", flush=True)
        return
    if r["pushed"]:
        print(f"  LoRA {len(r['pushed'])}개 올림", flush=True)
    if r["missing_everywhere"]:
        print(f"  주의: 어디에도 없는 LoRA {len(r['missing_everywhere'])}개(다시 학습해야 쓴다): {', '.join(r['missing_everywhere'][:6])}", flush=True)


def llm_spec(c: dict | None = None) -> dict:
    """지금 띄울 LLM: 설정 llm 위에 llm_options[고른 이름](state llm_choice, `llm --model`)을 덮는다. down 하면 고른 것도 지워져 기본으로 돌아간다."""
    c = c or cfg()
    pick = load_state().get("llm_choice")
    opt = (c.get("llm_options") or {}).get(pick) if pick else None
    return {**c["llm"], **{k: v for k, v in opt.items() if not k.startswith("_")}} if opt else c["llm"]


def start_llm() -> None:
    c = cfg()
    L = llm_spec(c)
    env = {"LLM_REPO": L["repo"], "LLM_NAME": L["name"], "VLLM_VERSION": L["vllm_version"], "GPU_UTIL": str(L["gpu_util"]),
           "MAX_LEN": str(L["max_len"]), "LLM_ARGS": L.get("args", ""),
           "SCRIPT_B64": base64.b64encode(LLM_SH.read_bytes().replace(b"\r\n", b"\n")).decode("ascii")}
    dev = (c.get("gpu_split") or {}).get("llm")
    alone = "comfy" not in roles_of(c)
    if dev in (None, "") and alone:
        dev = "0"  # LLM 전용 상자: ComfyUI 와 나누지 않으니 llm.sh 의 큐 기다리기·모델 내리기를 건너뛴다
    if dev not in (None, ""):
        env["LLM_CUDA_DEVICES"] = str(dev)
    body = json.dumps({"name": "llm", "cmd": "echo $SCRIPT_B64 | base64 -d > /workspace/llm.sh && bash /workspace/llm.sh", "env": env}).encode()
    print(json.loads(agent("POST", "/run", body)))
    where = "전용 상자" if alone else ("그림과 같은 장" if dev in (None, "") else dev)
    print(f"로그: {local('agent')}/job_llm.log · 주소: {local('llm')}/v1 (모델 {L['name']}, GPU {where})")


def write_switch() -> None:
    """comfy_client(state/comfy_url.txt)·judge_ep·미사일 스튜디오(state/vlm_url.txt '주소|모델|키')·켜기 버튼(state/gpu_cloud.json)이 읽는 스위치.
    주소는 늘 터널(127.0.0.1)이라 인스턴스를 새로 빌려도 바뀌지 않는다. 키는 화면에 찍지 않는다."""
    c, s = cfg(), load_state()
    roles = roles_of(c)
    st = ROOT / "state"
    st.mkdir(exist_ok=True)
    if "comfy" in roles:
        (st / "comfy_url.txt").write_text(local("comfy") + "\n", encoding="utf-8")
        print("ComfyUI:", local("comfy"))
    if "llm" in roles and c.get("llm"):
        name = llm_spec(c)["name"]
        (st / "vlm_url.txt").write_text(f"{local('llm')}/v1|{name}|{s.get('token', '')}\n", encoding="utf-8")
        print(f"LLM: {local('llm')}/v1 ({name}, 키 {len(s.get('token', ''))}자)")
    (st / "gpu_cloud.json").write_text(json.dumps({"tool": "tools/vast_cloud.py", "profiles": wake_profiles(add=PROFILE)}, ensure_ascii=False) + "\n",
                                       encoding="utf-8")


def wake_profiles(add: str = "", drop: str = "") -> list:
    """켜기 버튼(state/gpu_cloud.json)이 함께 깨울 vast 설정 목록. 역할이 겹치는 설정은 새것이 밀어낸다(한 상자 ↔ 두 상자를 오가도
    같은 역할이 둘 켜지지 않게). 없어진 설정은 빼고, 다른 도구(RunPod)의 것이었으면 비우고 시작한다."""
    cur = {}
    with contextlib.suppress(OSError, ValueError):
        cur = json.loads((ROOT / "state" / "gpu_cloud.json").read_text(encoding="utf-8"))
    have = [] if Path(cur.get("tool", "")).name != "vast_cloud.py" else (cur.get("profiles") or ([cur["profile"]] if cur.get("profile") else []))
    mine = profile_roles(add) if add else set()
    out = [p for p in have if p not in (add, drop) and profile_roles(p) and not (mine & profile_roles(p))]
    return out + ([add] if add else [])


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
    roles = roles_of()
    s = load_state()
    inst = get_instance(s["instance_id"]) if s.get("instance_id") else None
    if running(inst):
        ensure_tunnel()
    key = s.get("token", "")
    # 역할에 없는 것은 '됐다'로 친다(그림 상자는 LLM 을, LLM 상자는 ComfyUI 를 보지 않는다). LLM 상자가 떴는지는 부트스트랩 로그로 본다
    box_ok = running(inst) and (http_ok(local("comfy") + "/queue") if "comfy" in roles else agent_ready())
    llm_ok = "llm" not in roles or (running(inst) and http_ok(local("llm") + "/v1/models", key))
    box_name = "ComfyUI" if "comfy" in roles else "에이전트"
    print(f"[{PROFILE}] 인스턴스 {(inst or {}).get('actual_status', '없음')} · {box_name} {'ok' if box_ok else '없음'}"
          + (f" · LLM {'ok' if llm_ok else '없음'}" if "llm" in roles else ""), flush=True)
    e_llm = e_llm if "llm" in roles else 0
    if box_ok and llm_ok:
        write_switch()
        progress(100, 0, "이미 켜져 있음")
        return 0
    total = (0 if box_ok else e_up) + e_llm
    if not box_ok:
        t, done = time.time(), threading.Event()

        def tick():
            while not done.wait(10):
                frac = min(0.97, (time.time() - t) / e_up)
                progress(100 * e_up * frac / total, max(10, e_up * (1 - frac)) + e_llm, f"GPU 서버 켜는 중(빌리기·설치·{box_name})")
        threading.Thread(target=tick, daemon=True).start()
        try:
            ensure()
        finally:
            done.set()
        _remember("up", time.time() - t)
    write_switch()
    if "llm" not in roles:
        progress(100, 0, "준비 완료")
        return 0
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
    iid = load_state().get("instance_id")
    while time.time() < deadline:
        if http_ok(local("llm") + "/v1/models", key):
            _remember("llm", time.time() - t)
            progress(100, 0, "준비 완료")
            return 0
        if load_state().get("instance_id") != iid:  # 기다리는 사이 상자가 지워졌거나 바뀌었다(down·guard·watchdog): 없는 LLM 을 40분 기다리지 않는다
            print("인스턴스가 지워졌거나 바뀌어 LLM 기다리기를 멈춘다", flush=True)
            return 4
        with contextlib.suppress(Exception):  # vLLM 작업이 죽었으면(드라이버·메모리) 바로 로그 끝을 보이고 끝낸다(2026-10-26 'driver too old' 를 40분 기다릴 뻔)
            jobs = json.loads(agent("GET", "/jobs") or b"{}")
            if jobs.get("llm") and not jobs["llm"].get("running") and time.time() - t > 30:
                tail = agent("GET", "/job_llm.log").decode("utf-8", "replace").splitlines()
                for line in [ln for ln in tail if "Error" in ln or "error" in ln][-3:] + tail[-3:]:
                    print("  " + line[:300], flush=True)
                print(f"LLM 작업이 끝나 버렸다(vLLM 이 죽음): 로그 {local('agent')}/job_llm.log", flush=True)
                return 3
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
        if "--model" in args[:-1]:  # 같은 서버에서 LLM 만 바꾼다(설정 llm_options 의 이름, default 면 기본 llm)
            pick = args[args.index("--model") + 1]
            opts = {k for k in (cfg().get("llm_options") or {}) if not k.startswith("_")}
            if pick != "default" and pick not in opts:
                sys.exit(f"llm_options 에 없는 이름: {pick} (있는 것: {', '.join(sorted(opts)) or '없음'}, 되돌리기는 default)")
            save_state(llm_choice=None if pick == "default" else pick)
        start_llm()
        write_switch()  # 스위치 파일의 모델 이름을 바꾼다(미사일 스튜디오·검수가 이 이름으로 부른다)
    elif cmd == "loras":
        # 학습한 LoRA 맞추기: push(PC → 서버, 켤 때 자동) · pull(서버 → PC, 학습 뒤) · status. 대상은 series/*/lora/loras.json 에 적힌 파일 전부
        direction = args[1] if len(args) > 1 else "status"
        if direction not in ("push", "pull", "status"):
            sys.exit("loras push|pull|status")
        try:
            r = loras_sync(direction)
        except (urllib.error.URLError, OSError, ValueError) as e:
            sys.exit(f"[{PROFILE}] 서버에 닿지 않는다(켜져 있고 터널이 살아 있나? status 로 확인): {str(e)[:120]}")
        print(json.dumps({k: v for k, v in r.items() if v}, ensure_ascii=False, indent=1) or "{}")
        if r["missing_everywhere"]:
            print(f"어디에도 없는 LoRA {len(r['missing_everywhere'])}개: 다시 학습해야 쓴다(loras.json 에서 on=false 로 두거나)")
    elif cmd == "hold":
        # 다른 상자(LLM)가 뜨길 기다리는 동안 이 상자가 '놀았다'고 스스로 지우지 않게 에이전트에 N분짜리 작업을 건다(watchdog 은 돌고 있는 작업을 바쁨으로 센다)
        minutes = int(args[1]) if len(args) > 1 else 20
        print(json.loads(agent("POST", "/run", json.dumps({"name": "hold", "cmd": f"sleep {minutes * 60}"}).encode())), f"→ {minutes}분 붙들기")
    elif cmd == "use":
        write_switch()
    elif cmd == "unuse":
        roles = roles_of()
        for role, f in (("comfy", "comfy_url.txt"), ("llm", "vlm_url.txt")):
            if role in roles:
                (ROOT / "state" / f).unlink(missing_ok=True)
        left, gc = wake_profiles(drop=PROFILE), ROOT / "state" / "gpu_cloud.json"
        if left:
            gc.write_text(json.dumps({"tool": "tools/vast_cloud.py", "profiles": left}, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            gc.unlink(missing_ok=True)
        print(f"[{PROFILE}] 스위치를 지웠다({'·'.join(sorted(roles))}: 도구가 다시 COMFY_URL·사내 게이트웨이를 쓴다). 켜기 버튼: {left or 'RunPod'}")
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
        clear_state()
        if not rows:
            print("지울 인스턴스 없음")
    elif cmd == "guard" and "--all" in args:
        # 예약 지킴이가 설정 하나만 보면 다른 설정(이름표)으로 빌린 인스턴스를 놓친다: vast 설정 전부를 각자의 바닥으로 지킨다
        for p in sorted(CLOUD.glob("vast_*.json")):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--profile", p.stem, "guard"], check=False)
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
        # 받는 중(loading)·만든 직후(created)도 켜는 중이라 켜진 쪽으로 센다(멈춘 것으로 세면 바닥에서 못 막는다)
        live = [i for i in rows if running(i) or i.get("intended_status") == "running"]
        line = (f"{time.strftime('%Y-%m-%d %H:%M')} vast 잔액 ${b['credit']:.2f} 바닥 ${c['min_balance_usd']} 켜진 인스턴스 {len(live)}"
                f" 시간당 ${sum(float(i.get('dph_total') or 0) for i in live):.2f}")
        stopped = [i for i in rows if i not in live]
        if stopped:
            line += f" · 멈춘 인스턴스 {len(stopped)}개(디스크 보관비)"
        if b["credit"] <= float(c["min_balance_usd"]) and (live or (stopped and not c.get("keep_instance"))):
            for i in live + (stopped if not c.get("keep_instance") else []):
                destroy(i["id"]) if not c.get("keep_instance") else stop_instance(i["id"])  # 다 쓰면 지우는 설정이면 보관비도 안 남게 지운다
            line += " → 바닥이라 " + ("지움" if not c.get("keep_instance") else "멈춤")
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
        print(f"터널: {'살아 있음' if pid_alive(s.get('tunnel_pid')) and port_open(cfg()['local_ports']['agent']) else '없음'}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    main()
