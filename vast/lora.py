"""인물 LoRA: 작품 에셋과 잘 나온 컷으로 학습 데이터를 만들고, vast ComfyUI 에 저장된 학습 워크플로우(webtoon_train_lora_<판>)로 학습한다.
(2026-10-19 사용자 "로라 쓰는 거 필요해, 노드 만들어서 워크플로우 구성해서 쓰면 되지, UC 기반으로")
학습은 서버 ComfyUI 기본 노드만 쓴다: ① webtoon_lora_dataset_<판>(LoadImageTextDataSetFromFolder → MakeTrainingDataset → SaveTrainingDataset)
→ 모델 내리기 → ② webtoon_train_lora_<판>(LoadTrainingDataset → ResolutionBucket → TrainLoraNode → SaveLoRA).
학습한 LoRA 는 series/<작품>/lora/loras.json 에 적히고, 같은 판으로 그릴 때 그 인물이 나오는 컷에 붙는다(comfy_panel → saved_workflow.to_api 의 '인물 LoRA' 노드).

    .venv/Scripts/python vast/lora.py data  <작품 폴더> <인물> [--panels 12] [--res 1024]   학습 데이터만 만든다(series/<작품>/lora/<인물>/data)
    .venv/Scripts/python vast/lora.py train <작품 폴더> <인물> [--steps N] [--rank N] [--lr X]  데이터 → 서버 → 학습 → lora/loras.json 에 등록
    .venv/Scripts/python vast/lora.py list  [<작품 폴더>]                                    서버의 LoRA 파일과 작품에 등록된 것
    .venv/Scripts/python vast/lora.py set   <작품 폴더> <인물> [--file F] [--strength 0.8] [--off]
    .venv/Scripts/python vast/lora.py slots <워크플로우 이름>                                  생성 워크플로우에 '인물 LoRA' 노드 둘을 단다(우회 상태)

학습 값의 기본은 학습 워크플로우의 노드 값(화면에서 고치면 따른다). 데이터·캡션을 만드는 규칙은 아래 build_data 설명.
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
from comfy_client import Comfy  # noqa: E402

from saved_workflow import LORA_SLOT as SLOT  # noqa: E402  생성 워크플로우에서 도구가 채우는 노드의 제목 머리(화면에서 보이고, 손으로도 켜고 끌 수 있다)


def _read(f: Path) -> dict:
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _sj(series: Path) -> dict:
    return _read(series / "series.json")


def registry(series: Path) -> dict:
    """작품에 등록된 인물 LoRA {인물: {file, trigger, model, strength, on, …}}. series/<작품>/lora/loras.json(작품 설정 파일 꼴이 작품마다 달라서
    — 새 작품은 series.json, D+18·무대 뒤는 cast.json·characters.json — LoRA 목록은 따로 둔다)."""
    return _read(series / "lora" / "loras.json")


def _plain(tags: str) -> str:
    """옛 ComfyUI 판 태그 글('(short neat black hair:1.3), sharp eyes')에서 가중치 괄호를 벗긴다."""
    return re.sub(r"\(([^():]+)(?::[\d.]+)?\)", r"\1", tags or "").strip(" ,")


def person(series: Path, who: str) -> dict:
    """인물의 {name, who(생김새 글), main(평소 옷)}. 새 작품은 series.json cast(who·main), 옛 작품은 characters.json(tags·outfits.default) + cast.json(en).
    없는 사람이면 SystemExit(있는 사람 목록)."""
    cast = (_sj(series).get("cast") or {})
    if who in cast:
        c = cast[who]
        return {"name": re.sub(r"\s*\(.*?\)", "", c.get("en") or who), "who": c.get("who", ""), "main": c.get("main", "")}
    old, ch = _read(series / "cast.json"), _read(series / "characters.json")
    if who in old or who in ch:
        o, c = old.get(who) or {}, ch.get(who) or {}
        return {"name": re.sub(r"\s*\(.*?\)", "", o.get("en") or c.get("name") or who), "who": _plain(c.get("tags", "")),
                "main": _plain((c.get("outfits") or {}).get("default", ""))}
    have = sorted(set(cast) | {k for k in old if not k.startswith("_")} | {k for k in ch if not k.startswith("_")})
    raise SystemExit(f"{series.name} 에 {who} 가 없다(있는 사람: {', '.join(have)})")


def people(series: Path) -> list:
    """학습할 수 있는 인물(설정에 있고 표정 은행 vast_refs/<id>_expr.json 이 있는 사람)."""
    ids = set(_sj(series).get("cast") or {}) | {k for k in _read(series / "cast.json") if not k.startswith("_")}
    return sorted(i for i in ids if (series / "assets" / "vast_refs" / f"{i}_expr.json").exists())


def trigger(series: Path, who: str) -> str:
    """캡션 맨 앞과 그릴 때 이름 옆에 붙는 낱말. 등록된 것 → 옛 판 characters.json 의 trigger(D+18 d18jaemin) → 작품 slug + 인물 id."""
    t = (registry(series).get(who) or {}).get("trigger") or (_read(series / "characters.json").get(who) or {}).get("trigger")
    if t:
        return t
    slug = _sj(series).get("slug") or series.name
    return re.sub(r"[^a-z0-9]+", "_", f"{slug}_{who}".lower()).strip("_")


def _fit(im: Image.Image, res: int) -> Image.Image:
    """면적을 res² 이하로 줄이고 두 변을 16 배수로(VAE 압축 8 × 패치 2). 키우지는 않는다."""
    im = im.convert("RGB")
    k = min(1.0, (res * res / (im.width * im.height)) ** 0.5)
    w, h = max(16, int(im.width * k) // 16 * 16), max(16, int(im.height * k) // 16 * 16)
    return im.resize((w, h), Image.LANCZOS) if (w, h) != im.size else im


def _labels(series: Path, who: str) -> list:
    """표정 은행: [(파일, 표정 이름)]. 표정 시트 칸(labels)과 make_assets 로 늘린 표정(extra)."""
    jf = series / "assets" / "vast_refs" / f"{who}_expr.json"
    if not jf.exists():
        return []
    meta = json.loads(jf.read_text(encoding="utf-8"))
    out = [(f"{who}_expr_{k:02d}.png", lb) for k, lb in enumerate(meta.get("labels") or [], 1)]
    out += [(x["file"], x["label"]) for x in meta.get("extra", []) if x.get("file")]
    # extra 를 적기 전에 만든 표정 그림(D+18 의 <id>_xp_NN)은 make_assets 의 표정 목록 순서로 이름을 붙인다(그 목록 순서대로 만들었다)
    from make_assets import STRONG, subtle_list
    have = {f for f, _ in out}
    for kind, lst in (("xp", STRONG), ("xs", subtle_list(series))):
        for k, ex in enumerate(lst, 1):
            f = f"{who}_{kind}_{k:02d}.png"
            if f not in have and (series / "assets" / "vast_refs" / f).exists():
                out.append((f, ex.split(":")[0]))
    return out


SCREEN = ("This image is a candidate for training a character LoRA of one person. Count the people visible anywhere in the image "
          "(including backs of heads, people cut off at the edge, small people in the background). Is the main person's face clearly visible "
          "(not from behind, not hidden), and how big is the face: large (face height over 1/6 of the image), medium (1/12 to 1/6) or small? "
          'Answer only JSON: {"people": <int>, "face_visible": true|false, "face_size": "large"|"medium"|"small"}')


def _screen(img: Path) -> bool:
    """학습에 쓸 만한 컷인가: 그림 보는 모델이 사람 수·얼굴이 보이나·얼굴 크기를 보고 답한다. 컷 설명의 인물 칸은 한 명이어도 어깨 너머 앞사람이나
    먼 행인이 그려진 컷, 사람이 콩알만 한 넓은 컷이 섞였다(2026-10-19 외출 p053·p120). 판정 모델이 안 되면 쓰지 않는다(데이터는 적어도 깨끗하게)."""
    import base64
    import io
    from judge_ep import chat, parse_json
    im = Image.open(img).convert("RGB")
    im.thumbnail((768, 768))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    try:
        r = parse_json(chat([{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": SCREEN}], max_tokens=200))
    except Exception:  # noqa: BLE001
        return False
    return r.get("people") == 1 and r.get("face_visible") is True and r.get("face_size") in ("large", "medium")


def _solo_panels(series: Path, who: str, limit: int) -> list:
    """이 인물 혼자 나온 컷 중 검수에 걸린 게 없는 것([(그림, 컷)]). 샷 종류가 고루 섞이게 돌려 가며 고르고, 하나씩 _screen 을 통과한 것만 넣는다
    (얼굴 칸만이면 클로즈업만 배운다). 인서트(손·소품)는 뺀다."""
    pools = {}
    for pj in sorted((series / "episodes").glob("*/panels.json")):
        ep = pj.parent
        d = json.loads(pj.read_text(encoding="utf-8"))
        panels = d["panels"] if isinstance(d, dict) else d
        sf = ep / "review" / "staging.json"
        flagged = set(json.loads(sf.read_text(encoding="utf-8"))) if sf.exists() else set()
        # 동일인 검사(tools/char_check, 합성 단계의 '얼굴 튐')에 걸린 이 인물 컷도 뺀다: 외출 상수 데이터에 든 p039·p219 가 그 검사에서 튐이었다(2026-10-19)
        try:
            from char_check import check_episode
            flagged |= {pid for pid, cid, _, _, verdict in check_episode(ep, verbose=False) if cid == who and verdict}
        except Exception:  # noqa: BLE001  검사 모델이 없거나 그 회차에 놓인 컷이 없으면 검수 기록만으로
            pass
        for p in panels:
            shot = p.get("shot") or ""
            if p.get("chars") != [who] or p["id"] in flagged or re.search(r"insert|detail|top view", shot or ""):
                continue
            for folder in ("panels_hf", "panels_hf_vast"):  # 힉스필드 판(D+18·무대 뒤 본편)과 vast 판 둘 다
                img = ep / folder / f"{p['id']}.png"
                if img.exists():
                    pools.setdefault(shot, []).append((img, p))
    out, keys, tried = [], sorted(pools, key=lambda k: -len(pools[k])), 0
    while len(out) < limit and any(pools.values()) and tried < limit * 4:
        for k in keys:
            if pools[k] and len(out) < limit:
                lst = pools[k]
                cand = lst.pop(len(lst) // 2)  # 회차 가운데쯤부터(앞쪽 컷만 몰리지 않게)
                tried += 1
                if _screen(cand[0]):
                    out.append(cand)
    return out


def build_data(series: Path, who: str, panels: int = 12, res: int = 1024) -> dict:
    """학습 데이터(그림 + 같은 이름의 .txt 캡션)를 series/<작품>/lora/<인물>/data 에 만든다.
    - 그림: 표정 은행(정면 상반신, 표정마다 한 장) + 기본 전신 + 혼자 나온 잘 된 컷(자세·배경·샷이 다양해야 얼굴만이 아니라 사람을 배운다).
      짧은 변이 448 미만인 잘라낸 조각(시트의 작은 머리·옷 칸)은 흐려서 뺀다.
    - 캡션: 'trigger, 이름. 생김새(person: series.json who 또는 옛 판 characters.json tags). 옷. 표정·하는 일. 샷·배경.' 바뀌는 것(옷·표정·배경·샷)을 글로 적어야 그것이 trigger 에 묻지 않고,
      생김새 문장은 그릴 때 프롬프트에도 들어가는 같은 문장이라 학습 때와 그릴 때 글이 맞는다."""
    from comfy_panel import panel_text
    sj = _sj(series)
    cast = person(series, who)
    tg, name = trigger(series, who), cast["name"]
    head = f"{tg}, {name}." + (f" {cast['who'].rstrip('.')}." if cast["who"] else "")
    vr = series / "assets" / "vast_refs"
    items = []
    for f, lb in _labels(series, who):
        if (vr / f).exists():
            items.append((vr / f, f"{head} Wearing {cast['main'] or 'the usual outfit'}. Expression: {lb}. "
                                  "Head and shoulders portrait facing the viewer, plain light background."))
    base = vr / f"{who}_base.png"
    if base.exists():
        items.append((base, f"{head} Wearing a plain heather-gray crew-neck t-shirt and gray knee-length shorts, barefoot. Neutral face. "
                            "Full body standing straight, front view, plain light background."))
    for img, p in _solo_panels(series, who, panels):
        outfit = (p.get("outfit") or {}).get(who) or cast["main"]
        place = str((sj.get("locations") or {}).get(p.get("location") or "", "")).split(".")[0]
        items.append((img, f"{head} Wearing {outfit}. {panel_text(p)}" + (f" Place: {place}." if place else "")))
    out = series / "lora" / who / "data"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        old.unlink()
    kept, skipped = [], []
    for k, (src, cap) in enumerate(items, 1):
        im = Image.open(src)
        if min(im.size) < 448:
            skipped.append(src.name)
            continue
        im = _fit(im, res)
        stem = f"{k:03d}"
        im.save(out / f"{stem}.png")
        (out / f"{stem}.txt").write_text(cap, encoding="utf-8")
        kept.append({"file": f"{stem}.png", "from": str(src.relative_to(series)).replace("\\", "/"), "size": list(im.size), "caption": cap})
    info = {"who": who, "trigger": tg, "res": res, "count": len(kept), "skipped": skipped, "items": kept}
    (out.parent / "data.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def upload_data(c: Comfy, series: Path, who: str) -> str:
    """데이터 폴더를 서버 input/<폴더> 에 올린다(학습 워크플로우의 LoadImageTextDataSetFromFolder 가 고르는 폴더). 폴더 이름을 돌려준다."""
    folder = f"lora_{trigger(series, who)}"
    for f in sorted((series / "lora" / who / "data").iterdir()):
        c.upload_image(f, f.name, subfolder=folder, ctype="text/plain" if f.suffix == ".txt" else "image/png")
    return folder


def _set(api: dict, cls: str, **vals):
    hit = [v for v in api.values() if v["class_type"] == cls]
    if not hit:
        raise SystemExit(f"학습 워크플로우에 {cls} 노드가 없다")
    for v in hit:
        v["inputs"].update({k: x for k, x in vals.items() if x is not None})
    return hit[0]["inputs"]


def _run(c: Comfy, api: dict, ui: dict, timeout: float) -> dict:
    """큐에 넣고 끝까지 기다린다. 오류면 RuntimeError(서버 오류 문장)."""
    hist = c.wait(c.queue(api, ui), timeout=timeout)
    st = hist.get("status", {})
    if st.get("status_str") == "error":
        msg = next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "")
        raise RuntimeError(str(msg)[:400])
    return hist


def _free(c: Comfy):
    """서버가 올려 둔 모델을 내린다(① 의 글 인코더가 VRAM 에 남으면 ② 학습이 메모리가 모자랐다)."""
    try:
        c.req("/free", {"unload_models": True, "free_memory": True})
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)


def _role_wf(m: dict, name: str, role: str) -> str:
    wf = m[name]["roles"].get(role)
    if not wf:
        have = sorted(k for k, v in m.items() if role in v.get("own_roles", []))
        raise SystemExit(f"역할 {role} 워크플로우가 서버에 없다(있는 판: {have or '없음'}). make_workflow.py train 으로 만든다")
    return wf


def train(series: Path, who: str, steps=None, rank=None, lr=None, panels: int = 12, res: int = 1024, log=print, register: bool = True,
          reuse_data: bool = False, overrides: dict | None = None, force: bool = False) -> dict:
    """데이터 만들기 → 올리기 → ① 학습 데이터 워크플로우(역할 lora_dataset) → 모델 내리기 → ② 학습 워크플로우(역할 train_lora) → lora/loras.json 등록.
    워크플로우는 작품의 판에서(없으면 기본 판의 것), ② 의 판(extra.webtoon.model)이 이 LoRA 를 붙일 수 있는 판이다.
    reuse_data: 서버에 이미 만든 학습 데이터(datasets/webtoon/<trigger>)로 ② 만 다시(스텝·rank 만 바꿔 볼 때).
    overrides: 이번만 TrainLoraNode 칸을 바꾼다(예: {"training_dtype": "bf16"}). force: 학습 워크플로우의 disabled 를 무시(원인 찾는 시험).
    """
    from saved_workflow import load_ui, models, pick, to_api
    c = Comfy()
    m = models(refresh=True)
    name = pick(series)[0]
    wf_data, wf_train = _role_wf(m, name, "lora_dataset"), _role_wf(m, name, "train_lora")
    ui_train = load_ui(wf_train)
    tmeta = (ui_train.get("extra") or {}).get("webtoon") or {}
    if tmeta.get("disabled") and not force:  # 학습 워크플로우에 막아 둔 이유가 적혀 있으면 돌리지 않는다(공용 서버를 넘어뜨린 설정, make_workflow.TRAIN_DISABLED)
        raise SystemExit(f"학습을 막아 둠: {tmeta['disabled']}")
    lmodel = tmeta.get("model") or name
    tg = trigger(series, who)
    dataset = f"webtoon/{tg}"
    t0 = time.time()
    info = json.loads((series / "lora" / who / "data.json").read_text(encoding="utf-8")) if reuse_data else None
    if not reuse_data:
        info = build_data(series, who, panels, res)
        log(f"데이터 {info['count']}장(뺀 것 {len(info['skipped'])}) → 서버에 올림")
        folder = upload_data(c, series, who)
        ui_data = load_ui(wf_data)
        api = to_api(ui_data)
        _set(api, "LoadImageTextDataSetFromFolder", folder=folder)
        _set(api, "SaveTrainingDataset", folder_name=dataset)
        log(f"① 학습 데이터 만들기 {wf_data} → datasets/{dataset}")
        _run(c, api, ui_data, 3600)
    _free(c)
    api = to_api(ui_train)
    _set(api, "LoadTrainingDataset", folder_name=dataset)
    t = _set(api, "TrainLoraNode", steps=steps, rank=rank, learning_rate=lr, **(overrides or {}))
    _set(api, "SaveLoRA", prefix=f"loras/webtoon/{tg}", steps=t["steps"])
    for v in api.values():
        if v["class_type"] == "LossGraphNode":
            v["inputs"]["filename_prefix"] = f"webtoon_studio/lora_loss_{tg}"
    before = set(c.req("/models/loras") or [])
    t1 = time.time()
    log(f"② 학습 시작 {wf_train}: {t['steps']}스텝 rank {t['rank']} lr {t['learning_rate']}" + (f" 바꾼 칸 {overrides}" if overrides else ""))
    # 메모리 부족 때 offloading 을 켜고 저절로 다시 돌리던 것은 뺐다: 그 재시도 직후 공용 서버 ComfyUI 가 재시작됐다(2026-10-19 20:44)
    try:
        hist = _run(c, api, ui_train, 6 * 3600)
    except RuntimeError as e:
        raise RuntimeError(f"학습 실패: {e}") from e
    new = sorted(set(c.req("/models/loras") or []) - before)
    pick_file = next((f for f in reversed(new) if tg in f), None)
    if not pick_file:
        raise RuntimeError(f"학습은 끝났는데 새 LoRA 파일을 못 찾음(새 파일 {new})")
    secs, train_s = int(time.time() - t0), int(time.time() - t1)
    loss = [i for o in hist.get("outputs", {}).values() for i in o.get("images", [])]
    if loss:
        c.download(loss[0], series / "lora" / who / "loss.png")
    entry = {"file": pick_file, "trigger": tg, "model": lmodel, "strength": 0.8, "on": True,
             "steps": t["steps"], "rank": t["rank"], "lr": t["learning_rate"], "offloading": bool(t.get("offloading")), "images": info["count"],
             "train_s": train_s, "s_per_step": round(train_s / max(1, t["steps"]), 2), "at": time.strftime("%Y-%m-%d %H:%M")}
    if register:  # 시험 학습(--no-register)은 작품에 등록하지 않는다
        set_entry(series, who, entry)
    log(f"학습 끝 전체 {secs // 60}분 {secs % 60}초(학습 {train_s}초, 스텝당 {entry['s_per_step']}초) → {pick_file}"
        + (f" (lora/loras.json {who}, 강도 0.8)" if register else " (등록 안 함)"))
    return entry


def training_block(series: Path) -> str:
    """그 작품의 판에서 학습 워크플로우(역할 train_lora)에 막아 둔 이유가 있으면 그 글, 없으면 ""(서버에 못 닿으면 "")."""
    try:
        from saved_workflow import load_ui, models, pick
        m = models()
        wf = m[pick(series)[0]]["roles"].get("train_lora")
        return str(((load_ui(wf).get("extra") or {}).get("webtoon") or {}).get("disabled") or "") if wf else ""
    except Exception:  # noqa: BLE001
        return ""


def set_entry(series: Path, who: str, entry: dict) -> dict:
    """lora/loras.json 의 <인물> 을 고친다(없던 키는 더하고, 준 키만 바꾼다)."""
    f = series / "lora" / "loras.json"
    reg = registry(series)
    cur = reg.get(who) or {}
    cur.update({k: v for k, v in entry.items() if v is not None})
    reg[who] = cur
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    return cur


def for_panel(series, chars, model: str) -> list:
    """이 컷에 붙일 LoRA [{who, file, strength, trigger}]: 컷에 나오는 인물 중 켜진(on) LoRA 가 있고 그 LoRA 의 판이 지금 그리는 판인 것.
    다른 판(예: UC 로 학습한 것을 Anima 로 그릴 때)에는 붙이지 않는다. 두 명 이상이면 각 항목의 multi 강도(없으면 strength)."""
    if not series:
        return []
    loras = registry(Path(series))
    hit = [(w, loras[w]) for w in (chars or []) if isinstance(loras.get(w), dict) and loras[w].get("on", True) and loras[w].get("file")
           and str(loras[w].get("model") or "").lower() == str(model).lower()]
    return [{"who": w, "file": e["file"], "trigger": e.get("trigger") or "",
             "strength": float(e.get("multi", e.get("strength", 0.8)) if len(hit) > 1 else e.get("strength", 0.8))} for w, e in hit]


def add_slots(wf_name: str, count: int = 2) -> dict:
    """생성 워크플로우(UI)에 '인물 LoRA 1·2' 노드를 모델 로더 바로 뒤에 단다(우회 상태). 도구가 그 컷 인물의 LoRA 를 넣고 켠다.
    모델 로더의 MODEL 을 받던 노드들은 마지막 인물 LoRA 노드에서 받게 바꾼다. 이미 있으면 그대로 둔다."""
    from saved_workflow import load_ui
    c = Comfy()
    ui = load_ui(wf_name)
    if any(str(n.get("title", "")).startswith(SLOT) for n in ui["nodes"]):
        return {"workflow": wf_name, "added": 0}
    unet = next(n for n in ui["nodes"] if n["type"] == "UNETLoader")
    links = ui["links"]
    nid = max(n["id"] for n in ui["nodes"]) + 1
    lid = max([lk[0] for lk in links] + [ui.get("last_link_id", 0)]) + 1
    consumers = [lk for lk in links if lk[1] == unet["id"] and lk[2] == 0]
    unet["outputs"][0]["links"] = []
    prev, made = unet, []
    for k in range(1, count + 1):
        n = {"id": nid, "type": "LoraLoaderModelOnly", "pos": [unet["pos"][0], unet["pos"][1] - 120 * (count - k + 1)], "size": [340, 82],
             "flags": {}, "order": 1, "mode": 4, "title": f"{SLOT} {k} (도구가 그 컷 인물 것을 넣고 켠다)",
             "inputs": [{"name": "model", "type": "MODEL", "link": lid}],
             "outputs": [{"name": "MODEL", "type": "MODEL", "links": [], "slot_index": 0}],
             "properties": {"Node name for S&R": "LoraLoaderModelOnly"}, "widgets_values": ["", 0.8]}
        links.append([lid, prev["id"], 0, nid, 0, "MODEL"])
        prev["outputs"][0]["links"].append(lid)
        ui["nodes"].append(n)
        made.append(n)
        prev, nid, lid = n, nid + 1, lid + 1
    for lk in consumers:  # 모델을 받던 입력은 마지막 슬롯에서
        lk[1] = prev["id"]
        prev["outputs"][0]["links"].append(lk[0])
    ui["last_node_id"], ui["last_link_id"] = nid, lid
    c.req("/api/userdata/" + urllib.parse.quote("workflows/" + wf_name, safe="") + "?overwrite=true", json.dumps(ui, ensure_ascii=False).encode("utf-8"))
    (ROOT / "vast" / wf_name).write_text(json.dumps(ui, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"workflow": wf_name, "added": len(made)}


def server_loras() -> list:
    return Comfy().req("/models/loras") or []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["data", "train", "list", "set", "slots"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("who", nargs="?")
    ap.add_argument("--panels", type=int, default=12)
    ap.add_argument("--res", type=int, default=1024)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--rank", type=int)
    ap.add_argument("--lr", type=float)
    ap.add_argument("--file")
    ap.add_argument("--strength", type=float)
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--reuse-data", action="store_true", help="train: 서버에 만든 학습 데이터로 학습만 다시")
    ap.add_argument("--set", action="append", default=[], metavar="칸=값", help="train: 이번만 TrainLoraNode 칸을 바꾼다(예 training_dtype=bf16)")
    ap.add_argument("--force", action="store_true", help="train: 학습 워크플로우의 disabled 를 무시(원인 찾는 시험)")
    ap.add_argument("--no-register", action="store_true", help="train: 학습만 하고 작품에 등록하지 않는다(시간·메모리 시험)")
    a = ap.parse_args()
    if a.cmd == "slots":
        return print(add_slots(a.target))
    if a.cmd == "list":
        print("서버 LoRA:", "\n  ".join([""] + server_loras()))
        if a.target:
            print("작품에 등록:", json.dumps(registry(Path(a.target)), ensure_ascii=False, indent=1), "· 학습할 수 있는 인물:", people(Path(a.target)))
        return
    series = Path(a.target).resolve()
    if a.cmd == "data":
        info = build_data(series, a.who, a.panels, a.res)
        print(f"{info['count']}장 → {series / 'lora' / a.who / 'data'} (trigger {info['trigger']}, 뺀 것 {info['skipped']})")
        for it in info["items"][:3] + info["items"][-2:]:
            print(" ", it["file"], it["size"], it["caption"][:160])
    elif a.cmd == "train":
        ov = {}
        for kv in a.set:
            k, v = kv.split("=", 1)
            ov[k] = {"true": True, "false": False}.get(v.lower(), int(v) if v.isdigit() else v)
        train(series, a.who, a.steps, a.rank, a.lr, a.panels, a.res, register=not a.no_register, reuse_data=a.reuse_data, overrides=ov, force=a.force)
    elif a.cmd == "set":
        print(set_entry(series, a.who, {"file": a.file, "strength": a.strength, "on": False if a.off else True}))


if __name__ == "__main__":
    main()
