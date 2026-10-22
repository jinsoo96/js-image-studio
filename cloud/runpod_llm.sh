#!/usr/bin/env bash
# 파드 안에서 Qwen 3.8 무검열(그림도 보는 LLM)을 vLLM 으로 띄운다(OpenAI 호환, 포트 8000). ComfyUI 와 같은 GPU 를 나눠 쓴다.
# tools/runpod_cloud.py llm 이 파드 에이전트로 이 스크립트를 돌린다. 넘겨받는 env: LLM_REPO, LLM_NAME, VLLM_VERSION, GPU_UTIL, MAX_LEN.
# 프록시 주소는 누구나 닿으니 키를 건다: 파드 에이전트 토큰(AGENT_TOKEN, 파드 env). 로컬은 runpod_cloud.py use 가 같은 값을 state/vlm_url.txt 에 적는다.
# (2026-10-22 회사 vast·사내 Qwen 게이트웨이가 함께 꺼져, 그림 검수·미사일 스튜디오가 쓸 모델을 RunPod 파드에 같이 둔다)
# --max-num-seqs: 기본 1024 면 Qwen3.8(선형 어텐션 섞임)의 Mamba 캐시 칸(gpu_util 0.4 에서 130)을 넘어 기동 실패
set -e
pkill -f 'vllm serve' 2>/dev/null || true  # 다시 부르면 띄워 둔 것을 내리고 새로
V=/workspace/vllm-venv
export HF_HOME=/workspace/hf HF_HUB_ENABLE_HF_TRANSFER=1
if [ ! -x "$V/bin/vllm" ] || [ "$($V/bin/python -c 'import vllm; print(vllm.__version__)' 2>/dev/null)" != "$VLLM_VERSION" ]; then
  echo "[llm] install vllm==$VLLM_VERSION $(date -u)"
  python3 -m venv "$V"
  "$V/bin/pip" install -q --no-cache-dir "vllm==$VLLM_VERSION" hf_transfer
fi
echo "[llm] download $LLM_REPO $(date -u)"
"$V/bin/python" -c "import os; from huggingface_hub import snapshot_download; snapshot_download(os.environ['LLM_REPO'])"
# ComfyUI 가 GPU 에 모델을 올려 두면 vLLM 이 KV 캐시 자리를 못 얻는다(2026-10-22 파드 재시작 직후 "No available memory for the cache blocks").
# ComfyUI 큐가 빌 때까지 기다렸다가 모델을 내리게 하고 띄운다(최대 1시간)
for i in $(seq 1 120); do
  q=$(curl -s localhost:8188/queue | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['queue_running'])+len(d['queue_pending']))" 2>/dev/null || echo 0)
  [ "$q" = "0" ] && break
  echo "[llm] ComfyUI 큐 $q 개, 기다림 $(date -u)"
  sleep 30
done
curl -s -X POST localhost:8188/free -H 'Content-Type: application/json' -d '{"unload_models": true, "free_memory": true}' >/dev/null || true
sleep 10
echo "[llm] serve $LLM_NAME $(date -u)"
# 생각(thinking)은 기본으로 끈다: 검수·대화는 짧은 답이 필요하고, 켜려면 요청에 chat_template_kwargs.enable_thinking=true
exec "$V/bin/vllm" serve "$LLM_REPO" --served-model-name "$LLM_NAME" --host 0.0.0.0 --port 8000 --api-key "$AGENT_TOKEN" \
  --gpu-memory-utilization "$GPU_UTIL" --max-model-len "$MAX_LEN" --limit-mm-per-prompt '{"image": 4}' \
  --max-num-seqs 16 \
  --reasoning-parser qwen3 --default-chat-template-kwargs '{"enable_thinking": false}'
