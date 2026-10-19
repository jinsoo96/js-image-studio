"""vast.ai ComfyUI(Qwen Image 2.1)로 컷 그리기. 힉스필드 판(tools/hf_panel.py)과 같은 컷 설명(panels.json)을 쓴다(크레딧 0, GPU 시간만).
(2026-10-15 사용자 "vastai 에서 이정도 이미지 수준의 웹툰 만들게 워크플로우 세로로"). 힉스필드 결과는 건드리지 않게 panels_hf_vast/ 에 따로 그린다.

    .venv/Scripts/python vast/comfy_panel.py <series_hf_dir> <panels.json> [--episode ep002_v2] [--only p003,p008] [--aspect 2:3] [--turbo]

먼저 한 번: .venv/Scripts/python vast/sheet_refs.py <series_hf_dir>  (시트에서 인물 머리 참고 그림을 잘라 둔다)
모델(서버 models/ 에 Model Manager Neo 로 받아 둠, 없으면 comfy_client.py fetch):
  diffusion_models/qwen_image_2.1_bf16.safetensors · text_encoders/qwen3vl_8b_int8_convrot.safetensors · vae/qwen_image_2.1_vae_bf16.safetensors
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

UNET, CLIP, VAE = "qwen_image_2.1_bf16.safetensors", "qwen3vl_8b_int8_convrot.safetensors", "qwen_image_2.1_vae_bf16.safetensors"
if os.environ.get("WEBTOON_MODEL", "uc").strip().lower() == "uc":  # 저장 워크플로우를 못 불러 코드로 짤 때도 같은 판(saved_workflow.MODELS)
    UNET = "qwen-image-2.1-UC-fp8.safetensors"
TURBO = "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"  # 공식 Turbo LoRA: 8스텝 cfg 1(시안용, 모델 카드)
# 약 2MP(모델 기본 1MP, 최대 2048x2048), 32 배수. 합성 폭 1600 보다 크게
SIZES = {"9:16": (1088, 1920), "2:3": (1184, 1760), "3:4": (1248, 1664), "1:1": (1440, 1440),
         "4:3": (1664, 1248), "3:2": (1760, 1184), "16:9": (1920, 1088)}
MAX_REFS = 10
OTS_WORDS = re.compile(r"over[- ]the[- ]shoulder|shoulder[- ]over|\bOTS\b|어깨 너머", re.I)


def is_ots(camera) -> bool:
    """카메라 칸이 어깨 너머 숏인가. D+18 은 카메라 칸을 어깨 너머에만 써서 '카메라가 있으면 앞사람 하나 더'로 셌는데,
    추몽은 측면·바스트 같은 다른 지시에도 써서 그 '앞사람'이 같은 인물로 하나 더 그려졌다(2026-10-18 p075 재석 둘)."""
    return bool(camera) and bool(OTS_WORDS.search(camera))


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


def graph(prompt, ref_names, w, h, seed, steps, ref_res, turbo=False, edit=False):
    """vast 에 저장된 워크플로우(생성 webtoon_vertical_qwen21 · 편집 webtoon_edit_qwen21)를 불러 입력값만 채운다(saved_workflow.py).
    화면에서 고친 워크플로우가 그대로 돈다. 서버에서 못 불러오면 같은 구성을 코드로 짠다(_graph_code, 경고 한 번).
    edit=True: 첫 참고 그림(<image1>)을 고치는 편집. 캔버스는 그 그림 크기(인코더가 내주는 잠재)를 쓴다(w·h 무시)."""
    global _SAVED_WARNED
    try:
        from saved_workflow import EDIT, GEN, fill, load_ui, to_api
        return fill(to_api(load_ui(EDIT if edit else GEN), turbo=turbo), prompt, ref_names, w, h, seed, steps, ref_res)
    except Exception as e:  # noqa: BLE001
        if not _SAVED_WARNED:
            print(f"[경고] 저장 워크플로우를 못 불러 코드 그래프로 그림: {type(e).__name__}: {e}", file=sys.stderr)
            _SAVED_WARNED = True
        return _graph_code(prompt, ref_names, w, h, seed, steps, ref_res, turbo, edit)


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
            m = re.search(r"\d+", json.loads(r.read().decode())["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"  표정 고르기 실패({cid}): {str(e)[:80]}")
        return None
    k = int(m.group()) if m else 0
    return (files[k - 1], labels[k - 1]) if 1 <= k <= len(labels) else None


CAMERA_WANT = re.compile(r"\b(look\w*|star\w*|fac\w*|gaz\w*)\b[^.;]{0,40}?\b(the\s+)?(camera|viewer|reader)\b(?!\s+side)|\b(pov|point[- ]of[- ]view)\b", re.I)
CAMERA_NOT = re.compile(r"\b(not|never|no|nobody|no one|none|away from|unaware of|instead of|without)\b[^.;]{0,25}$", re.I)


def wants_camera(p) -> bool:
    """컷 설명이 인물에게 카메라(독자)를 보라고 했나(맞은편 사람의 눈으로 보는 시점 컷 포함). 그때만 정면 시선을 둔다.
    "faces the viewer, who sits across the table"·"looks across the table at the viewer" 처럼 전치사가 없거나 사이에 말이 끼면 못 알아봐,
    시점 컷을 매번 '독자를 쳐다봄'으로 걸고 agent app 에이전트가 같은 컷을 열 번 다시 그렸다(2026-10-19 D+18 2화 p022). 부정(not·away from)은 뺀다."""
    # 장면 배치 글(action 끝의 SPACE …)의 가구("the sofa faces the viewer")는 사람이 아니다 → action 에서만 떼어 낸다
    action = re.sub(r"\s*SPACE \(identical in every panel of this scene\):.*$", "", str(p.get("action") or ""), flags=re.S)
    t = " ".join([action] + [str(p.get(k) or "") for k in ("acting", "camera", "shot")])
    for m in CAMERA_WANT.finditer(t):
        if not CAMERA_NOT.search(t[max(0, m.start() - 30):m.start()]):
            return True
    return False


def qwen_refs(series, p, episode=None, front=False, expr=False, turned=False):
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
    loc = series / "assets" / "locations" / f"{p['location']}.png" if p.get("location") and not insert else None
    # view: 같은 장소를 반대편에서 본 그림(<장소>_reverse.png). 장소 그림이 앞(스크린 쪽) 한 장뿐이라 듣는 사람 컷 뒤에도 스크린을 그렸다
    # (2026-10-19 외출 p052·p078 사용자 "회의를 보는 자의 앞모습으로 보여야, 배경이 아니라")
    if loc and p.get("view"):
        alt = loc.with_name(f"{p['location']}_{p['view']}.png")
        loc = alt if alt.exists() else loc
    if loc and not close:
        refs.append(loc)
        lines.append(f"<image{len(refs)}> is the location: draw the panel inside this same place with the same furniture, colors and lighting.")
    bind = {}
    for cid in p.get("chars", []):
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
        lines.append(f"<image{len(refs)}> is the place, for the background colors and lighting only: this is a close-up, so only a small, "
                     "softly blurred part of it shows behind the person. Do not draw the whole room.")
    chars = p.get("chars", [])
    n = len(chars)
    if n:
        lines.append("The person references only show who each person is. Do NOT copy their crop, plain background, outfit, pose or expression.")
    # 참고 얼굴이 모두 정면이라 거의 모든 컷이 독자를 쳐다봤다(2026-10-18 추몽 사용자 "왜 시점을 자꾸 독자 보는 거로 하냐").
    # 연출이 카메라를 보라고 할 때만 정면, 아니면 영화 스틸처럼 인물은 행동의 상대(사람·물건)를 본다. 부정어 대신 보이는 상태로 쓴다
    gaze = ("GAZE: a candid film still. Every person's eyes, head and body are turned toward the person or the thing they are dealing with "
            "in this moment (three-quarter view, profile or from behind as the staging needs), unaware of the camera."
            if n and not wants_camera(p) else "")
    action = p["action"]
    # 장면 전체 배치(SPACE)는 넓은 컷용. 클로즈업에 두면 그 방 배치를 배경에 통째로 그려 인물이 또 나왔고(p079),
    # 어깨 너머 숏(camera)에 두면 앞사람 뒷모습과 배치 속 그 사람이 따로 그려져 두 번 나왔다(p075 재민). 카메라 칸에 위치·시선이 다 있다
    if close or p.get("camera"):
        action = re.sub(r"\s*SPACE \(identical in every panel of this scene\):.*$", "", action, flags=re.S)
    for cid, k in bind.items():
        short = re.sub(r"\s*\(.*?\)", "", NAMES[cid]).split()[-1]
        action = re.sub(rf"\b{re.escape(short)}\b", f"{short} (the person in <image{k}>)", action, count=1)
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
        count = (f"Exactly {n} {'person' if n == 1 else 'people'}" + (" from the references" if extras else " in the panel")
                 + (" plus the over-the-shoulder foreground figure described in CAMERA" if is_ots(p.get("camera")) else "") + ", each appearing only once."
                 + (f" Besides them only these unnamed people: {extras}." if extras else
                    " No other people anywhere in the frame, not even small in the background." if n == 1 and not is_ots(p.get("camera")) else ""))
    else:
        count = f"No main characters. Only these unnamed people: {extras}." if extras else "No people in the panel."
    sj = series / "series.json"  # 작품마다 사람 규칙(기본은 성인 작품용 문장, 학생이 나오는 작품은 series.json 의 people_rule)
    people_rule = (json.loads(sj.read_text(encoding="utf-8")).get("people_rule") if sj.exists() else None) or "Adults with realistic proportions."
    # 시대 규칙(series.json era_rule): 2008년 작품에 스마트폰이 일곱 컷 나왔다(2026-10-18 외출 1화) → 그리기 지시에 시대 소품을 박는다
    sjd = json.loads(sj.read_text(encoding="utf-8")) if sj.exists() else {}
    era_rule = sjd.get("era_rule") or ""
    if era_rule:
        people_rule = f"{people_rule} {era_rule}"
    # 시대 소품 중 휴대폰 규칙은 컷 설명에 휴대폰이 나올 때만 붙인다. 늘 붙였더니 열세 컷에서 설명에 없는 휴대폰을 손에 쥐여 줬다(외출 1화)
    if sjd.get("era_phone") and re.search(r"\bphone", " ".join(str(p.get(k) or "") for k in ("action", "acting", "camera", "extras")), re.I):
        people_rule = f"{people_rule} {sjd['era_phone']}"
    text_rule = (f"One single panel: no speech bubbles, no sound effects, no panel border. The only readable text drawn in the picture: {p['text_in_image']}; "
                 "no other letters, signs or logos." if p.get("text_in_image") else
                 "One single panel: no text, no speech bubbles, no sound effects, no panel border.")
    framing = ("FRAMING: a macro close-up. The object or hands fill most of the frame; behind them only a soft, dark, out-of-focus blur. "
               "No room overview, no furniture layout, no bed, no people." if insert else
               "FRAMING: a tight close-up. The head and shoulders fill most of the frame; only a small, softly blurred part of the place shows."
               if "close-up" in (p.get("shot") or "") else "")
    parts = [f"Korean webtoon panel, {p.get('shot', 'medium shot')}.", framing,
             f"ACTING (strong, instantly readable webtoon expression and body language): {p['acting']}" if p.get("acting") else "",
             action, outfit, f"CAMERA: {p['camera']}" if p.get("camera") else "", gaze, count,
             ("REFERENCES: " + " ".join(lines)) if lines else "", style,
             f"COLOR AND TEXTURE OF THIS PANEL: {p['look']}" if p.get("look") else "",
             text_rule
             + (" " + people_rule + (" " + extra.read_text(encoding="utf-8").strip() if extra.exists() else "") if n or extras else
                " An empty scene: no people, no faces, no figures, no silhouettes, no reflections of people.")]
    return "\n\n".join(x for x in parts if x), refs[:MAX_REFS]


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
    args = ap.parse_args()
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
    for p in panels:
        if only and p["id"] not in only:
            continue
        if p.get("reuse") or not p.get("action"):
            continue
        prompt, refs = qwen_refs(series, p, expr=args.expr)
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

        def render(sd, turned=False, note=""):
            pr, rf = (qwen_refs(series, p, expr=False, turned=True) if turned else (prompt, refs))
            if note:  # 연출 확인에서 걸린 점(지난 판의 실수)을 피하라고 붙인다
                pr += "\n\nTHE LAST ATTEMPT HAD THESE MISTAKES, AVOID THEM: " + note
            names = [upload(c, r, cache) for r in rf]
            steps = args.steps or (8 if args.turbo else 40)
            from make_workflow import build as ui_build  # 화면용 워크플로우(같은 구성)를 같이 실어 보낸다
            ui = ui_build(pr, names, steps=steps, seed=sd, turbo=args.turbo, size=(w, h), prefix="webtoon_studio/panel")
            hist = c.wait(c.queue(graph(pr, names, w, h, sd, steps, args.ref_res, args.turbo), ui), timeout=1200)
            st = hist.get("status", {})
            if st.get("status_str") == "error":
                msg = next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "?")
                raise RuntimeError(msg)
            img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
            c.download(img, dst)
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
            print(f"{p['id']}: 실패 {str(e)[:300]}", flush=True)
            failed.append(p["id"])
    if failed:
        print("실패한 컷:", ",".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
