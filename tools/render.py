"""스토리보드 → 패널 이미지 (ComfyUI 로 생성).

    python tools/render.py style-test [--variants 1]
        그림체 확인용 샘플을 style/samples/ 에 만든다(시리즈 캐릭터와 무관한 일반 인물).
    python tools/render.py sheet <series_dir> <char_id> [--variants 2] [--views face,full,intro] [--outfit stage]
        캐릭터 시트(얼굴·전신·소개컷)를 <series_dir>/characters/<char_id>/ 에 만든다.
    python tools/render.py panel <series_dir> <panel.json> <out.png> [--variants 1]
        컷 하나(표지·홍보 이미지)를 그린다.
    python tools/render.py episode <episode_dir> [--only p001,p002] [--variants 1] [--force] [--user-slots]
        storyboard.json 의 컷을 <episode_dir>/panels/ 에 만든다. 이미 있는 컷은 건너뛴다(--force 로 다시).
        직접 작성 칸(author: user)은 기본으로 빼고, --user-slots 일 때만 그 칸들을 그린다.
    python tools/render.py fix <episode_dir> <panel_id> --mask <mask.png> --prompt "..." [--denoise 0.6] [--variants 2]
        본컷은 그대로 두고 마스크(흰 곳)만 다시 칠해 <panel_id>_fix1.png… 로(작은 결함 고치기, 고른 뒤 본컷으로 복사).
    python tools/render.py pick <episode_dir> <panel_id> <variant>
        후보(p003_v2.png)를 본컷(p003.png)으로 고른다.
"""
import argparse
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import (ROOT, Comfy, build_workflow, char_loras, compose_prompt, load_json, load_style,  # noqa: E402
                   panel_workflow, speech_cues, stable_seed)

STYLE_SAMPLES = [
    {"id": "s1_backstage_drama", "mode": "drama", "scene": "backstage", "shot": "close", "aspect": "portrait",
     "characters": ["w"], "prompt": "sitting on equipment case, looking at viewer, slight smile, tired eyes"},
    {"id": "s2_stage_drama", "mode": "drama", "scene": "stage", "shot": "medium", "aspect": "portrait",
     "characters": ["m"], "prompt": "standing on stage, holding microphone, looking up, determined"},
    {"id": "s3_daily_gag", "mode": "gag", "scene": "none", "shot": "close", "aspect": "square",
     "characters": ["w"], "prompt": "embarrassed, flustered, hands on own cheeks, surprised"},
    {"id": "s4_backstage_two", "mode": "drama", "scene": "backstage", "shot": "medium", "aspect": "landscape",
     "characters": ["w", "m"], "prompt": "face to face, close together, whispering, blush, behind stage curtain"},
]
STYLE_SAMPLE_CHARS = {
    "w": {"gender": "girl", "tags": "adult woman, mid twenties, long dark brown hair, brown eyes, slim, tall",
          "outfits": {"default": "black t-shirt, headset, lanyard, jeans"}},
    "m": {"gender": "boy", "tags": "adult man, late twenties, short black hair, sharp eyes, tall, broad shoulders",
          "outfits": {"default": "white shirt, black jacket"}},
}


