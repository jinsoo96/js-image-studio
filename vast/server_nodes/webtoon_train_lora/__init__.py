"""WebtoonTrainLora: ComfyUI 기본 'Train LoRA'(comfy_extras/nodes_train.py TrainLoraNode)를 그대로 부르되 몇 가지만 바꾼 학습 노드
+ 편집 LoRA 짝 데이터를 이름으로 맞춰 읽는 WebtoonPairDataset.
코어 파일은 고치지 않는다(공용 서버). 학습이 도는 동안만 nodes_train 의 함수 셋을 바꿔 끼우고 끝나면 되돌린다(ComfyUI 는 한 번에 한 작업).

왜(2026-10-21 webtoon-studio 조사, worklogs/2026-10-21-uc-optimize.md): 기본 노드로 학습한 그림체 LoRA 가 거의 안 배워지고 얼룩이 났다.
  1. alpha 가 1 로 고정(배율 1/rank)인데, 그 배율은 ComfyUI 의 초기값(lora_up 을 ±1/sqrt(rank) 로 크게, lora_down 을 0)과 짝이다.
     → alpha = rank 로 쓰려면 초기값도 PEFT 처럼(lora_down 을 ±1/sqrt(입력 폭), lora_up 을 0) 바꿔야 한다(init=peft).
     처음엔 alpha 만 rank 로 바꿨더니 실제 학습 세기가 PEFT 의 약 14배(lr 5e-5 가 7e-4 처럼)라 강도 1 에서 그림이 무너졌다(2026-10-21 D+18).
  2. 블록 밖 전역 층(32 블록이 같이 쓰는 modulation, 시간 임베딩, img_in/proj_out)과 norm·bias 를 배율 없이 통째로 학습한다 →
     transformer_blocks 안의 Linear 만(ai-toolkit·musubi·DiffSynth 와 같은 범위).
  3. 손실이 x0 공간 MSE 라 flow 모델에서는 속도 오차 × σ² 와 같다 → 선·음영이 정해지는 낮은 노이즈 구간이 손실의 약 1% 뿐 →
     σ² 로 나눠 속도 손실과 같게(ComfyUI 이슈 #15847).
  4. **묶음 모드(bucket_mode)에서 학습 잠재를 모델 기준으로 정규화하지 않는다(0.42.1 버그).** 일반 모드는 guider.sample 이
     process_latent_in 을 거치지만, 묶음 잠재는 TrainSampler 로 바로 간다 → Qwen(Wan21 잠재 평균·분산)에서 첫 손실이 10 안팎이고
     LoRA 가 그 차이를 메우도록 배워, 강도 1 에서 그림이 보라·초록으로 무너졌다(2026-10-21 D+18, 그전 인물 LoRA 를 겹칠 때의 얼룩도 같은 원인).
     → 묶음 잠재를 학습 전에 process_latent_in. 상류 nodes_train 이 정규화를 하게 되면(소스에 process_latent_in) 건너뛴다.
  5. 편집 조건(참고 그림 reference_latents)이 든 학습 데이터에서 process_cond_list 가 목록 속 텐서를 목록처럼 순회해
     0차원 텐서에서 'iteration over a 0-d tensor' 로 멈춘다(2026-10-21 무대 뒤 Restyle 짝 학습) → 텐서는 잎으로 두는 판으로 바꿔 끼운다.
     (2026-10-22 다시 봄: 이 판은 참고 잠재를 지우지 않는다. 짝마다 model_conds 의 ref_latents 가 그대로 모델 forward 까지 간다)

Restyle(편집 LoRA)이 입력 그림을 무시하던 진짜 원인(2026-10-22, ComfyUI 0.42.1 소스 대조):
  6. 짝을 두 폴더 노드(LoadImageTextDataSetFromFolder = 목표, LoadImageDataSetFromFolder = 입력)로 읽었는데 둘 다 os.listdir 순서 그대로다
     (정렬도 이름 맞추기도 없음). 폴더마다 순서가 다르면 목표 i 와 입력 i 가 다른 컷이라, 입력을 따르지 말라고 배운다
     → 이름(확장자 뺀 것)으로 짝을 맞추는 WebtoonPairDataset 노드.
  7. 참고 그림을 TextEncodeQwenImage21 resolution 1024 로 늘려 넣어, 768 짝은 참고 잠재가 목표보다 1.33배 컸다(1024 짝도 73/118 이 한 칸 차이).
     Qwen Image 2.1 은 참고와 목표 격자를 둘 다 가운데 기준 같은 좌표(RoPE)에 두므로 크기가 다르면 위치가 어긋난 편집을 배운다
     (그릴 때는 같은 크기) → 짝 노드가 두 그림을 같은 크기·32 배수로 맞추고 인코더는 resolution 0(그 크기 그대로). 학습 전에 크기를 점검한다.
  8. batch_size 가 2 이상이면 코어 샘플러가 묶음 안 조건 하나하나를 묶음 전체에 돌려 평균한다(짝마다의 참고 잠재·글이 섞임)
     → 묶음 1 + 같은 수의 기울기 누적으로 바꿔 돈다.
"""
import inspect
import logging
import os

