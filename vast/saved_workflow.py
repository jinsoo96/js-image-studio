"""vast ComfyUI 에 저장된 워크플로우(화면용 UI 형식)를 불러 API 형식으로 바꾸고, 그리기 도구가 정하는 입력값만 채운다.
(2026-10-17 사용자 "vast ComfyUI 에 짜 놓은 워크플로우를 불러서 도구로 쓰는 구조 아닌가": 전에는 같은 구성을 코드로 다시 짰다)
화면에서 워크플로우를 고치면(모델·LoRA·샘플러·cfg·네거티브, 노드 추가) 작업실 도구(agent app MCP)와 agent platform 카탈로그 등록이 그대로 따른다.
코드가 채우는 것: 프롬프트 · 참고 그림 · 참고 그림 크기 · 캔버스 크기 · 시드 · 스텝 · 터보 LoRA 켜기/끄기 · 저장 이름.

    from saved_workflow import GEN, EDIT, load_ui, to_api, fill
    g = fill(to_api(load_ui(GEN), turbo=True), prompt, ["loc.png", "face.png"], 1184, 1760, seed, 8, 1024)
"""
import copy
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from comfy_client import Comfy

# 그리는 판 = vast ComfyUI 에 저장된 웹툰 워크플로우(2026-10-19 사용자 "하드코딩 말고 vastai 에서 설정할 수 있는 모델을 따르게").
# 서버의 workflows/ 에서 `webtoon_vertical_<이름>.json` 을 찾아 판으로 본다: 화면에서 저장하면 새 판이 생기고 지우면 사라진다.
# 판의 성질은 워크플로우의 extra.webtoon 에 적는다(없으면 이름·노드에서 읽는다):
#   name(판 이름, 없으면 파일 이름의 <이름>) · label(사람이 읽는 설명) · refs(참고 그림을 받나, 기본 True) · edit(고치기 워크플로우 파일)
#   · default(true 면 작품에 판이 없을 때 쓰는 기본 판) · cfg {turbo, full} · steps {turbo, full}
# 고치기 워크플로우는 판의 edit → webtoon_edit_<이름>.json → 기본 판의 것 순서로 찾는다.
# 서버에 닿지 않으면 마지막으로 읽은 목록, 그것도 없으면 OFFLINE 의 이름만(그리기는 서버가 있어야 하니 그때는 어차피 실패한다).
OFFLINE = {"uc": ("webtoon_vertical_qwen21_uc.json", "webtoon_edit_qwen21_uc.json")}
_models, _models_at = {}, 0.0
MODELS_TTL = 60.0


def models(refresh: bool = False) -> dict:
    """{판 이름: {"gen": 파일, "edit": 파일, "refs": bool, "label": 설명, "default": bool, "models": {노드: 모델 파일}, "missing": [없는 모델 파일]}}.
    서버 워크플로우 목록에서 읽고 60초 동안 재사용한다."""
    global _models, _models_at
    if _models and not refresh and time.time() - _models_at < MODELS_TTL:
        return _models
    try:
        c = _c()
        names = [x for x in (c.req("/api/userdata?dir=workflows&recurse=true") or []) if isinstance(x, str)]
        have = {f: set(c.req(f"/models/{f}") or []) for f in ("diffusion_models", "loras", "text_encoders", "vae")}
    except Exception:  # noqa: BLE001
        if _models:
            return _models
        return {k: {"gen": g, "edit": e, "refs": True, "label": k, "default": k == "uc", "models": {}, "missing": []} for k, (g, e) in OFFLINE.items()}
    out = {}
    for fn in sorted(n for n in names if re.fullmatch(r"webtoon_vertical_.+\.json", n)):
        suffix = fn[len("webtoon_vertical_"):-len(".json")]
        try:
            ui = load_ui(fn)
        except Exception:  # noqa: BLE001
            continue
        meta = (ui.get("extra") or {}).get("webtoon") or {}
        nodes = ui.get("nodes", []) + [n for sg in (ui.get("definitions") or {}).get("subgraphs", []) for n in sg.get("nodes", [])]
        files = {}
        for n in nodes:
            v = (n.get("widgets_values") or [None])[0]
            folder = {"UNETLoader": "diffusion_models", "LoraLoaderModelOnly": "loras", "CLIPLoader": "text_encoders", "VAELoader": "vae"}.get(n.get("type"))
            if folder and isinstance(v, str) and n.get("mode", 0) != 2:
                files.setdefault(folder, []).append(v)
        unet = (files.get("diffusion_models") or ["?"])[0]
        edit = meta.get("edit") or (f"webtoon_edit_{suffix}.json" if f"webtoon_edit_{suffix}.json" in names else None)
        out[str(meta.get("name") or suffix).lower()] = {
            "gen": fn, "edit": edit, "refs": bool(meta.get("refs", True)), "label": meta.get("label") or unet,
            "default": bool(meta.get("default")), "models": {k: v for k, v in files.items()},
            "missing": [f"{k}/{v}" for k, vs in files.items() for v in vs if v not in have.get(k, set())]}
    if out:
        dflt = next((k for k, v in out.items() if v["default"]), None) or ("uc" if "uc" in out else sorted(out)[0])
        out[dflt]["default"] = True
        for v in out.values():  # 고치기 워크플로우가 없는 판은 기본 판의 것을 쓴다
            v["edit"] = v["edit"] or out[dflt]["edit"]
        _models, _models_at = out, time.time()
    return _models or {k: {"gen": g, "edit": e, "refs": True, "label": k, "default": k == "uc", "models": {}, "missing": []} for k, (g, e) in OFFLINE.items()}


