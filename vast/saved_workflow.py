"""vast ComfyUI 에 저장된 워크플로우(화면용 UI 형식)를 불러 API 형식으로 바꾸고, 그리기 도구가 정하는 입력값만 채운다.
(2026-10-17 사용자 "vast ComfyUI 에 짜 놓은 워크플로우를 불러서 도구로 쓰는 구조 아닌가": 전에는 같은 구성을 코드로 다시 짰다)
화면에서 워크플로우를 고치면(모델·LoRA·샘플러·cfg·네거티브, 노드 추가) 작업실 도구(MCP)가 그대로 따른다.
코드가 채우는 것: 프롬프트 · 참고 그림 · 참고 그림 크기 · 캔버스 크기 · 시드 · 스텝 · 터보 LoRA 켜기/끄기 · 저장 이름.

    from saved_workflow import GEN, EDIT, load_ui, to_api, fill
    g = fill(to_api(load_ui(GEN), turbo=True), prompt, ["loc.png", "face.png"], 1184, 1760, seed, 8, 1024)
"""
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

from comfy_client import Comfy

# 그리는 판 = vast ComfyUI 에 저장된 웹툰 워크플로우(2026-10-19 사용자 "하드코딩 말고 vastai 에서 설정할 수 있는 모델을 따르게").
# 서버의 workflows/ 에서 `webtoon_vertical_<이름>.json` 을 찾아 판으로 본다: 화면에서 저장하면 새 판이 생기고 지우면 사라진다.
# 판의 성질은 워크플로우의 extra.webtoon 에 적는다(없으면 이름·노드에서 읽는다):
#   name(판 이름, 없으면 파일 이름의 <이름>) · label(사람이 읽는 설명) · refs(참고 그림을 받나, 기본 True) · edit(고치기 워크플로우 파일)
#   · default(true 면 작품에 판이 없을 때 쓰는 기본 판) · cfg {turbo, full} · steps {turbo, full}
#   · sigmas {turbo, full}: "1.0, 0.97, …, 0" 처럼 적으면 첫 샘플러(KSampler)를 그 시그마로 그린다(SamplerCustomAdvanced + ManualSigmas, _custom_sigmas)
# 고치기 워크플로우는 판의 edit → webtoon_edit_<이름>.json → 기본 판의 것 순서로 찾는다.
# 서버에 닿지 않으면 마지막으로 읽은 목록, 그것도 없으면 OFFLINE 의 이름만(그리기는 서버가 있어야 하니 그때는 어차피 실패한다).
# MCP 도구가 실제로 쓰는 서버 워크플로우는 이름 앞에 mcp_use_ 를 붙인다(2026-10-21 사용자 "vast 의 진짜 MCP 에 쓰는 워크플로우들은 앞에 mcp_use 를").
# 서버 목록에서 사람이 손으로 만든 것·남의 것(image_anima_base_v1 등)과 바로 갈린다. 찾을 땐 옛 이름(webtoon_*)도 읽고, 같은 판·역할이 둘이면 mcp_use_ 쪽.
MCP_PREFIX = "mcp_use_"
OFFLINE = {"uc": (MCP_PREFIX + "webtoon_vertical_qwen21_uc.json", MCP_PREFIX + "webtoon_edit_qwen21_uc.json")}
# 시험용으로 워크플로우의 sigmas 를 덮는다: off(워크플로우 KSampler 그대로) · lq(스케줄러만 linear_quadratic) · 숫자 목록(그 값으로)
SIGMAS_ENV = os.environ.get("WEBTOON_SIGMAS", "").strip()
# 터보 LoRA 도 옆길로(시험용, 1 이면). 기본은 워크플로우대로 섞기(LoraLoaderModelOnly): 학습한 LoRA 는 옆길이 깨끗했지만 터보는 아직 견주지 않았다
TURBO_BYPASS = os.environ.get("WEBTOON_TURBO_BYPASS", "0") == "1"
_models, _models_at = {}, 0.0
MODELS_TTL = 60.0


