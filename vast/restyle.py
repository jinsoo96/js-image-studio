"""짝 데이터 Restyle: 'Qwen 느낌 판 → 그 작품 정본' 을 배우는 편집 LoRA (UC 만, 2026-10-21 사용자 "Restyle 기법 쓰면 좋다는데, 무대 뒤 학습을 그렇게").
그림체 LoRA(글 → 그림)는 그 작품 그림체가 약하게만 묻었다. 짝 학습은 같은 장면의 '우리 Qwen UC 가 그리는 결'과 '정본 결'의 차이만 배우므로
그릴 때 1단 Qwen UC 초안(참고 그림으로 구도·얼굴)을 image1 로 넣고 "Convert image 1 to <그림체>." 로 마무리한다(조사: DiffSynth 차분 LoRA · Zaytron StyleTransfer).

    .venv/Scripts/python vast/restyle.py pairs   <작품>   [--strength 0.6]   정본 컷 → 같은 구도를 Qwen UC 로 다시 그린 판(lora/_restyle/in) + 정본(lora/_restyle/out)
    .venv/Scripts/python vast/restyle.py dataset <작품>                      짝을 서버에 올려 학습 데이터로(image1 = Qwen 판을 편집 조건으로, 목표 = 정본 잠재)
    .venv/Scripts/python vast/restyle.py train   <작품> [--steps 1500] [--rank 32]   공식 bf16 + UC LoRA 위에서 학습(역할 train_lora_base), loras.json '_restyle'
    .venv/Scripts/python vast/restyle.py apply   <작품> <회차> <컷,…> [--src 폴더] [--file LoRA] [--variant restyle]   2단: 초안을 그 작품 그림체로
    .venv/Scripts/python vast/restyle.py eval    <작품> [--files a,b]          학습에 안 쓴 정본 컷: 정답 | 1단 Qwen UC | 2단 Restyle(중간 파일마다)

정본 중 샷 종류마다 둘은 학습에 넣지 않고 평가에 쓴다(lora._style_panels 의 남김과 같다).
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))
import lora  # noqa: E402

WHO = "_restyle"
SEED = 77


def _dir(series: Path) -> Path:
    return series / "lora" / WHO


def _style_word(series: Path) -> str:
    return lora.trigger(series, lora.STYLE)


def _held_and_train(series: Path) -> tuple:
    """(학습 짝 [(그림, 컷)], 평가 [(그림, 컷)]). 평가 = 샷 종류마다 마지막 둘(lora._style_panels 가 남기는 것)."""
    train = lora._style_panels(series, 999)
    used = {str(src) for src, _ in train}
    held = []
    for pj in sorted((series / "episodes").glob("*/panels.json")):
        ep = pj.parent
        if "vast" in ep.name:
            continue
        d = json.loads(pj.read_text(encoding="utf-8"))
        for p in d["panels"] if isinstance(d, dict) else d:
            img = ep / "panels_hf" / f"{p['id']}.png"
            if img.exists() and str(img) not in used and not (ep / "panels_hf_vast" / img.name).exists():
                held.append((img, p))
    return train, held


def _env_no_lora(series: Path):
    os.environ["WEBTOON_LORA_OVERRIDE"] = json.dumps({k: {"on": False} for k in lora.registry(series)})


def pairs(series: Path, strength: float = 0.6, res: int = 1024, log=print, limit: int = 0) -> dict:
    """정본마다 같은 크기로 줄인 정본(out/NNN.png + 캡션)과, 그 정본을 Qwen UC 로 strength 만큼 다시 그린 판(in/NNN.png).
    다시 그릴 때 그 컷의 글·참고 그림(얼굴·장소)을 그대로 쓰고 LoRA 는 모두 끈다: 구도는 정본, 결은 지금 우리 Qwen UC."""
    _env_no_lora(series)
    import comfy_panel as CP
    from comfy_client import Comfy
    d = _dir(series)
    for sub in ("in", "out"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    train, held = _held_and_train(series)
    if limit:  # 시험용: 앞에서 몇 쌍만(같은 이름·시드라 본 작업이 그대로 다시 쓴다)
        train = train[:limit]
    c, cache = Comfy(), {}
    word = _style_word(series)
    items = []
    for k, (src, p) in enumerate(train, 1):
        name = f"{k:03d}"
        tgt = lora._fit(Image.open(src).convert("RGB"), res)
        w, h = tgt.width // 16 * 16, tgt.height // 16 * 16
        tgt = tgt.resize((w, h), Image.LANCZOS)
        tgt.save(d / "out" / f"{name}.png")
        body = re.sub(r"\s*SPACE \(identical.*$", "", CP.panel_text(p), flags=re.S)
        cap = f"Convert image 1 to {word}." + (f" {body}" if k % 2 else "")  # 반은 장면 글까지, 반은 지시만
        (d / "out" / f"{name}.txt").write_text(cap, encoding="utf-8")
        if not (d / "in" / f"{name}.png").exists():
            prompt, refs = CP.qwen_refs(series, p, expr=False)
            names = [CP.upload(c, r, cache) for r in refs]
            g = CP.graph(prompt, names, w, h, SEED + k, 20, res, turbo=False, series=series, chars=p.get("chars"))
            lat = next(i for i, v in g.items() if v["class_type"] in ("EmptyLatentImage", "EmptySD3LatentImage"))
            vae = next(i for i, v in g.items() if v["class_type"] == "VAELoader")
            g["6i"] = {"class_type": "LoadImage", "inputs": {"image": CP.upload(c, d / "out" / f"{name}.png", cache)}}
            g[lat] = {"class_type": "VAEEncode", "inputs": {"pixels": ["6i", 0], "vae": [vae, 0]}}
            ks = next(i for i, v in g.items() if v["class_type"] == "KSampler" and v["inputs"].get("latent_image") == [lat, 0])
            g[ks]["inputs"]["denoise"] = strength
            g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/restyle_pair"
            h_ = c.wait(c.queue(g), 900)
            c.download(next(i for o in h_["outputs"].values() for i in o.get("images", [])), d / "in" / f"{name}.png")
            Image.open(d / "in" / f"{name}.png").convert("RGB").resize((w, h), Image.LANCZOS).save(d / "in" / f"{name}.png")
        items.append({"name": name, "from": str(src.relative_to(series)).replace("\\", "/"), "size": [w, h], "caption": cap})
        log(f"짝 {k}/{len(train)} {p['id']} {w}x{h}", flush=True) if log is print else log(f"짝 {k}/{len(train)}")
    info = {"who": WHO, "word": word, "strength": strength, "count": len(items), "items": items,
            "held": [str(src.relative_to(series)).replace("\\", "/") for src, _ in held]}
    (d / "data.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def _role(role: str):
    """그리는 판(uc)의 그 역할 서버 워크플로우 이름, 없으면 None(예전 코드 그래프로)."""
    from saved_workflow import models
    try:
        return models(refresh=True)["uc"]["roles"].get(role)
    except Exception:  # noqa: BLE001
        return None


def dataset(series: Path, log=print, name: str = "") -> str:
    """짝을 서버 input/restyle_<작품>_{in,out} 에 올리고 학습 데이터를 만든다. 목록 노드라 짝마다 한 번씩 돈다:
    정본 → VAEEncode(목표 잠재) · Qwen 판 → TextEncodeQwenImage21 images.image_1(편집 조건, 캡션은 'Convert image 1 to …')."""
    from concurrent.futures import ThreadPoolExecutor
    from comfy_client import Comfy
    from comfy_panel import CLIP, VAE
    c = Comfy()
    d = _dir(series)
    slug = series.name.replace("-", "")
    fin, fout = f"restyle_{slug}_in", f"restyle_{slug}_out"
    have = {f.stem for f in (d / "in").glob("*.png")}  # 입력이 있는 짝만(짝 만들기가 중간이면 목표만 있는 것은 뺀다)
    up = [(f, fin, "image/png") for f in sorted((d / "in").glob("*.png"))]
    up += [(f, fout, "text/plain" if f.suffix == ".txt" else "image/png") for f in sorted((d / "out").iterdir()) if f.stem in have]
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda a: c.upload_image(a[0], a[0].name, subfolder=a[1], ctype=a[2]), up))
    name = name or f"webtoon/{slug}_restyle"
    wf = _role("restyle_dataset")
    if wf:  # 서버 워크플로우 webtoon_restyle_dataset_uc(화면에서 열어 고칠 수 있다): 폴더·이름만 넣는다
        from saved_workflow import load_ui, to_api
        ui = load_ui(wf)
        api = to_api(ui)
        lora._set(api, "LoadImageTextDataSetFromFolder", folder=fout)
        lora._set(api, "LoadImageDataSetFromFolder", folder=fin)
        lora._set(api, "SaveTrainingDataset", folder_name=name)
        log(f"학습 데이터 만들기({wf}) → datasets/{name} (짝 {len(have)})")
        h = c.wait(c.queue(api, ui), 3600)
        st = h.get("status", {})
        if st.get("status_str") == "error":
            raise RuntimeError(next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "?"))
        return name
    api = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "3": {"class_type": "LoadImageTextDataSetFromFolder", "inputs": {"folder": fout}},
        "4": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": fin}},
        "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["3", 0], "vae": ["2", 0]}},
        "6": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["1", 0], "prompt": ["3", 1], "negative_prompt": "", "resolution": 1024,
                                                                "vae": ["2", 0], "images.image_1": ["4", 0]}},
        "7": {"class_type": "SaveTrainingDataset", "inputs": {"latents": ["5", 0], "conditioning": ["6", 0], "folder_name": name, "shard_size": 1000}},
    }
    log(f"학습 데이터 만들기 → datasets/{name} (짝 {len(list((d / 'in').glob('*.png')))})")
    h = c.wait(c.queue(api), 3600)
    st = h.get("status", {})
    if st.get("status_str") == "error":
        raise RuntimeError(next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "?"))
    return name


def train(series: Path, steps: int = 1500, rank: int = 32, lr: float = 1e-4, chunk: int = 500, log=print, role: str = "train_lora") -> dict:
    """기본은 UC fp8 원본 위(역할 train_lora, 옆길). 공식 bf16 원본 위(train_lora_base)는 참고 그림만큼 길어진 입력에서 메모리가 빠듯해
    ComfyUI 가 CUDA 'invalid argument' 로 통째로 죽었다(2026-10-21, 같은 bf16 이라도 짧은 입력의 그림체 학습은 3000스텝 무사)."""
    info = json.loads((_dir(series) / "data.json").read_text(encoding="utf-8"))
    key = WHO if role == "train_lora" else f"{WHO}@{role.replace('train_lora_', '')}"
    lora.set_entry(series, key, {"trigger": f"{series.name.replace('-', '')}restyle", "model": "uc"})
    return lora.train(series, WHO, steps, rank, lr, log=log, register=True, chunk=chunk, role=role,
                      dataset=f"webtoon/{series.name.replace('-', '')}_restyle", count=info["count"])


def apply(series: Path, episode: str, ids: list, src_folder: str = "panels_hf_vast", file: str = "", variant: str = "restyle",
          strength: float = 1.0, steps: int = 25, log=print) -> list:
    """2단: <회차>/<src_folder>/<컷>.png 을 image1 로 'Convert image 1 to <그림체>.' 편집(UC 편집 워크플로우 + Restyle LoRA 옆길, 터보 없이).
    결과는 <회차>/panels_hf_<variant>/."""
    _env_no_lora(series)
    import comfy_panel as CP
    from comfy_client import Comfy
    from saved_workflow import fill, load_ui, models, to_api
    reg = lora.registry(series)
    e = reg.get(WHO) or reg.get(f"{WHO}@base") or {}
    file = file or e.get("file")
    if not file:
        raise SystemExit("Restyle LoRA 가 없다(restyle.py train)")
    ep = series / "episodes" / episode
    out = ep / f"panels_hf_{variant}"
    out.mkdir(exist_ok=True)
    c, cache = Comfy(), {}
    word = _style_word(series)
    res = []
    wf = _role("restyle")
    for pid in ids:
        src = ep / src_folder / f"{pid}.png"
        if wf:  # 서버 워크플로우 webtoon_restyle_uc: 'Restyle LoRA' 노드에 이 작품 LoRA 를 넣는다
            ui = load_ui(wf)
            for n in ui["nodes"]:
                if str(n.get("title", "")).startswith("Restyle LoRA"):
                    n["widgets_values"] = [file, float(strength)]
                    n["mode"] = 0
            g = fill(to_api(ui, turbo=False), f"Convert image 1 to {word}.", [CP.upload(c, src, cache)], 0, 0, SEED, steps, 1024, prefix="webtoon_studio/restyle")
        else:
            g = fill(to_api(load_ui(models()["uc"]["edit"]), turbo=False), f"Convert image 1 to {word}.", [CP.upload(c, src, cache)], 0, 0, SEED, steps, 1024,
                     prefix="webtoon_studio/restyle")
            g["R1"] = {"class_type": "LoraLoaderBypassModelOnly", "inputs": {"model": ["1", 0], "lora_name": file, "strength_model": strength}}
            for k, v in g.items():
                if k != "R1":
                    for kk, x in list(v["inputs"].items()):
                        if x == ["1", 0]:
                            v["inputs"][kk] = ["R1", 0]
        h = c.wait(c.queue(g), 900)
        c.download(next(i for o in h["outputs"].values() for i in o.get("images", [])), out / f"{pid}.png")
        res.append(out / f"{pid}.png")
        log(f"{pid} → {out / f'{pid}.png'}")
    return res


def evaluate(series: Path, files: list | None = None, log=print) -> Path:
    """학습에 안 쓴 정본 컷: 정답 | 1단 Qwen UC(그 컷 글 + 참고 그림, 터보) | 2단 Restyle(중간 파일마다)."""
    _env_no_lora(series)
    import comfy_panel as CP
    from comfy_client import Comfy
    info = json.loads((_dir(series) / "data.json").read_text(encoding="utf-8"))
    e = lora.registry(series).get(WHO) or lora.registry(series).get(f"{WHO}@base") or {}
    files = files or [ck["file"] for ck in e.get("checkpoints", [])] or [e.get("file")]
    c, cache = Comfy(), {}
    by = {}
    for rel in info["held"]:
        ep_name, pid = rel.split("/")[1], Path(rel).stem
        by.setdefault(ep_name, []).append(pid)
    rows = []
    for ep_name, pids in by.items():
        ep = series / "episodes" / ep_name
        spec = {p["id"]: p for p in (lambda d: d["panels"] if isinstance(d, dict) else d)(json.loads((ep / "panels.json").read_text(encoding="utf-8")))}
        stage1 = ep / "panels_hf_restyle_in"
        stage1.mkdir(exist_ok=True)
        for pid in pids:
            p = spec[pid]
            if not (stage1 / f"{pid}.png").exists():
                prompt, refs = CP.qwen_refs(series, p, expr=False)
                w, h = CP.SIZES[p.get("aspect", "16:9")]
                g = CP.graph(prompt, [CP.upload(c, r, cache) for r in refs], w, h, SEED, 8, 1024, turbo=True, series=series, chars=p.get("chars"))
                hh = c.wait(c.queue(g), 900)
                c.download(next(i for o in hh["outputs"].values() for i in o.get("images", [])), stage1 / f"{pid}.png")
        for f in files:
            tag = "rs_" + Path(f).stem.split("_steps_")[0].split("_")[-1]
            apply(series, ep_name, pids, "panels_hf_restyle_in", f, tag, log=log)
        for pid in pids:
            rows.append([("정답(정본)", ep / "panels_hf" / f"{pid}.png"), ("1단 Qwen UC", stage1 / f"{pid}.png")]
                        + [(f"2단 Restyle {Path(f).stem.split('_steps_')[0].split('_')[-1]}스텝",
                            ep / f"panels_hf_rs_{Path(f).stem.split('_steps_')[0].split('_')[-1]}" / f"{pid}.png") for f in files])
    font = ImageFont.truetype("C:/Windows/Fonts/malgunbd.ttf", 16)
    W = 360
    cells = []
    for row in rows:
        r = []
        for lab, f in row:
            im = Image.open(f).convert("RGB") if f.exists() else Image.new("RGB", (W, 240), "gray")
            im = im.resize((W, int(im.height * W / im.width)))
            cell = Image.new("RGB", (W, im.height + 24), "white")
            cell.paste(im, (0, 24))
            ImageDraw.Draw(cell).text((4, 2), f"{f.stem} {lab}", fill="black", font=font)
            r.append(cell)
        cells.append(r)
    sheet = Image.new("RGB", (max(len(r) for r in cells) * (W + 4), sum(max(x.height for x in r) + 6 for r in cells)), (60, 60, 60))
    y = 0
    for r in cells:
        for k, x in enumerate(r):
            sheet.paste(x, (k * (W + 4), y))
        y += max(x.height for x in r) + 6
    dst = ROOT / "exports" / time.strftime("%Y-%m-%d") / "비교" / f"{series.name}_Restyle_평가.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst, quality=86)
    log(str(dst))
    return dst


def status(series: Path) -> dict:
    """짝·학습·평가 상태(GPU 안 씀)."""
    d = _dir(series)
    info = json.loads((d / "data.json").read_text(encoding="utf-8")) if (d / "data.json").exists() else {}
    e = lora.registry(series).get(WHO) or lora.registry(series).get(f"{WHO}@base") or {}
    ev = ROOT / "exports"
    sheets = sorted(ev.glob(f"*/비교/{series.name}_Restyle_평가.jpg"))
    return {"series": series.name, "style_word": _style_word(series),
            "pairs_made": len(list((d / "in").glob("*.png"))) if (d / "in").exists() else 0, "pairs_planned": len(_held_and_train(series)[0]),
            "held_out": len(info.get("held", [])), "lora": e.get("file"), "checkpoints": [c["steps"] for c in e.get("checkpoints", [])],
            "trained_at": e.get("at"), "eval_sheet": str(sheets[-1]) if sheets else None,
            "workflows": {"dataset": _role("restyle_dataset"), "apply": _role("restyle"), "train": _role("train_lora_base")}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pairs", "dataset", "train", "apply", "eval", "status"])
    ap.add_argument("series")
    ap.add_argument("episode", nargs="?")
    ap.add_argument("panels", nargs="?")
    ap.add_argument("--strength", type=float, default=0.6)
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--src", default="panels_hf_vast")
    ap.add_argument("--file", default="")
    ap.add_argument("--files", default="")
    ap.add_argument("--variant", default="restyle")
    ap.add_argument("--limit", type=int, default=0, help="pairs: 앞에서 몇 쌍만(시험)")
    a = ap.parse_args()
    series = (ROOT / "series" / a.series).resolve() if not Path(a.series).exists() else Path(a.series).resolve()
    if a.cmd == "pairs":
        print(json.dumps({k: v for k, v in pairs(series, a.strength, limit=a.limit).items() if k != "items"}, ensure_ascii=False)[:400])
    elif a.cmd == "dataset":
        print(dataset(series))
    elif a.cmd == "train":
        print(train(series, a.steps, a.rank))
    elif a.cmd == "apply":
        apply(series, a.episode, a.panels.split(","), a.src, a.file, a.variant)
    elif a.cmd == "eval":
        evaluate(series, a.files.split(",") if a.files else None)
    elif a.cmd == "status":
        print(json.dumps(status(series), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