def default_model() -> str:
    m = models()
    return next((k for k, v in m.items() if v.get("default")), "uc")


def pick(series=None) -> tuple:
    """(판 이름, 생성 워크플로우, 편집 워크플로우). 환경변수 WEBTOON_MODEL 이 있으면 그것, 없으면 작품 설정의 model, 없으면 기본 판.
    이름이 서버에 없으면 기본 판으로 돌린다."""
    m = models()
    name = os.environ.get("WEBTOON_MODEL", "").strip().lower()
    if not name and series:
        sj = Path(series) / "series.json"
        if sj.exists():
            name = str(json.loads(sj.read_text(encoding="utf-8")).get("model") or "").strip().lower()
    name = name if name in m else default_model()
    return (name, m[name]["gen"], m[name]["edit"])


def takes_refs(ui: dict) -> bool:
    """이 워크플로우가 참고 그림을 받는가(워크플로우의 extra.webtoon.refs, 없으면 받는다고 본다)."""
    return bool(((ui.get("extra") or {}).get("webtoon") or {}).get("refs", True))


def refs_model() -> str:
    """참고 그림을 받는 판 중 기본 판(참고 그림이 꼭 필요한 일을 글만 받는 판이 맡게 됐을 때 대신 쓴다)."""
    m = models()
    ok = {k: v for k, v in m.items() if v["refs"]}
    return default_model() if default_model() in ok else (sorted(ok)[0] if ok else default_model())


MODEL = os.environ.get("WEBTOON_MODEL", "").strip().lower() or None  # 하위 호환(예전엔 이 환경변수로만 골랐다)
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
# 부르는 쪽이 노드를 찾는 번호. 전에 코드로 짜던 그래프와 같은 번호라 comfy_panel·refine·make_assets·xgen_catalog 는 그대로 쓴다.
# 클래스가 워크플로우에 하나뿐일 때만 붙이고, 화면에서 더한 노드는 u<번호>.
CANON = {"UNETLoader": "1", "CLIPLoader": "2", "VAELoader": "3", "QwenImage21Cache": "4", "TextEncodeQwenImage21": "5",
         "EmptyLatentImage": "6", "KSampler": "7", "VAEDecode": "8", "SaveImage": "9", "LoraLoaderModelOnly": "10"}

_client, _ui, _spec = None, {}, {}


def _c():
    global _client
    if _client is None:
        _client = Comfy()
    return _client


def load_ui(name: str) -> dict:
    """서버 workflows/<name> (한 번 부른 건 이 실행 동안 재사용)."""
    if name not in _ui:
        r = _c().req("/api/userdata/" + urllib.parse.quote("workflows/" + name, safe=""))
        _ui[name] = r if isinstance(r, dict) else json.loads(r)
    return copy.deepcopy(_ui[name])


def _node_spec(cls: str):
    if cls not in _spec:
        try:
            o = _c().req(f"/object_info/{cls}")
        except Exception:
            o = {}
        _spec[cls] = o.get(cls) if isinstance(o, dict) else None
    return _spec[cls]


def _widget_names(spec: dict) -> list:
    """화면 widgets_values 순서대로 입력 이름. 시드 뒤의 '생성 뒤 바꾸기' 칸은 API 에 없어서 None."""
    inp = spec["input"]
    order = spec.get("input_order") or {k: list((inp.get(k) or {}).keys()) for k in ("required", "optional")}
    names = []
    for sec in ("required", "optional"):
        for k in order.get(sec, []):
            v = (inp.get(sec) or {}).get(k)
            t = v[0] if isinstance(v, list) else v
            if isinstance(t, list) or t in WIDGET_TYPES:
                names.append(k)
                opt = v[1] if isinstance(v, list) and len(v) > 1 and isinstance(v[1], dict) else {}
                if opt.get("control_after_generate"):
                    names.append(None)
    return names