def models(refresh: bool = False) -> dict:
    """{판 이름: {"gen": 생성 워크플로우, "edit": 고치기, "roles": {역할: 워크플로우}, "refs": 참고 그림을 받나, "label": 설명, "use_for": 쓰임새, "default": bool,
    "models": {폴더: [모델 파일]}, "missing": [서버에 없는 모델 파일]}}. 서버의 webtoon_*.json 을 extra.webtoon 의 model·role 로 묶고(역할 없는 옛 이름은
    webtoon_vertical_<이름> 이면 그 판의 vertical), 판 = vertical(생성) 워크플로우가 있는 것. 판에 없는 역할은 기본 판의 것. 60초 동안 재사용."""
    global _models, _models_at
    if _models and not refresh and time.time() - _models_at < MODELS_TTL:
        return _models
    try:
        from concurrent.futures import ThreadPoolExecutor
        c = _c()
        folders = ("diffusion_models", "loras", "text_encoders", "vae")
        with ThreadPoolExecutor(8) as ex:  # vast 프록시는 호출마다 1~4초라 순서대로 읽으면 30초 가까이 걸렸다(클라이언트 호출 한도 60초)
            futs = {f: ex.submit(lambda f=f: set(c.req(f"/models/{f}") or [])) for f in folders}
            names = _list_workflows(c)
            have = {f: fu.result() for f, fu in futs.items()}
            names += _push_local(c, set(names), have)
            # 옛 이름을 먼저, mcp_use_ 를 나중에 읽어 같은 판·역할이면 mcp_use_ 가 덮는다
            files = sorted((n for n in names if re.fullmatch(r"(mcp_use_)?webtoon_.+\.json", n)), key=lambda n: (n.startswith(MCP_PREFIX), n))
            uis = {n: ex.submit(load_ui, n) for n in files}
            loaded = {}
            for n, fu in uis.items():
                try:
                    loaded[n] = fu.result()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        # 서버 목록을 못 읽으면 기본 이름으로 간다(작업실 사본도 올리지 못한 채). 까닭을 남긴다: 조용히 빠져 뒤의 404 만 보였다(2026-10-27)
        print(f"[경고] 서버 워크플로우·모델 목록을 못 읽어 기본 판 이름으로 간다(작업실 사본도 못 올림): {type(e).__name__}: {e}", file=sys.stderr)
        if _models:
            return _models
        return {k: {"gen": g, "edit": e, "roles": {"vertical": g, "edit": e}, "refs": True, "label": k, "use_for": "", "default": k == "uc", "models": {}, "missing": []}
                for k, (g, e) in OFFLINE.items()}
    by = {}
    for fn in files:
        if fn not in loaded:
            continue
        ui = loaded[fn]
        base = fn[len(MCP_PREFIX):] if fn.startswith(MCP_PREFIX) else fn
        meta = (ui.get("extra") or {}).get("webtoon") or {}
        role = meta.get("role") or ("vertical" if base.startswith("webtoon_vertical_") else None)
        mid = str(meta.get("model") or meta.get("name") or (base[len("webtoon_vertical_"):-len(".json")] if base.startswith("webtoon_vertical_") else "")).lower()
        if role and mid:
            d = by.setdefault(mid, {"roles": {}, "meta": {}, "ui": None})
            d["roles"][role] = fn
            if role == "vertical":
                d["meta"], d["ui"] = meta, ui
    out = {}
    for mid, d in by.items():
        ui, meta = d["ui"], d["meta"]
        if ui is None:  # 생성 워크플로우가 없으면 판이 아니다
            continue
        used = _used_models(ui)
        unet = (used.get("diffusion_models") or ["?"])[0]
        out[mid] = {"gen": d["roles"]["vertical"], "roles": dict(d["roles"]), "refs": meta.get("refs", True) is True, "label": meta.get("label") or unet,
                    "use_for": meta.get("use_for") or "", "default": bool(meta.get("default")), "models": used, "meta": meta,
                    "missing": [f"{k}/{v}" for k, vs in used.items() for v in vs if v not in have.get(k, set())]}
    if out:
        dflt = next((k for k, v in out.items() if v["default"]), None) or ("uc" if "uc" in out else sorted(out)[0])
        out[dflt]["default"] = True
        for v in out.values():  # 판에 없는 역할(고치기·에셋 등)은 기본 판의 것을 쓴다
            v["own_roles"] = sorted(v["roles"])
            for role, wf in out[dflt]["roles"].items():
                v["roles"].setdefault(role, wf)
            v["edit"] = v["roles"].get("edit")
        _models, _models_at = out, time.time()
    return _models or {k: {"gen": g, "edit": e, "roles": {"vertical": g, "edit": e}, "refs": True, "label": k, "use_for": "", "default": k == "uc", "models": {}, "missing": []}
                       for k, (g, e) in OFFLINE.items()}


