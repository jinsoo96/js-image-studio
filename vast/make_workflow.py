"""서버 ComfyUI 화면에서 열어 쓰는 세로 웹툰 워크플로우(UI 형식)를 만들어 저장한다. 기준선 image_anima_base_v1.json 은 건드리지 않는다.
comfy_panel.py 가 큐에 넣는 그래프와 같은 구성(Qwen Image 2.1 + 참고 그림 3장 + 세로 2:3 약 2MP)을 사람이 손으로 돌릴 수 있게 펼쳐 놓은 것.

    .venv/Scripts/python vast/make_workflow.py [--name webtoon_vertical_qwen21_uc.json] [--prompt-file 컷.txt] [--ref 그림 ...] [--save]
--save 없이 돌리면 파일만 만든다(scratch). 같은 이름이 서버에 있으면 --force 없이는 덮지 않는다.
"""
import argparse
import json
import re
import sys
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402
from saved_workflow import save_ui  # noqa: E402
from comfy_panel import CLIP, SIZES, TURBO, UNET, VAE, upload  # noqa: E402

HF = "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main"
CLIP_URL = f"https://huggingface.co/NidAll/Qwen3VL-8B-Abliterated-INT8-ConvRot/resolve/main/{CLIP}"  # 무검열 텍스트 인코더(2026-10-22)
GUIDE = """# 세로 웹툰 컷 (Qwen Image 2.1)
webtoon-studio 의 컷 그리기와 같은 구성. 기준선 image_anima_base_v1 은 그대로 두고 따로 만든 것.

**쓰는 법**
1. 왼쪽 참고 그림에 캐릭터 시트·옷 시트·장소 그림을 넣는다(10장까지). 넣은 순서가 `<image1>`, `<image2>` …
2. 컷 설명(프롬프트)에서 그림을 `<image1>` 처럼 부른다. 예: `<image1> is the character sheet of Jaemin.`
3. 세로 캔버스 크기를 고르고 실행. 결과는 output/webtoon_vertical/

**세로 크기(약 2MP, 32 배수)**: 2:3 = 1184x1760 · 9:16 = 1088x1920 · 3:4 = 1248x1664 (가로 컷은 4:3 = 1664x1248, 16:9 = 1920x1088)

**터보**: "터보 LoRA" 노드를 켜고(Ctrl+B) steps 8 이면 시안이 5배 빠르다.

**설정**: cfg 1(공식, 네거티브는 cfg 1 에서 안 씀) · euler / simple · 40 스텝(공식 40~50). 참고 그림 크기 1024(TextEncode 의 resolution).
참고 그림이 많을수록 느려진다. 한 컷에 인물 시트 + 장소면 충분하다.

**모델**(models/ 에 받아 둠, Model Manager Neo · HuggingFace Comfy-Org/Qwen-Image-2.1)
- diffusion_models/qwen-image-2.1-UC-fp8.safetensors (Uncensored 판)
- text_encoders/qwen3vl_8b_abliterated_int8_convrot.safetensors(무검열 판, NidAll/Qwen3VL-8B-Abliterated-INT8-ConvRot. 2026-10-22 전엔 공식 qwen3vl_8b_int8_convrot)
- vae/qwen_image_2.1_vae_bf16.safetensors

글자·말풍선은 그리지 않는다(합성 단계에서 얹는다)."""


FIX_GUIDE = """**2단 다듬기**: 1단 그림이 그대로 `<image1>` 로 들어온다. 지시는 짧게, 고칠 것만.
- 남는 사람 지우기: `Remove the young man in the light blue shirt behind the table. Fill the space with the background. Keep everything else the same.`
- 연기: `Make her cry: tears streaming down both cheeks, biting her lower lip. Keep everything else the same.`
2단이 필요 없으면 이 묶음 노드를 우회(Ctrl+B)."""


class G:
    def __init__(self):
        self.nodes, self.links, self.groups = [], [], []

    def node(self, typ, pos, size, widgets=None, inputs=(), outputs=(), title=None, models=None):
        n = {"id": len(self.nodes) + 1, "type": typ, "pos": list(pos), "size": list(size), "flags": {}, "order": len(self.nodes), "mode": 0,
             "inputs": [{"name": nm, "type": t, "link": None} for nm, t in inputs],
             "outputs": [{"name": nm, "type": t, "links": [], "slot_index": i} for i, (nm, t) in enumerate(outputs)],
             "properties": {"Node name for S&R": typ}, "widgets_values": widgets if widgets is not None else []}
        if title:
            n["title"] = title
        if models:
            n["properties"]["models"] = models
        self.nodes.append(n)
        return n

    def link(self, a, aslot, b, bname):
        lid = len(self.links) + 1
        bslot = next(i for i, x in enumerate(b["inputs"]) if x["name"] == bname)
        typ = a["outputs"][aslot]["type"]
        self.links.append([lid, a["id"], aslot, b["id"], bslot, typ])
        a["outputs"][aslot]["links"].append(lid)
        b["inputs"][bslot]["link"] = lid

    def group(self, title, x, y, w, h, color):
        self.groups.append({"id": len(self.groups) + 1, "title": title, "bounding": [x, y, w, h], "color": color, "font_size": 22, "flags": {}})

    def dump(self):
        return {"id": str(uuid.uuid4()), "revision": 0, "last_node_id": len(self.nodes), "last_link_id": len(self.links), "nodes": self.nodes,
                "links": self.links, "groups": self.groups, "config": {}, "extra": {"ds": {"scale": 0.75, "offset": [40, 40]}}, "version": 0.4}


