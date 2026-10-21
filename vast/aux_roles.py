"""Qwen UC 보조 역할 워크플로우 셋을 서버에 만든다(지금 서버의 그리기·편집 워크플로우에 노드를 더해 새 이름으로, 원본은 그대로).
(2026-10-20 사용자 "진행" — 보조 도구 시험(worklogs/2026-10-20-moss-qwen-aux.md)에서 쓸 만했던 셋을 판 역할로)

  webtoon_layout_lock_uc   (role layout_lock) 그리기 + Fun ControlNet: 같은 장면 앞 컷의 선(Canny)으로 사람 자리·테이블 배치를 고정.
                           시험: 0.6 은 배치를 지켰고 0.4·없이는 좌우가 바뀌었다. 0.55 로 낮췄더니 터보 8스텝에서 또 바뀌어(외출 p016) 0.7 · 끝 0.6.
  webtoon_next_scene_uc    (role next_scene)  편집 + Next-Scene LoRA 0.8: 같은 사람이 이어 나오는 클로즈업을 앞 컷에서 잇는다(얼굴·옷·배경·그림체 유지).
  webtoon_asset_sheet_uc   (role asset_sheet) 편집 + 캐릭터 시트 LoRA 1.0: 서 있는 전신 → 얼굴 + 앞·뒤·옆·3/4 전신 시트(터보 끔 25스텝, 작성자 권장).
  webtoon_restyle_uc       (role restyle)     편집 + Restyle LoRA(작품마다 도구가 넣는다, 옆길) 25스텝: 1단 Qwen UC 초안을 그 작품 그림체로 마무리.
                           'Convert image 1 to <그림체>.' (vast/restyle.py, 2026-10-21 짝 데이터 Restyle). aux_roles.py restyle 로 이것만 저장.
  webtoon_head_swap_uc     (role head_swap)   편집 + BFS head swap LoRA 1.0(옆길): <image1> 컷의 얼굴·머리만 <image2> 인물로, 시선·고개·표정은 컷 그대로
                           (2026-10-21 조사: 얼굴 고치기가 정면 참고 그림을 따라 시선을 카메라로 돌렸다. BFS 는 시선·고개를 image1 에서 가져오라고 학습됨, MIT).

    .venv/Scripts/python vast/aux_roles.py          세 워크플로우를 서버에 저장(vast/ 에도 사본)
"""
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402
from saved_workflow import _node_spec, _widget_names, load_ui, models, save_ui  # noqa: E402

CONTROLNET = "qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors"
NEXT_SCENE = "qwen21_next_scene_step2500.safetensors"
SHEET = "qwen21_character_sheet_edit_e9.safetensors"
HEAD_SWAP = "bfs_head_v1.1_qwen_2.1.safetensors"  # huggingface.co/Alissonerdx/BFS-Best-Face-Swap


def _widgets(cls: str, vals: dict) -> list:
    return [vals.get(n) for n in _widget_names(_node_spec(cls)) if n]


def _add(ui: dict, cls: str, pos, vals: dict, inputs: list, outputs: list, title: str = "") -> dict:
    nid = max(n["id"] for n in ui["nodes"]) + 1
    n = {"id": nid, "type": cls, "pos": list(pos), "size": [340, 100], "flags": {}, "order": len(ui["nodes"]), "mode": 0,
         "inputs": [{"name": nm, "type": t, "link": None} for nm, t in inputs],
         "outputs": [{"name": nm, "type": t, "links": [], "slot_index": k} for k, (nm, t) in enumerate(outputs)],
         "properties": {"Node name for S&R": cls}, "widgets_values": _widgets(cls, vals)}
    if title:
        n["title"] = title
    ui["nodes"].append(n)
    return n


def _link(ui: dict, src: dict, slot: int, dst: dict, name: str):
    lid = max([lk[0] for lk in ui["links"]] + [ui.get("last_link_id", 0)]) + 1
    inp = next(i for i in dst["inputs"] if i["name"] == name)
    if inp.get("link") is not None:  # 이미 꽂힌 선은 뺀다
        ui["links"] = [lk for lk in ui["links"] if lk[0] != inp["link"]]
        for n in ui["nodes"]:
            for o in n.get("outputs", []):
                o["links"] = [x for x in (o.get("links") or []) if x != inp["link"]]
    typ = src["outputs"][slot]["type"]
    ui["links"].append([lid, src["id"], slot, dst["id"], next(k for k, i in enumerate(dst["inputs"]) if i["name"] == name), typ])
    src["outputs"][slot]["links"].append(lid)
    inp["link"] = lid
    ui["last_link_id"] = lid
    ui["last_node_id"] = max(n["id"] for n in ui["nodes"])


def _node(ui: dict, cls: str, pred=lambda n: True) -> dict:
    return next(n for n in ui["nodes"] if n["type"] == cls and pred(n))


def _insert_before_cache(ui: dict, new: dict):
    """모델 사슬에서 QwenImage21Cache 바로 앞에 new(MODEL→MODEL)를 끼운다."""
    cache = _node(ui, "QwenImage21Cache")
    lid = next(i for i in cache["inputs"] if i["name"] == "model")["link"]
    lk = next(x for x in ui["links"] if x[0] == lid)
    src = next(n for n in ui["nodes"] if n["id"] == lk[1])
    _link(ui, src, lk[2], new, "model")
    _link(ui, new, 0, cache, "model")