def _used_models(ui: dict) -> dict:
    """워크플로우가 불러오는 모델 파일 {폴더: [파일]}(꺼 둔 노드·비워 둔 인물 LoRA 칸은 뺌)."""
    nodes = ui.get("nodes", []) + [n for sg in (ui.get("definitions") or {}).get("subgraphs", []) for n in sg.get("nodes", [])]
    used = {}
    for n in nodes:
        v = (n.get("widgets_values") or [None])[0]
        folder = {"UNETLoader": "diffusion_models", "LoraLoaderModelOnly": "loras", "CLIPLoader": "text_encoders", "VAELoader": "vae"}.get(n.get("type"))
        if str(n.get("title", "")).startswith(LORA_SLOT):  # 인물 LoRA 칸은 비어 있는 게 기본(그 컷 인물 것을 도구가 넣는다)
            continue
        if folder and isinstance(v, str) and v and n.get("mode", 0) != 2:
            used.setdefault(folder, []).append(v)
    return used


def _list_workflows(c) -> list:
    """서버 workflows/ 의 파일 이름들. 새 서버는 그 폴더가 없어 404 라 빈 목록(예전엔 이 404 로 판 목록 전체가 '서버에 닿지 않음'으로 떨어졌다)."""
    try:
        return [x for x in (c.req("/api/userdata?dir=workflows&recurse=true") or []) if isinstance(x, str)]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


LOCAL_DIR = Path(__file__).resolve().parent  # 저장 워크플로우의 작업실 사본(vast/mcp_use_webtoon_*.json)


def _push_local(c, names: set, have: dict) -> list:
    """서버에 없는 우리 저장 워크플로우(작업실 사본)를 올리고 올린 이름을 돌려준다. 그 서버에 모델 파일이 다 있는 것만 올린다(없는 판은 올려도 못 그린다).
    이미 있는 건 덮지 않는다(화면에서 고친 서버 쪽이 정본). GPU 서버를 갈아끼우면 저장 워크플로우가 없어 그리기가 모두 실패해, 올리는 스크립트를
    손으로 돌렸다(2026-10-23 RunPod 새 파드마다). 끄려면 WEBTOON_PUSH_WORKFLOWS=0."""
    if os.environ.get("WEBTOON_PUSH_WORKFLOWS", "1") == "0":
        return []
    out = []
    for f in sorted(LOCAL_DIR.glob(MCP_PREFIX + "webtoon_*.json")):
        if f.name in names:
            continue
        try:
            ui = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if any(v not in have.get(k, set()) for k, vs in _used_models(ui).items() for v in vs):
            continue
        try:
            c.req("/api/userdata/" + urllib.parse.quote("workflows/" + f.name, safe="") + "?overwrite=false", f.read_bytes())
            out.append(f.name)
        except urllib.error.HTTPError:  # 동시에 다른 작업이 올렸으면(409) 그대로 둔다
            continue
    return out


def workflow_for(role: str, series=None) -> str:
    """그 작품의 판에서 이 역할(vertical · edit · asset_expression · asset_base · candidate_location · candidate_sheet)을 하는 서버 워크플로우 이름.
    판에 그 역할이 없으면 기본 판의 것."""
    m = models()
    name = pick(series)[0]
    return m[name]["roles"].get(role) or m[default_model()]["roles"].get(role) or m[name]["gen"]


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


def ref_mode(ui: dict):
    """워크플로우의 extra.webtoon.refs 그대로: true(없음)=참고 그림을 <image> 로 받는다 · false=글만 · "character"=글 + 인물 그림 한 장
    (Anima 캐릭터 참고 IP-Adapter. 2026-10-22 사용자 "anima 도 그림 보고 같은 캐릭터로 맞추는 게 있다던데")."""
    return ((ui.get("extra") or {}).get("webtoon") or {}).get("refs", True)


def takes_refs(ui: dict) -> bool:
    """이 워크플로우가 참고 그림을 <image> 로 받는가(extra.webtoon.refs, 없으면 받는다고 본다). "character" 판은 글로 그리니 받지 않는다고 본다."""
    return ref_mode(ui) is True


def refs_model() -> str:
    """참고 그림을 받는 판 중 기본 판(참고 그림이 꼭 필요한 일을 글만 받는 판이 맡게 됐을 때 대신 쓴다)."""
    m = models()
    ok = {k: v for k, v in m.items() if v["refs"]}
    return default_model() if default_model() in ok else (sorted(ok)[0] if ok else default_model())


MODEL = os.environ.get("WEBTOON_MODEL", "").strip().lower() or None  # 하위 호환(예전엔 이 환경변수로만 골랐다)
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
# 부르는 쪽이 노드를 찾는 번호. 전에 코드로 짜던 그래프와 같은 번호라 comfy_panel·refine·make_assets 는 그대로 쓴다.
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


