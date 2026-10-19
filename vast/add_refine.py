"""생성 워크플로우(vertical)에 '2차 다듬기'(픽셀 확대 방식)를 넣는다. 새 판(webtoon_vertical_<이름>)을 ComfyUI 화면에서 저장한 뒤 이걸 한 번 돌리면 도구가 refine 으로 켤 수 있다.
(2026-10-19 시험: 잠재 확대는 입자·흐림, 픽셀 확대 후 재인코딩이 선명하고 구도·표정은 그대로.)
첫 디코더 → 이미지 확대(lanczos) → 다시 인코딩 → 두 번째 샘플러 → 두 번째 디코더 → 저장. 네 노드는 우회(mode 4)로 두고 saved_workflow.to_api(refine=True) 가 켠다.
꺼져 있으면 저장 노드가 첫 디코더로 이어진다(to_api 가 우회된 다듬기 사슬을 건너뜀). 이미 있으면 다시 만든다(옛 노드 지움).

    .venv/Scripts/python vast/add_refine.py <워크플로우 파일> [배율 1.3] [강도 0.25] [스텝 16]     예: webtoon_vertical_noctq.json
"""
import copy
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy

c = Comfy()
PLAN = {sys.argv[1]: (float(sys.argv[2]) if len(sys.argv) > 2 else 1.3, float(sys.argv[3]) if len(sys.argv) > 3 else 0.25, int(sys.argv[4]) if len(sys.argv) > 4 else 16)} if len(sys.argv) > 1 else {}
T = "2차 다듬기"


def path(n):
    return "/api/userdata/" + urllib.parse.quote("workflows/" + n, safe="")