def refine_regions(comfy, style, panel, characters, data, seed):
    """여러 인물 컷(layout)을 그린 뒤, 인물마다 그 사람 영역만 잘라 그 사람 혼자 설정(인물 LoRA·옷)으로 다시 그려 이어 붙인다.
    영역 프롬프트로 한 장을 통째로 그리면 인물 LoRA 가 약하게 먹어 바로 옆 혼자 컷과 머리·옷·얼굴이 달랐다
    (2026-10-10 사용자 "첫 컷의 여친이랑 두 번째 컷의 여친 얼굴이 같아 보이냐"). style.json 의 region_refine 로 켜고 끈다."""
    cfg = style.get("region_refine") or {}
    if not cfg.get("enabled") or cfg.get("mode") == "faces" or not panel.get("layout"):
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    out = img.copy()
    W, H = img.size
    for k, r in enumerate(panel["layout"]):
        x0, x1 = int(r.get("x", 0) * W), int(min(1.0, r.get("x", 0) + r.get("w", 0.5)) * W)
        crop = img.crop((x0, 0, x1, H))
        sc = 1216 / max(crop.size)
        tw, th = max(64, round(crop.width * sc / 64) * 64), max(64, round(crop.height * sc / 64) * 64)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / f"region_{panel.get('id', 'x')}_{k}.png"
            crop.resize((tw, th), Image.LANCZOS).save(tmp)
            name = comfy.upload(tmp)
        solo = {kk: v for kk, v in panel.items() if kk not in ("layout", "characters", "count", "dialogue")}
        solo["characters"] = [r["char"]]
        solo["prompt"] = r.get("prompt", "")
        # 입모양은 원래 컷에서 이 사람에게 간 말풍선 기준으로(혼자 컷처럼 계산하면 남의 대사에도 입이 열린다)
        cue = speech_cues(panel).get(r["char"])
        if cue:
            solo["prompt"] = f"{cue}, {solo['prompt']}"
        pos, neg = compose_prompt(style, solo, characters)
        wf = build_workflow(style, pos, neg, (tw, th), seed + 100 + k, prefix=f"region/{panel.get('id', 'x')}",
                            loras=char_loras(solo, characters))
        wf["70"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf["71"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["70", 0], "vae": ["1", 2]}}
        wf["5"]["inputs"]["latent_image"] = ["71", 0]
        wf["5"]["inputs"]["denoise"] = cfg.get("denoise", 0.6)
        imgs, _ = comfy.run(wf)
        patch = Image.open(io.BytesIO(imgs[0])).convert("RGB").resize(crop.size, Image.LANCZOS)
        mask = Image.new("L", patch.size, 255)
        feather = 48  # 안쪽 가장자리만 섞는다(컷 테두리 쪽은 그대로)
        for i in range(feather):
            v = int(255 * i / feather)
            if x0 > 0:
                mask.paste(v, (i, 0, i + 1, H))
            if x1 < W:
                mask.paste(v, (patch.width - 1 - i, 0, patch.width - i, H))
        out.paste(patch, (x0, 0), mask.filter(ImageFilter.GaussianBlur(2)))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def refine_faces(comfy, style, panel, characters, data, seed):
    """두 사람 컷(layout)을 한 장으로 그린 뒤, 인물마다 얼굴 둘레만 잘라 그 사람 혼자 설정(인물 LoRA·표정·입모양)으로 다시 그려
    타원으로 부드럽게 붙인다. 배경·몸·구도는 한 장 그대로라 이음새가 없다. 얼굴은 face_detect(애니 얼굴 YOLO)로 찾고,
    영역(layout x·w) 가운데에 가장 가까운 얼굴을 그 사람으로 본다. 못 찾은 인물은 건너뛰고 알린다."""
    cfg = style.get("region_refine") or {}
    if not cfg.get("enabled") or cfg.get("mode") != "faces" or not panel.get("layout"):
        return data
    from face_detect import detect
    img = Image.open(io.BytesIO(data)).convert("RGB")
    W, H = img.size
    boxes = [b[:4] for b in detect(img)]
    used = set()
    for k, r in enumerate(panel["layout"]):
        cx = (r.get("x", 0) + r.get("w", 0.5) / 2) * W
        cand = [(abs((b[0] + b[2]) / 2 - cx), i) for i, b in enumerate(boxes) if i not in used]
        cand = [c for c in cand if c[0] < W * 0.3]
        if not cand:
            print(f"  {panel.get('id')}: {r['char']} 얼굴을 못 찾아 다듬지 않음", flush=True)
            continue
        i = min(cand)[1]
        used.add(i)
        x0, y0, x1, y1 = boxes[i]
        fw, fh = x1 - x0, y1 - y0
        side = int(min(max(fw, fh) * 2.2, W, H))
        fx, fy = (x0 + x1) / 2, (y0 + y1) / 2 + fh * 0.1
        bx = int(min(max(fx - side / 2, 0), W - side))
        by = int(min(max(fy - side / 2, 0), H - side))
        crop = img.crop((bx, by, bx + side, by + side))
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / f"face_{panel.get('id', 'x')}_{k}.png"
            crop.resize((1024, 1024), Image.LANCZOS).save(tmp)
            name = comfy.upload(tmp)
        solo = {kk: v for kk, v in panel.items() if kk not in ("layout", "characters", "count", "dialogue")}
        solo["characters"] = [r["char"]]
        solo["shot"] = "close"
        cue = speech_cues(panel).get(r["char"])
        solo["prompt"] = ", ".join(x for x in (cue, r.get("prompt", ""), "face focus") if x)
        solo["negative"] = ", ".join(x for x in (panel.get("negative", ""), r.get("negative", "")) if x)  # 그 인물만 막을 표정(웃음 등)
        pos, neg = compose_prompt(style, solo, characters)
        wf = build_workflow(style, pos, neg, (1024, 1024), seed + 200 + k, prefix=f"face/{panel.get('id', 'x')}",
                            loras=char_loras(solo, characters))
        wf["70"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf["71"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["70", 0], "vae": ["1", 2]}}
        wf["5"]["inputs"]["latent_image"] = ["71", 0]
        wf["5"]["inputs"]["denoise"] = r.get("face_denoise", cfg.get("face_denoise", 0.5))
        imgs, _ = comfy.run(wf)
        patch = Image.open(io.BytesIO(imgs[0])).convert("RGB").resize(crop.size, Image.LANCZOS)
        mask = Image.new("L", patch.size, 0)
        m = int(side * 0.12)
        ImageDraw.Draw(mask).ellipse((m, m, side - m, side - m), fill=255)
        img.paste(patch, (bx, by), mask.filter(ImageFilter.GaussianBlur(side * 0.06)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def inpaint_fix(comfy, style, panel, characters, img, mask, extra_prompt, denoise, seed):
    """지난번 그림은 그대로 두고 마스크(흰 곳)만 다시 칠한다. 사용자가 그림 결은 좋다고 한 컷의 작은 결함(끈·목걸이·소품)을 고칠 때
    (2026-10-12 "지난번 그림체가 더 낫다"). 마스크 밖은 원본 픽셀을 그대로 되돌려 붙인다."""
    img, mask = img.convert("RGB"), mask.convert("L").resize(img.size)
    with tempfile.TemporaryDirectory() as td:
        ip, mp = Path(td) / f"fix_{panel.get('id', 'x')}.png", Path(td) / f"fixmask_{panel.get('id', 'x')}.png"
        img.save(ip)
        Image.merge("RGB", (mask, mask, mask)).save(mp)
        name, mname = comfy.upload(ip), comfy.upload(mp)
    p = dict(panel)
    p["prompt"] = ", ".join(x for x in (extra_prompt, panel.get("prompt", "")) if x)
    pos, neg = compose_prompt(style, p, characters)
    wf = build_workflow(style, pos, neg, img.size, seed, prefix=f"fix/{panel.get('id', 'x')}", loras=char_loras(p, characters))
    wf["70"] = {"class_type": "LoadImage", "inputs": {"image": name}}
    wf["71"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["70", 0], "vae": ["1", 2]}}
    wf["72"] = {"class_type": "LoadImageMask", "inputs": {"image": mname, "channel": "red"}}
    wf["73"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["71", 0], "mask": ["72", 0]}}
    wf["5"]["inputs"]["latent_image"] = ["73", 0]
    wf["5"]["inputs"]["denoise"] = denoise
    imgs, _ = comfy.run(wf)
    out = Image.open(io.BytesIO(imgs[0])).convert("RGB").resize(img.size, Image.LANCZOS)
    return Image.composite(out, img, mask.filter(ImageFilter.GaussianBlur(4))), pos