# 학습한 LoRA(인물·그림체)를 그릴 때 fp8 가중치에 섞지 않고 옆길(LoraLoaderBypassModelOnly)로 건다. 섞으면 fp8 로 다시 무작위 반올림하고
# (comfy/model_patcher patch_weight_to_device), D+18 그림체 0.8 + 재민 0.8 을 섞으니 그림 전체가 초록·보라 얼룩이 됐는데 옆길로는 같은 강도에서
# 멀쩡했다(2026-10-21 p008). 학습도 bypass_mode 로 했다. 환경변수 WEBTOON_LORA_BYPASS=0 이면 예전처럼 섞는다
LORA_BYPASS = os.environ.get("WEBTOON_LORA_BYPASS", "1") != "0"
LORA_SLOT = "인물 LoRA"  # 생성 워크플로우에서 그 컷 인물의 LoRA 를 넣는 노드의 제목 머리(vast/lora.py slots 가 단다)


def saved_name(name: str) -> str:
    """서버에 저장할 이름(앞에 mcp_use_)."""
    return name if name.startswith(MCP_PREFIX) else MCP_PREFIX + name


def save_ui(name: str, ui: dict, c=None) -> str:
    """MCP 도구가 쓰는 워크플로우를 서버 workflows/mcp_use_<이름> 과 vast/ 사본에 저장하고 저장한 이름을 돌려준다."""
    name = saved_name(name)
    (c or _c()).req("/api/userdata/" + urllib.parse.quote("workflows/" + name, safe="") + "?overwrite=true", json.dumps(ui, ensure_ascii=False).encode("utf-8"))
    (Path(__file__).resolve().parent / name).write_text(json.dumps(ui, ensure_ascii=False, indent=1), encoding="utf-8")
    global _models_at
    _models_at = 0.0  # 다음 models() 가 다시 읽게
    return name


