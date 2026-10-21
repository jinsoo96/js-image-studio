"""그림체 고르기: 같은 장면을 그림체 후보 여러 개로 그려 나란히 비교한다(vast Qwen Image 2.1, 크레딧 0).
새 작품의 series.json style 문단을 정하기 전에 쓴다. 장면마다 시드를 같게 둬서 구도는 비슷하고 그림체만 달라지게 한다.
(2026-10-18 사용자 "새 웹툰, 그림체부터 탐색하자". 전에는 그림체 글을 바로 정하고 인물 시트부터 그렸다)

    .venv/Scripts/python tools/style_probe.py <probe.json> <결과 폴더>

probe.json:
  {"styles": {"<한글 이름>": "English art style paragraph", ...},
   "scenes": {"<한글 이름>": {"text": "English: who, where, what happens, light", "aspect": "3:2"}, ...},
   "people_rule": "선택: 사람 규칙 한 문장(기본은 성인)", "seed": 20261018, "full": false}   full=true 면 터보 없이 40스텝
결과: <폴더>/<장면>__<그림체>.png 와 비교표 compare.jpg(줄 = 그림체, 칸 = 장면), prompts.json(쓴 문장)
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vast"))
from comfy_client import Comfy  # noqa: E402
from comfy_panel import SIZES, graph  # noqa: E402

LABEL_FONT = ROOT / "fonts" / "NanumBarunGothicBold.ttf"


def prompt_for(scene: dict, style: str, people_rule: str) -> str:
    return "\n\n".join([
        "Korean webtoon panel.",
        scene["text"],
        "GAZE: a candid film still. Every person looks at the person or thing they are dealing with, unaware of the camera.",
        "ART STYLE: " + style,
        "One single panel: no text, no speech bubbles, no sound effects, no panel border. " + people_rule,
    ])


def main():
    probe = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    rule = probe.get("people_rule") or "All people are adults with realistic proportions."
    seed = int(probe.get("seed", 20261018))
    c = Comfy()
    used, cells, queued = {}, {}, []
    for si, (sname, scene) in enumerate(probe["scenes"].items()):
        w, h = SIZES[scene.get("aspect", "3:2")]
        for stname, style in probe["styles"].items():
            dst = out / f"{sname}__{stname}.png"
            pr = prompt_for(scene, style, rule)
            used[dst.name] = pr
            if not dst.exists():  # 다시 부르면 빠진 것만 그린다
                full = bool(probe.get("full"))  # "full": true 면 터보 없이 40스텝(고른 그림체를 품질로 확인할 때)
                g = graph(pr, [], w, h, seed + si, 40 if full else 8, 1024, turbo=not full)
                g["9"]["inputs"]["filename_prefix"] = "webtoon_studio/style_probe"
                queued.append((c.queue(g), dst))
            cells[(stname, sname)] = dst
    # 한꺼번에 큐에 넣고 기다린다: 한 장씩 넣고 기다리면 공용 서버에서 학습 구간(30분 넘게)마다 한 장씩 밀렸다(2026-10-21)
    for pid, dst in queued:
        hist = c.wait(pid, 6 * 3600)
        c.download(next(i for o in hist["outputs"].values() for i in o.get("images", [])), dst)
        print(dst, flush=True)
    (out / "prompts.json").write_text(json.dumps(used, ensure_ascii=False, indent=1), encoding="utf-8")
    # 비교표: 줄마다 그림체 하나, 왼쪽에 이름
    cw, lab = 520, 150
    styles, scenes = list(probe["styles"]), list(probe["scenes"])
    rows = []
    for st in styles:
        ims = [Image.open(cells[(st, sc)]).convert("RGB") for sc in scenes]
        ims = [im.resize((cw, round(im.height * cw / im.width))) for im in ims]
        rows.append((st, ims, max(im.height for im in ims)))
    W = lab + len(scenes) * (cw + 8)
    H = sum(r[2] + 8 for r in rows)
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(str(LABEL_FONT), 22)
    y = 0
    for st, ims, rh in rows:
        words, line, lines = st.split(), "", []
        for wd in words:  # 이름이 길면 줄을 바꾼다
            if d.textlength((line + " " + wd).strip(), font=f) > lab - 16:
                lines.append(line)
                line = wd
            else:
                line = (line + " " + wd).strip()
        lines.append(line)
        for k, ln in enumerate(lines):
            d.text((8, y + 10 + k * 28), ln, font=f, fill=(20, 20, 20))
        x = lab
        for im in ims:
            sheet.paste(im, (x, y))
            x += cw + 8
        y += rh + 8
    sheet.save(out / "compare.jpg", quality=88)
    print(out / "compare.jpg")


if __name__ == "__main__":
    main()
