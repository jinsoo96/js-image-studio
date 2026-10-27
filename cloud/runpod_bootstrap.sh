#!/bin/bash
# js-image-studio: GPU 서버(vast·RunPod) 안에서 ComfyUI 를 띄운다.
# tools/runpod_cloud.py 가 파드를 만들 때 이 파일을 base64(env BOOTSTRAP_B64)로 넘기고 dockerStartCmd 에서 풀어 실행한다.
#
# 두 가지 이미지를 받는다.
# - ComfyUI 가 깔린 이미지(runpod/worker-comfyui, /comfyui + /opt/venv): 설치 없이 바로 띄운다(기본, 2026-10-07 부터).
#   PyPI·GitHub 가 느린 날 pip 설치가 23~30분 걸리거나 멈춰서(2026-10-07) 설치 단계를 없앴다.
# - 그냥 pytorch 이미지: /workspace 에 ComfyUI(COMFY_REF)를 받고 venv 에 의존성을 깐다(예전 방식).
# 체크포인트는 HuggingFace 에서 여러 연결로 나눠 받는다(파이썬 표준 라이브러리만).
# 넘겨받는 env: CKPT_URL, CKPT_NAME, IDLE_MIN(분), MAX_HOURS(시간), ON_IDLE(stop|terminate), COMFY_REF, CUSTOM_NODES, EXTRA_MODELS,
# COMFY_ARGS(서버마다 ComfyUI 에 더 붙일 인자).
# RunPod 가 RUNPOD_POD_ID·RUNPOD_API_KEY 를 넣어 준다. vast.ai 는 tools/vast_cloud.py 가 이 파일을 SSH 로 올려 돌리고,
# vast 가 넣는 CONTAINER_ID·CONTAINER_API_KEY(인스턴스 전용 키)로 스스로 멈춘다.
set -u
mkdir -p /workspace/logs && cd /workspace
exec > >(tee -a /workspace/logs/bootstrap.log) 2>&1
echo "[boot] $(date -u) pod=${RUNPOD_POD_ID:-?} on_idle=${ON_IDLE:-terminate}"
SYSPY=$(command -v python3 || command -v python)
# 파드 에이전트(8189): 로그 폴더는 누구나 읽고(설치 진행 표시), 토큰(AGENT_TOKEN)이 있으면 파일 올리기·명령 실행·파일 받기.
# LoRA 학습처럼 파드 안에서 돌려야 하는 일을 runpod_cloud.py 가 이걸로 시킨다(2026-10-07)
cat > /workspace/pod_agent.py <<'PY'
import http.server, json, os, subprocess, tarfile, time, urllib.parse
TOKEN, LOGS, JOBS = os.environ.get("AGENT_TOKEN", ""), "/workspace/logs", {}
LAST = [time.time()]  # 마지막 요청 시각. 올리기 중에도 watchdog 이 idle 로 세지 않게(2026-10-11 1천 개 올리다 15분에 스스로 지워짐)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def _auth(self):
        if TOKEN and self.headers.get("X-Token") == TOKEN:
            return True
        self.send_error(403)
        return False
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/jobs":
            return self._send(200, {k: {"running": p.poll() is None, "rc": p.poll()} for k, p in JOBS.items()})
        if u.path == "/activity":
            return self._send(200, {"last": LAST[0], "ago": time.time() - LAST[0]})
        if u.path == "/info":  # 설치가 정한 ComfyUI 폴더·모드: LoRA 를 올릴 경로를 PC 쪽이 짐작하지 않게(이미지에 따라 /comfyui 또는 /workspace/ComfyUI)
            cd = ""
            try:
                with open("/workspace/.comfy_dir") as f:
                    cd = f.read().strip()
            except OSError:
                pass
            return self._send(200, {"comfy_dir": cd, "mode": os.environ.get("MODE", "comfy")})
        if u.path == "/download":
            if self._auth():
                with open(q["path"][0], "rb") as f:
                    self._send(200, f.read(), "application/octet-stream")
            return
        p = os.path.join(LOGS, os.path.basename(u.path) or "bootstrap.log")
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/plain; charset=utf-8")
        self.send_error(404)
    def do_POST(self):
        if not self._auth():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        LAST[0] = time.time()
        if u.path == "/untar":  # 묶음 올리기: tar 하나를 받아 dest 아래에 푼다
            dest = q["dest"][0]
            os.makedirs(dest, exist_ok=True)
            tmp = dest.rstrip("/") + ".upload.tar"
            with open(tmp, "wb") as f:
                f.write(body)
            with tarfile.open(tmp) as t:
                t.extractall(dest)
            os.remove(tmp)
            return self._send(200, {"ok": True, "bytes": len(body)})
        if u.path == "/upload":
            p = q["path"][0]
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(body)
            return self._send(200, {"ok": True, "bytes": len(body)})
        if u.path == "/run":
            spec = json.loads(body)
            name = spec.get("name", "job")
            log = open(os.path.join(LOGS, f"job_{name}.log"), "ab")
            JOBS[name] = subprocess.Popen(["bash", "-c", spec["cmd"]], stdout=log, stderr=subprocess.STDOUT,
                                          cwd=spec.get("cwd", "/workspace"), env={**os.environ, **spec.get("env", {})})
            return self._send(200, {"started": name})
        self.send_error(404)