def to_api(ui: dict, turbo=None, refine=None, loras=None) -> dict:
    """UI 형식 → API 형식. 우회(mode 4) 노드는 같은 타입 입력으로 건너 이어 주고, 끈(mode 2) 노드와 메모처럼 화면에만 있는 노드는 뺀다.
    turbo 가 True/False 면 이름에 turbo 가 든 LoRA 를 켜거나 우회한다(부르는 쪽 스텝 8·40 에 맞춤).
    refine 이 True/False 면 제목이 '2차 다듬기'로 시작하는 노드(잠재 확대 + 두 번째 샘플러)를 켜거나 우회한다.
    loras([{file, strength}], lora.for_panel)를 주면 제목이 '인물 LoRA' 로 시작하는 노드에 차례로 넣어 켜고 남는 노드는 우회한다.
    노드가 모자라면(슬롯 없는 워크플로우) 모델 로더 바로 뒤에 LoRA 노드를 이어 붙인다."""
    nodes = {n["id"]: n for n in ui["nodes"]}
    meta = (ui.get("extra") or {}).get("webtoon") or {}
    more = []
    if loras is not None:
        slots = sorted((n for n in nodes.values() if str(n.get("title", "")).startswith(LORA_SLOT)), key=lambda n: str(n.get("title")))
        for k, n in enumerate(slots):
            n["mode"] = 0 if k < len(loras) else 4
            if k < len(loras):
                n["widgets_values"] = [loras[k]["file"], float(loras[k].get("strength", 0.8))]
                if LORA_BYPASS:  # 학습한 LoRA 를 fp8 가중치에 섞지 않고 옆길로(학습도 bypass 로 했다)
                    n["type"] = "LoraLoaderBypassModelOnly"
        more = list(loras[len(slots):])
    if turbo is not None:
        for n in nodes.values():
            # 터보 LoRA: 파일 이름에 turbo 가 들었거나 노드 제목이 터보/turbo(MOSS 의 8스텝 DMD LoRA 는 이름에 turbo 가 없다)
            if n["type"] == "LoraLoaderModelOnly" and ("turbo" in str((n.get("widgets_values") or [""])[0]).lower()
                                                        or re.search(r"터보|turbo", str(n.get("title", "")), re.I)):
                n["mode"] = 0 if turbo else 4
                if TURBO_BYPASS:
                    n["type"] = "LoraLoaderBypassModelOnly"
    if refine is not None:
        for n in nodes.values():
            if str(n.get("title", "")).startswith("2차 다듬기"):
                n["mode"] = 0 if refine else 4
    links = {lk[0]: lk for lk in ui["links"]}
    # 2차 다듬기를 끈 그래프에선 두 번째 디코더가 건너뛰어 저장 노드의 입력이 비므로, 다듬기 사슬의 시작점(픽셀 확대의 image 입력)으로 이어 준다
    origin = next((n["inputs"][0]["link"] for n in nodes.values() if str(n.get("title", "")).startswith("2차 다듬기") and n["type"] == "ImageScaleBy"), None)

    def source(lid):
        _, fid, fslot, _, _, typ = links[lid]
        n = nodes.get(fid)
        while n is not None and n["mode"] == 4:
            up = next((i for i in n.get("inputs", []) if i.get("link") is not None and i["type"] == typ), None)
            if up is None and origin is not None and str(n.get("title", "")).startswith("2차 다듬기") and typ == "IMAGE":
                up = {"link": origin}
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
    api = _renumber(_first_stage(api))
    if more:  # 슬롯이 모자란 만큼 모델 로더(UNETLoader) 바로 뒤에 사슬로: 모델을 받던 입력은 마지막 LoRA 에서 받는다
        src = next((k for k, v in api.items() if v["class_type"] == "UNETLoader"), None)
        if src:
            users = [(k, nm) for k, v in api.items() for nm, x in v["inputs"].items() if x == [src, 0]]
            prev = [src, 0]
            for i, lz in enumerate(more, 1):
                api[f"L{i}"] = {"class_type": "LoraLoaderBypassModelOnly" if LORA_BYPASS else "LoraLoaderModelOnly",
                                "inputs": {"model": prev, "lora_name": lz["file"], "strength_model": float(lz.get("strength", 0.8))}}
                prev = [f"L{i}", 0]
            for k, nm in users:
                api[k]["inputs"][nm] = prev
    if turbo is not None and meta.get("cfg"):  # 판마다 터보·일반 cfg 가 다르면 워크플로우 데이터대로(Anima 1 · 4), 두 번째 샘플러도 같이
        for k, v in api.items():
            if v["class_type"] == "KSampler":
                v["inputs"]["cfg"] = float(meta["cfg"]["turbo" if turbo else "full"])
                if (meta.get("sampler") or {}).get("turbo" if turbo else "full"):  # 판마다 샘플러도 다르면(MOSS 터보 euler · 일반 er_sde)
                    v["inputs"]["sampler_name"] = meta["sampler"]["turbo" if turbo else "full"]
    sig = SIGMAS_ENV or (str((meta.get("sigmas") or {}).get("turbo" if turbo else "full") or "") if turbo is not None else "")
    if sig:  # 처음부터 다 그리는 샘플러에만 표시해 두고 fill 이 스텝을 넣은 뒤 바꾼다(2차 다듬기처럼 일부만 다시 그리는 샘플러는 그대로)
        for v in api.values():
            if v["class_type"] == "KSampler" and float(v["inputs"].get("denoise", 1.0)) == 1.0:
                v.setdefault("_meta", {})["sigmas"] = sig
    return api


def _first_stage(api: dict) -> dict:
    """KSampler 가 둘 이상(그리기 + 2단 다듬기 같은 묶음)이면 도구는 첫 단 결과만 필요하다: KSampler 를 가장 적게 거친 SaveImage 와 그 위쪽 노드만 남긴다.
    번호·이름이 아니라 그래프 모양으로 가른다(시안 장소 워크플로우가 이 모양이라 도구가 못 불러 조용히 코드 그래프로 대신 그렸다, 2026-10-19)."""
    if sum(1 for v in api.values() if v["class_type"] == "KSampler") < 2:
        return api

    def up(nid, seen):
        for x in api[nid]["inputs"].values():
            if isinstance(x, list) and len(x) == 2 and x[0] in api and x[0] not in seen:
                seen.add(x[0])
                up(x[0], seen)
        return seen

    saves = [k for k, v in api.items() if v["class_type"] == "SaveImage"]
    if len(saves) < 2:
        return api
    best = min(saves, key=lambda s: sum(1 for a in up(s, set()) if api[a]["class_type"] == "KSampler"))
    keep = up(best, set()) | {best}
    return {k: v for k, v in api.items() if k in keep}