def to_api(ui: dict, turbo=None) -> dict:
    """UI 형식 → API 형식. 우회(mode 4) 노드는 같은 타입 입력으로 건너 이어 주고, 끈(mode 2) 노드와 메모처럼 화면에만 있는 노드는 뺀다.
    turbo 가 True/False 면 이름에 turbo 가 든 LoRA 를 켜거나 우회한다(부르는 쪽 스텝 8·40 에 맞춤)."""
    nodes = {n["id"]: n for n in ui["nodes"]}
    meta = (ui.get("extra") or {}).get("webtoon") or {}
    if turbo is not None:
        for n in nodes.values():
            if n["type"] == "LoraLoaderModelOnly" and "turbo" in str((n.get("widgets_values") or [""])[0]).lower():
                n["mode"] = 0 if turbo else 4
    links = {lk[0]: lk for lk in ui["links"]}

    def source(lid):
        _, fid, fslot, _, _, typ = links[lid]
        n = nodes.get(fid)
        while n is not None and n["mode"] == 4:
            up = next((i for i in n.get("inputs", []) if i.get("link") is not None and i["type"] == typ), None)
            if up is None:
                return None
            _, fid, fslot, _, _, typ = links[up["link"]]
            n = nodes.get(fid)
        return [fid, fslot] if n is not None and n["mode"] == 0 and _node_spec(n["type"]) else None

    api = {}
    for nid, n in nodes.items():
        spec = _node_spec(n["type"]) if n["mode"] == 0 else None
        if spec is None:
            continue
        inputs = {nm: val for nm, val in zip(_widget_names(spec), n.get("widgets_values") or []) if nm}
        for i in n.get("inputs", []):
            if i.get("link") is not None:
                s = source(i["link"])
                if s:
                    inputs[i["name"]] = s
        api[nid] = {"class_type": n["type"], "inputs": inputs}
    api = _renumber(api)
    if turbo is not None and meta.get("cfg") and "7" in api:  # 판마다 터보·일반 cfg 가 다르면 워크플로우 데이터대로(Anima 1 · 4)
        api["7"]["inputs"]["cfg"] = float(meta["cfg"]["turbo" if turbo else "full"])
    return api


def _renumber(api: dict) -> dict:
    count = {}
    for v in api.values():
        count[v["class_type"]] = count.get(v["class_type"], 0) + 1
    new = {nid: CANON[v["class_type"]] for nid, v in api.items() if v["class_type"] in CANON and count[v["class_type"]] == 1}
    enc = next((nid for nid, k in new.items() if k == "5"), None)
    if enc is not None:  # 컷 설명에 꽂힌 참고 그림: <imageK> → 1KK(101, 102 …)
        for k, val in api[enc]["inputs"].items():
            if k.startswith("images.image_") and isinstance(val, list) and api[val[0]]["class_type"] == "LoadImage":
                new[val[0]] = f"1{int(k.rsplit('_', 1)[1]):02d}"
    out = {}
    for nid, v in api.items():
        ins = {k: [new.get(x[0], f"u{x[0]}"), x[1]] if isinstance(x, list) and len(x) == 2 and x[0] in api else x
               for k, x in v["inputs"].items()}
        out[new.get(nid, f"u{nid}")] = {"class_type": v["class_type"], "inputs": ins}
    return out


def fill(g: dict, prompt: str, ref_names: list, w: int, h: int, seed: int, steps: int, ref_res: int,
         prefix: str = "webtoon_studio/panel") -> dict:
    """그리기 도구가 정하는 값만 넣는다. 참고 그림은 워크플로우의 LoadImage 대신 넘긴 이름으로 <image1>… 를 다시 꽂는다.
    글만 받는 판(Anima)은 KSampler 의 positive 로 이어진 글 칸에 프롬프트를 넣고 참고 그림은 받지 않는다(부르는 쪽 comfy_panel.graph 가 거른다)."""
    if g.get("5", {}).get("class_type") != "TextEncodeQwenImage21":
        pos = g["7"]["inputs"]["positive"][0]
        g[pos]["inputs"]["text"] = prompt
        if "6" in g:
            g["6"]["inputs"].update(width=w, height=h, batch_size=1)
        g["7"]["inputs"].update(seed=seed, steps=steps)
        g["9"]["inputs"]["filename_prefix"] = prefix
        return g
    enc = g["5"]["inputs"]
    enc["prompt"], enc["resolution"] = prompt, ref_res
    for k in [k for k in enc if k.startswith("images.")]:
        del enc[k]
    for k in [k for k in g if len(k) == 3 and k.startswith("1") and g[k]["class_type"] == "LoadImage"]:
        del g[k]
    for k, name in enumerate(ref_names, 1):
        g[f"1{k:02d}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{k}"] = [f"1{k:02d}", 0]
    if "6" in g:  # 편집 워크플로우엔 빈 캔버스가 없다(고칠 그림 크기)
        g["6"]["inputs"].update(width=w, height=h, batch_size=1)
    g["7"]["inputs"].update(seed=seed, steps=steps)
    g["9"]["inputs"]["filename_prefix"] = prefix
    return g