def render_two_pass(comfy, style, panel, characters, seed):
    """두 사람 컷을 두 번에 나눠 그린다(콘티 "two_pass": true). ① layout 첫 사람만 그 자리(영역)에 그리고 나머지는 빈자리로
    ② 두 번째 사람 영역만 마스크로 열어, 첫 그림(배경·첫 사람)을 보면서 그 사람 혼자 설정으로 채워 그린다.
    한 번에 한 사람이라 성별·옷·얼굴이 섞이지 않고, 배경·빛은 첫 그림에서 이어진다(2026-10-12: 한 장 통째·영역+반쪽 다시 그리기·
    어깨 너머 모두 섞이거나 증명사진처럼 나왔다)."""
    first, second = panel["layout"][0], panel["layout"][1]
    p1 = {k: v for k, v in panel.items() if k not in ("two_pass",)}
    p1["layout"] = [first]
    p1["characters"] = [first["char"]]
    p1["negative"] = ", ".join(x for x in (panel.get("negative", ""), "multiple people, crowd, 2boys, 2girls") if x)
    wf, pos, neg, regions, size = panel_workflow(style, p1, characters, seed, prefix=f"two/{panel.get('id', 'x')}")
    imgs, _ = comfy.run(wf)
    img = Image.open(io.BytesIO(imgs[0])).convert("RGB")
    W, H = img.size
    x0 = int(second.get("x", 0) * W)
    x1 = int(min(1.0, second.get("x", 0) + second.get("w", 0.5)) * W)
    mask = Image.new("L", (W, H), 0)
    mask.paste(255, (x0, 0, x1, H))
    p2 = {k: v for k, v in panel.items() if k not in ("layout", "two_pass", "count")}
    p2["characters"] = [second["char"]]
    cue = speech_cues(panel).get(second["char"])
    p2["prompt"] = ", ".join(x for x in (cue, second.get("prompt", ""), panel.get("scene_prompt", "")) if x)
    p2["negative"] = ", ".join(x for x in (panel.get("negative", ""), second.get("negative", "")) if x)
    out, pos2 = inpaint_fix(comfy, style, p2, characters, img, mask.filter(ImageFilter.GaussianBlur(12)), "",
                            second.get("denoise", 1.0), seed + 500)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), f"{pos} || {pos2}", neg, size