import torch

import comfy_extras.nodes_train as NT
import folder_paths
from comfy_api.latest import ComfyExtension, io


def _peft_lora(weight, rank: int, alpha: float):
    """PEFT 초기값의 학습용 LoRA: lora_down 은 kaiming(입력 폭 기준, ±1/sqrt(in)), lora_up 은 0. ΔW = alpha/rank · up @ down."""
    from comfy.weight_adapter.lora import LoraDiff
    out_dim, in_dim = weight.shape[0], weight.shape[1:].numel()
    up = torch.zeros(out_dim, rank, device=weight.device, dtype=torch.float32)
    down = torch.empty(rank, in_dim, device=weight.device, dtype=torch.float32)
    torch.nn.init.kaiming_uniform_(down, a=5 ** 0.5)
    return LoraDiff((up, down, alpha, None, None, None))


def _filtered_setup(bypass: bool, blocks_only: bool, alpha_mode: str, init: str = "peft"):
    def make_adapter(m, n, existing_weights, algorithm, lora_dtype, rank):
        key = f"{n}.weight"
        # 저장된 LoRA 의 alpha 는 '<층>.alpha' 에 있다(기본 노드는 '<층>.weight.alpha' 를 찾아 못 읽고 기본값을 썼다)
        a0 = existing_weights.get(f"{n}.alpha", existing_weights.get(f"{key}.alpha"))
        alpha = float(a0) if a0 is not None else float(rank if alpha_mode == "rank" else 1.0)
        dora_scale = existing_weights.get(f"{key}.dora_scale", None)
        params = {}
        existing = None
        for cls in NT.adapters:
            existing = cls.load(n, existing_weights, alpha, dora_scale)
            if existing is not None:
                break
        if existing is not None:
            ad = existing.to_train().to(lora_dtype)
        elif init == "peft" and algorithm == "LoRA":
            ad = _peft_lora(m.weight, rank, alpha).to(lora_dtype)
        else:
            ad = NT.adapter_maps[algorithm].create_train(m.weight, rank=rank, alpha=alpha).to(lora_dtype)
        for name, p in ad.named_parameters():
            params[f"{n}.{name}"] = p
        ad.train().requires_grad_(True)
        if isinstance(getattr(ad, "alpha", None), torch.nn.Parameter):
            ad.alpha.requires_grad_(False)  # 배율은 학습하지 않는다(기본 노드는 alpha 까지 조금씩 움직였다)
        return ad, params

    def setup(mp, existing_weights, algorithm, lora_dtype, rank):
        lora_sd, adapters_all = {}, []
        manager = NT.BypassInjectionManager() if bypass else None
        kept = skipped = 0
        for n, m in mp.model.named_modules():
            if not hasattr(m, "weight_function"):
                continue
            w = getattr(m, "weight", None)
            if w is None:
                continue
            if blocks_only and ("transformer_blocks." not in n or w.ndim < 2):
                skipped += 1
                continue
            if w.ndim < 2:  # blocks_only 를 끈 경우: 원래처럼 1D 는 BiasDiff
                ad, params = NT._create_weight_adapter(m, n, existing_weights, algorithm, lora_dtype, rank)
                mp.add_weight_wrapper(f"{n}.weight", ad)
            else:
                ad, params = make_adapter(m, n, existing_weights, algorithm, lora_dtype, rank)
                if bypass:
                    manager.add_adapter(f"{n}.weight", ad, strength=1.0)
                else:
                    mp.add_weight_wrapper(f"{n}.weight", ad)
            lora_sd.update(params)
            adapters_all.append(ad)
            kept += 1
            if not blocks_only and getattr(m, "bias", None) is not None:
                bad, bparams = NT._create_bias_adapter(m, n, lora_dtype)
                lora_sd.update(bparams)
                mp.add_weight_wrapper(f"{n}.bias", bad)
                adapters_all.append(bad)
        logging.info(f"[WebtoonTrainLora] LoRA 를 단 층 {kept} · 뺀 층 {skipped} (blocks_only={blocks_only}, alpha={alpha_mode}, init={init})")
        return (lora_sd, adapters_all, manager) if bypass else (lora_sd, adapters_all)

    return setup


