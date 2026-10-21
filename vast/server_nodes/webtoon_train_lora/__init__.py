"""WebtoonTrainLora: ComfyUI 기본 'Train LoRA'(comfy_extras/nodes_train.py TrainLoraNode)를 그대로 부르되 세 가지만 바꾼 학습 노드.
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
"""
import inspect
import logging

import torch

import comfy_extras.nodes_train as NT
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


class WebtoonTrainExtension(ComfyExtension):
    async def get_node_list(self):
        return [WebtoonTrainLora]


async def comfy_entrypoint():
    return WebtoonTrainExtension()
