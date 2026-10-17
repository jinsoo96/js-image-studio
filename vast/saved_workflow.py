"""vast ComfyUI 에 저장된 워크플로우(화면용 UI 형식)를 불러 API 형식으로 바꾸고, 그리기 도구가 정하는 입력값만 채운다.
(2026-10-17 사용자 "vast ComfyUI 에 짜 놓은 워크플로우를 불러서 도구로 쓰는 구조 아닌가": 전에는 같은 구성을 코드로 다시 짰다)
화면에서 워크플로우를 고치면(모델·LoRA·샘플러·cfg·네거티브, 노드 추가) 작업실 도구(agent app MCP)와 agent platform 카탈로그 등록이 그대로 따른다.
코드가 채우는 것: 프롬프트 · 참고 그림 · 참고 그림 크기 · 캔버스 크기 · 시드 · 스텝 · 터보 LoRA 켜기/끄기 · 저장 이름.

    from saved_workflow import GEN, EDIT, load_ui, to_api, fill
    g = fill(to_api(load_ui(GEN), turbo=True), prompt, ["loc.png", "face.png"], 1184, 1760, seed, 8, 1024)
"""
import copy
import json
import urllib.parse

from comfy_client import Comfy

GEN, EDIT = "webtoon_vertical_qwen21.json", "webtoon_edit_qwen21.json"  # 생성 · 편집(표정 은행·기본복·남는 사람 지우기·연출 손보기)
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
    return _renumber(api)


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
    """그리기 도구가 정하는 값만 넣는다. 참고 그림은 워크플로우의 LoadImage 대신 넘긴 이름으로 <image1>… 를 다시 꽂는다."""
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