http.server.ThreadingHTTPServer(("0.0.0.0", 8189), H).serve_forever()
PY
$SYSPY /workspace/pod_agent.py > /workspace/logs/agent.err 2>&1 &

# 안전장치: 로컬 스크립트가 죽어도 GPU 과금이 이어지지 않게 한다. ComfyUI 큐가 비어 있고 LLM(vLLM)도 요청이 없는 채로 IDLE_MIN 분이 지나거나
# 이번 기동 후 MAX_HOURS 가 지나면 파드를 멈춘다(ON_IDLE=terminate 면 지운다). 설치 중(ComfyUI 응답 없음)은 idle 로 세지 않는다.
cat > /workspace/watchdog.py <<'PY'
import json, os, subprocess, time, urllib.request
idle, start = 0, time.time()
idle_min, max_min = int(os.environ.get("IDLE_MIN", 15)), int(os.environ.get("MAX_HOURS", 4)) * 60
pod, key, action = os.environ.get("RUNPOD_POD_ID", ""), os.environ.get("RUNPOD_API_KEY", ""), os.environ.get("ON_IDLE", "terminate")
agent_mode = os.environ.get("MODE") == "agent"
llm_tok = [None]
comfy_last = [None]  # 지난 샘플에서 본 ComfyUI 마지막 프롬프트 번호(/history?max_items=1)


def llm_busy():
    """같은 GPU 의 vLLM(8000, runpod_llm.sh)이 요청을 처리 중이거나 지난 1분 사이에 요청을 받았으면 일하는 중.
    ComfyUI 큐만 보던 때 미사일 스튜디오 대화 도중에 파드가 멈춰 LLM 이 404 를 냈다(2026-10-22 D+18 3화 콘티를 쓰다 끊김)."""
    try:
        t = urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5).read().decode("utf-8", "replace")
    except Exception:
        return False  # LLM 을 안 띄운 파드면 셀 것이 없다
    run = tok = 0.0
    for ln in t.splitlines():
        if ln.startswith(("vllm:num_requests_running{", "vllm:num_requests_waiting{")):
            run += float(ln.rsplit(" ", 1)[-1])
        elif ln.startswith("vllm:prompt_tokens_total{"):
            tok += float(ln.rsplit(" ", 1)[-1])
    moved = llm_tok[0] is not None and tok > llm_tok[0]
    llm_tok[0] = tok
    return run > 0 or moved