def render_one(comfy, style, panel, characters, out_path, seed, log, log_key):
    if panel.get("two_pass") and len(panel.get("layout") or []) == 2:
        import time as _t
        t0 = _t.time()
        data, pos, neg, size = render_two_pass(comfy, style, panel, characters, seed)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        log[log_key] = {"file": out_path.name, "seed": seed, "size": size, "seconds": round(_t.time() - t0, 1), "positive": pos,
                        "negative": neg, "method": "two_pass"}
        print(f"  {out_path.name}  seed={seed}  two_pass", flush=True)
        return
    wf, pos, neg, regions, size = panel_workflow(style, panel, characters, seed, prefix=f"webtoon/{out_path.parent.parent.name}/{out_path.stem}",
                                                 style_ref=comfy.style_ref(style))
    init = panel.get("init")
    if init:
        # 장소 밑그림: 그 장소 그림(sets/*.png)을 컷 비율로 잘라 깔고 높은 강도로 새로 그린다. 인물 LoRA 가 사무실로 끄는 컷을
        # 식당 구도·색으로 붙잡는다(2026-10-12 점심 컷). 오려 붙이는 게 아니라 처음부터 다시 그리므로 그림체는 같다
        src = Image.open(ROOT / init["image"]).convert("RGB")
        if init.get("zoom"):  # 장소 그림의 일부만 크게(가까운 샷 배경). x 는 가로 어디를 볼지
            zw, zh = int(src.width * init["zoom"]), int(src.height * init["zoom"])
            zx = int((src.width - zw) * init.get("x", 0.5))
            zy = int((src.height - zh) * init.get("y", 0.4))
            src = src.crop((zx, zy, zx + zw, zy + zh))
        tw, th = size
        sc = max(tw / src.width, th / src.height)
        src = src.resize((round(src.width * sc), round(src.height * sc)), Image.LANCZOS)
        cx = int((src.width - tw) * (0.5 if init.get("zoom") else init.get("x", 0.5)))
        cy = (src.height - th) // 2
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / f"init_{out_path.stem}.png"
            src.crop((cx, cy, cx + tw, cy + th)).save(tmp)
            name = comfy.upload(tmp)
        wf["70"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf["71"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["70", 0], "vae": ["1", 2]}}
        wf["5"]["inputs"]["latent_image"] = ["71", 0]
        wf["5"]["inputs"]["denoise"] = init.get("denoise", 0.8)
    imgs, secs = comfy.run(wf)
    # 빈 그림(한 가지 색) 막기: 깨진 모델 파일·NaN 이면 회색 한 장이 나온다. 그대로 두면 회차 전체를 헛그린다(2026-10-12)
    from PIL import ImageStat
    if max(ImageStat.Stat(Image.open(io.BytesIO(imgs[0])).convert("RGB")).stddev) < 4:
        raise RuntimeError(f"{out_path.name}: 빈 그림이 나왔다(모델 파일·LoRA 확인)")
    if (style.get("region_refine") or {}).get("mode") == "faces":
        data = refine_faces(comfy, style, panel, characters, imgs[0], seed)
    else:
        data = refine_regions(comfy, style, panel, characters, imgs[0], seed)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    log[log_key] = {"file": out_path.name, "seed": seed, "size": size, "seconds": round(secs, 1), "positive": pos, "negative": neg}
    if regions:
        log[log_key]["regions"] = [{"area": [x, y, w, h], "prompt": t} for t, x, y, w, h in regions]
    print(f"  {out_path.name}  seed={seed}  {secs:.0f}s", flush=True)


