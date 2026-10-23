"""ComfyUI HTTP API 클라이언트와 패널 프롬프트 조립.

ComfyUI 주소는 COMFY_URL 환경변수 > style.json 의 comfy_url 순서로 정한다.
평소엔 비어 있고, tools/runpod_cloud.py 가 RunPod 파드를 켜면서 COMFY_URL 을 넣어 준다.

    python tools/runpod_cloud.py run -- python tools/comfy.py check      # 파드를 켜서 서버·모델 확인
"""
import copy
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = ROOT / "style" / "style.json"
WORKFLOW_PATH = ROOT / "workflows" / "panel_txt2img.api.json"

COUNT_TAGS = {"girl": "1girl", "boy": "1boy", "other": "1other"}
PLURAL = {"girl": "girls", "boy": "boys", "other": "others"}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_style(series_dir=None):
    """그림체 정본. 시리즈 폴더에 style_override.json 이 있으면 그 값을 덮어쓴다(예: 시리즈별 그림체 LoRA, 2026-10-12 D+18)."""
    style = load_json(STYLE_PATH)
    over = Path(series_dir) / "style_override.json" if series_dir else None
    if over and over.exists():
        for k, v in load_json(over).items():
            if k.startswith("_"):
                continue
            style[k] = {**style[k], **v} if isinstance(v, dict) and isinstance(style.get(k), dict) else v
    return style


class Comfy:
    def __init__(self, url=None):
        self.url = (url or os.environ.get("COMFY_URL") or load_style()["comfy_url"]).rstrip("/")
        if not self.url:
            raise RuntimeError("ComfyUI 주소가 없다. tools/runpod_cloud.py run -- <명령> 으로 파드를 켜서 실행한다")
        self.client_id = uuid.uuid4().hex

    def _req(self, path, payload=None, raw=False, timeout=60):
        data = None
        # RunPod 프록시(Cloudflare)는 파이썬 기본 User-Agent 를 403(1010)으로 막는다
        headers = {"User-Agent": "webtoon-studio/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return body if raw else json.loads(body)

    def stats(self):
        return self._req("/system_stats")

    def checkpoints(self):
        info = self._req("/object_info/CheckpointLoaderSimple")
        return info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]

    def queue(self, workflow):
        try:
            r = self._req("/prompt", {"prompt": workflow, "client_id": self.client_id})
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI 가 워크플로우를 거절했다: {e.read().decode('utf-8', 'replace')}") from e
        return r["prompt_id"]

    def wait(self, prompt_id, timeout=1800, poll=2.0):
        start = time.time()
        while time.time() - start < timeout:
            hist = self._req(f"/history/{prompt_id}")
            if prompt_id in hist:
                item = hist[prompt_id]
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"생성 실패: {json.dumps(status.get('messages', []), ensure_ascii=False)[:2000]}")
                if status.get("completed", True):
                    return item
            time.sleep(poll)
        raise TimeoutError(f"{timeout}s 안에 끝나지 않았다: {prompt_id}")

    def images(self, item):
        out = []
        for node_out in item.get("outputs", {}).values():
            for im in node_out.get("images", []):
                q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")})
                out.append(self._req(f"/view?{q}", raw=True, timeout=120))
        return out

    def run(self, workflow, timeout=1800):
        """워크플로우 하나를 돌려 PNG 바이트 목록과 걸린 초를 돌려준다."""
        t0 = time.time()
        item = self.wait(self.queue(workflow), timeout=timeout)
        return self.images(item), time.time() - t0

    def upload(self, path):
        """이미지를 ComfyUI input 폴더에 올리고 LoadImage 가 쓸 이름을 돌려준다."""
        path = Path(path)
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body, headers={
            "User-Agent": "webtoon-studio/1.0", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())["name"]

    _style_ref_cache = {}

    def style_ref(self, style):
        """style.json 의 style_ref(그림체 기준 이미지)를 이 서버에 한 번만 올리고 워크플로우용 설정을 돌려준다. 없으면 None."""
        ref = style.get("style_ref")
        if not ref or not ref.get("enabled", True):
            return None
        key = (self.url, tuple(ref["images"]))
        if key not in Comfy._style_ref_cache:
            Comfy._style_ref_cache[key] = [self.upload(ROOT / p) for p in ref["images"]]
        return {**ref, "names": Comfy._style_ref_cache[key]}


