"""새 웹툰(작품) 열기: 작품 설정 한 장(series.json)으로 작품 폴더를 만든다. 작품마다 파이썬(make_*_prompts.py)을 따로 쓰지 않는다.
(2026-10-18 사용자 "agent app 에이전트를 새 웹툰 생성기 · 이어 그리기 둘로". 전에는 새 작품을 손으로 열었다)

    .venv/Scripts/python tools/new_series.py <spec.json>                  series/<slug>-hf 를 새로 만든다(있으면 멈춤)
    .venv/Scripts/python tools/new_series.py <series_dir> --prompts       series.json 을 고친 뒤 cast.json·프롬프트를 다시 쓴다

series.json (영어 칸은 그림 모델에 그대로 들어간다):
  {"slug": "office-noir", "title": "작품 제목", "logline": "한 줄 소개", "genre": "장르·수위", "credit": "글: …  그림: …",
   "style": "English art style paragraph: line, coloring, light, faces, proportions",
   "cast": {"<id>": {"ko": "이름", "en": "English Name (age)", "role": "역할",
                     "who": "English: ethnicity, age, height, build, hair, eyes, face (adult)",
                     "main": "English default outfit", "exprs": "six English expressions, comma separated"}},
   "locations": {"<name>": "English description: place, time of day, light, furniture"}}
만드는 것: series.json · SERIES.md · STYLE.md · README.md · cast.json · characters.json · prompts/style.txt ·
          prompts/sheet_<id>.txt · prompts/loc_<name>.txt · episodes/
인물 시트·장소 그림은 1화를 만들 때 tools/make_episode.py 에셋 점검이 vast 로 시안을 그리고 멈춘다(사용자가 고른 뒤 --accept-new).
첫 인물 시트는 본보기 시트 없이 글로만 그리고, 그다음 인물부터 그 시트를 그림체·배치 본보기로 쓴다(vast/new_assets.py).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERIES_ROOT = ROOT / "series"

SHEET = ("IMAGE: character design reference sheet on a plain light warm-gray background, no text, no labels. Left: four full-body views of the "
         "same person standing in a row (front, three-quarter, side profile, back), consistent proportions. Right: a 3x2 grid of head-and-shoulders "
         "expressions: {exprs}.")
LOC_HEAD = ("IMAGE: background art for a Korean webtoon, an empty location with NO people, wide establishing view, eye level, detailed but clean, "
            "same illustration style and light as described, no text anywhere: shop signs, signboards and menu boards are plain blank colored panels with no letters or characters at all (no Korean, Chinese or English), no logos.")  # 읽을 글자 금지만 적으면 중국어 같은 글자를 간판에 그렸다(2026-10-19 외출 장소 시안 넷)
ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
SLUG = re.compile(r"[a-z][a-z0-9-]{1,40}")
CAST_KEYS = ("ko", "en", "role", "who", "main", "exprs")


HANGUL = re.compile(r"[\uac00-\ud7a3]")
EN_KEYS = ("who", "main", "exprs")  # 그림 모델에 그대로 들어가는 칸(영어 글)


def _by_id(items: list, text_keys: tuple) -> dict:
    """[{"id": ..., ...}] 목록을 {id: 항목} 으로. text_keys 를 주면 값은 그 칸의 글(장소 설명)."""
    out = {}
    for x in items:
        if not isinstance(x, dict):
            continue
        key = next((str(x[k]) for k in ("id", "key", "slug") if x.get(k)), "")
        if not key:
            continue
        out[key] = next((x[k] for k in text_keys if x.get(k)), "") if text_keys else {k: v for k, v in x.items() if k not in ("id", "key", "slug")}
    return out


def normalize(spec: dict) -> dict:
    """받아 줄 수 있는 꼴은 고쳐 둔다: exprs 목록 → 쉼표 글. (2026-10-23 미사일 스튜디오 시험: 모델이 exprs 를 목록으로 넣어 프롬프트에 ['calm', ...] 가 들어갈 뻔)
    cast·locations 가 [{"id": ...}] 목록이면 {id: ...} 로(이름 칸 name → ko, 장소 설명 desc·description·text). 목록으로 넣은 설정에
    'list' object has no attribute 'values' 로 죽었다(2026-10-24 등불 상점 시험)."""
    # 폴더 이름은 <slug>-hf 라 slug 끝의 -hf 는 뗀다(deungbul-store-hf → 폴더 deungbul-store-hf-hf 가 됐다, 2026-10-24)
    if isinstance(spec.get("slug"), str) and spec["slug"].endswith("-hf"):
        spec["slug"] = spec["slug"][:-3]
    if isinstance(spec.get("cast"), list):
        spec["cast"] = _by_id(spec["cast"], ())
        for c in spec["cast"].values():
            if not c.get("ko") and isinstance(c.get("name"), str):
                c["ko"] = c.pop("name")
    if isinstance(spec.get("locations"), list):
        spec["locations"] = _by_id(spec["locations"], ("desc", "description", "text", "prompt"))
    for c in (spec.get("cast") or {}).values():
        if isinstance(c, dict) and isinstance(c.get("outfit"), str) and not isinstance(c.get("main"), str):
            c["main"] = c.pop("outfit")  # 옷 칸을 outfit 이라 부른 설정
        if isinstance(c, dict) and isinstance(c.get("exprs"), (list, tuple)):
            c["exprs"] = ", ".join(str(x).strip() for x in c["exprs"] if str(x).strip())
    return spec


def check(spec: dict) -> list:
    normalize(spec)
    errs = []
    if not SLUG.fullmatch(str(spec.get("slug", ""))):
        errs.append("slug: 영어 소문자·숫자·하이픈(예: office-noir)")
    for k in ("title", "style"):
        if not str(spec.get(k, "")).strip():
            errs.append(f"{k}: 비어 있다")
    cast = spec.get("cast") or {}
    if not cast:
        errs.append("cast: 인물이 하나는 있어야 한다")
    for cid, c in cast.items():
        if not ID.fullmatch(cid):
            errs.append(f"cast.{cid}: id 는 영어 소문자·숫자·밑줄")
        miss = [k for k in CAST_KEYS if not str((c or {}).get(k, "")).strip()]
        if miss:
            errs.append(f"cast.{cid}: {', '.join(miss)} 가 비어 있다")
        # 그림 모델 칸은 영어 글이어야 한다: who 를 한국어로, main 을 true(주인공 여부로 읽음)로 넣은 설정이 그대로 통과해
        # 시트 프롬프트가 'Outfit: True' 가 될 뻔했다(2026-10-23 미사일 스튜디오 시험)
        for k in EN_KEYS:
            v = (c or {}).get(k)
            if v is not None and not isinstance(v, str):
                errs.append(f"cast.{cid}.{k}: 글이어야 한다(받은 값 {v!r}). main 은 주인공 여부가 아니라 기본 옷차림(영어)")
            elif isinstance(v, str) and HANGUL.search(v):
                errs.append(f"cast.{cid}.{k}: 영어로 쓴다(그림 모델에 그대로 들어간다)")
        if isinstance((c or {}).get("exprs"), str) and len([x for x in c["exprs"].split(",") if x.strip()]) < 4:
            errs.append(f"cast.{cid}.exprs: 영어 표정 6개 안팎, 쉼표로")
    for name, desc in (spec.get("locations") or {}).items():
        if not ID.fullmatch(name) or not str(desc).strip():
            errs.append(f"locations.{name}: 이름은 영어 소문자·숫자·밑줄, 설명은 영어 한 문장")
        elif HANGUL.search(str(desc)):
            errs.append(f"locations.{name}: 설명은 영어로(그림 모델에 그대로 들어간다)")
    for k in ("style", "people_rule", "era_rule"):
        if HANGUL.search(str(spec.get(k) or "")):
            errs.append(f"{k}: 영어로(그림 모델에 그대로 들어간다)")
    return errs


def style_text(spec: dict) -> str:
    s = spec["style"].strip()
    return s if s.upper().startswith("ART STYLE") else "ART STYLE (identical in every image): " + s


def write_prompts(d: Path, spec: dict) -> list:
    """cast.json · characters.json · prompts 를 series.json 에서 다시 쓴다(이미 그린 시트·장소 그림은 건드리지 않는다)."""
    style = style_text(spec)
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "style.txt").write_text(style, encoding="utf-8")
    cast = {"_why": "series.json 에서 만든다(tools/new_series.py --prompts). 고칠 때는 series.json 을 고친다."}
    chars = {}
    for cid, c in spec["cast"].items():
        cast[cid] = {"en": c["en"], "ko": c["ko"], "role": c["role"]}
        chars[cid] = {"name": c["ko"], "role": c["role"], "brief": c["who"]}  # run_episode 가 읽는다(옛 ComfyUI 판 칸은 비움)
        (d / "prompts" / f"sheet_{cid}.txt").write_text(
            f"{style}\n\nCharacter: {c['who']} Outfit: {c['main']}.\n\n{SHEET.format(exprs=c['exprs'])}", encoding="utf-8")
    for name, desc in (spec.get("locations") or {}).items():
        (d / "prompts" / f"loc_{name}.txt").write_text(f"{style}\n\n{LOC_HEAD}\n\nLOCATION: {desc.strip().rstrip('.')}.", encoding="utf-8")
    (d / "cast.json").write_text(json.dumps(cast, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "characters.json").write_text(json.dumps(chars, ensure_ascii=False, indent=1), encoding="utf-8")
    return sorted(p.name for p in (d / "prompts").iterdir())


def write_docs(d: Path, spec: dict):
    cast_rows = "\n".join(f"| `{cid}` | {c['ko']} | {c['en']} | {c['role']} |" for cid, c in spec["cast"].items())
    locs = "\n".join(f"- `{n}`: {v}" for n, v in (spec.get("locations") or {}).items()) or "- (아직 없음)"
    (d / "SERIES.md").write_text(
        f"# {spec['title']}\n\n{spec.get('logline', '')}\n\n- 장르·수위: {spec.get('genre', '')}\n- 크레딧: {spec.get('credit', '')}\n\n"
        f"## 인물\n| id | 이름 | 영어 | 역할 |\n|---|---|---|---|\n{cast_rows}\n\n## 장소\n{locs}\n\n"
        "설정 정본은 `series.json`. 고친 뒤 `tools/new_series.py <이 폴더> --prompts`.\n", encoding="utf-8")
    (d / "STYLE.md").write_text(f"# 그림체\n\n{style_text(spec)}\n\n그림 모델에 그대로 들어가는 문장(`prompts/style.txt`). 정본은 `series.json` 의 style.\n",
                                encoding="utf-8")
    (d / "README.md").write_text(
        f"# {spec['title']}\n\n이미지 스튜디오 웹툰 템플릿으로 만든 작품(tools/new_series.py). 설정 `series.json` → `SERIES.md`·`STYLE.md`.\n\n"
        "- 인물 시트·장소 그림: `assets/`(1화 에셋 점검이 vast 로 시안을 만들고, 고른 것만 들어온다)\n"
        "- 회차: `episodes/epNNN/`(make_storyboard.py 콘티 → panels_spec.json 컷 설명 → tools/make_episode.py)\n", encoding="utf-8")


def create(spec: dict) -> Path:
    errs = check(spec)
    if errs:
        raise SystemExit("작품 설정을 고친다:\n- " + "\n- ".join(errs))
    d = SERIES_ROOT / f"{spec['slug']}-hf"
    if d.exists():
        raise SystemExit(f"이미 있다: {d.relative_to(ROOT)} (설정을 바꾸려면 series.json 을 고치고 --prompts)")
    for sub in ("episodes", "assets/locations", "assets/vast_refs", "prompts"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "series.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    write_docs(d, spec)
    write_prompts(d, spec)
    return d


def add(d: Path, kind: str, key: str, value) -> list:
    """인물(kind=cast, value=dict) 또는 장소(kind=location, value=영어 설명)를 series.json 에 더하거나 바꾸고 프롬프트를 다시 쓴다.
    kind=style·people_rule 이면 그 문단(value)을 통째로 바꾼다(key 는 안 쓴다). 그림체를 고르고도 series.json·style.txt·시트 프롬프트를
    손으로 고쳐야 했다(2026-10-23 개항 무대 뒤 그림체)."""
    spec = json.loads((d / "series.json").read_text(encoding="utf-8"))
    if kind in ("style", "people_rule"):
        spec[kind] = str(value).strip()
    else:
        spec.setdefault("cast" if kind == "cast" else "locations", {})[key] = value
    errs = check(spec)
    if errs:
        raise SystemExit("고친다:\n- " + "\n- ".join(errs))
    (d / "series.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    write_docs(d, spec)
    return write_prompts(d, spec)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    p = Path(sys.argv[1])
    if "--prompts" in sys.argv:
        spec = json.loads((p / "series.json").read_text(encoding="utf-8"))
        errs = check(spec)
        if errs:
            raise SystemExit("series.json 을 고친다:\n- " + "\n- ".join(errs))
        write_docs(p, spec)
        print("다시 씀:", ", ".join(write_prompts(p, spec)))
        return
    d = create(json.loads(p.read_text(encoding="utf-8")))
    print(f"새 작품 {d.relative_to(ROOT)}: 인물 {len(json.loads((d / 'series.json').read_text(encoding='utf-8'))['cast'])}명, 프롬프트 "
          f"{len(list((d / 'prompts').iterdir()))}개. 다음: 1화 콘티(MCP new_episode) → 컷 설명 → make_episode(에셋 시안에서 멈춤)")


if __name__ == "__main__":
    main()
