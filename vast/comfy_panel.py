"""vast.ai ComfyUI(Qwen Image 2.1)로 컷 그리기. 힉스필드 판(tools/hf_panel.py)과 같은 컷 설명(panels.json)을 쓴다(크레딧 0, GPU 시간만).
(2026-10-15 사용자 "vastai 에서 이정도 이미지 수준의 웹툰 만들게 워크플로우 세로로"). 힉스필드 결과는 건드리지 않게 panels_hf_vast/ 에 따로 그린다.

    .venv/Scripts/python vast/comfy_panel.py <series_hf_dir> <panels.json> [--episode ep002_v2] [--only p003,p008] [--aspect 2:3] [--turbo]

먼저 한 번: .venv/Scripts/python vast/sheet_refs.py <series_hf_dir>  (시트에서 인물 머리 참고 그림을 잘라 둔다)
모델(서버 models/ 에 Model Manager Neo 로 받아 둠, 없으면 comfy_client.py fetch):
  diffusion_models/qwen-image-2.1-UC-fp8.safetensors · text_encoders/qwen3vl_8b_int8_convrot.safetensors · vae/qwen_image_2.1_vae_bf16.safetensors
  loras/qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors(--turbo)
참고 그림은 프롬프트에서 <image1>…<image10> 으로 부른다(Qwen Image 2.1 규칙, 10장까지). 프롬프트·참고 그림을 고른 이유는 qwen_refs 설명.
RTX 5090 기준 한 컷: 40스텝 약 100~160초, --turbo(8스텝) 약 30~50초.
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402

# 저장 워크플로우를 못 불러 코드로 짤 때의 Qwen 판(UC). 일반 판은 2026-10-19 사용자 요청으로 지웠다(saved_workflow.MODELS)
UNET, CLIP, VAE = "qwen-image-2.1-UC-fp8.safetensors", "qwen3vl_8b_int8_convrot.safetensors", "qwen_image_2.1_vae_bf16.safetensors"
TURBO = "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"  # 공식 Turbo LoRA: 8스텝 cfg 1(시안용, 모델 카드)
# 약 2MP(모델 기본 1MP, 최대 2048x2048), 32 배수. 합성 폭 1600 보다 크게
SIZES = {"9:16": (1088, 1920), "2:3": (1184, 1760), "3:4": (1248, 1664), "1:1": (1440, 1440),
         "4:3": (1664, 1248), "3:2": (1760, 1184), "16:9": (1920, 1088)}
MAX_REFS = 10


PEOPLE_WORDS = re.compile(r"\b(eyes?|hair|faces?|blush|chest|adults?|lash(es)?|chibi|proportions?|skin|body|bodies|lips|nose)\b", re.I)


def strip_people(text: str) -> str:
    """사람 없는 그림(장소·인서트)용: 그림체 글에서 눈·머리·얼굴·몸 묘사만 뺀다(그 말이 사람을 불러냈다, p073·호프집 시안).
    사람 얘기로 시작하는 문장("Eyes: …", "Faces: …")은 통째로, 아니면 그 문장 안의 사람 마디만 뺀다.
    전에는 사람 말이 든 문장을 통째로 빼서 한 문장짜리 그림체(새 작품)가 다 사라져 사진처럼 그려졌다(2026-10-18 옥상 카페 시안)."""
    sents = re.split(r"(?<=\.)\s+", text)
    old = [s for s in sents if not re.search(r"^(Eyes|Hair)\b|face|blush|chest|adult|lash|chibi", s, re.I)]
    if old and old[0] == sents[0]:  # 그림체 첫 문장이 남으면 예전 방식 그대로(D+18·무대 뒤 결과가 바뀌지 않게)
        return " ".join(old)
    out = []
    for sent in sents:
        parts = re.split(r"(?<=[,;:])\s+", sent)
        if PEOPLE_WORDS.search(parts[0]):
            continue
        keep = [p for p in parts if not PEOPLE_WORDS.search(p)]
        out.append(" ".join(keep).rstrip(",;:. ") + ".")
    return " ".join(out)


_SAVED_WARNED = False


def lora_for(series, chars, model=None, edit=False) -> list:
    """이 컷 인물들의 학습한 LoRA(series.json loras, 지금 그리는 판으로 학습한 것만). 모듈을 못 부르면 없음."""
    try:
        from lora import for_panel
        from saved_workflow import pick
        return for_panel(series, chars, model or pick(series)[0], edit)
    except Exception:  # noqa: BLE001
        return []


NO_LORA = False  # --no-lora(같은 컷을 LoRA 없이 견줘 볼 때)

# 클로즈업의 장소 참고 그림 문장. 예전 문장("배경 색·조명만, 흐린 작은 부분만, 방 전체를 그리지 말 것")은 배경을 빈 베이지 벽으로 비웠다
# (2026-10-21 D+18 정본과 견줌: 정본 클로즈업은 뒤에 흐린 밤 사무실·술집 불빛이 꽉 찬다)
CLOSEUP_PLACE_OLD = ("<image{n}> is the place, for the background colors and lighting only: this is a close-up, so only a small, "
                     "softly blurred part of it shows behind the person. Do not draw the whole room.")
CLOSEUP_PLACE = CLOSEUP_PLACE_OLD


def graph(prompt, ref_names, w, h, seed, steps, ref_res, turbo=False, edit=False, series=None, role=None, refine=False, chars=None, control=None):
    """vast 에 저장된 워크플로우(생성 webtoon_vertical_qwen21 · 편집 webtoon_edit_qwen21)를 불러 입력값만 채운다(saved_workflow.py).
    화면에서 고친 워크플로우가 그대로 돈다. 서버에서 못 불러오면 같은 구성을 코드로 짠다(_graph_code, 경고 한 번).
    edit=True: 첫 참고 그림(<image1>)을 고치는 편집. 캔버스는 그 그림 크기(인코더가 내주는 잠재)를 쓴다(w·h 무시).
    series 를 주면 그 작품의 판(series.json model, saved_workflow.pick). role 을 주면(asset_expression · asset_base · candidate_location · candidate_sheet)
    그 판의 그 역할 워크플로우(없으면 기본 판의 것)를 쓴다 — 화면에서 에셋용 설정을 고치면 에셋 도구가 따른다. 그 판의 생성 워크플로우가 참고 그림을 못 받는데(Anima) 참고 그림을
    넘기면(인물 시트 시안처럼 본보기가 꼭 필요한 일) 참고 그림을 받는 UC 판으로 그린다."""
    global _SAVED_WARNED
    try:
        from saved_workflow import fill, load_ui, models, pick, refs_model, takes_refs, to_api, workflow_for
        mid, gen, ed = pick(series)
        ui = load_ui(workflow_for(role, series) if role else (ed if edit else gen))
        if ref_names and not edit and not takes_refs(ui):
            mid = refs_model()
            ui = load_ui(models()[mid]["gen"])
        mp = ((ui.get("extra") or {}).get("webtoon") or {}).get("max_pixels")  # 판이 학습된 크기(Anima 는 약 100만 화소). 더 큰 캔버스는 같은 장면을 타일처럼 반복해 그렸다(2026-10-19 외출 p112 4분할)
        if mp and w * h > mp:
            k = (mp / (w * h)) ** 0.5
            w, h = max(512, int(w * k) // 32 * 32), max(512, int(h * k) // 32 * 32)
        # 인물 LoRA 는 컷 생성에만(고치기·에셋·시안은 그대로). 그 판으로 학습한 것만 붙는다(lora.for_panel)
        # 그림체 LoRA 는 사람 없는 컷에도 붙어서 chars 가 없어도 묻는다
        draw_role = role in (None, "layout_lock")  # 배치 고정도 컷 그리기라 인물·그림체 LoRA 를 붙인다
        # 앞 컷에서 잇기(편집 경로)도 컷 그리기다: 같은 모델이라 그리기로 학습한 인물 LoRA 가 그대로 붙는다(2026-10-20 조사, 빠져 있던 연결)
        if role == "next_scene" and chars and not NO_LORA:
            lz = [x for x in lora_for(series, chars, mid, edit=True) if x["who"] != "_style"]
        else:
            lz = lora_for(series, chars, mid) if not edit and draw_role and not NO_LORA else None
        g = fill(to_api(ui, turbo=turbo, refine=bool(refine), loras=lz), prompt, ref_names, w, h, seed, steps, ref_res)
        if control:  # 배치 기준 컷(올린 이름): 선 따기(Canny) 앞의 크기 맞추기·불러오기 노드에 넣는다(노드 번호가 아니라 연결로 찾는다)
            cny = next(k for k, v in g.items() if v["class_type"] == "Canny")
            sc = g[cny]["inputs"]["image"][0]
            if g[sc]["class_type"] == "ImageScale":
                g[sc]["inputs"].update(width=w, height=h)
                sc = g[sc]["inputs"]["image"][0]
            g[sc]["inputs"]["image"] = control
        return g
    except Exception as e:  # noqa: BLE001
        if not _SAVED_WARNED:
            print(f"[경고] 저장 워크플로우를 못 불러 코드 그래프로 그림: {type(e).__name__}: {e}", file=sys.stderr)
            _SAVED_WARNED = True
        return _graph_code(prompt, ref_names, w, h, seed, steps, ref_res, turbo, edit)


def copied_ref(img: Path, refs: list, limit: float = 8.0):
    """결과가 참고 그림을 거의 그대로 옮긴 것이면 그 참고 그림(아니면 None). ComfyUI 0.42.x 에서 프롬프트를 무시하고 참고 그림을
    픽셀 평균 5~6/255 차이로 내놓는 버그가 오류 없이 나고 재시작 전까지 계속된다(github.com/comfyanonymous/ComfyUI/issues/17187, 2026-10-21 조사)."""
    from PIL import Image as _Im, ImageChops, ImageStat
    out = _Im.open(img).convert("L").resize((256, 256))
    for r in refs:
        try:
            ref = _Im.open(r).convert("L").resize((256, 256))
        except Exception:  # noqa: BLE001
            continue
        if ImageStat.Stat(ImageChops.difference(out, ref)).mean[0] < limit:
            return Path(r)
    return None


def _graph_code(prompt, ref_names, w, h, seed, steps, ref_res, turbo=False, edit=False):
    """저장 워크플로우와 같은 구성을 코드로 짠 것(서버 워크플로우를 못 불러올 때만)."""
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "negative_prompt": "", "resolution": ref_res}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": ["6", 0], "seed": seed,
                                                    "steps": steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "webtoon_studio/panel"}},
    }
    for k, name in enumerate(ref_names, 1):
        g[f"1{k:02d}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["5"]["inputs"][f"images.image_{k}"] = [f"1{k:02d}", 0]
    if turbo:
        g["10"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": TURBO, "strength_model": 1.0}}
        g["4"]["inputs"]["model"] = ["10", 0]
    if edit:
        del g["6"]
        g["7"]["inputs"]["latent_image"] = ["5", 2]
    return g


# 판단하는 모델(표정 칸 고르기·남는 사람 찾기). 그림 검수(mcp/vision_review.py)와 같은 환경변수, 기본은 사내 vLLM(키 없음)
LLM_URL = os.environ.get("VLM_URL", "https://judge-gateway.invalid/v1").rstrip("/")
LLM_MODEL = os.environ.get("VLM_MODEL", "qwen3.8-27b")


def pick_expr(series, cid, name, text):
    """컷 연기 글에 가장 가까운 표정 칸 번호(1~12)를 사내 Qwen 이 고른다. 이 인물 얘기가 없거나 맞는 칸이 없으면 None.
    (2026-10-15 사용자 "연기 강도 강하게": Qwen Image 는 참고 얼굴의 표정을 그대로 따라 그린다 → 맞는 칸을 넣으면 그 연기가 나온다)"""
    jf = series / "assets" / "vast_refs" / f"{cid}_expr.json"
    if not jf.exists() or not text:
        return None
    meta = json.loads(jf.read_text(encoding="utf-8"))
    # 표정 시트 12칸 + make_assets.py 로 늘린 센 표정(extra)
    files = [f"{cid}_expr_{k:02d}.png" for k in range(1, len(meta["labels"]) + 1)] + [x["file"] for x in meta.get("extra", [])]
    labels = meta["labels"] + [x["label"] for x in meta.get("extra", [])]
    # 연기 글이 우는 걸 말하지 않으면 우는 칸은 후보에서 뺀다. 프롬프트로 일러도 "dry hopeful eyes" 에 눈물 칸을 골랐다(2026-10-18 추몽 p067)
    said = re.sub(r"\b(no|without|dry|not)\s+(\w+\s+)?(tears?|crying|cry)\b", "", text, flags=re.I)
    if not re.search(r"\b(tears?|tearful|crying|cries|cry|sob\w*|weep\w*)\b", said, re.I):
        keep = [k for k, lb in enumerate(labels) if not re.search(r"tear|cry|crying|sob|weep|welling", lb, re.I)]
        files, labels = [files[k] for k in keep], [labels[k] for k in keep]
    # 홍조도 같다: 연기 글이 붉어진다고 하지 않으면 볼·귀가 빨간 칸을 뺀다. 마흔여섯 재석이 다락에서 얼굴이 빨갛게 나왔다(2026-10-18 추몽 사용자 지적)
    # 명시적으로 붉어진다고 쓸 때만(embarrassed·shy 로는 열지 않는다: 외출 p080 "a little shy" 에 마흔 상수가 볼이 빨갛게 나왔다)
    if not re.search(r"\b(blush\w*|flush\w*|red(dened)? (face|cheeks))\b", text, re.I):
        keep = [k for k, lb in enumerate(labels) if not re.search(r"blush|red ears|red cheeks|flush|fluster|shy|embarrass", lb, re.I)]
        files, labels = [files[k] for k in keep], [labels[k] for k in keep]
    ask = (f"A webtoon panel describes this acting:\n{text}\n\nWhich of these expressions of {name} is the closest match for {name}'s face in this panel?\n"
           + "\n".join(f"{i}. {lb}" for i, lb in enumerate(labels, 1))
           + f"\n\nAnswer with the number only. Answer 0 if {name} is not the one acting or nothing fits. "
             "Choose a tearful or crying expression only if the acting text says the character is crying or has tears "
             "(phrases like 'no tears' or 'dry eyes' mean NOT crying); for quiet, calm or "
             "restrained acting choose a matching quiet expression if the list has one, never a strong or exaggerated one; otherwise answer 0.")
    # 2026-10-18 추몽: "조용히 듣기만" 에 눈물 칸을 골라 은주·재석이 울었다(p059·p069). 2026-10-19 외출: 센 표정만 있어 "calm and confident" 에
    # 우쭐한 미소(smug smirk)가 골라졌다 → 잔잔한 표정 묶음(make_assets.py subtle)이 있으면 그걸 고르게 했다
    body = {"model": LLM_MODEL, "temperature": 0.0, "max_tokens": 8, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": ask}]}
    req = urllib.request.Request(LLM_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            m = re.search(r"\d+", json.loads(r.read().decode())["choices"][0]["message"].get("content") or "")
    except Exception as e:
        print(f"  표정 고르기 실패({cid}): {str(e)[:80]}")
        return None
    k = int(m.group()) if m else 0
    return (files[k - 1], labels[k - 1]) if 1 <= k <= len(labels) else None


def panel_text(p) -> str:
    """검사가 그림과 맞대는 이 컷의 글(샷·일어나는 일·연기·카메라·이름 없는 사람들). 장면 배치(SPACE)는 장면 전체 얘기라 뺀다.
    검사는 이 글대로인지를 판정 모델이 읽고 본다. 검사 쪽에 '독자를 보면 안 됨' 같은 고정 규칙이나 문구 판별을 두지 않는다:
    시점 컷·뒷모습·어깨 너머·옷·표정 같은 지시는 표현이 끝없이 다양해서, 문구로 가르면 조금만 달라도 틀렸다
    (2026-10-19 D+18 p022 시점 컷을 '독자를 쳐다봄'으로 열 번 걸었다. 사용자 "특정 문구를 정규식으로 찾는 건 땜질")."""
    action = re.sub(r"\s*SPACE \(identical in every panel of this scene\):.*$", "", str(p.get("action") or ""), flags=re.S)
    parts = [f"Shot: {p.get('shot')}." if p.get("shot") else "", action, f"Acting: {p['acting']}." if p.get("acting") else "",
             f"Camera: {p['camera']}." if p.get("camera") else "", f"Also in the panel: {p['extras']}." if p.get("extras") else ""]
    return " ".join(x for x in parts if x)


def _shot_class(p: dict) -> str:
    s = (p.get("shot") or "").lower()
    return "insert" if any(w in s for w in ("insert", "detail", "top view")) else "close" if "close" in s else "wide" if "wide" in s else "medium"


CONT_MADE: set = set()  # 이번 실행에서 앞 컷에서 이어 그린 컷: 거기서 또 잇지 않는다(이어 쓰기가 쌓이면 두 번째부터 그림이 거칠어졌다, 2026-10-20 D+18 p017)


def continuity(series, ep: Path, out: Path, p: dict, mid: str, closeups: bool = False):
    """같은 장면 앞 컷을 이용할지(2026-10-20 보조 도구 시험 → 사용자 "진행"). (방식, 기준 그림, 기준 컷) 또는 None.
    next_scene: 바로 앞 컷이 같은 장소·같은 한 사람·같은 비율의 클로즈업 → 그 컷에서 잇는다(얼굴·옷·배경·그림체 유지, Next-Scene LoRA).
    layout_lock: 같은 장소가 이어지는 앞 컷(여덟 컷 안) 중 같은 인물 묶음·같은 샷 종류(넓은·미디엄) → 그 컷의 선으로 사람 자리·테이블 배치 고정.
    그 판에 그 역할 워크플로우가 있을 때만(지금은 Qwen UC). 앞 컷 그림은 이번 결과 폴더와 본편 중 새것."""
    from saved_workflow import models
    own = (models().get(mid) or {}).get("own_roles", [])
    if "next_scene" not in own and "layout_lock" not in own:
        return None
    sb = ep / "storyboard.json"
    d = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    spec = {q["id"]: q for q in (d["panels"] if isinstance(d, dict) else d)}
    board = {b["id"]: b for b in json.loads(sb.read_text(encoding="utf-8"))["panels"]} if sb.exists() else {}
    ids = [i for i in board if i in spec] if board else list(spec)
    if p["id"] not in ids:
        return None
    k = ids.index(p["id"])
    # 장면이 끊기는 곳: 시간 자막(상자 내레이션 '수요일'·'일주일 뒤'·'밤 10시')이 붙은 컷부터는 새 장면이다. 같은 장소가 이어져도
    # 월요일 사무실(p226) 다음 수요일(p228)처럼 날이 바뀐다(2026-10-20 사용자 "장소가 바뀌어야 하는 씬이면 그것도 고려하나")
    def time_cut(i):
        return any(dd.get("box") for dd in (board.get(i) or {}).get("dialogue") or [])

    def same_outfit(q):  # 같은 인물의 옷이 다르면 다른 때다(같은 방에서 밤 → 다음 날, p028 → p210)
        oa, ob = q.get("outfit") or {}, p.get("outfit") or {}
        return all(oa.get(c) == ob.get(c) for c in set(oa) & set(ob))

    def img(q):
        fs = [f for f in (out / f"{q}.png", ep / "panels_hf" / f"{q}.png") if f.exists()]
        return max(fs, key=lambda f: f.stat().st_mtime) if fs else None

    def solo_close(f) -> bool:
        """그림이 실제로 한 사람 클로즈업인가(얼굴 하나, 얼굴 높이가 그림의 18% 이상). 컷 설명은 클로즈업인데 그림은 세 사람 어깨 너머
        컷인 기준 컷(D+18 p008)에서 이으니 다음 컷도 그 세 사람 구도로 나왔다(2026-10-20): 잇기는 기준 그림의 구도를 따라간다"""
        try:
            from PIL import Image as _Im
            from face_detect import detect
            im = _Im.open(f).convert("RGB")
            faces = [b for b in detect(im) if (b[3] - b[1]) / im.height > 0.06]  # 뒤쪽의 아주 작은 얼굴은 셈하지 않는다
            return len(faces) == 1 and (faces[0][3] - faces[0][1]) / im.height >= 0.18
        except Exception:  # noqa: BLE001
            return False

    chars, sc = p.get("chars") or [], _shot_class(p)
    if time_cut(p["id"]):  # 이 컷이 새 장면의 첫 컷
        return None
    if closeups and "next_scene" in own and len(chars) == 1 and sc == "close":
        # 별 모양: 같은 장면 안에서 그 사람의 가장 가까운 '보통으로 그린' 클로즈업에서 각각 잇는다(사슬로 이으면 두 번째부터 거칠어졌다.
        # 조사: 기준 컷 하나에서 각각 파생하는 쪽이 낫다). 주고받는 대화(A·B·A·B)면 A 는 앞의 A 클로즈업에서
        for j in range(k - 1, max(-1, k - 7), -1):
            q = spec[ids[j]]
            if q.get("location") != p.get("location") or not same_outfit(q):
                break
            if ((q.get("chars") or []) == chars and _shot_class(q) == "close" and q.get("aspect") == p.get("aspect")
                    and img(q["id"]) and q["id"] not in CONT_MADE and solo_close(img(q["id"]))):
                return "next_scene", img(q["id"]), q["id"]
            if time_cut(q["id"]):
                break
    if "layout_lock" in own and len(chars) >= 2 and sc in ("wide", "medium"):
        for j in range(k - 1, max(-1, k - 9), -1):
            q = spec[ids[j]]
            if q.get("location") != p.get("location") or not same_outfit(q):
                break
            if set(q.get("chars") or []) == set(chars) and _shot_class(q) == sc and img(q["id"]):
                return "layout_lock", img(q["id"]), q["id"]
            if time_cut(q["id"]):  # 이 컷부터가 이 장면: 더 앞으로 가지 않는다
                break
    return None


def gen_takes_refs(series) -> bool:
    """이 작품의 생성 판이 참고 그림을 받는가(Anima 는 못 받는다). 못 불러오면 받는다고 본다(코드 그래프는 Qwen)."""
    try:
        from saved_workflow import load_ui, pick, takes_refs
        return takes_refs(load_ui(pick(series)[1]))
    except Exception:  # noqa: BLE001
        return True


def qwen_refs(series, p, episode=None, front=False, expr=False, turned=False, text_only=False):
    """같은 컷 설명(panels.json)으로 Qwen 용 짧은 프롬프트와 참고 그림을 만든다. 2026-10-15 시험에서 정한 것:
    - 힉스필드 프롬프트(약 4천 자, 맥락·규칙·연기 일반론)를 그대로 쓰면 연기가 묻힌다(p045 울 컷이 무표정, 짧게 쓰니 눈물·입술 깨묾이 나옴)
      → 컷·연기·동작·옷·카메라를 앞에, 그림체는 뒤에, 규칙은 한 줄
    - 시트·옷 시트·표정 시트를 통째로 넣으면 그 배치를 베낀다(p003 인물 줄 세움, p045 표정 칸 그대로)
      → 인물은 시트 정면 전신의 머리만(vast/sheet_refs.py, 무표정·옷 거의 안 보임). 얼굴 칸은 웃음이 섞여 그 표정을 따라 그렸고,
        상반신·전신은 시트 기본 옷을 입은 사람이 한 명 더 생겼다
    - 앞 컷(refs_extra)을 넣으면 그 컷 구도를 통째로 가져와 사람이 늘어난다(p008) → 넣지 않는다
    - 장소를 맨 앞 <image1>(결과 구도가 첫 그림을 따른다)
    - 글 속 이름 옆에 그림 번호를 붙여 묶는다. 시드에 따라 한 명이 더 생기는 판이 있으니(p003 시드 1234) 이상하면 시드만 바꿔 다시"""
    from hf_panel import names
    NAMES = names(series)
    refs, lines = [], []
    # 클로즈업은 장소를 맨 뒤로: 맨 앞에 두면 그 방 구도를 따라 넓은 컷이 되고 인물이 한 번 더 나왔다(p079 얼굴 꽉 찬 컷이 방 전경 + 재민 둘)
    # 인서트·탑뷰·디테일도 장소를 앞에 두면 그 넓은 구도를 따라 그렸다(2026-10-18 추몽 p017·p018·p068·p078) → 클로즈업처럼 뒤에
    close = any(w in (p.get("shot") or "") for w in ("close-up", "insert", "top view", "detail"))
    # 인서트(손·소품 접사)엔 장소 그림을 아예 넣지 않는다: 뒤에 '배경 색만'이라 붙여도 방 전체를 그려, 앞 컷에 누워 있던 상수의 침대가 비어
    # 사람이 사라진 꼴이 됐다(2026-10-18 외출 p021·p025 사용자 지적)
    insert = any(w in (p.get("shot") or "") for w in ("insert", "top view", "detail"))
    # 장소 "none"(합성 재료·회상·풍경처럼 정해진 장소 그림이 없는 컷)은 장소 그림을 찾지 않는다: 없는 none.png 를 찾다 그 컷이 '참고 그림 없음'으로 빠졌다(2026-10-19 외출 p345·p419)
    loc = (series / "assets" / "locations" / f"{p['location']}.png"
           if p.get("location") and p["location"] != "none" and not insert else None)
    # view: 같은 장소를 반대편에서 본 그림(<장소>_reverse.png). 장소 그림이 앞(스크린 쪽) 한 장뿐이라 듣는 사람 컷 뒤에도 스크린을 그렸다
    # (2026-10-19 외출 p052·p078 사용자 "회의를 보는 자의 앞모습으로 보여야, 배경이 아니라")
    if loc and p.get("view"):
        alt = loc.with_name(f"{p['location']}_{p['view']}.png")
        loc = alt if alt.exists() else loc
    if text_only:  # 글만 받는 판(Anima): 장소·인물은 그림 대신 글로(아래 PLACE·PEOPLE)
        loc = None
    if loc and not close:
        refs.append(loc)
        lines.append(f"<image{len(refs)}> is the location: draw the panel inside this same place with the same furniture, colors and lighting.")
    bind = {}
    # 뒷모습·옆모습 컷이면 캐릭터 시트에서 자른 그 방향 그림을 더 준다(make_assets turn → <id>_view_back·side). 정면 참고 그림만 주면 뒷모습의
    # 머리 모양·옷 뒤판을 지어냈다. 방향은 컷 설명의 카메라 칸이 정한다(panel-spec 스킬이 'seen from behind'·'profile' 로 적는다)
    cam = " ".join(str(p.get(k) or "") for k in ("camera", "shot")).lower()
    view_kind = "back" if any(w in cam for w in ("from behind", "back view", "back to the camera", "seen from the back")) else (
        "side" if any(w in cam for w in ("profile", "side view")) else None)
    for cid in ([] if text_only else p.get("chars", [])):
        vf = series / "assets" / "vast_refs" / f"{cid}_view_{view_kind}.png" if view_kind else None
        if vf is not None and vf.exists() and (len(p.get("chars") or []) == 1 or re.sub(r"\s*\(.*?\)", "", NAMES[cid]).split()[-1].lower() in cam):
            refs.append(vf)
            lines.append(f"<image{len(refs)}> shows {NAMES[cid]} from the {view_kind}: use it for how the hair, build and clothes look from that side.")
        # --expr: 연기에 맞는 표정 칸을 얼굴 참고로(그 표정을 따라 그린다). 없으면 무표정 머리(얼굴 칸 첫 장은 웃음이라 그 표정을 따라 그렸다, p045)
        # turned: 독자를 봐서 다시 그릴 때. 정면 표정 칸·정면 머리가 고개 방향까지 끌고 갔다(2026-10-18 추몽 p016) → 시트의 3/4 머리만 넣는다
        turn = series / "assets" / "vast_refs" / f"{cid}_turn.png"
        if turned and turn.exists():
            refs.append(turn)
            bind[cid] = len(refs)
            lines.append(f"<image{len(refs)}> is {NAMES[cid]}'s head in three-quarter view (identity only: same face, eyes, hairstyle and hair color).")
            continue
        pk = pick_expr(series, cid, NAMES[cid], p.get("acting") or p["action"]) if expr else None
        if pk:
            refs.append(series / "assets" / "vast_refs" / pk[0])
            bind[cid] = len(refs)
            lines.append(f"<image{len(refs)}> is {NAMES[cid]}'s face with the expression to act in this panel ({pk[1]}): give {NAMES[cid]} this exact "
                         "facial expression (eyebrows, eyelids, eyes, mouth) and the same face and hair, but not its crop, background, outfit or "
                         "head direction: the reference faces front only because it is a reference sheet.")
            print(f"  {p['id']} {cid}: 표정 칸 {pk[0]} {pk[1]}", flush=True)
            continue
        kinds = [("bust", "neutral bust portrait (identity only: same face, eyes, hairstyle and hair color)")]
        if front:
            kinds.append(("front", "full body (identity and body proportions only, not the outfit)"))
        for kind, what in kinds:
            f = series / "assets" / "vast_refs" / f"{cid}_{kind}.png"
            if f.exists():
                refs.append(f)
                bind.setdefault(cid, len(refs))
                lines.append(f"<image{len(refs)}> is {NAMES[cid]}'s {what}.")
    if loc and close:
        refs.append(loc)
        lines.append(CLOSEUP_PLACE.format(n=len(refs)))
    chars = p.get("chars", [])
    n = len(chars)
    if n and refs:
        lines.append("The person references only show who each person is. Do NOT copy their crop, plain background, outfit, pose or expression.")
    people = place = ""
    if text_only:  # 참고 그림이 없으니 작품 설정의 생김새(who)·장소 설명을 글로 넣는다. 컷마다 같은 문장이라 얼굴이 덜 흔들린다
        sj = json.loads((series / "series.json").read_text(encoding="utf-8")) if (series / "series.json").exists() else {}
        cast = sj.get("cast") or {}
        people = "; ".join(f"{re.sub(r'\s*\(.*?\)', '', NAMES[c])}: {cast.get(c, {}).get('who', '')}" for c in chars if c in NAMES)
        place = str((sj.get("locations") or {}).get(p.get("location") or "", "")) if not insert else ""
    # 참고 얼굴이 모두 정면이라 거의 모든 컷이 독자를 쳐다봤다(2026-10-18 추몽 사용자 "왜 시점을 자꾸 독자 보는 거로 하냐").
    # 시선은 컷 글이 정한 대로, 글에 없을 때만 행동의 상대를 본다. 시점 컷인지는 그리기 모델이 글을 읽고 판단한다: 문구를 찾아 가르던 판별은
    # 표현이 조금만 달라도 틀렸다(2026-10-19 D+18 p022 "faces the viewer, who sits across the table" 을 놓침, 사용자 "문구 정규식은 땜질")
    gaze = ("GAZE: every person looks where the panel text says. Where the text does not say, their eyes, head and body turn toward the "
            "person or thing they are dealing with in this moment, like a candid film still, not toward the camera." if n else "")
    action = p["action"]
    # 장면 전체 배치(SPACE)는 넓은 컷용. 클로즈업에 두면 그 방 배치를 배경에 통째로 그려 인물이 또 나왔고(p079),
    # 어깨 너머 숏(camera)에 두면 앞사람 뒷모습과 배치 속 그 사람이 따로 그려져 두 번 나왔다(p075 재민). 카메라 칸에 위치·시선이 다 있다
    if close or p.get("camera"):
        action = re.sub(r"\s*SPACE \(identical in every panel of this scene\):.*$", "", action, flags=re.S)
    # 학습한 LoRA 가 있는 인물은 이름 옆에 그 trigger 를 붙인다(학습 캡션 맨 앞 낱말, lora.build_data)
    trig = {} if NO_LORA else {x["who"]: x["trigger"] for x in lora_for(series, p.get("chars", [])) if x.get("trigger")}
    for cid, k in bind.items():
        short = re.sub(r"\s*\(.*?\)", "", NAMES[cid]).split()[-1]
        tag = f"{trig[cid]}, " if cid in trig else ""
        action = re.sub(rf"\b{re.escape(short)}\b", f"{short} ({tag}the person in <image{k}>)", action, count=1)
    for cid in [c for c in trig if c not in bind and c in NAMES]:
        short = re.sub(r"\s*\(.*?\)", "", NAMES[cid]).split()[-1]
        action = re.sub(rf"\b{re.escape(short)}\b", f"{short} ({trig[cid]})", action, count=1)
    outfit = " ".join(f"{NAMES[c]} wears: {o}." for c, o in (p.get("outfit") or {}).items())
    sp = series / "prompts" / "style.txt"
    style = (sp if sp.exists() else series / "prompts" / "style_v2.txt").read_text(encoding="utf-8").strip()
    # 컷 설명의 선택 칸(2026-10-18 추몽 1화: 시대·화자마다 색, 이름 없는 군중, 그림 속 글자):
    #   look = 이 컷의 색·질감(예: 1996 세피아 + 필름 그레인), extras = 이름 없는 사람들(줄 선 사람·반 아이들), text_in_image = 그려도 되는 글자(삐삐 액정 8282)
    extras = (p.get("extras") or "").strip()
    if not n and not extras:  # 사람 없는 컷: 그림체 글의 눈·머리·얼굴·몸 묘사가 사람을 불러냈다(p073 호텔 문 인서트에 여자 둘) → 선·색·빛 문장만
        style = strip_people(style)
    extra = series / "prompts" / "rules_extra.txt"
    if n:
        # 어깨 너머 앞사람처럼 CAMERA 글이 따로 말하는 사람은 그 글대로(문구로 가르지 않는다)
        count = (f"Exactly {n} {'person' if n == 1 else 'people'}" + (" from the references" if extras else " in the panel")
                 + (" plus anyone else the CAMERA text describes" if p.get("camera") else "") + ", each appearing only once."
                 + (f" Besides them only these unnamed people: {extras}." if extras else
                    " No other people anywhere in the frame, not even small in the background." if n == 1 and not p.get("camera") else ""))
    else:
        count = f"No main characters. Only these unnamed people: {extras}." if extras else "No people in the panel."
    sj = series / "series.json"  # 작품마다 사람 규칙(기본은 성인 작품용 문장, 학생이 나오는 작품은 series.json 의 people_rule)
    people_rule = (json.loads(sj.read_text(encoding="utf-8")).get("people_rule") if sj.exists() else None) or "Adults with realistic proportions."
    # 시대 규칙(series.json era_rule): 2008년 작품에 스마트폰이 일곱 컷 나왔다(2026-10-18 외출 1화) → 그리기 지시에 시대 소품을 박는다
    sjd = json.loads(sj.read_text(encoding="utf-8")) if sj.exists() else {}
    era_rule = sjd.get("era_rule") or ""
    # 연기 세기는 작품마다(series.json acting_rule). 고정 문장 '강하고 즉시 읽히는 연기'가 잔잔한 표정 작품(외출)과 부딪혔다
    acting_rule = sjd.get("acting_rule") or "strong, instantly readable webtoon expression and body language"
    if era_rule:
        people_rule = f"{people_rule} {era_rule}"
    # 시대 소품 중 휴대폰 규칙은 컷 설명에 휴대폰이 나올 때만 붙인다. 늘 붙였더니 열세 컷에서 설명에 없는 휴대폰을 손에 쥐여 줬다(외출 1화)
    if sjd.get("era_phone") and re.search(r"\bphone", " ".join(str(p.get(k) or "") for k in ("action", "acting", "camera", "extras")), re.I):
        people_rule = f"{people_rule} {sjd['era_phone']}"
    text_rule = (f"One single panel: no speech bubbles, no sound effects, no panel border. The only readable text drawn in the picture: {p['text_in_image']}; "
                 "no other letters, signs or logos." if p.get("text_in_image") else
                 "One single panel: no text, no speech bubbles, no sound effects, no panel border.")
    # 인서트 배경을 늘 '어두운 흐림'으로 적었더니 낮 호프의 건배 컷이 밤처럼 나왔다(2026-10-19 외출 p110·p119) → 그 컷의 때와 빛을 따른다
    framing = ("FRAMING: a macro close-up. The object or hands fill most of the frame; behind them only a soft out-of-focus blur in the colors "
               "and light of the place and time the panel text describes. "
               "No room overview, no furniture layout, no bed, no people." if insert else
               "FRAMING: a tight close-up. The head and shoulders fill most of the frame; only a small, softly blurred part of the place shows."
               if "close-up" in (p.get("shot") or "") else "")
    # 그림체 LoRA(lora.STYLE)가 있으면 그 trigger 로 시작한다(학습 캡션 'trigger, a Korean webtoon panel. …' 과 같은 머리)
    head = f"{trig['_style']}, a Korean webtoon panel" if trig.get("_style") else "Korean webtoon panel"
    parts = [f"{head}, {p.get('shot', 'medium shot')}.", framing,
             f"ACTING ({acting_rule}): {p['acting']}" if p.get("acting") else "",
             action, outfit, f"PEOPLE (draw each person exactly like this): {people}." if people else "",
             f"PLACE: {place}" if place else "", f"CAMERA: {p['camera']}" if p.get("camera") else "", gaze, count,
             ("REFERENCES: " + " ".join(lines)) if lines else "", style,
             f"COLOR AND TEXTURE OF THIS PANEL: {p['look']}" if p.get("look") else "",
             text_rule
             + (" " + people_rule + (" " + extra.read_text(encoding="utf-8").strip() if extra.exists() else "") if n or extras else
                " An empty scene: no people, no faces, no figures, no silhouettes, no reflections of people.")]
    prompt = "\n\n".join(x for x in parts if x)
    if text_only:  # 글만 받는 판이 못 알아듣는 문구는 그 판의 워크플로우 데이터(extra.webtoon.prompt_drop)에 적는다. 그림체 글의 '(identical in every image)' 가
        # 작은 글 인코더에게 여러 장짜리 세트로 읽혀 클로즈업마다 4분할로 그렸다(2026-10-19 Anima, 두 시드로 확인). 작품의 style 파일은 건드리지 않는다
        try:
            from saved_workflow import models, pick
            wmeta = models()[pick(series)[0]].get("meta") or {}
            for ph in wmeta.get("prompt_drop", []):
                prompt = prompt.replace(ph, "")
            # 태그 머리말(판 데이터 extra.webtoon.prompt_template): 품질 접두 + 샷 종류 태그 + 인원 태그. Anima 는 '태그 + 문장 두 개 이상'을 권장하고(모델 카드),
            # 문장만 주면 클로즈업이 중간 샷으로, 한 명이어야 할 컷에 두 명이 나왔다. 샷 종류는 norm_shot 이 정한 몇 가지 값이라 매핑은 데이터다
            tpl = wmeta.get("prompt_template") or {}
            if tpl:
                head = [tpl.get("prefix", ""), (tpl.get("shot_tags") or {}).get(p.get("shot") or "", ""), (tpl.get("count_tags") or {}).get(str(min(n, 2)), "")]
                head = ", ".join(x.strip(" ,") for x in head if x.strip(" ,"))
                prompt = head + "\n\n" + prompt if head else prompt
        except Exception:  # noqa: BLE001
            pass
    return prompt, refs[:MAX_REFS]


def upload(c, path: Path, cache: dict) -> str:
    """같은 시트를 컷마다 다시 올리지 않게 내용 해시로 이름을 붙인다(서버 input/ 에 webtoon_<해시>.png)."""
    h = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    if h not in cache:
        cache[h] = c.upload_image(path, f"webtoon_{h}{path.suffix}")
    return cache[h]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("panels_json")
    ap.add_argument("--episode", default="ep001")
    ap.add_argument("--only")
    ap.add_argument("--aspect", help="모든 컷을 이 비율로(세로 웹툰 시안은 2:3·9:16). 없으면 컷 설명의 aspect")
    ap.add_argument("--steps", type=int, help="기본 40(공식 40~50, euler), --turbo 면 8")
    ap.add_argument("--turbo", action="store_true", help="Turbo LoRA 8스텝 시안(빠름)")
    ap.add_argument("--expr", action="store_true", help="연기에 맞는 표정 칸(표정 시트)을 얼굴 참고로(사내 Qwen 이 고름)")
    ap.add_argument("--fix", action="store_true", help="그린 뒤 남는 사람(중복·낯선 사람)을 그림 보는 사내 Qwen 이 찾아 편집으로 지움(원본은 _stage1/)")
    ap.add_argument("--check", action="store_true", help="연출 확인: 그림 검수(인원·좌우시선·옷·표정연기·배경)로 보고, 표정·옷만 틀리면 편집, "
                                                         "좌우·시선·배경·인원이 틀리면 시드를 바꿔 다시(나은 쪽을 남김)")
    ap.add_argument("--retries", type=int, default=1, help="--check 에서 다시 그리는 횟수")
    ap.add_argument("--ref-res", type=int, default=1024, help="참고 그림을 이 크기 정사각 면적으로 줄여 넣는다(0 = 원본)")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--variant", default="vast", help="결과 폴더 panels_hf_<이름>/")
    ap.add_argument("--refine", action="store_true", help="2차 다듬기(잠재 확대 + 낮은 강도로 한 번 더): 얼굴·선이 또렷해지지만 컷당 시간이 약 1.5배")
    ap.add_argument("--model", help="이번만 이 판으로 그린다(서버 저장 워크플로우 webtoon_vertical_<판>, MCP draw_models 가 목록을 준다). 없으면 작품 설정 series.json model")
    ap.add_argument("--no-lora", action="store_true", help="학습한 인물 LoRA(series.json loras)를 이번엔 붙이지 않는다(견줘 보기)")
    ap.add_argument("--no-continuity", action="store_true", help="같은 장면 앞 컷 이용(배치 고정·클로즈업 잇기)을 모두 끈다")
    ap.add_argument("--continue-closeups", action="store_true",
                    help="같은 사람 연속 클로즈업을 앞 컷에서 잇기(Next-Scene)도 쓴다. 기본은 배치 고정만: 잇기는 2026-10-20 시험에서 성공(D+18 p015)과 "
                         "실패(이어 쓰면 거친 결 p017, 얼굴 얼룩 외출 p356)가 섞였다")
    args = ap.parse_args()
    global NO_LORA
    NO_LORA = args.no_lora
    forced = (args.model or "").strip().lower() or None  # 이번 작업에 지정한 판. 없으면 컷마다 router 가 고른다
    series = Path(args.series_dir)
    panels = json.loads(Path(args.panels_json).read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    out = series / "episodes" / args.episode / f"panels_hf_{args.variant}"
    pdir = series / "prompts" / "panels" / args.episode / args.variant
    out.mkdir(parents=True, exist_ok=True)
    pdir.mkdir(parents=True, exist_ok=True)
    c = Comfy()
    have = {f: set(c.req(f"/models/{f}")) for f in ("diffusion_models", "text_encoders", "vae")}
    miss = [n for f, n in (("diffusion_models", UNET), ("text_encoders", CLIP), ("vae", VAE)) if n not in have[f]]
    if miss:
        sys.exit(f"서버에 모델이 없다: {miss} (vast/comfy_client.py downloads 로 받는 중인지 확인)")
    cache, failed = {}, []
    from router import route
    for p in panels:
        if only and p["id"] not in only:
            continue
        if p.get("reuse") or not p.get("action"):
            continue
        # 컷마다 판을 고른다(vast/router.py: 지정 → 작품 그림체·그림체 LoRA → 인물 컷은 참고 그림을 받는 판). saved_workflow.pick 이 환경변수를 먼저 본다
        mid, why = route(series, p, forced)
        os.environ["WEBTOON_MODEL"] = mid
        text_only = not gen_takes_refs(series)  # 고른 판이 글만 받으면(Anima·MOSS) 장소·인물을 글로, 편집은 참고 그림을 받는 판
        print(f"{p['id']}: 판 {mid} ({why})", flush=True)
        prompt, refs = qwen_refs(series, p, expr=args.expr and not text_only, text_only=text_only)
        if any(not r.exists() for r in refs):
            print(f"{p['id']}: 참고 그림 없음 {[str(r) for r in refs if not r.exists()]}")
            failed.append(p["id"])
            continue
        aspect = args.aspect or p.get("aspect", "16:9")
        w, h = SIZES[aspect]
        if args.aspect and args.aspect != p.get("aspect"):
            prompt += f"\n\nFRAMING: this panel is a tall vertical {aspect} webtoon frame; compose the shot for a vertical frame."
        (pdir / f"{p['id']}.txt").write_text(prompt, encoding="utf-8")
        seed = args.seed if args.seed is not None else random.randrange(2**48)
        t0 = time.time()
        dst = out / f"{p['id']}.png"
        log = lambda m: print(m, flush=True)  # noqa: E731

        cont = None if args.no_continuity or text_only else continuity(series, out.parent, out, p, mid, closeups=args.continue_closeups)
        if cont:
            print(f"{p['id']}: {'앞 컷에서 잇기' if cont[0] == 'next_scene' else '배치 고정'} ← {cont[2]}", flush=True)

        def render(sd, turned=False, note=""):
            pr, rf = (qwen_refs(series, p, expr=False, turned=True, text_only=text_only) if turned else (prompt, refs))
            if note:  # 연출 확인에서 걸린 점(지난 판의 실수)을 피하라고 붙인다
                pr += "\n\nTHE LAST ATTEMPT HAD THESE MISTAKES, AVOID THEM: " + note
            names = [upload(c, r, cache) for r in rf]
            steps = args.steps or (8 if args.turbo else 40)
            first = cont and not turned and not note  # 다시 그리기(연출 확인에서 걸림)는 보통 방식으로: 같은 앞 컷을 또 베끼지 않게
            if first and cont[0] == "next_scene":
                from hf_panel import names as _names
                cid = p["chars"][0]
                face = series / "assets" / "vast_refs" / f"{cid}_bust.png"
                nm = re.sub(r"\s*\(.*?\)", "", _names(series).get(cid, cid))
                wear = (p.get("outfit") or {}).get(cid)
                # 옷은 지시문 positive 에 다시 적는다: CFG 1 이라 네거티브는 안 듣고, 적지 않으면 민소매가 긴소매로 바뀌었다(D+18 p015)
                pr_ns = (f"Next Scene: the same moment continues in the same place, a new panel of {nm}. {p['action']} "
                         + (f"Acting: {p['acting']}. " if p.get("acting") else "")
                         + (f"{nm} wears exactly the same clothes as in <image1>: {wear}. " if wear else "")
                         + f"Keep {nm}'s face, hair and clothes, the place and the light exactly as in <image1>; change only the expression, gaze, "
                           "head angle and hands as described." + (f" <image2> is {nm}'s face (identity only)." if face.exists() else "")
                         + " Korean webtoon panel, no text, no speech bubbles.")
                # 앞 컷을 반 픽셀 흐리게 넣는다(Next-Scene 작성자 권장: 이어 쓸 때 날카로운 결이 쌓이지 않게)
                from PIL import Image as _Im, ImageFilter as _Fl
                soft = out / "_cont" / f"{p['id']}_from_{cont[2]}.png"
                soft.parent.mkdir(exist_ok=True)
                _Im.open(cont[1]).convert("RGB").filter(_Fl.GaussianBlur(0.5)).save(soft)
                g = graph(pr_ns, [upload(c, soft, cache)] + ([upload(c, face, cache)] if face.exists() else []), 0, 0, sd, 28, args.ref_res,
                          turbo=False, edit=True, series=series, role="next_scene", chars=p.get("chars"))
                hist = c.wait(c.queue(g), timeout=1800)
                CONT_MADE.add(p["id"])
            elif first and cont[0] == "layout_lock":
                hist = c.wait(c.queue(graph(pr, names, w, h, sd, steps, args.ref_res, args.turbo, series=series, refine=args.refine,
                                            chars=p.get("chars"), role="layout_lock", control=upload(c, cont[1], cache))), timeout=1800)
            else:
                from make_workflow import build as ui_build, build_anima  # 화면용 워크플로우(같은 구성)를 같이 실어 보낸다
                ui = (build_anima(pr, size=(w, h), steps=steps, seed=sd, turbo=args.turbo) if text_only else
                      ui_build(pr, names, steps=steps, seed=sd, turbo=args.turbo, size=(w, h), prefix="webtoon_studio/panel"))
                hist = c.wait(c.queue(graph(pr, names, w, h, sd, steps, args.ref_res, args.turbo, series=series, refine=args.refine,
                                            chars=p.get("chars")), ui), timeout=1800)
            st = hist.get("status", {})
            if st.get("status_str") == "error":
                msg = next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "?")
                raise RuntimeError(msg)
            img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
            c.download(img, dst)
            cp = None if (first and cont[0] == "next_scene") else copied_ref(dst, rf)
            if cp:
                raise RuntimeError(f"참고 그림 {cp.name} 를 거의 그대로 내놓음(ComfyUI 0.42 버그 #17187, 서버를 재시작할 때까지 계속된다): "
                                   "다른 사람 작업이 없을 때 vast/comfy_admin.py restart")
            if args.fix:  # 2단: 남는 사람 지우기(2026-10-16, 무대 뒤 소파 6명 → 3명, D+18 p003 우빈 둘 → 하나)
                from refine import fix_extras
                fix_extras(c, cache, dst, series, p, seed=sd % 100000, log=log)

        try:
            render(seed)
            if args.check:  # 3단: 연출 확인(시점·좌우·시선·표정·배경·인원). 2026-10-16 사용자 "시점·표정·공간 자각·카메라 각도를 잘 고려해야"
                from refine import staging_pass
                staging_pass(c, cache, dst, series, out.parent, p, seed, render, args.retries, log)
            print(f"{p['id']}: {w}x{h} 참고 {len(refs)}장 {time.time() - t0:.0f}초 → {dst}", flush=True)
        except Exception as e:
            import traceback
            print(f"{p['id']}: 실패 {str(e)[:300]}", flush=True)
            # 어디서 터졌는지 남긴다(검사 단계의 None 오류가 메시지 한 줄로만 남아 원인을 못 찾았다, 2026-10-19 Cherry 시험 p054)
            # 우리 코드 줄만 남긴다: 끝 네 줄만 남기니 http 내부 줄뿐이라 어느 요청이 시간 초과였는지 몰랐다(2026-10-21 외출 p101)
            ours = [f for f in traceback.extract_tb(e.__traceback__) if "webtoon-studio" in f.filename.replace("\\", "/")]
            print("".join(traceback.format_list(ours[-4:])) + f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            failed.append(p["id"])
    if failed:
        print("실패한 컷:", ",".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