def join_tags(*parts):
    seen, out = set(), []
    for part in parts:
        for tag in (part or "").split(","):
            tag = tag.strip()
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                out.append(tag)
    return ", ".join(out)


def count_tags(chars):
    """캐릭터 성별 목록에서 1girl, 2boys 같은 인원 태그를 만든다. 인원 태그는 캐릭터 tags 에 넣지 않는다."""
    counts = {}
    for c in chars:
        g = c.get("gender", "other")
        counts[g] = counts.get(g, 0) + 1
    tags = []
    for g in ("girl", "boy", "other"):
        n = counts.get(g, 0)
        if n == 1:
            tags.append(COUNT_TAGS[g])
        elif n > 1:
            tags.append(f"{n}{PLURAL[g]}")
    if len(chars) == 1:
        tags.append("solo")  # 없으면 전신 구도에서 같은 인물이 둘로 복제돼 나온다
    # 등장인물이 없는 컷(군중·실루엣·소품)은 인원을 컷 prompt 가 정한다. 사람 없는 컷은 shot=insert 가 no humans 를 붙인다
    return ", ".join(tags)


def panel_chars(panel, characters):
    """컷의 등장인물 id 목록. layout 이 있으면 layout 순서가 정본이다."""
    ids = [r["char"] for r in panel["layout"]] if panel.get("layout") else panel.get("characters", [])
    for cid in ids:
        if cid not in characters:
            raise KeyError(f"characters.json 에 없는 캐릭터: {cid}")
    return ids


def char_desc(panel, cid, c):
    outfit_key = panel.get("outfits", {}).get(cid, "default")
    tags = c["tags"]
    # 컷에서만 빼야 하는 외형 태그(예: 안경 벗은 얼굴 공개 컷 "drop_tags": {"hero": ["glasses"]})
    drop = panel.get("drop_tags", {}).get(cid, [])
    if drop:
        tags = ", ".join(t.strip() for t in tags.split(",") if not any(d in t for d in drop))
    # 인물 LoRA(얼굴 고정, 2026-10-09)를 쓰는 인물은 학습 때 붙인 트리거를 맨 앞에
    trigger = c.get("trigger", "") if c.get("lora") else ""
    return join_tags(trigger, tags, c.get("outfits", {}).get(outfit_key, ""))


EATING = ("chewing", "sipping", "mouth full", "food in mouth", "drinking from")