def layout_lock(base: str) -> dict:
    ui = load_ui(base)
    img = _add(ui, "LoadImage", (-400, 1000), {"image": "example.png", "upload": "image"}, [], [("IMAGE", "IMAGE"), ("MASK", "MASK")],
               title="배치 기준 컷 (같은 장면 앞 컷, 도구가 넣는다)")
    scale = _add(ui, "ImageScale", (-400, 1340), {"upscale_method": "lanczos", "width": 1184, "height": 1760, "crop": "center"},
                 [("image", "IMAGE")], [("IMAGE", "IMAGE")], title="기준 컷을 캔버스 크기로 (도구가 맞춘다)")
    canny = _add(ui, "Canny", (-400, 1480), {"low_threshold": 0.15, "high_threshold": 0.35}, [("image", "IMAGE")], [("IMAGE", "IMAGE")], title="선 따기")
    patch = _add(ui, "ModelPatchLoader", (-400, 1620), {"name": CONTROLNET}, [], [("MODEL_PATCH", "MODEL_PATCH")])
    cn = _add(ui, "ZImageFunControlnet", (-400, 1720), {"strength": 0.7, "start_percent": 0.0, "end_percent": 0.6},
              [("model", "MODEL"), ("model_patch", "MODEL_PATCH"), ("vae", "VAE"), ("image", "IMAGE")], [("MODEL", "MODEL")],
              title="배치 고정 (강도 0.7 · 끝 0.6: 0.55 이하는 두 사람 좌우가 바뀌었다)")
    _link(ui, img, 0, scale, "image")
    _link(ui, scale, 0, canny, "image")
    _link(ui, patch, 0, cn, "model_patch")
    _link(ui, _node(ui, "VAELoader"), 0, cn, "vae")
    _link(ui, canny, 0, cn, "image")
    _insert_before_cache(ui, cn)
    meta = dict((ui.get("extra") or {}).get("webtoon") or {})
    meta.update(model="uc", role="layout_lock", label="같은 장면 배치 고정(Fun ControlNet canny, 앞 컷의 선으로 사람 자리·테이블 배치)",
                control={"strength": 0.7, "end": 0.6})
    meta.pop("name", None)
    ui.setdefault("extra", {})["webtoon"] = meta
    return ui


def lora_edit(base: str, lora: str, strength: float, role: str, label: str, steps: int, loader: str = "LoraLoaderModelOnly") -> dict:
    ui = load_ui(base)
    lz = _add(ui, loader, (-400, 400), {"lora_name": lora, "strength_model": strength}, [("model", "MODEL")], [("MODEL", "MODEL")], title=label)
    _insert_before_cache(ui, lz)
    ks = _node(ui, "KSampler")
    names = _widget_names(_node_spec("KSampler"))  # 시드 뒤에 이름 없는 칸(control_after_generate)이 낀다: 자리로 고친다
    wv = list(ks["widgets_values"])
    wv[names.index("steps")], wv[names.index("cfg")] = steps, 1.0
    ks["widgets_values"] = wv
    _node(ui, "LoraLoaderModelOnly", lambda n: "turbo" in str((n.get("widgets_values") or [""])[0]).lower())["mode"] = 4  # 터보 끔(시트는 터보가 격자를 깬다)
    ui.setdefault("extra", {})["webtoon"] = {"model": "uc", "role": role, "label": label, "steps": steps, "turbo": False}
    return ui


RESTYLE_TITLE = "Restyle LoRA"  # vast/restyle.py 가 이 제목으로 시작하는 노드에 그 작품 LoRA 를 넣는다


def restyle_edit(base: str) -> dict:
    lz = next((f for f in Comfy().req("/models/loras") or [] if "restyle" in f), "webtoon/restyle.safetensors")
    return lora_edit(base, lz, 1.0, "restyle", f"{RESTYLE_TITLE} (작품 그림체, 도구가 작품마다 넣는다) · 25스텝 · 'Convert image 1 to <그림체>.'",
                     25, loader="LoraLoaderBypassModelOnly")


def main():
    m = models(refresh=True)["uc"]
    c = Comfy()
    if sys.argv[1:2] == ["restyle"]:
        ui = restyle_edit(m["edit"])
        name = "webtoon_restyle_uc.json"
        name = save_ui(name, ui)
        return print("서버 저장", name, "노드", len(ui["nodes"]), "· uc 역할:", models(refresh=True)["uc"]["own_roles"])
    for name, ui in (("webtoon_layout_lock_uc.json", layout_lock(m["gen"])),
                     ("webtoon_next_scene_uc.json", lora_edit(m["edit"], NEXT_SCENE, 0.8, "next_scene",
                                                               "앞 컷에서 잇기 (Next-Scene LoRA 0.8, 글 앞에 'Next Scene:')", 25)),
                     ("webtoon_asset_sheet_uc.json", lora_edit(m["edit"], SHEET, 1.0, "asset_sheet",
                                                                "캐릭터 시트 LoRA (서 있는 전신 → 얼굴 + 앞·뒤·옆·3/4 전신, 25스텝)", 25)),
                     # 옆길(bypass): fp8 가중치에 섞으면 학습한 LoRA 가 얼룩졌다(2026-10-21). 얼굴 편집은 터보 말고 25스텝(조사: 4~8스텝은 정체성 유지가 약함)
                     ("webtoon_head_swap_uc.json", lora_edit(m["edit"], HEAD_SWAP, 1.0, "head_swap",
                                                              "얼굴 바꾸기 BFS head swap 1.0 (<image1> 컷 · <image2> 얼굴, 25스텝)", 25,
                                                              loader="LoraLoaderBypassModelOnly"))):
        name = save_ui(name, ui)
        print("서버 저장", name, "노드", len(ui["nodes"]))
    roles = models(refresh=True)["uc"]["own_roles"]
    print("uc 역할:", roles)


if __name__ == "__main__":
    main()