def _safe_process_cond_list(d, prefix=""):
    """nodes_train.process_cond_list 와 같되 텐서는 순회하지 않고 잎으로 둔다(목록 속 0차원 텐서에서 멈추던 것)."""
    if isinstance(d, torch.Tensor):
        return d
    if hasattr(d, "__iter__") and not hasattr(d, "items") and not isinstance(d, (str, bytes)):
        for index, item in enumerate(d):
            _safe_process_cond_list(item, f"{prefix}.{index}")
        return d
    if hasattr(d, "items"):
        for k, v in list(d.items()):
            if isinstance(v, dict):
                _safe_process_cond_list(v, f"{prefix}.{k}")
            elif isinstance(v, torch.Tensor):
                d[k] = v.clone()
            elif isinstance(v, NT.CONDList):
                v.cond = [t.detach() if isinstance(t, torch.Tensor) else t for t in v.cond]
            elif isinstance(v, NT.CONDRegular):
                if isinstance(v.cond, torch.Tensor):
                    v.cond = v.cond.detach()
            elif isinstance(v, (list, tuple)):
                for index, item in enumerate(v):
                    _safe_process_cond_list(item, f"{prefix}.{k}.{index}")
    return d


def _velocity_fwd_bwd(orig):
    def fwd_bwd(self, model_wrap, batch_sigmas, batch_noise, batch_latent, cond, indicies, extra_args, dataset_size, bwd=True):
        ms = model_wrap.inner_model.model_sampling
        xt = ms.noise_scaling(batch_sigmas, batch_noise, batch_latent, False)
        x0 = ms.noise_scaling(torch.zeros_like(batch_sigmas), torch.zeros_like(batch_noise), batch_latent, False)
        model_wrap.conds["positive"] = [cond[i] for i in indicies]
        batch_extra_args = NT.make_batch_extra_option_dict(extra_args, indicies, full_size=dataset_size)
        with torch.autocast(xt.device.type, dtype=self.training_dtype):
            x0_pred = model_wrap(xt.requires_grad_(True), batch_sigmas.requires_grad_(True), **batch_extra_args)
            per = (x0_pred.float() - x0.float()).pow(2)
            s2 = batch_sigmas.detach().float().pow(2).clamp_min(1e-3).view(-1, *([1] * (per.ndim - 1)))
            loss = (per / s2).mean()  # 속도 손실과 같게: x0 오차 = σ × 속도 오차
        if bwd:
            b = loss / self.grad_acc
            (self.grad_scaler.scale(b) if self.grad_scaler is not None else b).backward()
        return loss
    return fwd_bwd


def _check_conds(latents, positive, bucket_mode):
    """학습 조건 점검(6·7). 묶음 모드는 코어가 조건 수를 안 보고 잠재 순번으로 조건을 집으므로 수가 다르면 멈춘다.
    편집 조건이 든 학습이면 짝마다 참고 잠재가 그 목표 잠재와 같은 격자인지 보고, 전부 다르면 멈춘다(돈만 쓰고 참고를 안 따르는 LoRA)."""
    lat = [s[i] for s in (d["samples"] for d in latents) for i in range(s.shape[0])]
    conds = NT._process_conditioning(positive)
    if bucket_mode and len(conds) != len(lat):
        raise ValueError(f"[WebtoonTrainLora] 학습 잠재 {len(lat)}개와 조건 {len(conds)}개의 수가 다르다(묶음 모드는 순번으로 짝을 짓는다)")
    refs = [c[1].get("reference_latents") if len(c) > 1 and isinstance(c[1], dict) else None for c in conds]
    n_ref = sum(1 for r in refs if r)
    if not n_ref:
        logging.info(f"[WebtoonTrainLora] 학습 조건 {len(conds)}개, 참고 잠재 없음(글 → 그림 학습)")
        return
    bad = [(tuple(x.shape[-2:]), tuple(r[0].shape[-2:])) for x, r in zip(lat, refs) if r and tuple(r[0].shape[-2:]) != tuple(x.shape[-2:])] \
        if len(conds) == len(lat) else []
    logging.info(f"[WebtoonTrainLora] 학습 조건 {len(conds)}개 중 참고 잠재(편집 조건) {n_ref}개, 목표와 격자가 다른 참고 {len(bad)}개")
    if bad and len(bad) == n_ref:
        raise ValueError(f"[WebtoonTrainLora] 참고 잠재 {n_ref}개가 전부 목표 잠재와 크기가 다르다(예: 목표 {bad[0][0]} ↔ 참고 {bad[0][1]}). "
                         "그대로 배우면 참고 위치가 어긋난 편집을 배운다 → 짝을 같은 크기로(WebtoonPairDataset) + TextEncodeQwenImage21 resolution 0 으로 다시 만들 것")
    if bad:
        logging.warning(f"[WebtoonTrainLora] 목표와 크기가 다른 참고 {len(bad)}/{n_ref}개(예: 목표 {bad[0][0]} ↔ 참고 {bad[0][1]})")