def speech_cues(panel):
    """대사에 맞춘 입모양·말하는 표정 {인물: 태그}. 말풍선만 얹고 입은 닫혀 있던 것(2026-10-11 사용자 "대사 말할 때 입모양이 다 다르다")을 잡는다.
    말하는 사람: 혼자 나오는 컷이면 그 인물(꼬리 없는 말풍선 = 화면 밖 사람은 제외), 여러 인물 컷(layout)이면 말풍선 위치(왼쪽·오른쪽)에 가까운 영역의 인물.
    외침·느낌표 = 입 크게, 물음표 = 묻는 얼굴, 말줄임 = 입 조금, 속마음만 있으면 입 닫음. 먹는 컷은 건드리지 않는다."""
    ids = list(panel.get("characters", []))
    layout = panel.get("layout") or []
    if not ids and not layout:
        return {}
    centers = {r["char"]: r.get("x", 0) + r.get("w", 0.5) / 2 for r in layout}
    said, thought = {}, set()

    def owner(item):
        if len(ids) == 1 and not layout:
            return ids[0]
        if not centers:
            return None
        pos = item.get("pos", "top")
        x = pos[0] if isinstance(pos, (list, tuple)) else (0.25 if "left" in str(pos) else 0.75 if "right" in str(pos) else 0.5)
        return min(centers, key=lambda c: abs(centers[c] - x))

    for item in panel.get("dialogue", []):
        kind = item.get("type", "speech")
        if kind in ("narration", "monologue", "sfx") or item.get("tail", "auto") is None:
            continue
        # 어깨 너머 컷처럼 뒷모습으로만 나오는 사람의 대사는 "speaker" 로 적어 보이는 인물 입을 열지 않는다
        if item.get("speaker") and item["speaker"] not in set(ids) | set(centers):
            continue
        who = owner(item)
        if not who:
            continue
        if kind == "thought":
            thought.add(who)
            continue
        text = item.get("text", "").strip()
        if kind == "shout" or "!!" in text:
            level = 3
        elif text.endswith("!") or "!" in text:
            level = 2
        elif text.startswith("...") or text.endswith("...") or text.startswith("…"):
            level = 0
        else:
            level = 1
        if text.endswith("?") and level < 2:
            level = max(level, 1) + 0.5
        said[who] = max(said.get(who, -1), level)
    cues = {}
    region_prompt = {r["char"]: r.get("prompt", "") for r in layout}
    for who, level in said.items():
        own = (region_prompt.get(who) if layout else panel.get("prompt", "")) or ""
        if any(w in own.lower() for w in EATING):  # 그 인물이 입에 뭘 문 컷만 뺀다
            continue
        # 세기는 미소·차분 태그에 눌려 입이 닫히던 것(2026-10-12 p085) 때문에 한 단계씩 올렸다. 1.3 넘기면 노이즈
        cues[who] = {0: "(slightly open mouth:1.2), talking quietly, hesitant",
                     1: "(open mouth:1.25), talking",
                     1.5: "(open mouth:1.25), talking, asking, curious look",
                     2: "(open mouth:1.3), talking, energetic",
                     2.5: "(open mouth:1.3), talking, asking, energetic",
                     3: "(open mouth:1.3), shouting, energetic"}[level]
    for who in thought - set(said):
        cues[who] = "(closed mouth:1.15)"
    return cues


def char_loras(panel, characters):
    """컷에 나오는 인물들의 인물 LoRA [(파일, 세기)]. 같은 시리즈 인물은 한 파일을 같이 쓴다."""
    ids = list(panel.get("characters", [])) + [r["char"] for r in panel.get("layout", [])]
    out = {}
    for cid in ids:
        c = characters.get(cid, {})
        if c.get("lora"):
            out[c["lora"]] = max(out.get(c["lora"], 0), c.get("lora_strength", 0.8))
    return list(out.items())