def _renumber(api: dict) -> dict:
    count = {}
    for v in api.values():
        count[v["class_type"]] = count.get(v["class_type"], 0) + 1
    new = {nid: CANON[v["class_type"]] for nid, v in api.items() if v["class_type"] in CANON and count[v["class_type"]] == 1}
    if count.get("KSampler") == 2:  # 2차 다듬기가 켜진 그래프: 빈 캔버스(또는 인코더 잠재)에서 시작하는 샘플러가 "7", 확대 잠재를 받는 쪽이 "7r"
        ks = [nid for nid, v in api.items() if v["class_type"] == "KSampler"]
        first = next((n for n in ks if api[api[n]["inputs"]["latent_image"][0]]["class_type"] not in ("LatentUpscaleBy", "LatentUpscale", "KSampler", "VAEEncode")), ks[0])
        new[first] = "7"
        new[next(n for n in ks if n != first)] = "7r"
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
        # 캐릭터 참고 판(refs "character"): 첫 참고 그림을 인물 그림으로. 없으면(사람 없는 컷) 참고 단계를 건너뛴다
        # (IP-Adapter: 적용 노드가 모델을 바꾼다 · AnimaFastTrain: 학습 노드 → 덧씌우기 노드가 모델을 바꾼다)
        for k, v in list(g.items()):
            if v["class_type"] == "AnimaIPAdapterApply":
                src, patch, drop = v["inputs"]["ref_image"][0], k, [k, v["inputs"]["ip_adapter"][0]]
            elif v["class_type"] == "AnimaFastTrainReferenceContext":
                src = v["inputs"]["reference_image"][0]
                patch = next(pk for pk, pv in g.items() if pv["class_type"] == "AnimaFastTrainPatchModel" and pv["inputs"].get("context") == [k, 0])
                drop = [k, patch]
            else:
                continue
            if ref_names:
                g[src]["inputs"]["image"] = ref_names[0]
                continue
            for w in g.values():
                for key, val in w["inputs"].items():
                    if val == [patch, 0]:
                        w["inputs"][key] = g[patch]["inputs"]["model"]
            for d in drop + [src]:
                g.pop(d, None)
        if "6" in g:
            g["6"]["inputs"].update(width=w, height=h, batch_size=1)
        g["7"]["inputs"].update(seed=seed, steps=steps)
        if "7r" in g:
            g["7r"]["inputs"].update(seed=seed)
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
    if "7r" in g:  # 켜진 2차 다듬기 샘플러: 같은 시드. 스텝·강도는 워크플로우 값(첫 단이 터보 8스텝이어도 다듬기는 16스텝에서 일부만 다시 그린다)
        g["7r"]["inputs"].update(seed=seed)
    g["9"]["inputs"]["filename_prefix"] = prefix
    return _custom_sigmas(g)


def _custom_sigmas(g: dict) -> dict:
    """to_api 가 sigmas 를 표시한 KSampler 를 같은 번호의 SamplerCustomAdvanced(+ RandomNoise · KSamplerSelect · ManualSigmas · CFGGuider)로 바꾼다.
    (2026-10-21 조사: Qwen-Image-2.1 Turbo 는 8스텝 시그마가 정해져 증류됐는데 simple 스케줄러는 그걸 근사하지 못해 그림이 뿌옇다,
    huggingface.co/Comfy-Org/Qwen-Image-2.1/discussions/22. 터보 LoRA 도 같은 증류에서 뽑았다.)
    값의 개수가 스텝 + 1 이 아니면(부르는 쪽이 스텝을 바꿈) 그 샘플러는 그대로 둔다."""
    for k in [k for k, v in g.items() if v["class_type"] == "KSampler" and (v.get("_meta") or {}).get("sigmas")]:
        i, sig = g[k]["inputs"], g[k]["_meta"]["sigmas"]
        if sig == "off":
            continue
        if sig == "lq":
            i["scheduler"] = "linear_quadratic"
            continue
        vals = [float(x) for x in re.split(r"[,\s]+", sig) if x]
        if len(vals) != int(i["steps"]) + 1:
            continue
        g[f"{k}n"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": i["seed"]}}
        g[f"{k}s"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": i["sampler_name"]}}
        g[f"{k}m"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": ", ".join(f"{x:g}" for x in vals)}}
        g[f"{k}g"] = {"class_type": "CFGGuider", "inputs": {"model": i["model"], "positive": i["positive"], "negative": i["negative"], "cfg": i["cfg"]}}
        g[k] = {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": [f"{k}n", 0], "guider": [f"{k}g", 0], "sampler": [f"{k}s", 0],
                                                                  "sigmas": [f"{k}m", 0], "latent_image": i["latent_image"]}}
    return g
