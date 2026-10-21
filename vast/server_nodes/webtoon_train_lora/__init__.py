"""WebtoonTrainLora: ComfyUI 기본 'Train LoRA'(comfy_extras/nodes_train.py TrainLoraNode)를 그대로 부르되 세 가지만 바꾼 학습 노드.
코어 파일은 고치지 않는다(공용 서버). 학습이 도는 동안만 nodes_train 의 함수 셋을 바꿔 끼우고 끝나면 되돌린다(ComfyUI 는 한 번에 한 작업).

왜(2026-10-21 webtoon-studio 조사, worklogs/2026-10-21-uc-optimize.md): 기본 노드로 학습한 그림체 LoRA 가 거의 안 배워지고 얼룩이 났다.
  1. alpha 가 1 로 고정이라 효과가 alpha/rank(rank 16 이면 1/16) → alpha 를 rank 로(다른 트레이너와 같은 배율).
  2. 블록 밖 전역 층(32 블록이 같이 쓰는 modulation, 시간 임베딩, img_in/proj_out)과 norm·bias 를 배율 없이 통째로 학습한다 →
     transformer_blocks 안의 Linear 만(ai-toolkit·musubi·DiffSynth 와 같은 범위).
  3. 손실이 x0 공간 MSE 라 flow 모델에서는 속도 오차 × σ² 와 같다 → 선·음영이 정해지는 낮은 노이즈 구간이 손실의 약 1% 뿐 →
     σ² 로 나눠 속도 손실과 같게(ComfyUI 이슈 #15847).
"""
import logging

import torch

import comfy_extras.nodes_train as NT
from comfy_api.latest import ComfyExtension, io


def _filtered_setup(bypass: bool, blocks_only: bool, alpha_mode: str):
    def make_adapter(m, n, existing_weights, algorithm, lora_dtype, rank):
        key = f"{n}.weight"
        alpha = float(existing_weights.get(f"{key}.alpha", rank if alpha_mode == "rank" else 1.0))
        dora_scale = existing_weights.get(f"{key}.dora_scale", None)
        params = {}
        existing = None
        for cls in NT.adapters:
            existing = cls.load(n, existing_weights, alpha, dora_scale)
            if existing is not None:
                break
        if existing is not None:
            ad = existing.to_train().to(lora_dtype)
        else:
            ad = NT.adapter_maps[algorithm].create_train(m.weight, rank=rank, alpha=alpha).to(lora_dtype)
        for name, p in ad.named_parameters():
            params[f"{n}.{name}"] = p
        return ad.train().requires_grad_(True), params

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
        logging.info(f"[WebtoonTrainLora] LoRA 를 단 층 {kept} · 뺀 층 {skipped} (blocks_only={blocks_only}, alpha={alpha_mode})")
        return (lora_sd, adapters_all, manager) if bypass else (lora_sd, adapters_all)

    return setup


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
        ]
        return io.Schema(node_id="WebtoonTrainLora", display_name="Train LoRA (webtoon: alpha=rank · 블록만 · 속도 손실)",
                         category="model/training", is_experimental=True, is_input_list=True,
                         inputs=list(base.inputs) + extra, outputs=base.outputs)

    @classmethod
    def execute(cls, alpha_mode, blocks_only, loss_mode, **kw):
        alpha_mode, blocks_only, loss_mode = alpha_mode[0], blocks_only[0], loss_mode[0]
        saved = (NT._setup_lora_adapters, NT._setup_lora_adapters_bypass, NT.TrainSampler.fwd_bwd)
        try:
            NT._setup_lora_adapters = _filtered_setup(False, blocks_only, alpha_mode)
            NT._setup_lora_adapters_bypass = _filtered_setup(True, blocks_only, alpha_mode)
            if loss_mode == "velocity":
                NT.TrainSampler.fwd_bwd = _velocity_fwd_bwd(saved[2])
            return NT.TrainLoraNode.execute(**kw)
        finally:
            NT._setup_lora_adapters, NT._setup_lora_adapters_bypass, NT.TrainSampler.fwd_bwd = saved


class WebtoonTrainExtension(ComfyExtension):
    async def get_node_list(self):
        return [WebtoonTrainLora]


async def comfy_entrypoint():
    return WebtoonTrainExtension()