def compose_prompt(style, panel, characters):
    """패널 한 장의 긍정·부정 프롬프트. 순서 = 인원 > 캐릭터 > 컷 지시 > 샷·장면 > 모드 > 그림체 > 품질.
    layout(여러 인물 영역 배치)이 있는 컷이면 여기엔 인원·장면만 넣고, 인물 묘사는 compose_regions 가 영역별로 만든다."""
    mode = style["modes"][panel.get("mode", "drama")]
    ids = panel_chars(panel, characters)
    chars = [characters[cid] for cid in ids]
    regional = bool(panel.get("layout"))
    if regional:
        # 영역 컷의 장면 프롬프트에는 "왼쪽에 누구, 오른쪽에 누구" 짧은 설명만 넣어 배치를 잡아 준다(전체 태그를 넣으면 섞인다)
        char_tags = []
        for r in panel["layout"]:
            mid = r.get("x", 0.0) + r.get("w", 0.5) / 2
            side = "on the left" if mid < 0.4 else ("on the right" if mid > 0.6 else "in the middle")
            # 컷 옷이 기본 옷과 다르면(저녁 장면 카디건 등) layout 의 brief 로 바꿔 쓴다
            char_tags.append(f"{r.get('brief') or characters[r['char']].get('brief', '')} {side}".strip())
    else:
        cues = speech_cues(panel)
        char_tags = [join_tags(char_desc(panel, cid, characters[cid]), cues.get(cid, "")) for cid in ids]
    # 아이돌용 성별 규칙(gender_pos·gender_neg)은 조연(엄마·원장 등, skip_gender_style)에는 붙이지 않는다
    genders = dict.fromkeys(c.get("gender", "other") for c in chars if not c.get("skip_gender_style"))
    rating = panel.get("rating", style["rating_pos"])
    pos = join_tags(
        lora_trigger(style),  # 그림체 LoRA 를 쓰면 학습 때 붙인 트리거를 맨 앞에
        # 낮 장면 표시: LoRA 학습 세트에 밤·실내가 많아 아침 거리도 밤으로 나왔다(2026-10-07 p001)
        "(daytime:1.25), bright natural daylight" if panel.get("time") == "day" else "",
        panel.get("count") or count_tags(chars),  # 이름 없는 엑스트라가 같이 나오는 컷은 count 로 인원을 직접 준다(예: "2boys")
        *char_tags,
        panel.get("prompt", ""),
        style["shots"].get(panel.get("shot", "medium"), ""),
        style["scenes"].get(panel.get("scene", "none"), ""),
        "" if panel.get("time") == "day" else mode["pos"],  # 낮 컷엔 보케·빛 입자(밤 느낌)를 빼고
        mode.get("face_pos", "") if chars else "",  # 얼굴 태그는 등장인물이 있을 때만. 군중·소품 컷에 붙이면 엉뚱한 얼굴이 생긴다
        *(() if regional else (mode.get("gender_pos", {}).get(g, "") for g in genders)),
        style["style_pos"],
        rating,
        style["quality_pos"],
    )
    neg = join_tags(style["neg_base"], style.get("rating_neg", {}).get(rating, ""), mode.get("neg", ""),
                    *(mode.get("gender_neg", {}).get(g, "") for g in genders),
                    *(c.get("negative", "") for c in chars), panel.get("negative", ""),
                    "night, dark, night sky, bokeh" if panel.get("time") == "day" else "")
    # 컷에서만 풀어 줄 부정어(예: 사이코 웃음 컷의 그림자 진 얼굴 "drop_neg": ["harsh shadows", "dark"]). 조명 통일(4.2) 부정어와 부딪칠 때
    drop = {d.strip().lower() for d in panel.get("drop_neg", [])}
    if drop:
        neg = ", ".join(t for t in (x.strip() for x in neg.split(",")) if t and t.lower().strip("()").split(":")[0] not in drop)
    return pos, neg


def lora_trigger(style):
    lora = style.get("lora") or {}
    return lora.get("trigger", "") if lora.get("enabled") else ""


def compose_regions(style, panel, characters):
    """layout 의 인물마다 (프롬프트, x, y, w, h) 를 만든다. 좌표는 캔버스 비율(0~1).
    인물 하나당 영역 하나를 줘야 SDXL 이 두 사람의 머리색·옷을 섞지 않는다."""
    mode = style["modes"][panel.get("mode", "drama")]
    regions = []
    cues = speech_cues(panel)
    for r in panel.get("layout", []):
        cid = r["char"]
        c = characters[cid]
        g = c.get("gender", "other")
        gender_pos = "" if c.get("skip_gender_style") else mode.get("gender_pos", {}).get(g, "")
        text = join_tags(lora_trigger(style), COUNT_TAGS[g], "solo", char_desc(panel, cid, c), cues.get(cid, ""), r.get("prompt", ""),
                         mode.get("face_pos", ""), gender_pos, style["style_pos"], style["quality_pos"])
        regions.append((text, r.get("x", 0.0), r.get("y", 0.0), r.get("w", 0.5), r.get("h", 1.0)))
    return regions


def stable_seed(*keys):
    return int(hashlib.sha256("|".join(map(str, keys)).encode("utf-8")).hexdigest()[:12], 16) % (2**32)


REGION_BG_STRENGTH = 0.7  # 영역 컷에서 장면(배경) 프롬프트 세기. 1.0 이면 인물 영역을 덮어써 엄마가 젊은 여자로 바뀌었다
REGION_CHAR_STRENGTH = 1.0  # 인물 영역 세기. CFG 5.5 에선 색이 탔다. 지금은 모든 컷이 style.json 의 CFG(4.5)를 같이 쓴다