def write_log(path, log):
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def read_log(path):
    return load_json(path) if path.exists() else {}


def cmd_style_test(args):
    style, comfy = load_style(), Comfy()
    out_dir = ROOT / "style" / "samples"
    log_path = out_dir / "render_log.json"
    log = read_log(log_path)
    only = set(args.only.split(",")) if args.only else None
    for p in STYLE_SAMPLES:
        if only and p["id"] not in only:
            continue
        for v in range(1, args.variants + 1):
            name = p["id"] if args.variants == 1 else f"{p['id']}_v{v}"
            render_one(comfy, style, p, STYLE_SAMPLE_CHARS, out_dir / f"{name}.png",
                       stable_seed("style", style["version"], p["id"], v), log, name)
            write_log(log_path, log)


def cmd_sheet(args):
    style, comfy = load_style(), Comfy()
    series = Path(args.series_dir)
    characters = load_json(series / "characters.json")
    if args.char_id not in characters:
        sys.exit(f"characters.json 에 없는 캐릭터: {args.char_id}")
    out_dir = series / "characters" / args.char_id
    log_path = out_dir / "render_log.json"
    log = read_log(log_path)
    pose = characters[args.char_id].get("pose", "")
    views = [
        {"id": "face", "mode": "drama", "scene": "none", "shot": "close", "aspect": "portrait",
         "prompt": "looking at viewer, neutral expression, simple background, white background"},
        {"id": "full", "mode": "drama", "scene": "none", "shot": "full", "aspect": "tall",
         "prompt": "standing, looking at viewer, arms at sides, simple background, white background"},
        {"id": "intro", "mode": "drama", "scene": "none", "shot": "medium", "aspect": "portrait",
         "prompt": f"standing, looking at viewer, {pose}, gradient background, soft pink background"},
    ]
    wanted = args.views.split(",")
    for view in [v for v in views if v["id"] in wanted]:
        view["characters"] = [args.char_id]
        if args.outfit:
            view["outfits"] = {args.char_id: args.outfit}
        for v in range(1, args.variants + 1):
            name = f"{view['id']}_v{v}"
            render_one(comfy, style, view, characters, out_dir / f"{name}.png",
                       stable_seed(series.name, args.char_id, view["id"], v), log, name)
            write_log(log_path, log)


