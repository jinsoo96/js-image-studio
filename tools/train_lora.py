"""우리 그림체 LoRA 를 학습한다(RunPod 에이전트 파드, kohya sd-scripts).

    python tools/train_lora.py build <이름> <컷 목록 파일>     로컬에 학습 세트를 만든다(models/lora_data/<이름>/)
    python tools/train_lora.py train <이름> [--steps 1500]     파드를 켜서 학습하고 models/loras/<이름>.safetensors 로 받아 온다

왜: NoobAI-XL 은 우리 그림체·캐릭터를 배운 적이 없어 같은 태그로도 컷마다 반실사·납작한 애니·번들거림을 오갔다
(2026-10-07 사용자 "그림체가 왜 다 다른 느낌이냐"). 태그 조정·IP-Adapter 로는 한계라, 승인·선별한 컷의 그림체를 LoRA 로 가르친다.

학습 세트: 컷 목록 파일의 각 줄이 "series/.../panels/p012.png" 같은 그림 경로(# 뒤는 메모). 캡션은 같은 폴더 render_log.json 의
프롬프트에서 그림체·품질 태그를 뺀 내용 태그 + 맨 앞 트리거 "<이름> style"(LORA_TRIGGER) 이다(그림체는 LoRA 가 흡수하고 내용은 프롬프트가 맡게).
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import ROOT, load_json, load_style  # noqa: E402
import runpod_cloud  # noqa: E402

# 트리거 낱말 = LoRA 이름 + " style"(LORA_TRIGGER 로 바꿈). 예전엔 무대 뒤 전용 "mudae style" 이 박혀 다른 작품에도 그 낱말이 붙었다(2026-10-24)
TRIGGER = os.environ.get("LORA_TRIGGER", "")
TRAIN_IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
SD_SCRIPTS = ("https://github.com/kohya-ss/sd-scripts.git", "690ea7f96c23182352ec63def76d431c6120bd2f")


def bare(tag):
    return re.sub(r"[()]|:\d+(\.\d+)?", "", tag).strip().lower()


def style_tags():
    """캡션에서 뺄 그림체·품질·표정모드 공통 태그."""
    s = load_style()
    parts = [s["style_pos"], s["quality_pos"], s.get("rating_pos", "")]
    for m in s["modes"].values():
        parts += [m.get("pos", ""), m.get("face_pos", "")] + list(m.get("gender_pos", {}).values())
    return {bare(t) for p in parts for t in p.split(",") if t.strip()}


def caption_for(img, drop, trigger):
    log_p = img.parent / "render_log.json"
    log = load_json(log_p) if log_p.exists() else {}
    entry = log.get(img.stem) or next((v for v in log.values() if v.get("file") == img.name), None)
    if not entry:
        return None
    tags = [t.strip() for t in entry["positive"].split(",") if t.strip()]
    keep, seen = [], set()
    for t in tags:
        b = bare(t)
        if b and b not in drop and b not in seen:
            seen.add(b)
            keep.append(b)
    return ", ".join([trigger] + keep)


def cmd_build(name, list_file):
    trigger = TRIGGER or f"{name} style"
    out = ROOT / "models" / "lora_data" / name / f"10_{trigger.split()[0]}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    drop = style_tags()
    n = 0
    for line in Path(list_file).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        img = ROOT / line
        cap = caption_for(img, drop, trigger)
        if not img.exists() or cap is None:
            print(f"  건너뜀(그림 또는 기록 없음): {line}")
            continue
        stem = f"{n:03d}_{img.parent.parent.name}_{img.stem}"
        shutil.copyfile(img, out / f"{stem}.png")
        (out / f"{stem}.txt").write_text(cap, encoding="utf-8")
        n += 1
    print(f"학습 세트 {n}장 -> {out}")


def cmd_train(name, steps):
    # 학습 세트 = models/lora_data/<이름>/<반복수>_<트리거>/ 폴더들(kohya 규칙). 그림체 LoRA 는 10_<트리거> 하나,
    # 인물 LoRA 는 인물마다 폴더 하나(worklogs/char_lora_build_1009.py)
    root = ROOT / "models" / "lora_data" / name
    files = sorted(f for d in root.iterdir() if d.is_dir() for f in d.glob("*")) if root.exists() else []
    if not files:
        sys.exit(f"학습 세트가 없다: {root} (먼저 build)")
    ckpt = runpod_cloud.cfg()["checkpoint"]["name"]
    pod_id, _ = runpod_cloud.ensure_pod(mode="agent", image=TRAIN_IMAGE)
    try:
        t0 = time.time()
        n = runpod_cloud.agent_upload_dir(pod_id, root, "/workspace/dataset")
        print(f"학습 세트 올림: {n}개 파일 ({time.time() - t0:.0f}초)", flush=True)
        repo, ref = SD_SCRIPTS
        cmd = f"""set -e
cd /workspace
[ -d sd-scripts ] || {{ git clone -q {repo} sd-scripts && git -C sd-scripts checkout -q {ref}; }}
cd sd-scripts
python -m pip freeze | grep -iE '^(torch|torchvision|torchaudio)==' > /workspace/torch-pin.txt
pip install -q -c /workspace/torch-pin.txt -r requirements.txt
echo "deps ok $(date -u)"
accelerate launch --num_cpu_threads_per_process 4 --mixed_precision bf16 sdxl_train_network.py \\
  --pretrained_model_name_or_path /workspace/ckpt/{ckpt} --train_data_dir /workspace/dataset \\
  --output_dir /workspace/out --output_name {name} --save_model_as safetensors --save_precision fp16 \\
  --network_module networks.lora --network_dim 32 --network_alpha 16 --network_train_unet_only \\
  --resolution 1024,1024 --enable_bucket --min_bucket_reso 640 --max_bucket_reso 1536 --bucket_reso_steps 64 \\
  --train_batch_size 2 --max_train_steps {steps} --learning_rate 1e-4 --optimizer_type AdamW8bit \\
  --lr_scheduler cosine --lr_warmup_steps 100 --mixed_precision bf16 --gradient_checkpointing --sdpa \\
  --cache_latents --caption_extension .txt --shuffle_caption --keep_tokens 2 --noise_offset 0.03 --seed 42
echo "train done $(date -u)"
"""
        runpod_cloud.agent_run(pod_id, "train", cmd)
        rc = runpod_cloud.agent_wait(pod_id, "train")
        if rc != 0:
            raise RuntimeError(f"학습 실패(종료 코드 {rc}). 로그: {runpod_cloud.agent_url(pod_id)}/job_train.log")
        out = ROOT / "models" / "loras" / f"{name}.safetensors"
        size = runpod_cloud.agent_download(pod_id, f"/workspace/out/{name}.safetensors", out)
        print(f"LoRA 받음: {out} ({size / 2**20:.0f}MB, 총 {(time.time() - t0) / 60:.0f}분)", flush=True)
    finally:
        runpod_cloud.terminate(pod_id)


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "build":
        cmd_build(a[1], a[2])
    elif len(a) >= 2 and a[0] == "train":
        steps = int(a[a.index("--steps") + 1]) if "--steps" in a else 1500
        cmd_train(a[1], steps)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
