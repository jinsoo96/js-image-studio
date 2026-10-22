"""그리는 판·본체 파일을 같은 컷·같은 시드로 견준다(모델 고르기·지우기 전 시험).
(2026-10-22 사용자 "모델 여러 개 가져왔으니 성능 별로인 건 하나씩 빼자, 용량 많이 차지하니까. 테스트 잘해 놓고")

    .venv/Scripts/python tools/model_bench.py <series_hf_dir> <회차> --panels p002,p007 [--only 이름,…] [--list]

시험 항목(VARIANTS): (이름, 판, 본체 파일 바꾸기, 터보 쓰기, 스텝·cfg 덮기). 판의 저장 워크플로우를 실제 제작과 같은 길(comfy_panel.qwen_refs·graph)로
채운 뒤 본체(UNETLoader)만 바꾼다. 참고 그림을 받는 판은 같은 참고 그림, 글만 받는 판(MOSS·Anima)은 같은 글.
결과: exports/<오늘>/비교/모델시험/<이름>/<컷>.png + 판 묶음별 한 장(<묶음>.jpg, 컷 x 항목) + times.json(장당 초).

    .venv/Scripts/python tools/model_bench.py <series_hf_dir> <회차> --score     따라 그리기 채점 → scores.json · 점수.md
채점 기준은 '참고 그림과 컷 설명을 파이프라인에 넣었을 때 그대로 따라 그리나'(2026-10-22 사용자):
- 얼굴: 컷 인물마다 그림 속 얼굴 중 가장 닮은 것과 그 인물 기준 얼굴(char_check.references)의 CCIP 거리(작을수록 같은 사람), 기준선(threshold) 안이면 '같은 사람'.
- 옷·장소: 그림 보는 모델(judge_ep)이 컷 설명의 옷·장소·인원을 따랐는지 0~2 로.
"""
import argparse
import json
import re
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
sys.path.insert(0, str(ROOT / "tools"))

# (이름, 판, 본체 파일(None 이면 워크플로우 그대로), 터보, {steps, cfg} 덮기)
VARIANTS = [
    ("uc_fp8", "uc", None, True, {}),
    ("uc_int8", "uc", "qwen-image-2.1-UC-int8_convrot.safetensors", True, {}),
    ("uc_nvfp4", "uc", "qwen-image-2.1-UC-NVFP4.safetensors", True, {}),
    ("noctq_anime_v1", "noctq", None, True, {}),
    ("noctq_real_v4", "noctq_real", None, True, {}),
    ("moss_sft_bf16", "moss", None, True, {}),
    ("moss_sft_int8", "moss", "moss_image_sft_int8_convrot.safetensors", True, {}),
    ("moss_base_int8", "moss", "moss_image_base_int8_convrot.safetensors", False, {}),
    ("moss_sft_turbo_int8", "moss", "moss_image_sft_turbo_int8_convrot.safetensors", False, {"steps": 8, "cfg": 1.0}),
    ("moss_deband_plus_int8", "moss", "moss_image_sft_deband_plus_int8_convrot.safetensors", True, {}),
    ("anima_base_turbo_lora", "anima", None, True, {}),
    ("anima_turbo_v1.1", "anima", "anima-turbo-v1.1.safetensors", False, {"steps": 8, "cfg": 1.0}),
    ("anima_aesthetic_v1.1", "anima", "anima-aesthetic-v1.1.safetensors", False, {}),
    ("animayume_v1.5", "anima", "animayume_v15Base.safetensors", False, {}),
    ("hassaku_v1.3", "anima", "hassakuAnima_v13.safetensors", False, {}),
    ("nova_anime_2.9b", "anima", "novaAnimeAM_v6029B.safetensors", False, {}),
    ("anima_2.9b_int8", "anima", "Anima-2.9B-preview-v1_int8_convrot.safetensors", False, {}),
    ("silvermoon_2.9b_int8", "anima", "SilvermoonMix-Anima29B-Evolved-V2.3-INT8-ConvRot.safetensors", False, {}),
    # Anima 캐릭터 참고(글 + 첫 인물 상반신 한 장): 글만으로는 얼굴·인원을 못 따라와서(위 시험) 참고 그림 방식으로 다시 견준다
    ("anima_ipadapter", "anima_ref", None, True, {}),
    ("anima_fasttrain", "anima_ft", None, True, {}),
]


