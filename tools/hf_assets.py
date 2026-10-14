"""힉스필드 판 에셋 목록·점검: 정본 그림(인물 시트·옷·표정·장소)과 그 그림을 만든 프롬프트가 전부 D: 에 있는지 확인하고
<series>/ASSETS.md(사람이 보는 목록) 와 <series>/assets/manifest.json(해시)을 쓴다.
힉스필드 계정을 바꿔도 이 폴더만 있으면 그대로 이어서 그린다(올린 그림 id 는 계정별 캐시라 새 계정에서 다시 올라간다).

    .venv/Scripts/python tools/hf_assets.py series/d-plus-18-hf
"""
import hashlib
import json
import sys
from pathlib import Path


def sha(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def main():
    series = Path(sys.argv[1])
    a = series / "assets"
    cast = json.loads((series / "cast.json").read_text(encoding="utf-8")) if (series / "cast.json").exists() else {}
    rows, man, problems = [], {}, []
    for cid, info in cast.items():
        if cid.startswith("_"):
            continue
        for kind in ("sheet", "outfits", "expressions"):
            f = a / f"{cid}_{kind}.png"
            if f.exists():
                man[str(f.relative_to(series)).replace("\\", "/")] = sha(f)
            elif kind == "sheet":
                problems.append(f"{cid}: 인물 시트 없음({f.name})")
        prompt = series / "prompts" / f"sheet_{cid}.txt"
        rows.append(f"| {cid} | {info.get('ko', '')} | {'O' if (a / f'{cid}_sheet.png').exists() else 'X'} | "
                    f"{'O' if (a / f'{cid}_outfits.png').exists() else '-'} | {'O' if (a / f'{cid}_expressions.png').exists() else '-'} | "
                    f"{'O' if prompt.exists() else '-'} |")
    locs = sorted((a / "locations").glob("*.png"))
    for f in locs:
        man[str(f.relative_to(series)).replace("\\", "/")] = sha(f)
        if not (series / "prompts" / f"loc_{f.stem}.txt").exists():
            problems.append(f"장소 {f.stem}: 프롬프트 없음")
    eps = []
    for ep in sorted((series / "episodes").glob("ep*")):
        pj = ep / "panels.json"
        if not pj.exists():
            continue
        specs = json.loads(pj.read_text(encoding="utf-8"))
        have = sum((ep / "panels_hf" / f"{p['id']}.png").exists() for p in specs)
        for p in specs:
            for c in p.get("chars", []):
                if not (a / f"{c}_sheet.png").exists():
                    problems.append(f"{ep.name} {p['id']}: {c} 시트 없음")
            if p.get("location") and not (a / "locations" / f"{p['location']}.png").exists():
                problems.append(f"{ep.name} {p['id']}: 장소 {p['location']} 없음")
            for x in p.get("refs_extra", []):
                if not (series / x["path"]).exists():
                    problems.append(f"{ep.name} {p['id']}: 참고 그림 {x['path']} 없음")
        eps.append(f"| {ep.name} | {len(specs)} | {have} | {len(list((ep / 'panels_hf' / '_rejected').glob('*.png')))} |")
    (a / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# 에셋 목록(자동, `tools/hf_assets.py`)", "",
          "정본 그림과 그 프롬프트는 전부 이 폴더(D:)에 있다. 힉스필드 계정을 바꾸면 `hf.exe` 로그인만 새로 하면 되고, "
          "올린 그림 id 는 `state/hf_uploads/<계정>.json` 에 계정별로 다시 쌓인다. 해시는 `assets/manifest.json`.", "",
          "## 인물", "", "| id | 이름 | 시트 | 옷 | 표정 | 시트 프롬프트 |", "|---|---|---|---|---|---|", *rows, "",
          f"## 장소 {len(locs)}곳", "", ", ".join(f.stem for f in locs), "",
          "## 회차", "", "| 회차 | 그릴 컷 | 그림 있음 | 다시 그려 뺀 원본 |", "|---|---|---|---|", *eps, "",
          "## 점검", "", *(f"- {x}" for x in problems or ["문제 없음"])]
    (series / "ASSETS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"인물 {len(rows)}, 장소 {len(locs)}, 해시 {len(man)}, 문제 {len(problems)}")
    for x in problems:
        print(" ", x)


if __name__ == "__main__":
    main()