def cmd_episode(args):
    ep = Path(args.episode_dir)
    board = load_json(ep / "storyboard.json")
    series = ep.parent.parent
    style, comfy = load_style(series), Comfy()
    characters = load_json(series / "characters.json")
    only = set(args.only.split(",")) if args.only else None
    panels_dir = ep / "panels"
    log_path = panels_dir / "render_log.json"
    log = read_log(log_path)
    todo = [p for p in board["panels"] if (not only or p["id"] in only) and (p.get("author") == "user") == args.user_slots
            and not p.get("composite")]  # 합성 컷은 그리지 않는다
    empty_user = [p["id"] for p in todo if not p.get("prompt")]
    todo = [p for p in todo if p.get("prompt")]
    print(f"{ep.name}: {'직접 작성 칸 ' if args.user_slots else ''}{len(todo)}컷 x 후보 {args.variants}", flush=True)
    if empty_user:
        print(f"  프롬프트가 비어 있어 건너뜀: {', '.join(empty_user)}")
    for p in todo:
        for v in range(1, args.variants + 1):
            name = p["id"] if args.variants == 1 else f"{p['id']}_v{v}"
            out = panels_dir / f"{name}.png"
            if out.exists() and not args.force:
                print(f"  {out.name} 있음, 건너뜀")
                continue
            seed = p["seed"] + v - 1 if "seed" in p else stable_seed(series.name, ep.name, p["id"], v)
            render_one(comfy, style, p, characters, out, seed, log, name)
            write_log(log_path, log)


def cmd_panel(args):
    series = Path(args.series_dir)
    style, comfy = load_style(series), Comfy()
    characters = load_json(series / "characters.json")
    panel = load_json(args.panel_json)
    out = Path(args.out_png)
    log_path = out.parent / "render_log.json"
    log = read_log(log_path)
    for v in range(1, args.variants + 1):
        name = out.stem if args.variants == 1 else f"{out.stem}_v{v}"
        seed = panel["seed"] + v - 1 if "seed" in panel else stable_seed(series.name, out.stem, v)
        render_one(comfy, style, panel, characters, out.with_name(f"{name}.png"), seed, log, name)
        write_log(log_path, log)