def self_stop():
    """GPU 과금을 끝낸다(action: stop 은 멈춤, terminate 는 지움). vast.ai 면 인스턴스 전용 키로, 아니면 RunPod REST 로."""
    vid, vkey = os.environ.get("CONTAINER_ID", ""), os.environ.get("CONTAINER_API_KEY", "")
    if vid and vkey:
        req = urllib.request.Request(f"https://console.vast.ai/api/v0/instances/{vid}/", method="PUT" if action == "stop" else "DELETE",
                                     data=json.dumps({"state": "stopped"}).encode() if action == "stop" else None,
                                     headers={"Authorization": f"Bearer {vkey}", "Content-Type": "application/json", "User-Agent": "webtoon-studio/1.0"})
    else:
        req = urllib.request.Request(f"https://rest.runpod.io/v1/pods/{pod}" + ("/stop" if action == "stop" else ""),
                                     method="POST" if action == "stop" else "DELETE",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": "webtoon-studio/1.0"})
    urllib.request.urlopen(req, timeout=30)


while True:
    time.sleep(60)
    try:
        # 에이전트 작업(받기·학습 등)과, 아직 응답하기 전인 LLM 작업(vLLM 설치·모델 내려받기·올리기)도 일하는 중으로 센다.
        # 놀면 멈춤을 15분으로 줄이자 4비트 LLM(17.6GB)을 받는 도중에 파드가 멈췄다(2026-10-23 L40S). 다 뜬 LLM 은 llm_busy 가 본다.
        # 'llm' 작업은 vLLM 이 떠 있는 동안 늘 '돌고 있음'이라 그것만으로는 바쁨이 아니다(세면 LLM 전용 상자가 영영 안 꺼진다, 2026-10-26)
        jobs = json.loads(urllib.request.urlopen("http://127.0.0.1:8189/jobs", timeout=5).read())
        act = json.loads(urllib.request.urlopen("http://127.0.0.1:8189/activity", timeout=5).read())
        llm_up = True
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5)
        except Exception:
            llm_up = False
        busy = (any(j["running"] for k, j in jobs.items() if k != "llm") or act.get("ago", 1e9) < 120
                or ((jobs.get("llm") or {}).get("running") and not llm_up) or llm_busy())
        if not agent_mode:  # 그림 상자: ComfyUI 큐도 본다(agent 모드엔 ComfyUI 가 없다)
            q = json.loads(urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=5).read())
            # 1분에 한 번 큐만 보면 30초짜리 turbo 그림은 매번 빈 큐로 보인다: 두 컷을 14분 그리는 동안 '놀았다'고 세어 그리기가 끝난 직후
            # 상자를 지웠다(2026-10-26 무대 뒤 L40S: 14:17 준비 → 14:32 삭제, 뒤이은 표정 편집이 서버 없음). 지난 1분 사이에 끝난 프롬프트가
            # 있으면(/history 의 마지막 번호가 바뀜) 바쁨으로 센다
            h = json.loads(urllib.request.urlopen("http://127.0.0.1:8188/history?max_items=1", timeout=5).read())
            newest = next(iter(h), None)
            drew = comfy_last[0] is not None and newest != comfy_last[0]
            comfy_last[0] = newest
            busy = busy or q.get("queue_running") or q.get("queue_pending") or drew
        idle = idle + 1 if not busy else 0
    except Exception:
        idle = 0
    age = (time.time() - start) / 60
    if idle >= idle_min or age >= max_min:
        print(f"[watchdog] idle={idle}m age={age:.0f}m -> {action} self", flush=True)
        try:
            self_stop()
        except Exception as e:
            print(f"[watchdog] REST failed: {e}", flush=True)
            if pod:
                subprocess.run(["runpodctl", "stop" if action == "stop" else "remove", "pod", pod])
        time.sleep(120)
PY
$SYSPY /workspace/watchdog.py &

if [ "${MODE:-comfy}" = "agent" ]; then
  # 에이전트 모드(학습 등): ComfyUI 는 띄우지 않고 체크포인트만 /workspace/ckpt 에 받아 둔 뒤 대기
  mkdir -p /workspace/ckpt && cd /workspace
  COMFY_DIR=/workspace
  CK_DIR=/workspace/ckpt
  echo "[boot] requirements cached (agent mode)"
