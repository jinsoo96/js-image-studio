"""서버 ComfyUI 화면에서 열어 쓰는 세로 웹툰 워크플로우(UI 형식)를 만들어 저장한다. 기준선 image_anima_base_v1.json 은 건드리지 않는다.
comfy_panel.py 가 큐에 넣는 그래프와 같은 구성(Qwen Image 2.1 + 참고 그림 3장 + 세로 2:3 약 2MP)을 사람이 손으로 돌릴 수 있게 펼쳐 놓은 것.

    .venv/Scripts/python vast/make_workflow.py [--name webtoon_vertical_qwen21_uc.json] [--prompt-file 컷.txt] [--ref 그림 ...] [--save]
--save 없이 돌리면 파일만 만든다(scratch). 같은 이름이 서버에 있으면 --force 없이는 덮지 않는다.
"""
import argparse
import json
import sys
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import CLIP, SIZES, TURBO, UNET, VAE, upload  # noqa: E402

HF = "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main"
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
- text_encoders/qwen3vl_8b_int8_convrot.safetensors
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
                  models=[{"name": CLIP, "url": f"{HF}/text_encoders/{CLIP}", "directory": "text_encoders"}])
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
                  models=[{"name": CLIP, "url": f"{HF}/text_encoders/{CLIP}", "directory": "text_encoders"}])
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
        "webtoon_asset_expression_qwen21.json": build_edit(expr_prompt(STRONG[1]), face, ASSET_GUIDE.format(base=BASE),
                                                           prefix="webtoon_studio/asset", title="인물 얼굴 <image1>"),
        "webtoon_asset_base_qwen21.json": build_edit(BASE, front, ASSET_GUIDE.format(base=BASE), prefix="webtoon_studio/asset",
                                                     title="인물 정면 전신 <image1>"),
        "webtoon_candidate_location_qwen21.json": build(loc_prompt, [], steps=40, size=(1920, 1088), prefix="webtoon_studio/new_location",
                                                        fix_prompt=remove),
        "webtoon_candidate_sheet_qwen21.json": build(sheet_prompt, [sheet_ref], steps=40, size=(1920, 1088), prefix="webtoon_studio/new_sheet"),
    }
    notes = {
        "webtoon_candidate_location_qwen21.json": CAND_GUIDE.format(what="장소", kind="location", how=(
            "장소 글은 작품 prompts/loc_<이름>.txt. 얼굴·눈 같은 사람 문장은 빼고 '앞·가운데는 비어 있음'을 붙인다(사람을 그려 넣는다). "
            "그래도 사람이 남으면 5번 2단 묶음이 지운다.")),
        "webtoon_candidate_sheet_qwen21.json": CAND_GUIDE.format(what="인물 시트", kind="sheet", how=(
            "`<image1>` 은 시트 모양·그림체 본보기(다른 사람)다. 글 맨 앞에 아래 머리말을 두고, 나이를 못 박는다(중년이 젊게 나왔다).\n`"
            + SHEET_HEAD.strip() + "`")),
    }
    have = c.req("/api/userdata?dir=workflows")
    for name, wf in wfs.items():
        if name in notes:  # 생성 워크플로우의 사용법 칸을 시안 설명으로 바꾼다
            wf["nodes"][0]["widgets_values"] = [notes[name]]
        if name in have and not force:
            print("있음(건너뜀)", name)
            continue
        c.req("/api/userdata/" + urllib.parse.quote("workflows/" + name, safe="") + "?overwrite=true", json.dumps(wf, ensure_ascii=False).encode("utf-8"))
        (ROOT / "vast" / name).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
        print("서버 저장", name, "노드", len(wf["nodes"]))
    print("지금 목록:", c.req("/api/userdata?dir=workflows"))


ANIMA_GUIDE = """# 세로 웹툰 컷 (Anima)
webtoon-studio 의 Anima 판 컷 그리기. 회사 기준선 image_anima_base_v1 과 같은 모델·설정을 서브그래프 없이 펼친 것(기준선은 그대로 둔다).

**Qwen 판과 다른 점**: 글만 받는다(참고 그림 입구가 없다). 인물 생김새는 컷 설명 글에 적는다. 고치기(표정·남는 사람 지우기·검수 편집)는
작업실 도구가 Qwen UC 편집 워크플로우(webtoon_edit_qwen21_uc)로 한다.

**터보**: "터보 LoRA" 켜고 steps 8 · cfg 1(기본). 끄면 steps 30 · cfg 4(기준선 값). 작업실 도구가 켜고 끌 때 cfg 도 같이 바꾼다(extra.webtoon).

**모델**: diffusion_models/anima-base-v1.0 · text_encoders/qwen_3_06b_base(stable_diffusion) · vae/qwen-image/qwen_image_vae · loras/anima-turbo-lora-v0.2

글자·말풍선은 그리지 않는다(합성 단계에서 얹는다)."""
ANIMA_NEGATIVE = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia, naked, extra toes, mutated hands"  # 기준선의 네거티브


def build_anima(prompt="", size=(1184, 1760), steps=8, seed=0, turbo=True, prefix="webtoon_studio/panel"):
    """Anima 판 생성 워크플로우(화면 형식). 참고 그림 없이 글로만 그린다. extra.webtoon 에 터보·일반 판 cfg 와 refs=False 를 적어
    작업실 도구(saved_workflow)가 읽는다."""
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
    g.link(lora, 0, ks, "model")
    g.link(clip, 0, pos, "clip")
    g.link(clip, 0, neg, "clip")
    g.link(pos, 0, ks, "positive")
    g.link(neg, 0, ks, "negative")
    g.link(lat, 0, ks, "latent_image")
    g.link(ks, 0, dec, "samples")
    g.link(vae, 0, dec, "vae")
    g.link(dec, 0, save, "images")
    wf = g.dump()
    wf["extra"]["webtoon"] = {"refs": False, "cfg": {"turbo": 1.0, "full": 4.0}, "steps": {"turbo": 8, "full": 30}}
    return wf


def main():
    if sys.argv[1:2] == ["library"]:
        return library("--force" in sys.argv)
    if sys.argv[1:2] == ["anima"]:  # Anima 판 생성 워크플로우를 서버에 저장(2026-10-19 사용자 "anima 버전 하나 만들어주라")
        wf = build_anima()
        c = Comfy()
        name = "webtoon_vertical_anima.json"
        c.req("/api/userdata/" + urllib.parse.quote("workflows/" + name, safe="") + "?overwrite=true", json.dumps(wf, ensure_ascii=False).encode("utf-8"))
        (ROOT / "vast" / name).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
        return print("서버 저장", name, "노드", len(wf["nodes"]), "연결", len(wf["links"]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="webtoon_vertical_qwen21_uc.json")
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