def cmd_fix(args):
    ep = Path(args.episode_dir)
    series = ep.parent.parent
    style, comfy = load_style(series), Comfy()
    characters = load_json(series / "characters.json")
    panel = next(p for p in load_json(ep / "storyboard.json")["panels"] if p["id"] == args.panel_id)
    src = ep / "panels" / f"{args.panel_id}.png"
    img = Image.open(src).convert("RGB")
    if args.prefill:  # 지울 것(끈 등)을 주변 색으로 먼저 메운다. 그대로 다시 칠하면 남은 모양을 따라 또 그렸다(2026-10-12 p104)
        import cv2
        import numpy as np
        m = (np.asarray(Image.open(args.mask).convert("L").resize(img.size)) > 127).astype(np.uint8)
        a = np.asarray(img).copy()
        if args.prefill == "flat":
            # 둘레의 무채색 밝은 픽셀(셔츠) 중간값 한 가지로 칠한다. telea 로 메우면 끈 색이 번져 들어가 그 자리에
            # 갈색 넥타이 같은 덩어리가 다시 생겼다(2026-10-13 p094·p076)
            ring = (cv2.dilate(m, np.ones((31, 31), np.uint8)) - m).astype(bool)
            hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
            pick = ring & (hsv[..., 1] < 40) & (hsv[..., 2] > 150)
            a[m.astype(bool)] = np.median(a[pick if pick.any() else ring], axis=0).astype(np.uint8)
            img = Image.fromarray(a)
        else:
            img = Image.fromarray(cv2.inpaint(a, m, 9, cv2.INPAINT_TELEA))
    for v in range(1, args.variants + 1):
        out, pos = inpaint_fix(comfy, style, panel, characters, img, Image.open(args.mask), args.prompt,
                               args.denoise, (panel.get("seed") or stable_seed(series.name, ep.name, panel["id"], 1)) + 300 + v)
        dst = ep / "panels" / f"{args.panel_id}_fix{v}.png"
        out.save(dst)
        print(f"  {dst.name}", flush=True)


def cmd_pick(args):
    panels_dir = Path(args.episode_dir) / "panels"
    src = panels_dir / f"{args.panel_id}_v{args.variant}.png"
    if not src.exists():
        sys.exit(f"후보가 없다: {src}")
    shutil.copyfile(src, panels_dir / f"{args.panel_id}.png")
    log_path = panels_dir / "render_log.json"
    log = read_log(log_path)
    if src.stem in log:
        log[args.panel_id] = {**log[src.stem], "file": f"{args.panel_id}.png", "picked_from": src.name}
        write_log(log_path, log)
    print(f"{src.name} -> {args.panel_id}.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("style-test")
    s.add_argument("--variants", type=int, default=1)
    s.add_argument("--only", help="샘플 id 쉼표 구분 (예: s2_stage_drama)")
    s = sub.add_parser("sheet")
    s.add_argument("series_dir")
    s.add_argument("char_id")
    s.add_argument("--variants", type=int, default=2)
    s.add_argument("--views", default="face,full", help="face, full, intro(허벅지 위 + characters.json 의 pose) 중 쉼표 구분")
    s.add_argument("--outfit", help="의상 키 (characters.json outfits). 없으면 default")
    s = sub.add_parser("panel")
    s.add_argument("series_dir")
    s.add_argument("panel_json", help="컷 하나의 JSON 파일(storyboard 컷과 같은 필드). 표지·홍보 이미지용")
    s.add_argument("out_png")
    s.add_argument("--variants", type=int, default=1)
    s = sub.add_parser("episode")
    s.add_argument("episode_dir")
    s.add_argument("--only")
    s.add_argument("--variants", type=int, default=1)
    s.add_argument("--force", action="store_true")
    s.add_argument("--user-slots", action="store_true", help="직접 작성 칸(author: user)만 그린다. 기본은 그 칸을 빼고 그린다")
    s = sub.add_parser("pick")
    s.add_argument("episode_dir")
    s.add_argument("panel_id")
    s.add_argument("variant", type=int)
    s = sub.add_parser("fix")
    s.add_argument("episode_dir")
    s.add_argument("panel_id")
    s.add_argument("--mask", required=True)
    s.add_argument("--prompt", default="")
    s.add_argument("--denoise", type=float, default=0.6)
    s.add_argument("--variants", type=int, default=2)
    s.add_argument("--prefill", nargs="?", const="telea", choices=["telea", "flat"],
                   help="마스크 안을 먼저 메우고 다시 칠한다(지울 것). flat = 둘레 셔츠 색 한 가지로")
    args = ap.parse_args()
    {"style-test": cmd_style_test, "sheet": cmd_sheet, "panel": cmd_panel, "episode": cmd_episode, "pick": cmd_pick,
     "fix": cmd_fix}[args.cmd](args)


if __name__ == "__main__":
    main()