elif [ -f /comfyui/main.py ] && [ -x /opt/venv/bin/python ]; then
  COMFY_DIR=/comfyui
  PY=/opt/venv/bin/python
  echo "[boot] requirements cached (image ComfyUI $(cd /comfyui && git describe --tags 2>/dev/null || echo ?))"
else
  COMFY_DIR=/workspace/ComfyUI
  PY=/workspace/venv/bin/python
  if [ ! -d "$COMFY_DIR" ]; then
    # 버전을 고정한다. 켤 때마다 최신을 받으면 시험한 것과 다른 ComfyUI 가 깔린다
    git clone --depth 1 --branch "${COMFY_REF:-master}" https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
  fi
  # 의존성은 /workspace/venv 에. 이미지의 torch 를 쓰도록 system-site-packages 로 만들고 torch 버전을 묶는다
  REQ_SHA=$(sha256sum "$COMFY_DIR/requirements.txt" | cut -c1-12)
  if [ ! -f "/workspace/venv/.deps_$REQ_SHA" ]; then
    $SYSPY -m pip freeze | grep -iE '^(torch|torchvision|torchaudio)==' > /workspace/torch-pin.txt
    $SYSPY -m venv --system-site-packages /workspace/venv
    if /workspace/venv/bin/pip install --no-cache-dir -q -c /workspace/torch-pin.txt -r "$COMFY_DIR/requirements.txt"; then
      touch "/workspace/venv/.deps_$REQ_SHA"
      echo "[boot] requirements done $(date -u)"
    else
      echo "[boot] ERROR requirements failed $(date -u)"
    fi
  else
    echo "[boot] requirements cached"
  fi
fi
cd "$COMFY_DIR"
echo "$COMFY_DIR" > /workspace/.comfy_dir  # 에이전트 /info 가 알려 준다(PC 의 LoRA 올리기가 이 폴더의 models/loras 에 쓴다)

# 파드에 넣을 파일(우리 노드·상류 패치, env EXTRA_FILES = {ComfyUI 안 경로: base64})과 패치 적용(env PATCHES = [경로]). 이미 붙은 패치는 건너뛴다
if [ "${MODE:-comfy}" != "agent" ] && [ -n "${EXTRA_FILES:-}" ] && [ "${EXTRA_FILES}" != "{}" ]; then
  COMFY_DIR="$COMFY_DIR" $SYSPY - <<'PY'
import base64, json, os
base = os.environ["COMFY_DIR"]
for rel, b in json.loads(os.environ["EXTRA_FILES"]).items():
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(base64.b64decode(b))
    print(f"[boot] file {rel}", flush=True)
PY
  for pf in $(echo "${PATCHES:-[]}" | $SYSPY -c 'import json,sys; print(" ".join(json.load(sys.stdin)))'); do
    if git -C "$COMFY_DIR" apply --check "$COMFY_DIR/$pf" 2>/dev/null; then
      git -C "$COMFY_DIR" apply "$COMFY_DIR/$pf" && echo "[boot] patch $pf applied"
    else
      echo "[boot] patch $pf skipped (already applied or does not fit)"
    fi
  done
fi

# 커스텀 노드(IP-Adapter 등)와 그 모델. 목록이 비어 있으면 건너뛴다(cloud/runpod.json 의 custom_nodes·extra_models)
if [ "${MODE:-comfy}" != "agent" ] && { [ "${CUSTOM_NODES:-[]}" != "[]" ] || [ "${EXTRA_MODELS:-[]}" != "[]" ]; }; then
  $PY -m pip install --no-cache-dir -q huggingface_hub hf_transfer
  COMFY_DIR="$COMFY_DIR" HF_HUB_ENABLE_HF_TRANSFER=1 $PY - <<'PY'
import hashlib, json, os, shutil, subprocess, urllib.request
from huggingface_hub import hf_hub_download
base = os.environ["COMFY_DIR"]
def fetch(m, dest):  # url 항목(Civitai 등): 받아서 sha256 이 맞을 때만 둔다
    tmp = dest + ".part"
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(m["url"], headers={"User-Agent": "webtoon-studio/1.0"}), timeout=120) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 22), b""):
            f.write(chunk)
            h.update(chunk)
    if m.get("sha256") and h.hexdigest().lower() != m["sha256"].lower():
        os.remove(tmp)
        raise RuntimeError(f"sha256 mismatch {m['dest']}")
    os.replace(tmp, dest)
