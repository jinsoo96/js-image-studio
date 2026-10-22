"""힉스필드 판 컷 그리기: 컷 설명(JSON) + 인물 시트·옷 시트·장소 에셋을 참고 그림으로 넣어 Seedream 5.0 Pro 로 한 컷씩.
(2026-10-13 사용자 "캐릭터 에셋을 정해 놓고 그걸로", 그림체 B 선택). 규칙은 LESSONS.md 15, series/d-plus-18-hf/STYLE.md.

    .venv/Scripts/python tools/hf_panel.py <series_hf_dir> <panels.json> [--episode ep002] [--only t1,t2] [--n 2]
panels.json: [{"id": "t1", "chars": ["jaemin"], "outfit": {"jaemin": "Saturday dinner: black crew-neck knit sweater, beige slacks"},
               "location": "bistro", "shot": "medium shot", "aspect": "16:9", "action": "..."}]
      "refs_extra": [{"path": "episodes/ep001/panels_hf/p059.png", "note": "the previous panel; keep the same door"}]  (앞 컷 이어 그리기, 선택)
결과: <series_hf_dir>/episodes/<episode>/panels_hf/<id>.png (--n 2 면 <id>_v1.png …)
인물 이름·참고 그림은 <series_hf_dir>/cast.json(2화·3화에 인물이 늘면 시트를 먼저 만들고 여기에 한 줄), 표정 시트 assets/<id>_expressions.png 는 있으면 같이 넣는다.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ("Draw ONE single webtoon panel (not a sheet, no panel borders, no split screen, no text, no speech bubbles, no sound effects). "
         "Keep each character's face, hairstyle, hair color, body proportions and outfit exactly as in their reference sheets. Adult realistic proportions, "
         "women with a natural medium-size chest. Only the listed characters appear unless background extras are requested.")
# 연기(2026-10-14 사용자 "표정이 너무 작고 다양성이 적어서 로봇들이 대화하는 것 같다"): 같은 "입 벌리고 말함"이 컷 절반에 붙고, 카메라를 보고,
# 표정 기호(땀방울·홍조 빗금)를 막아 둔 게 원인이었다. 컷마다 "acting" 칸에 그 순간의 감정을 눈썹·눈·입·몸·시선으로 적는다.
ACTING = ("ACTING (most important): this is a webtoon, so faces must ACT. Make the emotion strong and instantly readable: eyebrows, eyelids, eye direction, "
          "mouth shape, cheeks and shoulders all change together; never a default pretty neutral face or a generic open-mouth smile. The mouth shape follows "
          "the line (a shout is a wide open mouth, a mumble barely parts the lips, a smirk is lopsided). A character talking to someone looks at that person "
          "(off-panel left or right), not at the camera, unless the panel says they face the viewer. Use clear body language. Webtoon expression marks are "
          "welcome when they fit: sweat drops, blush hatching lines, an anger vein, shock lines, gloom lines, small sparkles. Keep faces on-model and attractive "
          "(no chibi, no flattened gag faces).")
# 맥락(2026-10-13 사용자: 힉스필드에 "가상 캐릭터, 개인 작업, 어디에도 공유 안 함"을 알려 두라). 사실 그대로만 적는다. 작품별 덧붙임은 <series>/prompts/rules_extra.txt
CONTEXT = "Context: original fictional characters (all adults) for a private, non-commercial webtoon project that will not be shared publicly."


def names(series):
    cast = json.loads((series / "cast.json").read_text(encoding="utf-8"))
    return {k: v["en"] for k, v in cast.items() if not k.startswith("_")}


def build(series, p, expr_dirs=("assets",), lean=False, episode="ep001"):
    """lean=True: 인물 시트·장소만(옷 시트·표정 시트 빼고). 검열 오탐 때 다시 시도용
    (2026-10-14 다은 컷: 크롭티·미니스커트가 든 옷 시트 + 센 연기 지시가 겹쳐 4번 연속 "nsfw", 옷 시트를 빼니 통과)."""
    sp = series / "prompts" / "style.txt"  # 무대 뒤는 style.txt, D+18 은 style_v2.txt
    style = (sp if sp.exists() else series / "prompts" / "style_v2.txt").read_text(encoding="utf-8").strip()
    extra = series / "prompts" / "rules_extra.txt"
    rules = RULES + " " + ACTING + (" " + extra.read_text(encoding="utf-8").strip() if extra.exists() else "")
    NAMES = names(series)
    refs, lines = [], []
    for cid in p.get("chars", []):
        refs.append(series / "assets" / f"{cid}_sheet.png")
        lines.append(f"Image {len(refs)} is the character sheet of {NAMES[cid]}.")
        # 회차마다 옷이 바뀌면 episodes/<회차>/assets/<id>_outfits.png 가 먼저(2026-10-14 D+18 2화 서아 새 옷)
        # 컷마다 옷 시트를 고를 수 있다(outfit_sheets: {인물: 작품 폴더 기준 경로}). 2화 v2 는 1화 저녁 옷과 2화 퇴근·집 옷을 한 회차에 같이 쓴다
        # 값이 null 이면 옷 시트를 넣지 않는다(시트의 옷이 글로 적은 옷을 이길 때. 2화 v2 호텔: V넥 블라우스 시트 때문에 "목까지 단추"가 무시됐다)
        sheets = p.get("outfit_sheets", {})
        pick = series / sheets[cid] if sheets.get(cid) else None
        ofit = None if cid in sheets and sheets[cid] is None else next(
            (f for f in (pick, series / "episodes" / episode / "assets" / f"{cid}_outfits.png", series / "assets" / f"{cid}_outfits.png") if f and f.exists()), None)
        if not lean and ofit:
            refs.append(ofit)
            lines.append(f"Image {len(refs)} shows {NAMES[cid]}'s outfits.")
        ex = None if lean else next((series / d / f"{cid}_expressions.png" for d in expr_dirs if (series / d / f"{cid}_expressions.png").exists()), None)
        if ex:
            refs.append(ex)
            lines.append(f"Image {len(refs)} shows {NAMES[cid]}'s range of facial expressions; act with this same face and this level of expressiveness.")
    if p.get("location"):
        refs.append(series / "assets" / "locations" / f"{p['location']}.png")
        lines.append(f"Image {len(refs)} is the location; draw the panel inside this same place with the same furniture, colors and lighting.")
    for x in p.get("refs_extra", []):  # 앞 컷과 이어지는 컷(같은 문·같은 자세)
        refs.append(series / x["path"])
        lines.append(f"Image {len(refs)} is {x['note']}.")
    outfit = " ".join(f"{NAMES[c]} wears: {o}." for c, o in (p.get("outfit") or {}).items())
    acting = f"\n\nACTING IN THIS PANEL: {p['acting']}" if p.get("acting") else ""
    # 어깨 너머 숏처럼 상대가 뒷모습으로 걸리는 컷(2026-10-14 D+18 2화 "마주 앉았는데 앞사람이 사라짐")은 camera 칸에 적고, 혼자 컷 규칙을 붙이지 않는다
    camera = f"\n\nCAMERA: {p['camera']}" if p.get("camera") else ""
    # 혼자 컷: 화면 밖 상대 이름이 나오면 싼 모델(Seedream 5.0 Flash 등)은 그 사람까지 그려 넣는다(2026-10-14 시험) → 한 명만이라고 못 박는다
    if len(p.get("chars", [])) == 1 and not camera:
        acting += (f"\n\nONLY ONE PERSON IN THIS PANEL: {NAMES[p['chars'][0]]}. Anyone else mentioned above (off-panel, across the table, in the doorway) "
                   "is outside the frame and must NOT be drawn: no second person, no back of anyone's head, no shoulder or silhouette in the foreground.")
    prompt = (f"{CONTEXT}\n\n{style}\n\n{rules}\n\nREFERENCES: {' '.join(lines)}\n\nPANEL ({p.get('shot', 'medium shot')}): {p['action']} {outfit}{camera}{acting}").strip()
    return prompt, refs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("panels_json")
    ap.add_argument("--episode", default="ep001")
    ap.add_argument("--only")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--model", default="seedream_v5_pro")
    ap.add_argument("--reverse", action="store_true", help="뒤에서부터(같은 목록을 두 개가 양쪽에서 나눠 그릴 때)")
    ap.add_argument("--resolution", default="1.5k", help="컷 기본 1.5k(Seedream 5.0 Pro 장당 1.25, 2k 의 반값. 결과 1760px 로 합성 폭 1600 보다 커서 손해 없음, 2026-10-14). 모델마다 다름(Lite·4.5 는 \"\")")
    ap.add_argument("--param", action="append", default=[], help="모델별 값 k=v (예: quality=high)")
    ap.add_argument("--variant", help="시안: panels_hf_<이름>/ 에 그리고(본편은 그대로), 표정 시트는 확정 전 후보(assets/_candidates)도 쓴다")
    args = ap.parse_args()
    series = Path(args.series_dir)
    panels = json.loads(Path(args.panels_json).read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    if args.reverse:
        panels = panels[::-1]
    out = series / "episodes" / args.episode / ("panels_hf" + (f"_{args.variant}" if args.variant else ""))
    pdir = series / "prompts" / "panels" / ("" if args.episode == "ep001" else args.episode)  # 2화부터는 회차 폴더(컷 번호가 겹친다)
    if args.variant:
        pdir = pdir / args.variant
    expr_dirs = ("assets", "assets/_candidates") if args.variant else ("assets",)
    pdir.mkdir(parents=True, exist_ok=True)
    failed = []
    for p in panels:
        if only and p["id"] not in only:
            continue
        ok = False
        for lean in (False, True):
            prompt, refs = build(series, p, expr_dirs, lean, args.episode)
            pf = pdir / f"{p['id']}.txt"
            pf.write_text(prompt, encoding="utf-8")
            cmd = [sys.executable, str(ROOT / "tools" / "hf_gen.py"), "--out", str(out), "--name", p["id"], "--prompt-file", str(pf),
                   "--aspect", p.get("aspect", "16:9"), "--model", args.model, "--n", str(args.n), "--resolution", args.resolution]
            for kv in args.param:
                cmd += ["--param", kv]
            for r in refs:
                cmd += ["--ref", str(r)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(r.stdout, end="", flush=True)
            if not r.returncode:
                ok = True
                break
            print(r.stderr[-600:], end="", flush=True)
            if "nsfw" not in (r.stdout + r.stderr) or lean:
                break
            print(f"  {p['id']}: 검열 오탐 → 옷·표정 시트 빼고 한 번 더", flush=True)
        if not ok:  # 한 컷이 실패(검열 오탐·서버 오류)해도 나머지는 계속 그린다
            failed.append(p["id"])
    if failed:
        print("실패한 컷:", ",".join(failed), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
