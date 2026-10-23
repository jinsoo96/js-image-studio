"""한 화를 끝까지 한 번에(작품을 가리지 않는다): 콘티 준비 → 에셋 점검 → vast 로 그리기 → 놓기·합성·점검 → 내보내기.
(2026-10-16 사용자 "D+18 별·무대 뒤 별이 아니라 범용 워크플로우 하나, 'D+18 3화 만들자' 하면 알잘딱으로 돌아가게")
작품마다 다른 건 데이터뿐이다: cast.json(인물) · prompts/style*.txt(그림체) · prompts/loc_*·sheet_*(장소·인물 프롬프트) · assets/(시트·장소 그림) ·
episodes/<회차>/make_storyboard.py·build_panels.py(콘티·컷 설명, 이야기를 받아 사람이나 에이전트가 쓴다).

    .venv/Scripts/python tools/make_episode.py <series_hf_dir> <회차> [--accept-new] [--until assets|draw|compose|export] [--no-check]

1) 콘티 준비: make_storyboard.py → build_panels.py(재사용 컷 복사 포함)
2) 에셋 점검: 이 화에 나오는 인물(cast.json·시트)·장소(장소 그림). 처음 나오는 장소·인물은 vast 로 시안을 만들어
   assets/_candidates/ 에 두고 **멈춘다**(종료 코드 3). 사용자가 보고 고르면 --accept-new 로 다시 부른다(시안을 assets 로 옮기고 이어감).
   인물 참고 그림(머리·표정 칸·센 표정)이 없으면 만든다(vast/sheet_refs.py · make_assets.py, 있는 건 건너뜀).
3) 그리기: 본편 그림(panels_hf)이 없는 새 컷만 vast(Qwen Image 2.1)로. 터보 + 표정 칸 + 2단 남는 사람 지우기 + 3단 연출 확인.
   본편 그림이 이미 있는 컷은 그대로 둔다(기존 그림은 건드리지 않는다).
4) 놓기 → 말풍선 합성·PDF → 기계 점검·말풍선 점검
5) 예고편·epub → exports/<오늘>/
종료 코드: 0 끝 · 3 새 에셋 확인 필요 · 1 실패
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TOOLS, VAST = ROOT / "tools", ROOT / "vast"
STAGES = ["assets", "draw", "compose", "export"]


def run(cmd, label, allow_fail=False, capture=False):
    print(f"\n== {label}", flush=True)
    env = {**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if not capture:
        r = subprocess.run([str(x) for x in cmd], cwd=ROOT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)
    else:
        # 줄마다 바로 넘기면서 모은다: 끝난 뒤 한꺼번에 찍으면 40컷 그리는 40분 동안 작업 로그가 안 늘어 job_status 의 진행(panels_done)이 0 이었고
        # 에이전트가 사용자에게 진행을 알릴 수 없었다(2026-10-23 개항 v3)
        p = subprocess.Popen([str(x) for x in cmd], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)
        lines = []
        for line in p.stdout:
            print(line, end="", flush=True)
            lines.append(line)
        r = subprocess.CompletedProcess(cmd, p.wait(), "".join(lines), "")
    if r.returncode and not allow_fail:
        raise SystemExit(f"실패: {label} (종료 {r.returncode})")
    return r


def audit(series: Path, ep: Path, accept: bool) -> list:
    """이 화에 필요한 인물·장소가 다 있는지 본다. 없는 건 시안을 만들고, accept 면 assets 로 옮긴다. 남은 확인 거리를 돌려준다."""
    cast = json.loads((series / "cast.json").read_text(encoding="utf-8"))
    pj = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    chars = sorted({c for p in pj for c in p.get("chars", [])})
    # 인서트(장소 그림을 안 씀)와 장소 "none" 은 장소 시안이 필요 없다(2026-10-19 외출 p122: "none" 을 새 장소로 보고 멈췄다)
    locs = sorted({p["location"] for p in pj if p.get("location") and p["location"] != "none"
                   and not any(w in str(p.get("shot") or "") for w in ("insert", "top view", "detail"))})
    # 컷 설명의 view(예: "reverse")가 가리키는 같은 장소의 다른 각도 그림 <장소>_<view>. 없으면 그리기가 조용히 앞쪽 그림으로 대신해 듣는 쪽 컷 뒤에 스크린이 그려졌다
    # (2026-10-19 외출 회의실). 앞쪽 장소가 아직 시안이면 그것부터 승인받아야 하니 그때는 뺀다(승인 뒤 다시 부르면 이 시안이 생긴다)
    views = sorted({f"{p['location']}_{p['view']}" for p in pj if p.get("view") and p.get("location") in locs
                    and (series / "assets" / "locations" / f"{p['location']}.png").exists()})
    locs = sorted(set(locs) | set(views))
    todo = []
    for c in chars:
        if c not in cast:
            todo.append(f"인물 {c}: cast.json 에 없다(en·ko·role 을 먼저 넣는다)")
            continue
        if not (series / "assets" / f"{c}_sheet.png").exists():
            todo.append(("sheet", c))
    for loc in locs:
        if not (series / "assets" / "locations" / f"{loc}.png").exists():
            todo.append(("location", loc))
    need = []
    for t in todo:
        if isinstance(t, str):
            need.append(t)
            continue
        kind, name = t
        prompt = series / "prompts" / (f"loc_{name}.txt" if kind == "location" else f"sheet_{name}.txt")
        maker = series / ("make_location_prompts.py" if kind == "location" else "make_sheet_prompts.py")
        if not prompt.exists() and maker.exists():
            run([PY, maker], f"{name} 프롬프트 만들기", allow_fail=True, capture=True)
        elif not prompt.exists() and (series / "series.json").exists():  # 새 작품 틀(tools/new_series.py): 설정 한 장에서 다시 쓴다
            run([PY, TOOLS / "new_series.py", series, "--prompts"], f"{name} 프롬프트 만들기(series.json)", allow_fail=True, capture=True)
            maker = series / "series.json"
        if not prompt.exists():
            need.append(f"{'장소' if kind == 'location' else '인물'} {name}: 프롬프트가 없다({maker.name} 에 한 줄 넣는다)")
            continue
        cand = series / "assets" / "_candidates" / (f"locations/{name}.png" if kind == "location" else f"{name}_sheet.png")
        if not cand.exists():
            run([PY, VAST / "new_assets.py", series, kind, name], f"새 {'장소' if kind == 'location' else '인물'} 시안: {name}", capture=True)
        if accept:
            run([PY, VAST / "new_assets.py", series, kind, name, "--accept"], f"시안을 에셋으로: {name}", capture=True)
        else:
            need.append(f"{'장소' if kind == 'location' else '인물'} {name} 시안 확인: {cand}")
    if not need:  # 인물 참고 그림(머리·표정 칸·센 표정·베이스). 있는 건 건너뛴다
        run([PY, VAST / "sheet_refs.py", series] + chars, "인물 참고 그림 자르기", capture=True)
        for c in chars:
            vr = series / "assets" / "vast_refs"
            if not (vr / f"{c}_xp_12.png").exists() or not (vr / f"{c}_base.png").exists():
                run([PY, VAST / "make_assets.py", series, c, "both", "--res", "768"], f"{c} 센 표정·베이스", allow_fail=True, capture=True)
            if not (vr / f"{c}_xs_01.png").exists():  # 잔잔한 표정 묶음: 센 표정만 있으면 차분한 연기에 우쭐한 미소·오열이 골라졌다(2026-10-19 외출). 있는 건 건너뛴다
                run([PY, VAST / "make_assets.py", series, c, "subtle", "--res", "768"], f"{c} 잔잔한 표정", allow_fail=True, capture=True)
            if not (vr / f"{c}_turnaround.png").exists():  # 캐릭터 시트(앞·뒤·옆): 뒷모습·옆모습 컷의 참고 그림(2026-10-20 보조 도구 시험)
                run([PY, VAST / "make_assets.py", series, c, "turn"], f"{c} 캐릭터 시트", allow_fail=True, capture=True)
    return need


def export(series: Path, ep: Path):
    run([PY, TOOLS / "trailer2.py", ep], "예고편", allow_fail=True, capture=True)
    run([PY, TOOLS / "to_epub.py", ep], "epub", capture=True)
    b = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    title = re.sub(r"[^\w가-힣]+", "", (b.get("title_card") or ep.name).replace(".", ""))
    # 본편 내보내기를 덮지 않게 판 이름을 붙인다(<회차>_vast = GPU 서버판, <회차>_restyle = UC 로 그리고 Restyle LoRA 로 정본 그림체를 입힌 판)
    name = f"{b.get('title', series.name)}_{title}".replace(" ", "") + {"_vast": "_vast판", "_restyle": "_리스타일판"}.get(
        next((s for s in ("_vast", "_restyle") if ep.name.endswith(s)), ""), "")
    day = ROOT / "exports" / time.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    for src, ext in ((next(ep.glob("out/*.pdf"), None), ".pdf"), (next(ep.glob("out/*.epub"), None), ".epub"),
                     (ep / "out" / "trailer.mp4", "_예고편.mp4"), (ep / "out" / "trailer.gif", "_예고편.gif")):
        if src and Path(src).exists():
            shutil.copy2(src, day / (name + ext))
            print("내보냄", day / (name + ext), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("episode")
    ap.add_argument("--accept-new", action="store_true", help="만들어 둔 새 장소·인물 시안을 assets 로 옮기고 이어간다(사용자가 본 뒤)")
    ap.add_argument("--until", choices=STAGES, default="export")
    ap.add_argument("--no-check", action="store_true", help="3단 연출 확인을 끈다(빠르게)")
    args = ap.parse_args()
    series = Path(args.series_dir).resolve()
    ep = series / "episodes" / args.episode
    if not (ep / "make_storyboard.py").exists() or not (ep / "build_panels.py").exists():
        raise SystemExit(f"{ep} 에 make_storyboard.py·build_panels.py 가 없다. 앞 회차 틀로 연 뒤(MCP new_episode) 이야기대로 콘티를 쓴다")
    t0 = time.time()
    run([PY, ep / "make_storyboard.py"], "콘티(storyboard.json)", capture=True)
    run([PY, ep / "build_panels.py"], "컷 설명(panels.json)·재사용 컷", capture=True)

    need = audit(series, ep, args.accept_new)
    if need:
        print("\n== 확인 필요(새 에셋). 사용자가 시안을 보고 고르면 --accept-new 로 다시 부른다:\n- " + "\n- ".join(need), flush=True)
        sys.exit(3)
    if args.until == "assets":
        return

    pj = json.loads((ep / "panels.json").read_text(encoding="utf-8"))
    new = [p["id"] for p in pj if not (ep / "panels_hf" / f"{p['id']}.png").exists()]
    todo = [i for i in new if not (ep / "panels_hf_vast" / f"{i}.png").exists()]
    print(f"\n새 컷 {len(new)}개(본편 그림 없음), 이 중 vast 로 그릴 컷 {len(todo)}개", flush=True)
    if todo:
        cmd = [PY, VAST / "comfy_panel.py", series, ep / "panels.json", "--episode", ep.name, "--only", ",".join(todo),
               "--variant", "vast", "--turbo", "--expr", "--fix"] + ([] if args.no_check else ["--check"])
        r = run(cmd, f"vast {len(todo)}컷 그리기(크레딧 0)", allow_fail=True, capture=True)
        look = [l.strip() for l in r.stdout.splitlines() if "사람 눈으로 확인" in l or "실패" in l]
    else:
        look = []
    (ep / "panels_hf").mkdir(exist_ok=True)
    for i in new:  # 본편 그림이 없는 자리만 채운다(있는 건 건드리지 않음)
        f = ep / "panels_hf_vast" / f"{i}.png"
        if f.exists() and not (ep / "panels_hf" / f"{i}.png").exists():
            shutil.copy2(f, ep / "panels_hf" / f"{i}.png")
    missing = [i for i in new if not (ep / "panels_hf" / f"{i}.png").exists()]
    if missing:
        raise SystemExit(f"그림이 없는 컷: {','.join(missing)}(다시 부르면 그 컷만 다시 그린다)")
    if new:  # 같은 장면 앞뒤 컷의 이어짐(자세·소품·옷·얼굴·그림체·좌우). 컷 하나씩 보는 연출 확인으로는 못 잡았다(2026-10-18 외출 1화)
        cc = run([PY, TOOLS / "continuity_check.py", ep, "--only", ",".join(new)], "장면 이어짐 점검", allow_fail=True, capture=True)
        look += [l.strip() for l in cc.stdout.splitlines() if l.startswith("이어짐 ")]
    if args.until == "draw":
        return

    run([PY, TOOLS / "hf_place.py", ep], "컷 놓기", capture=True)
    run([PY, TOOLS / "run_episode.py", ep], "말풍선 합성·PDF", capture=True)
    qa = run([PY, TOOLS / "qa_gate.py", ep], "기계 점검", allow_fail=True, capture=True)
    run([PY, TOOLS / "check_bubbles.py", ep], "말풍선 점검", allow_fail=True, capture=True)
    if qa.returncode:
        raise SystemExit("막는 경고가 있다(위 기계 점검). 고친 뒤 다시 부른다")
    if args.until == "compose":
        return
    export(series, ep)
    print(f"\n== 끝 ({(time.time() - t0) / 60:.0f}분)" + ("\n사람 눈으로 확인할 컷:\n- " + "\n- ".join(look) if look else ""), flush=True)


if __name__ == "__main__":
    main()