def family(model: str) -> str:
    return {"noctq": "qwen", "noctq_real": "qwen", "uc": "qwen", "anima_ref": "anima", "anima_ft": "anima"}.get(model, model)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("episode")
    ap.add_argument("--panels", default="p002,p007,p016,p057")
    ap.add_argument("--only", default="", help="이름 몇 개만(쉼표)")
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--score", action="store_true", help="그린 결과를 채점만 한다")
    a = ap.parse_args()
    if a.list:
        return print("\n".join(f"{n:24s} 판 {m:11s} 본체 {u or '(워크플로우 그대로)'} 터보 {t} {o or ''}" for n, m, u, t, o in VARIANTS))
    if a.score:
        return score(Path(a.series_dir).resolve(), a.episode, [i for i in a.panels.split(",") if i])

    import comfy_panel as CP
    from comfy_client import Comfy
    series = Path(a.series_dir).resolve()
    panels = {p["id"]: p for p in json.loads((series / "episodes" / a.episode / "panels.json").read_text(encoding="utf-8"))}
    ids = [i for i in a.panels.split(",") if i in panels]
    only = set(filter(None, a.only.split(",")))
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / "비교" / "모델시험"
    out.mkdir(parents=True, exist_ok=True)
    c, cache = Comfy(), {}
    tf = out / "times.json"
    times = json.loads(tf.read_text(encoding="utf-8")) if tf.exists() else {}
    for name, model, unet, turbo, over in VARIANTS:
        if only and name not in only:
            continue
        os.environ["WEBTOON_MODEL"] = model
        text_only = not CP.gen_takes_refs(series)
        for k, pid in enumerate(ids):
            dst = out / name / f"{pid}.png"
            if dst.exists():
                continue
            p = panels[pid]
            prompt, refs = CP.qwen_refs(series, p, expr=False, text_only=text_only)
            if text_only and p.get("chars") and CP.gen_ref_mode(series) == "character":  # comfy_panel 과 같게: 첫 인물 상반신 한 장
                refs = [series / "assets" / "vast_refs" / f"{p['chars'][0]}_bust.png"]
            names = [CP.upload(c, r, cache) for r in refs]
            w, h = CP.SIZES[p.get("aspect", "16:9")]
            steps = over.get("steps") or (8 if turbo else 30)
            g = CP.graph(prompt, names, w, h, a.seed + k, steps, 1024, turbo, series=series, chars=p.get("chars"))
            for v in g.values():
                if unet and v["class_type"] == "UNETLoader":
                    v["inputs"]["unet_name"] = unet
                if "cfg" in over and v["class_type"] in ("KSampler", "CFGGuider"):
                    v["inputs"]["cfg"] = over["cfg"]
            t0 = time.time()
            try:
                hist = c.wait(c.queue(g), timeout=1800)
                st = hist.get("status", {})
                if st.get("status_str") == "error":
                    raise RuntimeError(next((m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"), "?"))
                img = next(i for o in hist["outputs"].values() for i in o.get("images", []))
                dst.parent.mkdir(parents=True, exist_ok=True)
                c.download(img, dst)
                times.setdefault(name, {})[pid] = round(time.time() - t0, 1)
                print(f"{name} {pid} {times[name][pid]}초", flush=True)
            except Exception as e:  # noqa: BLE001
                times.setdefault(name, {})[pid] = f"실패: {str(e)[:160]}"
                print(f"{name} {pid} 실패 {str(e)[:200]}", flush=True)
            tf.write_text(json.dumps(times, ensure_ascii=False, indent=1), encoding="utf-8")
    sheets(out, ids)


JUDGE = ("This is one panel of a webtoon. The panel description says:\n- people: {people}\n- clothes: {clothes}\n- place: {place}\n\n"
         "Judge only what you can see. Answer only JSON: {{\"people\": 0-2, \"clothes\": 0-2, \"place\": 0-2, \"note\": \"short\"}} "
         "(2 = matches, 1 = partly, 0 = wrong). people = the right number of people and no extra people.")


def score(series: Path, episode: str, ids: list):
    """그린 결과 채점(얼굴 CCIP 거리 + 그림 보는 모델의 옷·장소·인원). 이미 채점한 칸은 건너뛴다."""
    import base64
    from PIL import Image
    from char_check import char_crop, differences, features, references, threshold
    from judge_ep import chat, parse_json
    from face_detect import detect
    import comfy_panel as CP
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / "비교" / "모델시험"
    sf = out / "scores.json"
    sc = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}
    panels = {p["id"]: p for p in json.loads((series / "episodes" / episode / "panels.json").read_text(encoding="utf-8"))}
    refs, th = references(series.name), threshold()
    # CCIP 기준선(th)은 이 작품엔 너무 느슨하다(검은 머리 젊은 남자는 다 같은 사람으로 봄). 인물마다 '가장 닮은 다른 인물'과의 거리를 재서
    # 그 60% 안쪽만 같은 사람으로 센다(2026-10-22 개항: 도경-재헌 0.068, 금수-재헌 0.082)
    ks = sorted(refs)
    rd = differences(features([refs[k] for k in ks])) if len(ks) > 1 else None
    near = {k: float(min(rd[i, j] for j in range(len(ks)) if j != i)) for i, k in enumerate(ks)} if rd is not None else {}
    from hf_panel import names as _names
    names = {k: re.sub(r"\s*\(.*?\)", "", v) for k, v in _names(series).items()}
    for name, *_ in VARIANTS:
        for pid in ids:
            img = out / name / f"{pid}.png"
            if not img.exists() or pid in sc.get(name, {}):
                continue
            p, im = panels[pid], Image.open(img).convert("RGB")
            faces = detect(im) or []
            crops = [c for c in (char_crop(im, k) for k in range(len(faces))) if c is not None]
            dist = {}
            for who in p.get("chars") or []:
                ref = refs.get(who)
                if ref is None:
                    b = series / "assets" / "vast_refs" / f"{who}_bust.png"
                    ref = (char_crop(Image.open(b).convert("RGB")) or Image.open(b).convert("RGB")) if b.exists() else None
                if ref is None or not crops:
                    dist[who] = None
                    continue
                d = differences(features(crops + [ref]))
                dist[who] = round(float(min(d[i, len(crops)] for i in range(len(crops)))), 4)
            people = ", ".join(names.get(c, c) for c in p.get("chars") or []) or "nobody"
            clothes = "; ".join(f"{names.get(k, k)}: {v}" for k, v in (p.get("outfit") or {}).items()) or "(not specified)"
            ask = JUDGE.format(people=people, clothes=clothes, place=p.get("location") or "")
            try:
                b64 = base64.b64encode(img.read_bytes()).decode()
                j = parse_json(chat([{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}, {"type": "text", "text": ask}], max_tokens=200))
            except Exception as e:  # noqa: BLE001
                j = {"note": f"채점 못 함: {str(e)[:80]}"}
            sc.setdefault(name, {})[pid] = {"faces_found": len(faces), "dist": dist, "same": {k: (v is not None and v < th) for k, v in dist.items()},
                                            "judge": j}
            print(name, pid, dist, {k: j.get(k) for k in ("people", "clothes", "place")}, flush=True)
            sf.write_text(json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8")
    # 표: 항목마다 얼굴 거리 평균 · 같은 사람 비율 · 옷/장소/인원 평균(0~2) · 장당 초
    times = json.loads((out / "times.json").read_text(encoding="utf-8")) if (out / "times.json").exists() else {}
    lines = [f"# 따라 그리기 시험 (컷 {', '.join(ids)}, 같은 시드)", "",
             "얼굴 거리 = 그림 속 얼굴과 인물 시트 얼굴의 CCIP 거리(작을수록 같은 사람). 같은 사람 = 그 인물과 가장 닮은 다른 인물 거리의 60% 안쪽 "
             f"({', '.join(f'{k} {v * 0.6:.3f}' for k, v in near.items())}). 인원·옷·장소 = 그림 보는 모델 채점 0~2.", "",
             "| 항목 | 판 | 얼굴 거리 평균(작을수록 같음) | 같은 사람 | 인원 | 옷 | 장소 | 장당 초 |", "|---|---|---|---|---|---|---|---|"]
    for name, model, *_ in VARIANTS:
        rows = sc.get(name) or {}
        if not rows:
            continue
        ds = [v for r in rows.values() for v in r["dist"].values() if v is not None]
        same = [v is not None and v < 0.6 * near.get(k, th / 0.6) for r in rows.values() for k, v in r["dist"].items()]
        avg = lambda k: (sum(r["judge"].get(k) for r in rows.values() if isinstance(r["judge"].get(k), (int, float))) /  # noqa: E731
                         max(1, sum(1 for r in rows.values() if isinstance(r["judge"].get(k), (int, float)))))
        tt = [v for v in (times.get(name) or {}).values() if isinstance(v, (int, float))]
        lines.append(f"| {name} | {model} | {sum(ds) / len(ds):.3f} | {sum(same)}/{len(same)} | {avg('people'):.1f} | {avg('clothes'):.1f} | "
                     f"{avg('place'):.1f} | {sum(tt) / len(tt):.0f} |" if ds else f"| {name} | {model} | 얼굴 못 찾음 | 0/{len(same)} | | | | |")
    (out / "점수.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def sheets(out: Path, ids: list):
    """판 묶음(qwen·moss·anima)마다 한 장: 줄 = 시험 항목, 칸 = 컷."""
    from PIL import Image, ImageDraw
    W = 420
    for fam in ("qwen", "moss", "anima"):
        rows = [(n, [out / n / f"{i}.png" for i in ids]) for n, m, *_ in VARIANTS if family(m) == fam and (out / n).exists()]
        if not rows:
            continue
        H = 300
        sheet = Image.new("RGB", (220 + W * len(ids), (H + 8) * len(rows)), "white")
        d = ImageDraw.Draw(sheet)
        for r, (n, fs) in enumerate(rows):
            y = r * (H + 8)
            d.text((8, y + H // 2), n, fill="black")
            for k, f in enumerate(fs):
                if f.exists():
                    im = Image.open(f).convert("RGB")
                    im.thumbnail((W - 6, H))
                    sheet.paste(im, (220 + k * W, y))
                else:
                    d.text((220 + k * W + 10, y + H // 2), "(없음/실패)", fill="red")
        sheet.save(out / f"{fam}.jpg", quality=88)
        print("한 장:", out / f"{fam}.jpg", flush=True)


if __name__ == "__main__":
    main()