def build(prompt, ref_names, aspect="2:3", steps=40, seed=0, turbo=False, size=None, prefix="webtoon_vertical/panel", fix_prompt=None):
    """화면용 워크플로우. comfy_panel.py 도 컷마다 이걸 결과 그림·서버 기록에 같이 실어 보낸다(그 컷을 그린 그대로 캔버스로 열림).
    size 를 주면 aspect 대신 그 크기, turbo 면 터보 LoRA 노드를 켜 둔다."""
    w, h = size or SIZES[aspect]
    if size:
        aspect = next((k for k, v in SIZES.items() if v == tuple(size)), f"{w}x{h}")
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 760), [GUIDE], title="사용법")
    g.group("1. 모델 (Qwen Image 2.1)", 0, 0, 380, 590, "#3f789e")
    unet = g.node("UNETLoader", (20, 50), (340, 82), [UNET, "default"], outputs=[("MODEL", "MODEL")],
                  models=[{"name": UNET, "url": f"{HF}/diffusion_models/{UNET}", "directory": "diffusion_models"}])
    clip = g.node("CLIPLoader", (20, 160), (340, 106), [CLIP, "qwen_image", "default"], outputs=[("CLIP", "CLIP")],
                  models=[{"name": CLIP, "url": CLIP_URL, "directory": "text_encoders"}])
    vae = g.node("VAELoader", (20, 295), (340, 58), [VAE], outputs=[("VAE", "VAE")],
                 models=[{"name": VAE, "url": f"{HF}/vae/{VAE}", "directory": "vae"}])
    # 터보: 기본은 우회(꺼짐). 켜면(Ctrl+B) KSampler steps 를 8 로
    lora = g.node("LoraLoaderModelOnly", (20, 380), (340, 82), [TURBO, 1.0], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")],
                  title="터보 LoRA (켜면 steps 8)", models=[{"name": TURBO, "url": f"{HF}/loras/{TURBO}", "directory": "loras"}])
    lora["mode"] = 0 if turbo else 4
    cache = g.node("QwenImage21Cache", (20, 490), (340, 82), ["auto", "default"], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")])
    g.link(unet, 0, lora, "model")
    g.link(lora, 0, cache, "model")

    g.group("2. 참고 그림 (넣은 순서 = <image1>, <image2> …)", 0, 620, 380, 330 * max(1, len(ref_names)) + 40, "#8a6a3f")
    loads = []
    for k, nm in enumerate(ref_names):
        loads.append(g.node("LoadImage", (20, 670 + 330 * k), (340, 310), [nm, "image"], outputs=[("IMAGE", "IMAGE"), ("MASK", "MASK")],
                            title=f"<image{k + 1}>"))

    g.group("3. 컷 설명", 420, 0, 560, 830, "#3f8a5a")
    n_in = max(len(ref_names) + 1, 4)
    enc = g.node("TextEncodeQwenImage21", (440, 50), (520, 760), [prompt, "", 1024],
                 inputs=[("clip", "CLIP"), ("vae", "VAE")] + [(f"images.image_{i}", "IMAGE") for i in range(1, n_in + 1)],
                 outputs=[("CONDITIONING", "CONDITIONING"), ("CONDITIONING", "CONDITIONING"), ("LATENT", "LATENT")], title="컷 설명 (<imageN> 으로 참고 그림 부르기)")
    g.link(clip, 0, enc, "clip")
    g.link(vae, 0, enc, "vae")
    for k, ld in enumerate(loads, 1):
        g.link(ld, 0, enc, f"images.image_{k}")

    g.group("4. 세로 캔버스 · 그리기", 1020, 0, 400, 830, "#7a3f8a")
    lat = g.node("EmptyLatentImage", (1040, 50), (360, 106), [w, h, 1], outputs=[("LATENT", "LATENT")],
                 title=f"{'세로 ' if h > w else ''}캔버스 {aspect} ({w}x{h})")
    ks = g.node("KSampler", (1040, 190), (360, 262), [seed, "fixed" if seed else "randomize", steps, 1, "euler", "simple", 1],
                inputs=[("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                outputs=[("LATENT", "LATENT")])
    g.link(cache, 0, ks, "model")
    g.link(enc, 0, ks, "positive")
    g.link(enc, 1, ks, "negative")
    g.link(lat, 0, ks, "latent_image")
    dec = g.node("VAEDecode", (1040, 490), (360, 46), inputs=[("samples", "LATENT"), ("vae", "VAE")], outputs=[("IMAGE", "IMAGE")])
    g.link(ks, 0, dec, "samples")
    g.link(vae, 0, dec, "vae")
    sv = g.node("SaveImage", (1440, 0), (560, 830), [prefix], inputs=[("images", "IMAGE")])
    g.link(dec, 0, sv, "images")
    if fix_prompt is not None:  # 2단: 1단 그림을 <image1> 로 받아 Qwen 편집으로 고친다(2026-10-16, 남는 사람 지우기·연기 손보기)
        g.group("5. 2단 다듬기 (1단 그림 = <image1>, 지시대로 편집)", 2040, 0, 1240, 830, "#8a3f3f")
        g.node("MarkdownNote", (2060, 40), (560, 150), [FIX_GUIDE], title="2단 쓰는 법")
        cache2 = g.node("QwenImage21Cache", (2060, 210), (560, 82), ["auto", "default"], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")],
                        title="2단 캐시")
        g.link(lora, 0, cache2, "model")
        enc2 = g.node("TextEncodeQwenImage21", (2060, 310), (560, 330), [fix_prompt, "", 1024],
                      inputs=[("clip", "CLIP"), ("vae", "VAE"), ("images.image_1", "IMAGE"), ("images.image_2", "IMAGE")],
                      outputs=[("CONDITIONING", "CONDITIONING"), ("CONDITIONING", "CONDITIONING"), ("LATENT", "LATENT")], title="다듬기 지시")
        g.link(clip, 0, enc2, "clip")
        g.link(vae, 0, enc2, "vae")
        g.link(dec, 0, enc2, "images.image_1")
        ks2 = g.node("KSampler", (2060, 560), (560, 262), [seed + 1, "fixed" if seed else "randomize", steps, 1, "euler", "simple", 1],
                     inputs=[("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                     outputs=[("LATENT", "LATENT")], title="2단 KSampler (캔버스 = 1단 그림 크기)")
        g.link(cache2, 0, ks2, "model")
        g.link(enc2, 0, ks2, "positive")
        g.link(enc2, 1, ks2, "negative")
        g.link(enc2, 2, ks2, "latent_image")
        dec2 = g.node("VAEDecode", (2660, 40), (600, 46), inputs=[("samples", "LATENT"), ("vae", "VAE")], outputs=[("IMAGE", "IMAGE")])
        g.link(ks2, 0, dec2, "samples")
        g.link(vae, 0, dec2, "vae")
        sv2 = g.node("SaveImage", (2660, 110), (600, 700), [prefix + "_fixed"], inputs=[("images", "IMAGE")], title="2단 결과")
        g.link(dec2, 0, sv2, "images")
    return g.dump()


def build_edit(prompt, image_name, guide, steps=8, seed=0, prefix="webtoon_studio/edit", title="고칠 그림"):
    """편집 워크플로우(화면용): <image1> 그림을 지시대로 고친다. 캔버스는 그 그림 크기(인코더가 내주는 잠재), 터보 8스텝.
    refine.py(남는 사람 지우기·연출 손보기)와 make_assets.py(표정·기본복)가 큐에 넣는 그래프와 같은 구성(comfy_panel.graph edit=True)."""
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 640), [guide], title="사용법")
    g.group("1. 모델 (Qwen Image 2.1 · 터보)", 0, 0, 380, 590, "#3f789e")
    unet = g.node("UNETLoader", (20, 50), (340, 82), [UNET, "default"], outputs=[("MODEL", "MODEL")],
                  models=[{"name": UNET, "url": f"{HF}/diffusion_models/{UNET}", "directory": "diffusion_models"}])
    clip = g.node("CLIPLoader", (20, 160), (340, 106), [CLIP, "qwen_image", "default"], outputs=[("CLIP", "CLIP")],
                  models=[{"name": CLIP, "url": CLIP_URL, "directory": "text_encoders"}])
    vae = g.node("VAELoader", (20, 295), (340, 58), [VAE], outputs=[("VAE", "VAE")],
                 models=[{"name": VAE, "url": f"{HF}/vae/{VAE}", "directory": "vae"}])
    lora = g.node("LoraLoaderModelOnly", (20, 380), (340, 82), [TURBO, 1.0], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")],
                  title="터보 LoRA (steps 8)", models=[{"name": TURBO, "url": f"{HF}/loras/{TURBO}", "directory": "loras"}])
    cache = g.node("QwenImage21Cache", (20, 490), (340, 82), ["auto", "default"], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")])
    g.link(unet, 0, lora, "model")
    g.link(lora, 0, cache, "model")
    g.group("2. 고칠 그림 = <image1>", 0, 620, 380, 380, "#8a6a3f")
    ld = g.node("LoadImage", (20, 670), (340, 310), [image_name, "image"], outputs=[("IMAGE", "IMAGE"), ("MASK", "MASK")], title=title)
    g.group("3. 고칠 것 (짧게, 고칠 것만)", 420, 0, 560, 830, "#3f8a5a")
    enc = g.node("TextEncodeQwenImage21", (440, 50), (520, 500), [prompt, "", 1024],
                 inputs=[("clip", "CLIP"), ("vae", "VAE"), ("images.image_1", "IMAGE"), ("images.image_2", "IMAGE")],
                 outputs=[("CONDITIONING", "CONDITIONING"), ("CONDITIONING", "CONDITIONING"), ("LATENT", "LATENT")], title="지시")
    g.link(clip, 0, enc, "clip")
    g.link(vae, 0, enc, "vae")
    g.link(ld, 0, enc, "images.image_1")
    g.group("4. 그리기 (캔버스 = <image1> 크기)", 1020, 0, 400, 830, "#7a3f8a")
    ks = g.node("KSampler", (1040, 50), (360, 262), [seed, "fixed" if seed else "randomize", steps, 1, "euler", "simple", 1],
                inputs=[("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                outputs=[("LATENT", "LATENT")])
    g.link(cache, 0, ks, "model")
    g.link(enc, 0, ks, "positive")
    g.link(enc, 1, ks, "negative")
    g.link(enc, 2, ks, "latent_image")
    dec = g.node("VAEDecode", (1040, 350), (360, 46), inputs=[("samples", "LATENT"), ("vae", "VAE")], outputs=[("IMAGE", "IMAGE")])
    g.link(ks, 0, dec, "samples")
    g.link(vae, 0, dec, "vae")
    sv = g.node("SaveImage", (1440, 0), (560, 830), [prefix], inputs=[("images", "IMAGE")])
    g.link(dec, 0, sv, "images")
    return g.dump()


EDIT_GUIDE = """# 편집: 그린 컷 손보기 (Qwen Image 2.1)
고칠 그림을 `<image1>` 에 넣고, 지시는 짧게 고칠 것만 쓴다. 긴 지시는 엉뚱한 곳을 바꾼다(LESSONS 16).
- 남는 사람 지우기: `Remove the young man in the light blue shirt behind the table. Fill the empty space with the matching background. Keep everything else in <image1> exactly the same.`
- 연기: `Make him cry: tears streaming down both cheeks, biting his lower lip. Keep everything else the same.`
- 옷: `Change only his jacket to a navy padded jacket. Keep everything else the same.`
작업실에서는 vast/refine.py 가 사내 Qwen 으로 남는 사람·어긋난 연출을 찾아 이 지시를 자동으로 만든다(--fix · --check). 결과는 output/webtoon_studio/edit"""

ASSET_GUIDE = """# 에셋: 표정 은행 · 기본복 (Qwen Image 2.1 편집)
인물 얼굴(또는 정면 전신)을 `<image1>` 에 넣고 바꿀 것 하나만 바꾼다. 얼굴·머리·그림체는 그대로.
- 표정 은행: 기본 지시 그대로 표정 문장만 바꾼다. 센 표정 12가지 목록은 vast/make_assets.py 의 STRONG.
- 기본복(옷 갈아입히기 출발점): 정면 전신을 넣고 아래 지시.
`{base}`
작업실에서는 `vast/make_assets.py <작품> <인물> expr|base` 가 한꺼번에 돌린다. 결과는 output/webtoon_studio/asset"""

CAND_GUIDE = """# 시안: 새 {what} (Qwen Image 2.1, 40스텝, 16:9)
새 화에 처음 나오는 {what}을 만든다. 사용자가 보고 고르면 작품 assets 에 넣는다.
{how}
작업실에서는 `vast/new_assets.py <작품> {kind} <이름>` 이 같은 구성으로 돌리고, tools/make_episode.py 가 새 {what}을 만나면 이걸 부르고 멈춘다."""


def library(force=False):
    """저장 워크플로우 한 벌(생성 · 편집 · 에셋 · 시안)을 서버에 저장한다. 그림은 예시 D+18 우빈 밤거리. 있는 이름은 force 없이는 덮지 않는다."""
    sys.path.insert(0, str(ROOT / "tools"))
    from make_assets import BASE, STRONG, expr_prompt
    from new_assets import SHEET_HEAD, candidate_prompt
    c, cache = Comfy(), {}
    s = ROOT / "series" / "d-plus-18-hf"
    vr = s / "assets" / "vast_refs"
    face, front = upload(c, vr / "woobin_bust.png", cache), upload(c, vr / "woobin_front.png", cache)
    panel = upload(c, s / "episodes" / "ep002_v2_vast" / "panels_hf" / "p043.png", cache)  # 편집 예시: 2화 vast 판 밤거리 우빈 클로즈업
    loc_prompt, _ = candidate_prompt(s, "location", "night_street")
    sheet_prompt, sheet_refs = candidate_prompt(s, "sheet", "teamlead")
    sheet_ref = upload(c, sheet_refs[0], cache)
    remove = ("Remove every person from <image1>. Fill the empty space with the matching background. "
              "Keep everything else in <image1> exactly the same: the place, lighting and framing.")
    wfs = {
        "webtoon_edit_qwen21_uc.json": build_edit("Make him look heartbroken: eyes welling up with tears, trembling pressed lips. Keep everything else in "
                                               "<image1> exactly the same.", panel, EDIT_GUIDE, title="고칠 그림 <image1>"),
        "webtoon_asset_expression_uc.json": build_edit(expr_prompt(STRONG[1]), face, ASSET_GUIDE.format(base=BASE),
                                                           prefix="webtoon_studio/asset", title="인물 얼굴 <image1>"),
        "webtoon_asset_base_uc.json": build_edit(BASE, front, ASSET_GUIDE.format(base=BASE), prefix="webtoon_studio/asset",
                                                     title="인물 정면 전신 <image1>"),
        "webtoon_candidate_location_uc.json": build(loc_prompt, [], steps=40, size=(1920, 1088), prefix="webtoon_studio/new_location",
                                                        fix_prompt=remove),
        "webtoon_candidate_sheet_uc.json": build(sheet_prompt, [sheet_ref], steps=40, size=(1920, 1088), prefix="webtoon_studio/new_sheet"),
    }
    notes = {
        "webtoon_candidate_location_uc.json": CAND_GUIDE.format(what="장소", kind="location", how=(
            "장소 글은 작품 prompts/loc_<이름>.txt. 얼굴·눈 같은 사람 문장은 빼고 '앞·가운데는 비어 있음'을 붙인다(사람을 그려 넣는다). "
            "그래도 사람이 남으면 5번 2단 묶음이 지운다.")),
        "webtoon_candidate_sheet_uc.json": CAND_GUIDE.format(what="인물 시트", kind="sheet", how=(
            "`<image1>` 은 시트 모양·그림체 본보기(다른 사람)다. 글 맨 앞에 아래 머리말을 두고, 나이를 못 박는다(중년이 젊게 나왔다).\n`"
            + SHEET_HEAD.strip() + "`")),
    }
    role = {"webtoon_edit_qwen21_uc.json": "edit", "webtoon_asset_expression_uc.json": "asset_expression", "webtoon_asset_base_uc.json": "asset_base",
            "webtoon_candidate_location_uc.json": "candidate_location", "webtoon_candidate_sheet_uc.json": "candidate_sheet"}
    for name, wf in wfs.items():  # 판(model)·역할(role) 표시: 도구가 서버에서 판마다 역할별 워크플로우를 찾는다(saved_workflow.models)
        wf.setdefault("extra", {})["webtoon"] = {"model": "uc", "role": role[name]}
    have = c.req("/api/userdata?dir=workflows")
    for name, wf in wfs.items():
        if name in notes:  # 생성 워크플로우의 사용법 칸을 시안 설명으로 바꾼다
            wf["nodes"][0]["widgets_values"] = [notes[name]]
        if ("mcp_use_" + name in have or name in have) and not force:
            print("있음(건너뜀)", name)
            continue
        name = save_ui(name, wf)
        print("서버 저장", name, "노드", len(wf["nodes"]))
    print("지금 목록:", c.req("/api/userdata?dir=workflows"))


ANIMA_GUIDE = """# 세로 웹툰 컷 (Anima)
webtoon-studio 의 Anima 판 컷 그리기. 회사 기준선 image_anima_base_v1 과 같은 모델·설정을 서브그래프 없이 펼친 것(기준선은 그대로 둔다).

**Qwen 판과 다른 점**: 글만 받는다(참고 그림 입구가 없다). 인물 생김새는 컷 설명 글에 적는다. 고치기(표정·남는 사람 지우기·검수 편집)는
작업실 도구가 Qwen UC 편집 워크플로우(webtoon_edit_qwen21_uc)로 한다.

**터보**: "터보 LoRA" 켜고 steps 8 · cfg 1(기본). 끄면 steps 30 · cfg 4(기준선 값). 작업실 도구가 켜고 끌 때 cfg 도 같이 바꾼다(extra.webtoon).

**모델**: diffusion_models/anima-base-v1.0 · text_encoders/qwen_3_06b_base(stable_diffusion) · vae/qwen-image/qwen_image_vae · loras/anima-turbo-lora-v0.2(Civitai 2560840. 예비: anima-turbo-v1.0-extracted-r64, Turbo v1.0 에서 Base 를 뺀 LoRA)

글자·말풍선은 그리지 않는다(합성 단계에서 얹는다)."""
ANIMA_NEGATIVE = ("worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia, naked, extra toes, mutated hands, "
                 "text, speech bubble, caption, comic page, multiple panels, collage, split screen, panel border")  # 기준선의 네거티브 + 한 컷용(글만 받는 모델은 '한 컷, 글자 없음' 지시를 안 따라 말풍선 있는 4분할 페이지를 그렸다, 2026-10-19)


def build_anima(prompt="", size=(1184, 1760), steps=8, seed=0, turbo=True, prefix="webtoon_studio/panel", ref=None, method="ipadapter"):
    """Anima 판 생성 워크플로우(화면 형식). 참고 그림 없이 글로만 그린다. extra.webtoon 에 터보·일반 판 cfg 와 refs=False 를 적어
    작업실 도구(saved_workflow)가 읽는다.
    ref 를 주면(인물 그림 이름) 캐릭터 참고 판: 글 + 그 인물 그림 한 장으로 얼굴·옷을 맞춘다(refs "character", 도구가 컷의 첫 인물 상반신을 꽂는다.
    2026-10-22 사용자 "anima 도 그림 보고 같은 캐릭터로 맞추는 게 있다던데"). method:
    ipadapter = anima_ref(IP-Adapter, SigLIP2, LuciferTC Character_Reference) · fasttrain = anima_ft(AnimaFastTrain: 그 그림으로 문맥 토큰을
    80스텝 학습해 덧씌움, 파일은 안 남김)."""
    w, h = size
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 640), [ANIMA_GUIDE], title="사용법")
    g.group("1. 모델 (Anima)", 0, 0, 380, 520, "#3f789e")
    unet = g.node("UNETLoader", (20, 50), (340, 82), ["anima-base-v1.0.safetensors", "default"], outputs=[("MODEL", "MODEL")])
    lora = g.node("LoraLoaderModelOnly", (20, 160), (340, 82), ["anima-turbo-lora-v0.2.safetensors", 1.0], inputs=[("model", "MODEL")],
                  outputs=[("MODEL", "MODEL")], title="터보 LoRA")
    lora["mode"] = 0 if turbo else 4
    clip = g.node("CLIPLoader", (20, 270), (340, 106), ["qwen_3_06b_base.safetensors", "stable_diffusion", "default"], outputs=[("CLIP", "CLIP")])
    vae = g.node("VAELoader", (20, 400), (340, 58), ["qwen-image/qwen_image_vae.safetensors"], outputs=[("VAE", "VAE")])
    g.group("2. 컷 설명", 400, 0, 520, 520, "#a1309b")
    pos = g.node("CLIPTextEncode", (420, 50), (480, 260), [prompt], inputs=[("clip", "CLIP")], outputs=[("CONDITIONING", "CONDITIONING")], title="컷 설명")
    neg = g.node("CLIPTextEncode", (420, 330), (480, 160), [ANIMA_NEGATIVE], inputs=[("clip", "CLIP")], outputs=[("CONDITIONING", "CONDITIONING")],
                 title="네거티브")
    g.group("3. 그리기", 940, 0, 700, 520, "#b58b2a")
    lat = g.node("EmptyLatentImage", (960, 50), (300, 106), [w, h, 1], outputs=[("LATENT", "LATENT")], title="캔버스")
    ks = g.node("KSampler", (960, 180), (300, 262), [seed, "fixed", steps, 1.0 if turbo else 4.0, "euler", "simple", 1.0],
                inputs=[("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                outputs=[("LATENT", "LATENT")])
    dec = g.node("VAEDecode", (1290, 50), (200, 46), None, inputs=[("samples", "LATENT"), ("vae", "VAE")], outputs=[("IMAGE", "IMAGE")])
    save = g.node("SaveImage", (1290, 130), (320, 360), [prefix], inputs=[("images", "IMAGE")])
    g.link(unet, 0, lora, "model")
    if ref is None:
        g.link(lora, 0, ks, "model")
    elif method == "fasttrain":
        g.group("1b. 캐릭터 참고 (AnimaFastTrain)", 0, 540, 920, 420, "#4a8a5c")
        img = g.node("LoadImage", (20, 590), (340, 240), [ref, "image"], outputs=[("IMAGE", "IMAGE"), ("MASK", "MASK")], title="인물 그림")
        # 작성자 기본값(80스텝 · lr 0.02 · 512 · 토큰 16) · 덧씌우기 0.45
        ft = g.node("AnimaFastTrainReferenceContext", (420, 590), (480, 300),
                    ["masterpiece, best quality, score_7, safe, same character, solo, clean white background, clean anime illustration",
                     0, "fixed", 80, 0.02, 512, 16, 0.02, "bf16"],
                    inputs=[("model", "MODEL"), ("clip", "CLIP"), ("vae", "VAE"), ("reference_image", "IMAGE")],
                    outputs=[("context", "ANIMA_REFERENCE_CONTEXT"), ("info", "STRING")], title="그림으로 잠깐 학습")
        patch = g.node("AnimaFastTrainPatchModel", (420, 910), (480, 106), [0.45, "positive"],
                       inputs=[("model", "MODEL"), ("context", "ANIMA_REFERENCE_CONTEXT")], outputs=[("model", "MODEL")], title="캐릭터 맞추기")
        g.link(lora, 0, ft, "model")
        g.link(clip, 0, ft, "clip")
        g.link(vae, 0, ft, "vae")
        g.link(img, 0, ft, "reference_image")
        g.link(lora, 0, patch, "model")
        g.link(ft, 0, patch, "context")
        g.link(patch, 0, ks, "model")
    else:
        g.group("1b. 캐릭터 참고 (IP-Adapter)", 0, 540, 920, 420, "#4a8a5c")
        ipl = g.node("AnimaIPAdapterLoader", (20, 590), (340, 82), ["ip_adapter-Character_Reference-10.safetensors", False],
                     outputs=[("ip_adapter", "ANIMA_IP_ADAPTER")], title="IP-Adapter")
        img = g.node("LoadImage", (20, 700), (340, 240), [ref, "image"], outputs=[("IMAGE", "IMAGE"), ("MASK", "MASK")], title="인물 그림")
        # 강도 0.8 · 참고 512 · 마지막 층 · IP CFG 4 를 따로(터보는 cfg 1 이라 글 CFG 에 묶으면 IP CFG 가 꺼진다)
        ipa = g.node("AnimaIPAdapterApply", (420, 590), (480, 300), [0.8, 512, -1, 4.0, True, False, True],
                     inputs=[("model", "MODEL"), ("ip_adapter", "ANIMA_IP_ADAPTER"), ("ref_image", "IMAGE")], outputs=[("model", "MODEL")],
                     title="캐릭터 맞추기")
        g.link(lora, 0, ipa, "model")
        g.link(ipl, 0, ipa, "ip_adapter")
        g.link(img, 0, ipa, "ref_image")
        g.link(ipa, 0, ks, "model")
    g.link(clip, 0, pos, "clip")
    g.link(clip, 0, neg, "clip")
    g.link(pos, 0, ks, "positive")
    g.link(neg, 0, ks, "negative")
    g.link(lat, 0, ks, "latent_image")
    g.link(ks, 0, dec, "samples")
    g.link(vae, 0, dec, "vae")
    g.link(dec, 0, save, "images")
    wf = g.dump()
    wf["extra"]["webtoon"] = {"name": "anima", "label": "Anima (애니메이션 그림체, 글만 받아 인물·장소를 글로 맞춘다)", "refs": False, "style": "anime",
                              "cfg": {"turbo": 1.0, "full": 4.0}, "steps": {"turbo": 8, "full": 30}}
    if ref is not None and method == "fasttrain":
        wf["extra"]["webtoon"].update(name="anima_ft", refs="character",
                                      label="Anima + 캐릭터 참고 (글 + 컷 첫 인물의 상반신 그림 한 장을 AnimaFastTrain 으로 잠깐 학습, 애니메이션 그림체)")
    elif ref is not None:
        wf["extra"]["webtoon"].update(name="anima_ref", refs="character",
                                      label="Anima + 캐릭터 참고 (글 + 컷 첫 인물의 상반신 그림 한 장을 IP-Adapter 로, 애니메이션 그림체)")
    return wf


MOSS_GUIDE = """# 세로 웹툰 컷 (MOSS-Image UC)
OpenMOSS 의 MOSS-Image SFT(5B, 2026-10-16, Apache-2.0)에 무검열 LoRA 를 얹은 판. ComfyUI 0.41.3 이상 기본 노드(서버 0.42.1).
공식 템플릿 image_moss_image_t2i 를 서브그래프 없이 펼쳤다(프롬프트를 늘려 주는 TextGenerate 단계는 뺐다: 작업실이 컷 설명으로 프롬프트를 쓴다).

**Qwen UC 판과 다른 점**: 글만 받는다(참고 그림·편집 없음). 인물 생김새는 컷 설명 글(작품 설정 who)로. 고치기·에셋은 Qwen UC 편집 워크플로우.
긴 글(4K 토큰)과 그림 속 글자에 강하다.

**터보**: "터보 LoRA"(8스텝 DMD) 켜고 steps 8 · cfg 1 · euler. 끄면 공식 값 25스텝 · cfg 2.5 · er_sde, 레이어 건너뛰기 가이드(22층).

**모델**: diffusion_models/moss_image_sft_bf16 · text_encoders/qwen3.5_9b_int8_convrot(moss_image) · vae/flux2-vae_small
· loras/moss-image-uncensored-lora-comfy(axiomofmind UC LoRA, PEFT 키를 ComfyUI 키로 바꾼 것: 원본은 키를 못 알아봐 안 붙었다) · loras/moss-image-sft-8step-dmd-lora(lodestones)"""

MOSS_NEGATIVE = "text, speech bubble, caption, watermark, comic page, multiple panels, collage, split screen, panel border, blurry, low quality, bad anatomy"


def build_moss(prompt="", size=(1184, 1760), steps=8, seed=0, turbo=True, prefix="webtoon_studio/panel"):
    """MOSS-Image UC 판 생성 워크플로우(화면 형식). extra.webtoon 에 판 이름·터보/일반 cfg·스텝·refs=False 를 적어 작업실 도구가 읽는다."""
    w, h = size
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 700), [MOSS_GUIDE], title="사용법")
    g.group("1. 모델 (MOSS-Image UC)", 0, 0, 380, 640, "#3f789e")
    unet = g.node("UNETLoader", (20, 50), (340, 82), ["moss_image_sft_bf16.safetensors", "default"], outputs=[("MODEL", "MODEL")])
    uc = g.node("LoraLoaderModelOnly", (20, 160), (340, 82), ["moss-image-uncensored-lora-comfy.safetensors", 1.0], inputs=[("model", "MODEL")],
                outputs=[("MODEL", "MODEL")], title="UC LoRA (무검열)")
    lora = g.node("LoraLoaderModelOnly", (20, 270), (340, 82), ["moss-image-sft-8step-dmd-lora.safetensors", 1.0], inputs=[("model", "MODEL")],
                  outputs=[("MODEL", "MODEL")], title="터보 LoRA (켜면 steps 8 · cfg 1)")
    lora["mode"] = 0 if turbo else 4
    slg = g.node("SkipLayerGuidanceDiTSimple", (20, 380), (340, 130), ["", "22", 0.0, 0.5], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")],
                 title="레이어 건너뛰기 가이드 (cfg 1 이면 효과 없음)")
    clip = g.node("CLIPLoader", (20, 530), (340, 106), ["qwen3.5_9b_int8_convrot.safetensors", "moss_image", "default"], outputs=[("CLIP", "CLIP")])
    vae = g.node("VAELoader", (400, 560), (340, 58), ["flux2-vae_small.safetensors"], outputs=[("VAE", "VAE")])
    g.group("2. 컷 설명", 400, 0, 520, 540, "#a1309b")
    pos = g.node("CLIPTextEncode", (420, 50), (480, 280), [prompt], inputs=[("clip", "CLIP")], outputs=[("CONDITIONING", "CONDITIONING")], title="컷 설명")
    neg = g.node("CLIPTextEncode", (420, 350), (480, 160), [MOSS_NEGATIVE], inputs=[("clip", "CLIP")], outputs=[("CONDITIONING", "CONDITIONING")],
                 title="네거티브 (cfg 1 이면 안 씀)")
    g.group("3. 그리기", 940, 0, 700, 540, "#b58b2a")
    lat = g.node("EmptyFlux2LatentImage", (960, 50), (300, 106), [w, h, 1], outputs=[("LATENT", "LATENT")], title="캔버스")
    ks = g.node("KSampler", (960, 180), (300, 262), [seed, "fixed", steps, 1.0 if turbo else 2.5, "euler" if turbo else "er_sde", "simple", 1.0],
                inputs=[("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                outputs=[("LATENT", "LATENT")])
    dec = g.node("VAEDecode", (1290, 50), (200, 46), None, inputs=[("samples", "LATENT"), ("vae", "VAE")], outputs=[("IMAGE", "IMAGE")])
    save = g.node("SaveImage", (1290, 130), (320, 360), [prefix], inputs=[("images", "IMAGE")])
    g.link(unet, 0, uc, "model")
    g.link(uc, 0, lora, "model")
    g.link(lora, 0, slg, "model")
    g.link(slg, 0, ks, "model")
    g.link(clip, 0, pos, "clip")
    g.link(clip, 0, neg, "clip")
    g.link(pos, 0, ks, "positive")
    g.link(neg, 0, ks, "negative")
    g.link(lat, 0, ks, "latent_image")
    g.link(ks, 0, dec, "samples")
    g.link(vae, 0, dec, "vae")
    g.link(dec, 0, save, "images")
    wf = g.dump()
    wf["extra"]["webtoon"] = {"name": "moss", "label": "MOSS-Image UC (OpenMOSS 5B + 무검열 LoRA, 글만 받음, 긴 글·그림 속 글자에 강함)",
                              "use_for": "글로 장면을 길게 적는 컷, 그림 속 글자(간판·화면)가 필요한 컷. 참고 그림을 못 받아 인물 얼굴 일관성은 Qwen UC 보다 약하다",
                              "refs": False, "style": "realistic", "strengths": ["text_in_image", "long_prompt"],  # vast/router.py 가 판을 고를 때 읽는다
                              "cfg": {"turbo": 1.0, "full": 2.5}, "steps": {"turbo": 8, "full": 25},
                              "sampler": {"turbo": "euler", "full": "er_sde"}, "max_pixels": 2200000}
    return wf


TRAIN_GUIDE = """# 인물 LoRA 학습 (Qwen Image 2.1 UC) — 2단계 중 ②
서버 ComfyUI 기본 노드만 쓴다(확장 설치 없음). 작업실 도구 `vast/lora.py train <작품> <인물>`(MCP lora_train)이 ① → 모델 내리기 → ② 순서로 돌린다.

① `webtoon_lora_dataset_uc`: input/ 폴더(그림 + 같은 이름 캡션 .txt) → VAE·글 인코더로 학습 데이터(잠재·글) → datasets/webtoon/<trigger> 에 저장
② 이 워크플로우: 저장된 학습 데이터 → 해상도 버킷 → TrainLoraNode(UC 모델, 앞부분 캐시 끔) → SaveLoRA · 손실 그래프

한 워크플로우로 이으면 ①에서 올린 글 인코더(약 9GB)가 VRAM 에 남은 채 학습이 돌아 메모리가 모자랐다(2026-10-19, 28.8GB 에서 멈춤). 나누면 학습만 다시 돌릴 때 ①을 건너뛴다.

**값**(이 노드 값이 도구의 기본): rank 16 · lr 0.0001 · AdamW · LoRA 는 bf16 · 기울기 체크포인트 깊이 2(블록마다, 1 은 모델 통째라 절약이 없다) · 해상도 버킷(그림 크기가 제각각이라).
모델이 fp8 이라 bypass_mode(가중치를 고치지 않고 LoRA 를 옆에 붙여 학습), 계산은 bf16. quantized_backward(fp8 역전파)는 켜지 않는다: 켜면 ComfyUI 가 학습 중 재시작됐다.

**결과**: output/loras/webtoon/<trigger>_<스텝>_steps_00001_.safetensors (LoRA 목록에 webtoon/… 로 보인다) · 손실 그래프 output/webtoon_studio/.
그릴 때는 생성 워크플로우의 '인물 LoRA' 노드에 도구가 넣고 켠다(작품의 lora/loras.json)."""

DATASET_GUIDE = """# 인물 LoRA 학습 데이터 만들기 (Qwen Image 2.1 UC) — 2단계 중 ①
input/ 아래 폴더 하나에 그림과 같은 이름의 캡션 .txt(001.png + 001.txt). 작업실 도구가 표정 은행 + 기본 전신 + 혼자 나온 잘 된 컷으로 만들어 올린다.
캡션 맨 앞 낱말(trigger, 예: d18jaemin)을 그릴 때 이름 옆에 붙인다.
결과는 datasets/webtoon/<trigger>. 다음은 ② `webtoon_train_lora_uc`."""


# 학습을 막아 둘 이유(워크플로우 데이터 extra.webtoon.disabled, 도구 lora.train·MCP lora_train 이 읽고 학습하지 않는다). 막을 일이 생기면 이유를 적는다.
# 2026-10-19 한때 막았다가 풀었다: 서버를 재시작시킨 것은 quantized_backward(fp8 역전파), 1024 메모리 초과는 checkpoint_depth 1(모델 통째),
# 크기 오류는 앞부분 캐시였다. bf16 · 깊이 2 · 캐시 끔으로 1024 학습 스텝당 약 1.9초.
# 2026-10-20 풀었다: 0.42.1 의 크래시는 Qwen 2.1 _gated_residual 의 제자리 연산(상류 PR #16654, vast/server_patches 로 덧댐)이었다.
# 덧댄 뒤 기본 실행 인자(dynamic VRAM 켬)로 학습 스텝당 약 2초. 수정이 빠지면 다시 죽으니 버전을 바꾸면 comfy_server(action="patch") 로 확인
TRAIN_DISABLED = ""

# 판마다 학습에 쓰는 모델 파일(① 글 인코더·VAE, ② 확산 모델과 그 위에 먼저 얹는 LoRA). MOSS 는 2026-10-20 조사로 정했다(worklogs/2026-10-20-moss-qwen-aux):
# 잠재 꼴이 Flux2(128채널·1/16)라 Qwen 데이터를 쓸 수 없고, Qwen 전용 앞부분 캐시는 빼고, 그리는 판과 같게 UC LoRA 위에서 학습한다.
# ModelSamplingAuraFlow shift 1.5: 기본 shift 5 로는 σ<0.5(얼굴·세부를 정하는 낮은 노이즈) 스텝이 1/6 뿐이라 닮음이 덜 배운다는 보고가 있다.
# ModelSamplingSD3 를 넣으면 시간값에 ×1000 이 두 번 걸려 망가진다(ComfyUI #17119)
# 공식 Qwen-Image-2.1 Turbo 8스텝 시그마(comfyui-wiki.com 2026-10-09, huggingface.co/Comfy-Org/Qwen-Image-2.1/discussions/22). 터보 LoRA 도 같은 증류에서 뽑았다.
# simple 스케줄러로 그리면 노이즈가 덜 걷혀 피부·배경에 잡티가 남았다(2026-10-21 외출 5컷 같은 시드 비교, exports/2026-10-21/비교/외출_터보시그마_비교.jpg)
QWEN21_TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"


def set_turbo_sigmas(sig: str = QWEN21_TURBO_SIGMAS) -> list:
    """서버 webtoon_*.json 중 Qwen 2.1 터보 LoRA 노드가 있는 워크플로우의 extra.webtoon.sigmas.turbo 를 sig 로 적는다(빈 글이면 뺀다).
    그리기 도구가 이 값으로 첫 샘플러를 바꿔 그린다(saved_workflow._custom_sigmas). 바꾼 이름 목록."""
    c = Comfy()
    done = []
    for name in [n for n in c.req("/api/userdata?dir=workflows") if isinstance(n, str) and re.fullmatch(r"(mcp_use_)?webtoon_.+\.json", n)]:
        ui = json.loads(c.req("/api/userdata/" + urllib.parse.quote("workflows/" + name, safe=""), raw=True))
        if not any(n.get("type", "").startswith("LoraLoader") and "qwen_image_2.1_turbo" in str((n.get("widgets_values") or [""])[0])
                   for n in ui.get("nodes", [])):
            continue
        meta = ui.setdefault("extra", {}).setdefault("webtoon", {})
        sg = dict(meta.get("sigmas") or {})
        if sig:
            sg["turbo"] = sig
        else:
            sg.pop("turbo", None)
        if sg:
            meta["sigmas"] = sg
        else:
            meta.pop("sigmas", None)
        c.req("/api/userdata/" + urllib.parse.quote("workflows/" + name, safe="") + "?overwrite=true", json.dumps(ui, ensure_ascii=False).encode("utf-8"))
        done.append(name)
    return done


TRAIN_MODELS = {
    "uc": {"clip": (CLIP, "qwen_image"), "vae": VAE, "unet": UNET, "pre_loras": [], "qwen_cache": True, "shift": None, "bypass": True,
           "label": "Qwen Image 2.1 UC"},
    # 공식 bf16 원본 + UC LoRA 위에서 학습해 UC 판에 붙인다(draw). 조사(2026-10-21): fp8 원본 위 학습은 크게 망가졌다는 보고,
    # UC 는 공식 가중치 + 33.6MB uncensored LoRA 를 합친 것(제작자 답) → 같은 LoRA 를 먼저 얹고 학습하면 UC 와 같은 바탕
    "uc_base": {"clip": (CLIP, "qwen_image"), "vae": VAE, "unet": "qwen_image_2.1_bf16.safetensors",
                "pre_loras": [("qwen-image-2.1-uncensored-lora.safetensors", 1.0, "UC LoRA (그리는 UC 판과 같은 바탕)")], "qwen_cache": True,
                "shift": None, "bypass": False, "label": "Qwen Image 2.1 공식 bf16 + UC LoRA", "draw": "uc", "role": "train_lora_base"},
    # 위 + SimpleTuner 학습 보조 LoRA v3(학습 때만 1.0 으로 얹고 그릴 땐 끈다, 2.1 학습 중 질감이 무너지는 것을 막는다, Qwen Research 라이선스)
    "uc_assist": {"clip": (CLIP, "qwen_image"), "vae": VAE, "unet": "qwen_image_2.1_bf16.safetensors",
                  "pre_loras": [("qwen-image-2.1-uncensored-lora.safetensors", 1.0, "UC LoRA (그리는 UC 판과 같은 바탕)"),
                                ("qwen21_training_assistant_v3.safetensors", 1.0, "학습 보조 LoRA v3 (학습 때만, 그릴 땐 끔)")],
                  "qwen_cache": True, "shift": None, "bypass": False, "label": "Qwen Image 2.1 공식 bf16 + UC LoRA + 학습 보조 v3",
                  "draw": "uc", "role": "train_lora_assist"},
    "moss": {"clip": ("qwen3.5_9b_int8_convrot.safetensors", "moss_image"), "vae": "flux2-vae_small.safetensors", "unet": "moss_image_sft_bf16.safetensors",
             "pre_loras": [("moss-image-uncensored-lora-comfy.safetensors", 1.0, "UC LoRA (그리는 판과 같게 그 위에서 학습)")], "qwen_cache": False,
             "shift": 1.5, "bypass": False, "label": "MOSS-Image SFT + UC"},
}


def build_lora_dataset(folder="3d", model="uc"):
    """① 학습 데이터 워크플로우(화면 형식): 폴더(그림+캡션) → MakeTrainingDataset(VAE·글 인코더) → SaveTrainingDataset."""
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 360), [DATASET_GUIDE], title="사용법")
    g.group("1. 인코더", 0, 0, 380, 260, "#3f789e")
    tm = TRAIN_MODELS[model]
    clip = g.node("CLIPLoader", (20, 50), (340, 106), [tm["clip"][0], tm["clip"][1], "default"], outputs=[("CLIP", "CLIP")])
    vae = g.node("VAELoader", (20, 180), (340, 58), [tm["vae"]], outputs=[("VAE", "VAE")])
    g.group("2. 데이터", 400, 0, 760, 260, "#a1309b")
    data = g.node("LoadImageTextDataSetFromFolder", (420, 50), (340, 82), [folder], outputs=[("images", "IMAGE"), ("texts", "STRING")],
                  title="데이터 폴더 (input/ 아래, 그림 + 같은 이름 .txt)")
    make = g.node("MakeTrainingDataset", (420, 150), (340, 100), [], inputs=[("images", "IMAGE"), ("vae", "VAE"), ("clip", "CLIP"), ("texts", "STRING")],
                  outputs=[("latents", "LATENT"), ("conditioning", "CONDITIONING")])
    save = g.node("SaveTrainingDataset", (800, 50), (340, 106), ["webtoon/dataset", 1000],
                  inputs=[("latents", "LATENT"), ("conditioning", "CONDITIONING")], title="학습 데이터 저장 (datasets/ 아래)")
    g.link(data, 0, make, "images")
    g.link(data, 1, make, "texts")
    g.link(vae, 0, make, "vae")
    g.link(clip, 0, make, "clip")
    g.link(make, 0, save, "latents")
    g.link(make, 1, save, "conditioning")
    wf = g.dump()
    wf["extra"]["webtoon"] = {"model": model, "role": "lora_dataset", "label": f"인물 LoRA 학습 데이터 만들기(①, {tm['label']}, 학습은 webtoon_train_lora_{model})"}
    return wf


def build_restyle_dataset(fin="restyle_in", fout="restyle_out", name="webtoon/restyle", model="uc"):
    """Restyle 학습 데이터(화면 형식, vast/restyle.py dataset 이 폴더·이름만 바꿔 돌린다): 정본(목표 그림 + 캡션) → VAEEncode(목표 잠재),
    Qwen UC 판(입력) → TextEncodeQwenImage21 images.image_1(편집 조건) + 캡션 'Convert image 1 to <그림체>.' → SaveTrainingDataset.
    목록 노드라 짝마다 한 번씩 돈다(두 폴더의 파일 이름 순서가 짝)."""
    tm = TRAIN_MODELS[model]
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 300), [
        "## Restyle 학습 데이터\n\n같은 장면의 **Qwen UC 판(입력)** 과 **정본(목표)** 짝으로 '그 작품 그림체로 바꾸기' 편집 LoRA 를 학습한다.\n\n"
        "- 목표 폴더: input/ 아래 정본 그림 + 같은 이름 .txt(캡션 'Convert image 1 to <그림체>.')\n- 입력 폴더: 같은 이름의 Qwen UC 판\n"
        "- 만든 뒤 webtoon_train_lora_uc_base 로 학습(도구: vast/restyle.py train)"], title="사용법")
    clip = g.node("CLIPLoader", (20, 50), (340, 106), [tm["clip"][0], tm["clip"][1], "default"], outputs=[("CLIP", "CLIP")])
    vae = g.node("VAELoader", (20, 180), (340, 58), [tm["vae"]], outputs=[("VAE", "VAE")])
    out = g.node("LoadImageTextDataSetFromFolder", (420, 50), (340, 82), [fout], outputs=[("images", "IMAGE"), ("texts", "STRING")],
                 title="목표: 정본 폴더 (그림 + 캡션)")
    inp = g.node("LoadImageDataSetFromFolder", (420, 170), (340, 82), [fin], outputs=[("images", "IMAGE")], title="입력: Qwen UC 판 폴더 (같은 이름)")
    enc = g.node("VAEEncode", (800, 50), (240, 60), [], inputs=[("pixels", "IMAGE"), ("vae", "VAE")], outputs=[("LATENT", "LATENT")], title="목표 잠재")
    cond = g.node("TextEncodeQwenImage21", (800, 150), (360, 200), ["", "", 1024],
                  inputs=[("clip", "CLIP"), ("vae", "VAE"), ("images.image_1", "IMAGE"), ("prompt", "STRING")],
                  outputs=[("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent", "LATENT")], title="편집 조건 (image1 = Qwen UC 판)")
    cond["inputs"][3]["widget"] = {"name": "prompt"}
    save = g.node("SaveTrainingDataset", (1200, 50), (340, 106), [name, 1000], inputs=[("latents", "LATENT"), ("conditioning", "CONDITIONING")],
                  title="학습 데이터 저장 (datasets/ 아래)")
    g.link(out, 0, enc, "pixels")
    g.link(vae, 0, enc, "vae")
    g.link(clip, 0, cond, "clip")
    g.link(vae, 0, cond, "vae")
    g.link(inp, 0, cond, "images.image_1")
    g.link(out, 1, cond, "prompt")
    g.link(enc, 0, save, "latents")
    g.link(cond, 0, save, "conditioning")
    wf = g.dump()
    wf["extra"]["webtoon"] = {"model": model, "role": "restyle_dataset", "label": "Restyle 학습 데이터(Qwen UC 판 → 정본 짝, 학습은 webtoon_train_lora_uc_base)"}
    return wf


TRAIN_NODE = "WebtoonTrainLora"  # 서버 custom_nodes/webtoon_train_lora(vast/server_nodes). 없으면 기본 TrainLoraNode


def build_train(dataset="webtoon/dataset", steps=500, rank=16, lr=1e-4, model="uc"):
    """② 인물 LoRA 학습 워크플로우(화면 형식): 저장된 학습 데이터 → 해상도 버킷 → TrainLoraNode → SaveLoRA · 손실 그래프.
    TrainLoraNode 위젯 순서는 서버 노드 정의(object_info)에서 읽는다(버전마다 칸이 늘었다)."""
    from saved_workflow import _node_spec, _widget_names
    tv = {"batch_size": 1, "grad_accumulation_steps": 1, "steps": steps, "learning_rate": lr, "rank": rank, "optimizer": "AdamW",
          # bf16 학습 + quantized_backward 끔. fp8 역전파(quantized_backward)를 켜면 ComfyUI 가 학습 중 통째로 재시작됐다(2026-10-19 두 번).
          # 메모리는 이것보다 checkpoint_depth 가 갈랐다: 깊이 1 에선 bf16·fp8 어느 쪽도 31GB 초과, 깊이 2 + bf16 은 512 에서 최대 약 17GB
          "loss_function": "MSE", "seed": 0, "training_dtype": "bf16", "lora_dtype": "bf16", "quantized_backward": False, "algorithm": "LoRA",
          # checkpoint_depth 2 = 트랜스포머 블록 하나하나. 1 은 모델 전체를 한 덩어리로 감싸(nodes_train.find_modules_at_depth 가 루트에서 1 을 센다)
          # 절약이 없어 같은 31GB 초과였다
          "gradient_checkpointing": True, "checkpoint_depth": 2, "offloading": False, "existing_lora": "[None]", "bucket_mode": True,
          "bypass_mode": TRAIN_MODELS[model]["bypass"],  # bypass 는 fp8(UC) 가중치에만. MOSS 는 bf16 이라 끈다
          # 2026-10-21(worklogs/2026-10-21-uc-optimize.md): 기본 노드는 묶음 모드에서 잠재를 정규화하지 않아 LoRA 가 그 차이를 배워 얼룩졌다.
          # 우리 노드: 묶음 잠재 정규화 · alpha=rank + PEFT 초기값(다른 트레이너의 lr 이 그대로 맞는다) · 블록 안 Linear 만 · 속도 손실,
          # LoRA 가중치 fp32, 학습률 1e-4(ai-toolkit Qwen-Image 기본, 200스텝 시험에서 강도 1 이 깨끗하고 그림체가 묻었다)
          "alpha_mode": "rank", "blocks_only": True, "loss_mode": "velocity", "init": "peft"}
    node_cls = TRAIN_NODE if _node_spec(TRAIN_NODE) else "TrainLoraNode"
    if node_cls == TRAIN_NODE:
        tv["lora_dtype"] = "fp32"
    else:
        lr = 1e-4
        tv["learning_rate"] = lr
    names = _widget_names(_node_spec(node_cls))
    miss = [n for n in names if n and n not in tv]
    if miss:
        sys.exit(f"서버 {node_cls} 에 모르는 칸이 있다: {miss}")
    g = G()
    g.node("MarkdownNote", (-470, 0), (420, 700), [TRAIN_GUIDE], title="사용법")
    g.group("1. 모델 · 학습 데이터", 0, 0, 380, 470, "#3f789e")
    tm = TRAIN_MODELS[model]
    unet = g.node("UNETLoader", (20, 50), (340, 82), [tm["unet"], "default"], outputs=[("MODEL", "MODEL")])
    chain = [unet]
    for k, (lf, st, title) in enumerate(tm["pre_loras"]):
        chain.append(g.node("LoraLoaderModelOnly", (20, 140 + 30 * k), (340, 82), [lf, st], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")], title=title))
    if tm["shift"] is not None:
        chain.append(g.node("ModelSamplingAuraFlow", (20, 170), (340, 58), [tm["shift"]], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")],
                            title="낮은 노이즈 몫 늘리기 (얼굴·세부를 더 배운다)"))
    if tm["qwen_cache"]:
        # 앞부분(글·참고 그림) K/V 캐시를 끈다. 그리기 속도용으로 기본이 켜짐(auto)인데, 학습은 스텝마다 그림 크기·캡션이 바뀌어 캐시 키 길이가 달라
        # "size of tensor a (540676) must match … b (344068)" 로 멈췄다(2026-10-19 1024 시험, qwen_image21.select_prefix_cache)
        chain.append(g.node("QwenImage21Cache", (20, 160), (340, 82), ["off", "default"], inputs=[("model", "MODEL")], outputs=[("MODEL", "MODEL")],
                            title="앞부분 캐시 끔 (학습은 스텝마다 크기가 바뀐다)"))
    data = g.node("LoadTrainingDataset", (20, 270), (340, 82), [dataset], outputs=[("latents", "LATENT"), ("conditioning", "CONDITIONING")],
                  title="학습 데이터 (① 이 datasets/ 에 저장한 것)")
    bucket = g.node("ResolutionBucket", (20, 380), (340, 60), [], inputs=[("latents", "LATENT"), ("conditioning", "CONDITIONING")],
                    outputs=[("latents", "LATENT"), ("conditioning", "CONDITIONING")])
    g.group("2. 학습", 400, 0, 760, 620, "#b58b2a")
    train = g.node(node_cls, (420, 50), (360, 620), [tv[n] for n in names if n],
                   inputs=[("model", "MODEL"), ("latents", "LATENT"), ("positive", "CONDITIONING")],
                   outputs=[("lora", "LORA_MODEL"), ("loss_map", "LOSS_MAP"), ("steps", "INT")])
    save = g.node("SaveLoRA", (800, 50), (340, 82), ["loras/webtoon/lora", steps], inputs=[("lora", "LORA_MODEL")])
    loss = g.node("LossGraphNode", (800, 170), (340, 300), ["webtoon_studio/lora_loss"], inputs=[("loss", "LOSS_MAP")])
    # SaveLoRA 의 steps(파일 이름에 들어가는 스텝 수)는 도구가 학습 스텝과 같은 값으로 채운다
    g.link(data, 0, bucket, "latents")
    g.link(data, 1, bucket, "conditioning")
    for x, y in zip(chain, chain[1:]):
        g.link(x, 0, y, "model")
    g.link(chain[-1], 0, train, "model")
    g.link(bucket, 0, train, "latents")
    g.link(bucket, 1, train, "positive")
    g.link(train, 0, save, "lora")
    g.link(train, 1, loss, "loss")
    wf = g.dump()
    wf["extra"]["webtoon"] = {"model": tm.get("draw", model), "role": tm.get("role", "train_lora"), "label": f"LoRA 학습(②, {tm['label']}, {node_cls})"}
    if TRAIN_DISABLED:
        wf["extra"]["webtoon"]["disabled"] = TRAIN_DISABLED
    return wf


def main():
    if sys.argv[1:2] == ["restyle"]:  # make_workflow.py restyle → Restyle 학습 데이터 워크플로우(편집 마무리 워크플로우는 aux_roles.py restyle)
        wf = build_restyle_dataset()
        name = "webtoon_restyle_dataset_uc.json"
        name = save_ui(name, wf)
        return print("서버 저장", name, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
    if sys.argv[1:2] == ["sigmas"]:  # make_workflow.py sigmas [off] → 터보 워크플로우에 공식 시그마를 적는다(off 면 뺀다)
        return print("터보 시그마", "뺌" if sys.argv[2:3] == ["off"] else "적음", set_turbo_sigmas("" if sys.argv[2:3] == ["off"] else QWEN21_TURBO_SIGMAS))
    if sys.argv[1:2] == ["library"]:
        return library("--force" in sys.argv)
    if sys.argv[1:2] == ["train"]:  # 인물 LoRA 학습 워크플로우 둘(① 데이터 ② 학습, 2026-10-19 사용자 "로라 쓰는 거 필요해, 노드 만들어서 워크플로우 구성해서, UC 기반")
        c = Comfy()
        mdl = sys.argv[2] if len(sys.argv) > 2 else "uc"  # make_workflow.py train moss → MOSS 판 학습 워크플로우 둘
        pairs = [(f"webtoon_train_lora_{mdl}.json", build_train(model=mdl))]
        if "draw" not in TRAIN_MODELS[mdl]:  # 다른 판에 붙이는 학습(uc_base)은 그 판의 학습 데이터 워크플로우를 같이 쓴다
            pairs.insert(0, (f"webtoon_lora_dataset_{mdl}.json", build_lora_dataset(model=mdl)))
        for name, wf in pairs:
            name = save_ui(name, wf)
            print("서버 저장", name, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
        return
    if sys.argv[1:2] == ["moss"]:  # MOSS-Image UC 판(2026-10-19 사용자 "MOSS-IMAGE 도 vast ComfyUI 워크플로우로, UC 로 최적화")
        wf = build_moss()
        c = Comfy()
        name = "webtoon_vertical_moss.json"
        name = save_ui(name, wf)
        return print("서버 저장", name, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
    if sys.argv[1:2] in (["anima_ref"], ["anima_ft"]):  # Anima 캐릭터 참고 판. 서버에 그 노드(ComfyUI-Anima_IP-Adapter · ComfyUI-AnimaFastTrain)가 있어야 돈다
        ft = sys.argv[1] == "anima_ft"
        wf = build_anima(ref="example.png", method="fasttrain" if ft else "ipadapter")
        name = save_ui(f"webtoon_vertical_{sys.argv[1]}.json", wf)
        return print("서버 저장", name, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
    if sys.argv[1:2] == ["anima"]:  # Anima 판 생성 워크플로우를 서버에 저장(2026-10-19 사용자 "anima 버전 하나 만들어주라")
        wf = build_anima()
        c = Comfy()
        name = "webtoon_vertical_anima.json"
        name = save_ui(name, wf)
        return print("서버 저장", name, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="mcp_use_webtoon_vertical_qwen21_uc.json")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--ref", action="append", default=[], help="참고 그림(순서대로 <image1>…), 서버 input/ 에 올린다")
    ap.add_argument("--aspect", default="2:3")
    ap.add_argument("--out", required=True, help="로컬에 남길 사본 경로")
    ap.add_argument("--fix-prompt", help="주면 2단 다듬기(편집) 노드를 붙인다. 이 글이 기본 지시")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.name.startswith("image_anima"):
        sys.exit("기준선 이름은 쓰지 않는다")
    c = Comfy()
    cache = {}
    names = [upload(c, Path(r), cache) for r in args.ref]
    wf = build(Path(args.prompt_file).read_text(encoding="utf-8"), names, args.aspect, fix_prompt=args.fix_prompt)
    Path(args.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("만듦", args.out, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
    if args.save:
        if args.name in c.req("/api/userdata?dir=workflows") and not args.force:
            sys.exit(f"서버에 {args.name} 이 이미 있다(--force 로 덮기)")
        c.req("/api/userdata/" + urllib.parse.quote("workflows/" + args.name, safe="") + "?overwrite=true",
              json.dumps(wf, ensure_ascii=False).encode("utf-8"))
        print("서버 저장", args.name, "→ 지금 목록:", c.req("/api/userdata?dir=workflows"))


if __name__ == "__main__":
    main()