def rebuild(ui, scale, den, steps):
    ui = copy.deepcopy(ui)
    nodes = {n["id"]: n for n in ui["nodes"]}
    # 1) 옛 2차 다듬기 노드·연결 제거
    old = {i for i, n in nodes.items() if str(n.get("title", "")).startswith(T)}
    if old:
        keep = []
        for lk in ui["links"]:
            if lk[1] in old or lk[3] in old:
                if lk[1] not in old and lk[1] in nodes:  # 살아 있는 노드의 출력 목록에서 이 링크 번호를 뺀다
                    outs = nodes[lk[1]]["outputs"][lk[2]]
                    outs["links"] = [x for x in (outs.get("links") or []) if x != lk[0]]
                if lk[3] not in old and lk[3] in nodes:
                    nodes[lk[3]]["inputs"][lk[4]]["link"] = None
                continue
            keep.append(lk)
        ui["links"] = keep
        ui["nodes"] = [n for n in ui["nodes"] if n["id"] not in old]
        nodes = {n["id"]: n for n in ui["nodes"]}
    ks = next(n for n in ui["nodes"] if n["type"] == "KSampler")
    vd = next(n for n in ui["nodes"] if n["type"] == "VAEDecode")
    sv = next(n for n in ui["nodes"] if n["type"] == "SaveImage")
    links = ui["links"]
    by_id = {lk[0]: lk for lk in links}

    def inp(node, name):
        return next(i for i in node["inputs"] if i["name"] == name)

    # 첫 샘플러 → 첫 디코더 연결 복구(옛 노드를 지우며 끊겼다면)
    if inp(vd, "samples")["link"] is None:
        lid0 = max([lk[0] for lk in links] + [ui.get("last_link_id", 0)]) + 1
        links.append([lid0, ks["id"], 0, vd["id"], next(k for k, i in enumerate(vd["inputs"]) if i["name"] == "samples"), "LATENT"])
        ks["outputs"][0].setdefault("links", []).append(lid0)
        inp(vd, "samples")["link"] = lid0
        by_id[lid0] = links[-1]
    nid = max(n["id"] for n in ui["nodes"]) + 1
    lid = max([lk[0] for lk in links] + [ui.get("last_link_id", 0)]) + 1

    def new_link(src, sslot, dst, dname, typ):
        nonlocal lid
        # 같은 입력에 걸린 옛 링크가 있으면 먼저 뗀다
        cur = inp(dst, dname)["link"]
        if cur is not None and cur in by_id:
            o = by_id.pop(cur)
            if o[1] in nodes_all:
                outs = nodes_all[o[1]]["outputs"][o[2]]
                outs["links"] = [x for x in (outs.get("links") or []) if x != cur]
            ui["links"][:] = [x for x in ui["links"] if x[0] != cur]
        lk = [lid, src["id"], sslot, dst["id"], next(k for k, i in enumerate(dst["inputs"]) if i["name"] == dname), typ]
        ui["links"].append(lk)
        by_id[lid] = lk
        src["outputs"][sslot].setdefault("links", []).append(lid)
        inp(dst, dname)["link"] = lid
        lid += 1

    def mk(typ, title, pos, size, ins, outs, widgets):
        nonlocal nid
        n = {"id": nid, "type": typ, "pos": pos, "size": size, "flags": {}, "order": 90 + nid, "mode": 4, "title": title,
             "inputs": [{"name": a, "type": b, "link": None} for a, b in ins],
             "outputs": [{"name": a, "type": b, "links": [], "slot_index": k} for k, (a, b) in enumerate(outs)],
             "properties": {"Node name for S&R": typ}, "widgets_values": widgets}
        nid += 1
        ui["nodes"].append(n)
        nodes_all[n["id"]] = n
        return n

    nodes_all = {n["id"]: n for n in ui["nodes"]}
    x0, y0 = ks["pos"][0], ks["pos"][1] + ks["size"][1] + 60
    scl = mk("ImageScaleBy", T + ": 픽셀 확대", [x0, y0], [300, 82], [("image", "IMAGE")], [("IMAGE", "IMAGE")], ["lanczos", scale])
    enc = mk("VAEEncode", T + ": 다시 인코딩", [x0 + 330, y0], [220, 60], [("pixels", "IMAGE"), ("vae", "VAE")], [("LATENT", "LATENT")], [])
    wv = list(ks["widgets_values"])
    wv[2], wv[-1] = steps, den  # [seed, control, steps, cfg, sampler, scheduler, denoise]
    k2 = copy.deepcopy(ks)
    k2.update({"id": nid, "pos": [x0 + 580, y0], "mode": 4, "title": T + ": 샘플러", "widgets_values": wv, "order": 90 + nid})
    nid += 1
    for i in k2["inputs"]:
        i["link"] = None
    for o in k2["outputs"]:
        o["links"] = []
    ui["nodes"].append(k2)
    nodes_all[k2["id"]] = k2
    dec = mk("VAEDecode", T + ": 디코더", [x0 + 880, y0], [220, 60], [("samples", "LATENT"), ("vae", "VAE")], [("IMAGE", "IMAGE")], [])
    vae_src = by_id[inp(vd, "vae")["link"]]
    vae_node = nodes_all[vae_src[1]]
    new_link(vd, 0, scl, "image", "IMAGE")          # 첫 디코더 → 확대(저장 노드로 가던 옛 연결은 아래에서 두 번째 디코더로 옮긴다)
    new_link(scl, 0, enc, "pixels", "IMAGE")
    new_link(vae_node, vae_src[2], enc, "vae", "VAE")
    for name in ("model", "positive", "negative"):
        o = by_id[inp(ks, name)["link"]]
        new_link(nodes_all[o[1]], o[2], k2, name, o[5])
    new_link(enc, 0, k2, "latent_image", "LATENT")
    new_link(k2, 0, dec, "samples", "LATENT")
    new_link(vae_node, vae_src[2], dec, "vae", "VAE")
    new_link(dec, 0, sv, "images", "IMAGE")         # 저장 노드의 입력을 두 번째 디코더로(첫 디코더 쪽 링크는 new_link 가 뗀다)
    ui["last_node_id"] = max(ui.get("last_node_id", 0), nid)
    ui["last_link_id"] = max(ui.get("last_link_id", 0), lid)
    meta = ui.setdefault("extra", {}).setdefault("webtoon", {})
    meta["refine"] = {"method": "pixel", "scale": scale, "denoise": den, "steps": steps,
                      "note": "2차 다듬기 노드 넷은 우회 상태. 도구가 refine=True 로 부르면 켠다(꺼져 있으면 저장 노드가 첫 디코더로 이어진다). 값은 노드 위젯."}
    return ui


for name, (scale, den, steps) in PLAN.items():
    ui = json.loads(c.req(path(name), raw=True).decode("utf-8"))
    new = rebuild(ui, scale, den, steps)
    c.req(path(name) + "?overwrite=true&full_info=true", new)
    (ROOT / "vast" / name).write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(name, "노드", len(new["nodes"]), "연결", len(new["links"]), "| 다듬기 노드", [n["type"] for n in new["nodes"] if str(n.get("title", "")).startswith(T)])
