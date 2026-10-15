"""서버 ComfyUI 화면에서 열어 쓰는 세로 웹툰 워크플로우(UI 형식)를 만들어 저장한다. 기준선 image_anima_base_v1.json 은 건드리지 않는다.
comfy_panel.py 가 큐에 넣는 그래프와 같은 구성(Qwen Image 2.1 + 참고 그림 3장 + 세로 2:3 약 2MP)을 사람이 손으로 돌릴 수 있게 펼쳐 놓은 것.

    .venv/Scripts/python vast/make_workflow.py [--name webtoon_vertical_qwen21.json] [--prompt-file 컷.txt] [--ref 그림 ...] [--save]
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
- diffusion_models/qwen_image_2.1_bf16.safetensors
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="webtoon_vertical_qwen21.json")
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