def build_workflow(style, pos, neg, size, seed, prefix="webtoon", regions=None, style_ref=None, loras=()):
    wf = copy.deepcopy(load_json(WORKFLOW_PATH))
    s = style["sampler"]
    wf["1"]["inputs"]["ckpt_name"] = style["checkpoint"]
    wf["2"]["inputs"]["text"] = pos
    wf["3"]["inputs"]["text"] = neg
    wf["4"]["inputs"].update(width=size[0], height=size[1])
    wf["5"]["inputs"].update(seed=seed, steps=s["steps"], cfg=s["cfg"], sampler_name=s["name"], scheduler=s["scheduler"])
    if "filename_prefix" in wf["7"]["inputs"]:
        wf["7"]["inputs"]["filename_prefix"] = prefix
    if regions:
        # 장면 프롬프트는 캔버스 전체에 약하게, 인물 프롬프트는 각자 영역에 세게 걸고 하나로 합쳐 샘플러에 넣는다
        wf["10"] = {"class_type": "ConditioningSetAreaPercentage",
                    "inputs": {"conditioning": ["2", 0], "width": 1.0, "height": 1.0, "x": 0.0, "y": 0.0, "strength": REGION_BG_STRENGTH}}
        last = ["10", 0]
        for i, (text, x, y, w, h) in enumerate(regions):
            enc, area, comb = str(20 + i * 3), str(21 + i * 3), str(22 + i * 3)
            wf[enc] = {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["1", 1]}}
            wf[area] = {"class_type": "ConditioningSetAreaPercentage",
                        "inputs": {"conditioning": [enc, 0], "width": w, "height": h, "x": x, "y": y, "strength": REGION_CHAR_STRENGTH}}
            wf[comb] = {"class_type": "ConditioningCombine", "inputs": {"conditioning_1": last, "conditioning_2": [area, 0]}}
            last = [comb, 0]
        wf["5"]["inputs"]["positive"] = last
    # 마무리 덧칠: 모든 컷을 같은 과정으로 그리려고 단독 컷에도 똑같이 건다(영역 컷만 걸었더니 질감이 갈렸다).
    # 영역 컷에선 영역 사이 이음새도 녹인다. 영역 컷은 그림체가 납작하게 흘러서(2026-10-07 p033·p037·p040) 장면 전체 프롬프트로
    # 더 세게(0.45) 덧칠해 단독 컷과 질감을 맞춘다
    wf["30"] = {"class_type": "KSampler", "inputs": {
        "seed": seed, "steps": 10, "cfg": s["cfg"], "sampler_name": s["name"], "scheduler": s["scheduler"],
        "denoise": s.get("refine_denoise_regions", 0.45) if regions else s.get("refine_denoise", 0.3),
        "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0]}}
    wf["6"]["inputs"]["samples"] = ["30", 0]
    lora = style.get("lora") or {}
    if lora.get("enabled") and lora.get("strength", 0) > 0:
        # 우리 그림체 LoRA(승인·선별 컷으로 학습, tools/train_lora.py): 체크포인트의 모델·CLIP 을 거쳐 모든 노드가 쓰게 바꿔 끼운다
        wf["50"] = {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": lora["name"],
                                                            "strength_model": lora["strength"], "strength_clip": lora.get("strength_clip", lora["strength"])}}
        for nid, node in wf.items():
            if nid == "50":
                continue
            for k, v in node["inputs"].items():
                if v == ["1", 0]:
                    node["inputs"][k] = ["50", 0]
                elif v == ["1", 1]:
                    node["inputs"][k] = ["50", 1]
    if loras:
        # 인물 LoRA(얼굴 고정): 그림체 LoRA 뒤(없으면 체크포인트 뒤)에 차례로 끼워 모든 노드가 쓰게 한다
        src_m, src_c = (["50", 0], ["50", 1]) if "50" in wf else (["1", 0], ["1", 1])
        for i, (name, strength) in enumerate(loras):
            nid = str(51 + i)
            wf[nid] = {"class_type": "LoraLoader", "inputs": {"model": src_m, "clip": src_c, "lora_name": name,
                                                             "strength_model": strength, "strength_clip": strength}}
            src_m, src_c = [nid, 0], [nid, 1]
        lora_ids = {"50", *(str(51 + i) for i in range(len(loras)))}
        for nid, node in wf.items():
            if nid in lora_ids:
                continue
            for k, v in node["inputs"].items():
                if v in (["1", 0], ["50", 0]):
                    node["inputs"][k] = src_m
                elif v in (["1", 1], ["50", 1]):
                    node["inputs"][k] = src_c
    if style_ref:
        # 그림체 고정: 기준 이미지의 그림체(선·명암·색감)만 IP-Adapter "style transfer" 로 모델에 건다. 구도·인물은 프롬프트를 따른다.
        # 시드마다 그림체가 반실사·납작한 애니·번들거림으로 갈리던 것(2026-10-07 사용자 "그림체가 일관되지 않는다")을 잡는다
        wf["40"] = {"class_type": "IPAdapterModelLoader", "inputs": {"ipadapter_file": style_ref["ipadapter"]}}
        wf["41"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": style_ref["clip_vision"]}}
        img = None
        for i, name in enumerate(style_ref["names"]):
            node = str(42 + i * 2)
            wf[node] = {"class_type": "LoadImage", "inputs": {"image": name}}
            if img is None:
                img = [node, 0]
            else:
                wf[str(43 + i * 2)] = {"class_type": "ImageBatch", "inputs": {"image1": img, "image2": [node, 0]}}
                img = [str(43 + i * 2), 0]
        wf["60"] = {"class_type": "IPAdapterAdvanced", "inputs": {
            "model": ["1", 0], "ipadapter": ["40", 0], "image": img, "clip_vision": ["41", 0],
            "weight": style_ref["weight"], "weight_type": style_ref["weight_type"], "combine_embeds": "average",
            "start_at": 0.0, "end_at": style_ref.get("end_at", 1.0), "embeds_scaling": "V only"}}
        # apply_to: both(두 단계 다) | refine(구도·조명은 첫 단계가 정하고 마무리 덧칠에서만 그림체를 당긴다)
        if style_ref.get("apply_to", "both") == "both":
            wf["5"]["inputs"]["model"] = ["60", 0]
        wf["30"]["inputs"]["model"] = ["60", 0]
        if "refine_denoise" in style_ref:
            wf["30"]["inputs"]["denoise"] = style_ref["refine_denoise"]
    return wf


def panel_workflow(style, panel, characters, seed, prefix="webtoon", style_ref=None):
    if "lora_strength" in panel and style.get("lora"):
        # 컷마다 LoRA 세기 조정(사람 없는 배경 컷은 학습 세트가 인물 위주라 약하게. 2026-10-07 p001 아침 거리가 밤으로)
        style = {**style, "lora": {**style["lora"], "strength": panel["lora_strength"], "strength_clip": panel["lora_strength"]}}
    """컷 하나를 그릴 워크플로우와 기록용 프롬프트. layout 이 있으면 영역 프롬프트를 쓴다.
    style_ref 는 Comfy.style_ref(style) 결과(그림체 기준 이미지). 컷에 "style_ref": false 면 그 컷만 끈다."""
    pos, neg = compose_prompt(style, panel, characters)
    # 영역 없이 한 장으로 그리면 두 사람의 성별·옷이 섞이고 한 사람만 나왔다(2026-10-12 v13 시험 21장). 영역은 늘 쓰고,
    # 증명사진처럼 만들던 반쪽 다시 그리기 대신 얼굴만 다듬는다(render.refine_faces). 컷에 "single": true 면 영역 없이
    regions = compose_regions(style, panel, characters) if panel.get("layout") and not panel.get("single") else None
    size = style["sizes"][panel.get("aspect", "portrait")]
    ref = style_ref if panel.get("style_ref", True) else None
    return build_workflow(style, pos, neg, size, seed, prefix, regions, ref, char_loras(panel, characters)), pos, neg, regions, size


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "check":
        print(__doc__)
        return
    c = Comfy()
    st = c.stats()
    dev = st["devices"][0]
    print(f"url={c.url} comfyui={st['system'].get('comfyui_version')} device={dev['name']} vram_free={dev['vram_free'] / 2**30:.1f}GB")
    ckpts = c.checkpoints()
    want = load_style()["checkpoint"]
    print(f"checkpoints={ckpts}")
    print("checkpoint OK" if want in ckpts else f"checkpoint MISSING: {want}")


if __name__ == "__main__":
    main()