class WebtoonTrainLora(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        base = NT.TrainLoraNode.define_schema()
        extra = [
            io.Combo.Input("alpha_mode", options=["rank", "one"], default="rank",
                           tooltip="rank: alpha = rank (배율 1, 다른 트레이너와 같음) · one: 기본 노드처럼 alpha 1(배율 1/rank)"),
            io.Boolean.Input("blocks_only", default=True,
                             tooltip="transformer_blocks 안의 Linear 만 학습(전역 modulation·임베딩·입출력 층·norm·bias 제외)"),
            io.Combo.Input("loss_mode", options=["velocity", "x0"], default="velocity",
                           tooltip="velocity: σ² 로 나눠 낮은 노이즈(선·음영) 구간도 배운다 · x0: 기본 노드와 같음"),
            io.Combo.Input("init", options=["peft", "comfy"], default="peft",
                           tooltip="peft: lora_down 을 작게·lora_up 을 0 으로(alpha=rank 와 짝, 다른 트레이너의 lr 이 그대로 맞는다) · "
                                   "comfy: 기본 노드 초기값(alpha 1 과 짝)"),
        ]
        return io.Schema(node_id="WebtoonTrainLora", display_name="Train LoRA (webtoon: alpha=rank · 블록만 · 속도 손실)",
                         category="model/training", is_experimental=True, is_input_list=True,
                         inputs=list(base.inputs) + extra, outputs=base.outputs)

    @classmethod
    def execute(cls, alpha_mode, blocks_only, loss_mode, init=("peft",), **kw):
        alpha_mode, blocks_only, loss_mode, init = alpha_mode[0], blocks_only[0], loss_mode[0], init[0]
        _check_conds(kw["latents"], kw["positive"], kw.get("bucket_mode", [False])[0])
        bs = kw.get("batch_size", [1])[0]
        if bs > 1:  # 8: 같은 표본 수를 묶음 1 × 기울기 누적으로(손실 평균·스텝 수 같음)
            kw["grad_accumulation_steps"] = [kw["grad_accumulation_steps"][0] * bs]
            kw["batch_size"] = [1]
            logging.warning(f"[WebtoonTrainLora] batch_size {bs} → 1, grad_accumulation_steps ×{bs}(코어가 묶음 안 조건을 섞는다)")
        if kw.get("bucket_mode", [False])[0] and "process_latent_in" not in inspect.getsource(NT):
            proc = kw["model"][0].model.process_latent_in
            kw["latents"] = [{**d, "samples": proc(d["samples"])} for d in kw["latents"]]
            logging.info(f"[WebtoonTrainLora] 묶음 잠재 {len(kw['latents'])}개를 모델 기준으로 정규화(process_latent_in)")
        saved = (NT._setup_lora_adapters, NT._setup_lora_adapters_bypass, NT.TrainSampler.fwd_bwd)
        saved_pcl = NT.process_cond_list
        try:
            NT.process_cond_list = _safe_process_cond_list
            NT._setup_lora_adapters = _filtered_setup(False, blocks_only, alpha_mode, init)
            NT._setup_lora_adapters_bypass = _filtered_setup(True, blocks_only, alpha_mode, init)
            if loss_mode == "velocity":
                NT.TrainSampler.fwd_bwd = _velocity_fwd_bwd(saved[2])
            return NT.TrainLoraNode.execute(**kw)
        finally:
            NT._setup_lora_adapters, NT._setup_lora_adapters_bypass, NT.TrainSampler.fwd_bwd = saved
            NT.process_cond_list = saved_pcl


_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def _pil_rgb(path):
    """코어 데이터 노드(load_and_process_images)와 같은 열기: 잘린 파일 허용, 16비트 회색은 0~1 로."""
    import node_helpers
    from PIL import Image
    img = node_helpers.pillow(Image.open, path)
    if img.mode == "I":
        img = img.point(lambda i: i * (1 / 255))
    return img.convert("RGB")


def _to_tensor(img):
    import numpy as np
    return torch.from_numpy(np.array(img).astype(np.float32) / 255.0)[None,]


def _pair_images(target, inp, multiple: int):
    """입력을 목표 크기로 맞추고(다르면 Lanczos) 두 그림을 같은 자리에서 multiple 배수로 가운데 자른다. (목표, 입력, 바꿨나)."""
    from PIL import Image
    changed = False
    if inp.size != target.size:
        inp = inp.resize(target.size, Image.LANCZOS)
        changed = True
    w, h = target.size
    m = max(1, int(multiple))
    w2, h2 = max(m, w // m * m), max(m, h // m * m)
    if (w2, h2) != (w, h):
        box = ((w - w2) // 2, (h - h2) // 2, (w - w2) // 2 + w2, (h - h2) // 2 + h2)
        target, inp = target.crop(box), inp.crop(box)
        changed = True
    return target, inp, changed


class WebtoonPairDataset(io.ComfyNode):
    """짝 데이터(6·7): 목표 폴더(그림 + 같은 이름 .txt 캡션)와 입력 폴더의 같은 이름 그림을 이름순으로 짝짓는다.
    코어 폴더 노드 둘은 os.listdir 순서라 폴더끼리 순서가 같다는 보장이 없다. 두 그림은 같은 크기·multiple 배수로 맞춘다."""

    @classmethod
    def define_schema(cls):
        folders = folder_paths.get_input_subfolders()
        return io.Schema(
            node_id="WebtoonPairDataset", display_name="Load Image Pairs (webtoon: 이름으로 짝 · 같은 크기)", category="model/training",
            is_experimental=True,
            inputs=[
                io.Combo.Input("target_folder", options=folders, tooltip="목표(정답) 폴더: input/ 아래 그림 + 같은 이름 .txt 캡션"),
                io.Combo.Input("input_folder", options=folders, tooltip="입력(편집 조건) 폴더: 목표와 같은 이름의 그림"),
                io.Int.Input("multiple", default=32, min=1, max=256,
                             tooltip="두 그림을 같은 크기로 맞추고 이 배수로 가운데를 자른다. 32 면 TextEncodeQwenImage21 resolution 0 에서 "
                                     "참고 잠재가 목표 잠재와 같은 격자가 된다"),
            ],
            outputs=[
                io.Image.Output(display_name="targets", is_output_list=True, tooltip="목표 그림 목록(이름순)"),
                io.String.Output(display_name="captions", is_output_list=True, tooltip="목표 캡션 목록(.txt, 없으면 빈 글)"),
                io.Image.Output(display_name="inputs", is_output_list=True, tooltip="같은 순번의 입력 그림 목록(목표와 같은 크기)"),
            ])

    @classmethod
    def execute(cls, target_folder, input_folder, multiple=32):
        from comfy_extras.nodes_dataset import secure_subfolder_path
        base = folder_paths.get_input_directory()
        tdir, idir = secure_subfolder_path(base, target_folder), secure_subfolder_path(base, input_folder)

        def stems(d):
            return {os.path.splitext(f)[0]: f for f in os.listdir(d) if f.lower().endswith(_IMG_EXT)}

        ts, ins = stems(tdir), stems(idir)
        names = sorted(set(ts) & set(ins))
        if not names:
            raise ValueError(f"[WebtoonPairDataset] 짝이 없다: {target_folder} {len(ts)}장 · {input_folder} {len(ins)}장, 같은 이름 0")
        only_t, only_i = sorted(set(ts) - set(ins)), sorted(set(ins) - set(ts))
        targets, captions, inputs, changed = [], [], [], 0
        for n in names:
            t, i, ch = _pair_images(_pil_rgb(os.path.join(tdir, ts[n])), _pil_rgb(os.path.join(idir, ins[n])), multiple)
            changed += ch
            targets.append(_to_tensor(t))
            inputs.append(_to_tensor(i))
            cap = os.path.join(tdir, n + ".txt")
            if os.path.exists(cap):
                with open(cap, "r", encoding="utf-8") as f:
                    captions.append(f.read().strip())
            else:
                captions.append("")
        logging.info(f"[WebtoonPairDataset] 짝 {len(names)}개({names[0]}…{names[-1]}), 크기 맞춤 {changed}, 짝 없는 목표 {len(only_t)} {only_t[:5]} · "
                     f"짝 없는 입력 {len(only_i)} {only_i[:5]}, 캡션 없음 {captions.count('')}")
        return io.NodeOutput(targets, captions, inputs)


class WebtoonTrainExtension(ComfyExtension):
    async def get_node_list(self):
        return [WebtoonTrainLora, WebtoonPairDataset]


async def comfy_entrypoint():
    return WebtoonTrainExtension()