for n in json.loads(os.environ.get("CUSTOM_NODES") or "[]"):
    d = f"{base}/custom_nodes/{n['dir']}"
    if not os.path.isdir(d):
        subprocess.run(["git", "clone", "-q", n["url"], d], check=True)
        subprocess.run(["git", "-C", d, "checkout", "-q", n["ref"]], check=True)
    print(f"[boot] node {n['dir']} ok", flush=True)
for m in json.loads(os.environ.get("EXTRA_MODELS") or "[]"):
    dest = f"{base}/models/{m['dest']}"
    if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if m.get("url"):
            fetch(m, dest)
        else:
            shutil.move(hf_hub_download(m["repo"], m["file"], local_dir="/workspace/hf_tmp"), dest)
    print(f"[boot] model {m['dest']} ok", flush=True)
PY
fi

# 체크포인트: 16개 연결로 나눠 받는다(한 줄 wget 은 기계에 따라 10분 넘게 걸렸다)
CK_DIR="${CK_DIR:-models/checkpoints}"
mkdir -p "$CK_DIR"
CK="$CK_DIR/${CKPT_NAME:-none}"
if [ -n "${CKPT_URL:-}" ] && [ ! -s "$CK" ]; then
  $SYSPY - "$CKPT_URL" "$CK" <<'PY' || wget -q -O "$CK.part" "$CKPT_URL" && [ -s "$CK.part" ] && mv "$CK.part" "$CK"
import concurrent.futures as cf, os, sys, threading, time, urllib.request
url, out = sys.argv[1], sys.argv[2]
UA = {"User-Agent": "webtoon-studio/1.0"}
r = urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA), timeout=60)
size = int(r.headers["Content-Length"])
final = r.geturl()
n = 16
step = size // n + 1
tmp = out + ".part"
with open(tmp, "wb") as f:
    f.truncate(size)
done = [0]
lock = threading.Lock()
def get(i):
    a, b = i * step, min(size, (i + 1) * step) - 1
    for attempt in range(5):
        try:
            req = urllib.request.Request(final, headers={**UA, "Range": f"bytes={a}-{b}"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "r+b") as f:
                f.seek(a)
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    with lock:
                        done[0] += len(chunk)
            return
        except Exception as e:
            print(f"[boot] range {i} retry {attempt}: {e}", flush=True)
            time.sleep(3)
    raise RuntimeError(f"range {i} failed")
def report():
    while done[0] < size:
        time.sleep(20)
        print(f"[boot] downloading {done[0] / 2**30:.1f}/{size / 2**30:.1f}G", flush=True)
threading.Thread(target=report, daemon=True).start()
with cf.ThreadPoolExecutor(n) as ex:
    list(ex.map(get, range(n)))
os.replace(tmp, out)
PY
fi
echo "[boot] checkpoint ready $( [ -n "${CKPT_URL:-}" ] && ls -la "$CK" || echo 'none (models from extra_models)') $(date -u)"
echo "[boot] driver $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1 | head -1)"

if [ "${MODE:-comfy}" = "agent" ]; then
  echo "[boot] agent ready $(date -u)"
  exec sleep infinity
fi
# 같은 GPU 를 vLLM(48GB 의 절반)과 나눠 쓴다: 1GB 를 남겨 두고 모자라면 내려 놓게(2026-10-23 L40S 에서 손님 많은 컷 편집 중 멈춤)
# COMFY_ARGS: vast 는 터널로만 붙어 '--listen 127.0.0.1'(뒤에 온 값이 이긴다), GPU 가 둘이면 '--cuda-device 1' 로 그림은 둘째 장(첫째 장은 LLM)
exec $PY main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --reserve-vram 1 ${COMFY_ARGS:-}
