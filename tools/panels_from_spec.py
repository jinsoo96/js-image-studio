"""컷 설명 표(panels_spec.json) → panels.json. 작품을 가리지 않는 공용 변환기(회차마다 build_panels.py 를 코드로 쓰지 않아도 되게).
(2026-10-16 agent app 시험: Qwen 은 콘티는 잘 썼지만 build_panels.py 를 코드로 쓰다 잘리고 없는 함수를 지어냈다 → 표를 채우는 MCP set_panels 로)

    .venv/Scripts/python tools/panels_from_spec.py <회차 폴더>

panels_spec.json:
  "panels": {"p003": {"chars": ["jaemin"], "outfit": {"jaemin": "white dress shirt, navy slacks"}, "location": "office",
                      "shot": "wide shot", "action": "영어로 그 컷에서 일어나는 일", "acting": "표정·몸짓", "camera": "어깨 너머 숏이면"}}
  "scenes": {"office": "장면 배치 한 문장(누가 화면 왼쪽·오른쪽에서 어디를 보나)"}   ← 인물이 있는 그 장소의 컷마다 붙는다
  "reuse":  {"p010": "episodes/ep001/panels_hf/p012.png"}                         ← 앞 화 그림을 그대로 쓰는 컷
"""
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

ASPECT = {"landscape": "3:2", "close": "4:3", "wide": "16:9", "portrait": "2:3"}  # 콘티 aspect → 그림 비율(2화 v2 build_panels 와 같음)


def norm_shot(s: str) -> str:
    """샷 이름을 그리기 코드가 아는 꼴로(close-up · wide shot · medium shot · full shot · insert shot). 낱말로 본다.
    (2026-10-18 agent app 새 웹툰 생성기 시험: 에이전트가 'close shot' 으로 써서 클로즈업 처리(장소를 뒤로, 배치 문장 빼기)가 안 걸렸다)"""
    t = (s or "medium shot").lower()
    if "close" in t:
        return "extreme close-up" if "extreme" in t else "close-up"
    for word, name in (("wide", "wide shot"), ("long", "wide shot"), ("establish", "wide shot"), ("medium", "medium shot"),
                       ("mid", "medium shot"), ("full", "full shot"), ("insert", "insert shot")):
        if word in t:
            return name
    return s


def main():
    ep = Path(sys.argv[1]).resolve()
    series = ep.parents[1]
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    spec = json.loads((ep / "panels_spec.json").read_text(encoding="utf-8")) if (ep / "panels_spec.json").exists() else {}
    by_id = {p["id"]: p for p in board["panels"]}
    scenes, reuse, P = spec.get("scenes", {}), spec.get("reuse", {}), spec.get("panels", {})

    (ep / "panels_hf").mkdir(exist_ok=True)
    (ep / "panels").mkdir(exist_ok=True)
    copied, bad_reuse = 0, []
    for pid, src in reuse.items():
        s = series / src
        if not s.exists():
            bad_reuse.append(f"{pid}({src})")
            continue
        dst = ep / "panels_hf" / f"{pid}.png"
        if not dst.exists():
            shutil.copy2(s, dst)
            copied += 1
        placed = ep / "panels" / f"{pid}.png"
        if not placed.exists():
            im = Image.open(dst).convert("RGB")
            im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS).save(placed)

    panels, unknown = [], []
    for pid, sp in P.items():
        if pid not in by_id:
            unknown.append(pid)
            continue
        chars = sp.get("chars", [])
        action = sp.get("action", "")
        loc = sp.get("location")
        if loc in scenes and chars and not sp.get("camera"):  # 어깨 너머 숏은 카메라 칸에 배치가 있다
            action = f"{action} SPACE (identical in every panel of this scene): {scenes[loc]}"
        e = {"id": pid, "chars": chars, "outfit": sp.get("outfit", {}), "location": loc, "shot": norm_shot(sp.get("shot", "medium shot")),
             "aspect": sp.get("aspect") or ASPECT.get(by_id[pid].get("aspect", "landscape"), "3:2"), "action": action}
        for k in ("acting", "camera", "refs_extra", "outfit_sheets", "extras", "text_in_image", "look"):
            if sp.get(k):
                e[k] = sp[k]
        panels.append(e)
    missing = [p["id"] for p in board["panels"] if p["id"] not in P and p["id"] not in reuse and not p.get("composite")]
    no_act = [p["id"] for p in panels if p["chars"] and "acting" not in p]
    crowded = [p["id"] for p in panels if "close-up" in p["shot"] and len(p["chars"]) > 1 and not p.get("camera")]  # 클로즈업은 한 사람(panel-spec 규칙)
    (ep / "panels.json").write_text(json.dumps(panels, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"새로 그릴 컷 {len(panels)}, 가져다 쓴 컷 {len(reuse)}(이번에 복사 {copied}), 빠진 컷 {missing}, 연기 없는 인물 컷 {no_act}"
          + (f", 콘티에 없는 번호 {unknown}" if unknown else "") + (f", 없는 재사용 그림 {bad_reuse}" if bad_reuse else "")
          + (f", 인물 둘 이상인 클로즈업 {crowded}(한 사람만 남기거나 medium·어깨 너머 숏 camera 로)" if crowded else ""))


if __name__ == "__main__":
    main()
