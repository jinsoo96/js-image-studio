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


def build(prompt, ref_names, aspect="2:3", steps=40):
    w, h = SIZES[aspect]
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
    lora["mode"] = 4
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
    lat = g.node("EmptyLatentImage", (1040, 50), (360, 106), [w, h, 1], outputs=[("LATENT", "LATENT")], title=f"세로 캔버스 {aspect} ({w}x{h})")
    ks = g.node("KSampler", (1040, 190), (360, 262), [0, "randomize", steps, 1, "euler", "simple", 1],
                inputs=[("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"), ("latent_image", "LATENT")],
                outputs=[("LATENT", "LATENT")])
    g.link(cache, 0, ks, "model")
    g.link(enc, 0, ks, "positive")
    g.link(enc, 1, ks, "negative")
    g.link(lat, 0, ks, "latent_image")
    dec = g.node("VAEDecode", (1040, 490), (360, 46), inputs=[("samples", "LATENT"), ("vae", "VAE")], outputs=[("IMAGE", "IMAGE")])
    g.link(ks, 0, dec, "samples")
    g.link(vae, 0, dec, "vae")
    sv = g.node("SaveImage", (1440, 0), (560, 830), ["webtoon_vertical/panel"], inputs=[("images", "IMAGE")])
    g.link(dec, 0, sv, "images")
    return g.dump()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="webtoon_vertical_qwen21.json")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--ref", action="append", default=[], help="참고 그림(순서대로 <image1>…), 서버 input/ 에 올린다")
    ap.add_argument("--aspect", default="2:3")
    ap.add_argument("--out", required=True, help="로컬에 남길 사본 경로")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.name.startswith("image_anima"):
        sys.exit("기준선 이름은 쓰지 않는다")
    c = Comfy()
    cache = {}
    names = [upload(c, Path(r), cache) for r in args.ref]
    wf = build(Path(args.prompt_file).read_text(encoding="utf-8"), names, args.aspect)
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
