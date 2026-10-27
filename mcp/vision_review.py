"""그림 검수: 컷 그림 + 그 컷 설명(인물·옷·구도·카메라·연기·대사)을 그림을 보는 모델(지금 켠 LLM 서버)에 보내
인원·좌우·시선·옷·표정·배경이 맞는지 판정과 고칠 점을 받는다.

    python mcp/vision_review.py <series_dir> <episode> [--ids p001,p002] [--scene hotel_room] [--out report.md]

주소·모델은 vast/judge_ep(VLM_URL → state/vlm_url.txt → VLM_FALLBACK, OpenAI 호환이면 어느 모델이든).
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vast"))  # judge_ep(그림 보는 모델 주소 고르기)

ASK = """너는 웹툰 작화 검수자다. 아래 '컷 설명'대로 그림이 그려졌는지 그림을 보고 판정한다. 그림에 실제로 보이는 것만 근거로 한다.
확인할 것:
1) 인원: 그림 속 사람 수(뒷모습·어깨만 걸린 사람도 센다)가 설명과 맞는가. 설명에 없는 사람이 있거나 있어야 할 사람이 빠졌나.
2) 좌우·시선: 설명의 화면 왼쪽/오른쪽 배치, 누가 누구를 보는지(마주 보는 대화면 서로를 봐야 한다, 벽을 보면 안 된다).
   설명이 카메라를 보라고 하지 않았는데 인물이 정면으로 카메라(독자)를 쳐다보고 서 있으면 문제다. 대화하는 두 사람이 나란히 서서 둘 다 독자를 보는 것도 문제다.
3) 옷: 설명의 옷과 같은가(색·종류·목선).
4) 표정·연기: 대사와 연기 설명에 맞는 표정·몸짓인가(웃으면 안 되는데 웃는 등).
5) 배경·장소: 설명의 장소·시간대(낮/밤)와 맞는가. 어깨 너머 숏이면 앞사람 뒤 배경이 맞는 방향인가.
6) 기타: 그림 속 글자, 손·얼굴 뭉개짐, 몸 개수(한 사람에 팔 둘·손 둘·다리 둘. 팔짱 낀 채 손을 하나 더 드는 등 팔이 셋이면 문제).
우선순위: '그림 설명(행동)'·'카메라'가 이 컷에서 일어나는 일이다. 옷 설명은 장면 공통이라, 행동이 옷 상태를 바꾸면(단추를 푼다, 겉옷을 벗는다 등) 행동을 따른다.
어깨 너머 숏에서 앞에 걸린 뒷모습 사람도 사람 수에 넣는다. 애매하면 '맞음'으로 두고 확실한 문제만 적는다.
반드시 JSON 한 개로만 답한다:
{"사람수": 숫자, "인원": "맞음" 또는 "문제: ...", "좌우시선": "맞음" 또는 "문제: ...", "옷": "맞음" 또는 "문제: ...", "표정연기": "맞음" 또는 "문제: ...", "배경": "맞음" 또는 "문제: ...", "기타": "없음" 또는 "문제: ...", "판정": "통과" 또는 "다시", "고칠점": "다시 그릴 때 컷 설명에 넣을 한 줄(통과면 빈 문자열)"}

컷 설명:
"""


def _load(ep: Path):
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    pj = {p["id"]: p for p in json.loads((ep / "panels.json").read_text(encoding="utf-8"))} if (ep / "panels.json").exists() else {}
    return board, pj


def _names(series_dir: Path) -> dict:
    c = json.loads((series_dir / "cast.json").read_text(encoding="utf-8")) if (series_dir / "cast.json").exists() else {}
    return {k: (v.get("en") or v.get("name") or k) if isinstance(v, dict) else str(v) for k, v in c.items() if not k.startswith("_")}


def spec_text(series_dir: Path, ep: Path, pid: str) -> str:
    board, pj = _load(ep)
    bp = next((p for p in board["panels"] if p["id"] == pid), {})
    names = _names(series_dir)
    lines = [f"컷 {pid} · 장면 {bp.get('scene', '')} · 구도 {bp.get('shot', '')}"]
    chars = bp.get("characters", [])
    lines.append("등장인물(콘티): " + (", ".join(f"{c}({names.get(c, c)})" for c in chars) or "없음"))
    dlg = [f"{d.get('type')}: {d.get('text')}" for d in bp.get("dialogue", [])]
    if dlg:
        lines.append("대사·자막: " + " / ".join(dlg))
    p = pj.get(pid)
    if p:
        lines.append(f"그림 설명(구도 {p.get('shot', '')}): {p.get('action', '')}")
        for c, o in (p.get("outfit") or {}).items():
            lines.append(f"{names.get(c, c)} 옷: {o}")
        if p.get("camera"):
            lines.append("카메라: " + p["camera"])
            # 어깨 너머 숏 앞의 뒷모습을 '설명에 없는 사람'으로 걸었다(2026-10-18, 등장인물 칸엔 화면 속 주인공만 있다). 카메라 글이 말하는
            # 사람인지는 검수 모델이 글을 읽고 판단한다: 'over the shoulder' 같은 문구로 가르던 조건은 표현이 다르면 놓쳤다(2026-10-19 사용자 "문구 정규식은 땜질")
            lines.append(f"기대 사람 수: 등장인물 {len(p.get('chars') or chars)}명 + 카메라 설명이 말하는 사람(앞에 걸린 어깨·뒷모습 등). 그 사람도 설명에 있는 사람이다")
        if p.get("acting"):
            lines.append("연기: " + p["acting"])
        if p.get("extras"):  # 이름 없는 군중은 설명에 있는 사람이다(추몽: 공중전화 줄, 반 아이들)
            lines.append("이름 없는 사람들(설명에 있음, 인원에 넣는다): " + p["extras"])
        if p.get("text_in_image"):
            lines.append("그림 속에 있어도 되는 글자: " + p["text_in_image"])
        if p.get("look"):
            lines.append("이 컷의 색·질감(의도한 것): " + p["look"])
    else:
        lines.append("(이 컷은 앞 화·다른 판 그림을 그대로 쓴 컷이다. 대사와 표정·인원이 맞는지 위주로 본다)")
    return "\n".join(lines)


DIRS = ["panels_hf", "panels"]  # 그림을 찾는 폴더 순서. vast 판 검수는 ["panels_hf_vast"](--folder)


def image_of(ep: Path, pid: str):
    for d in DIRS:
        f = ep / d / f"{pid}.png"
        if f.exists():
            return f
    return None


def ask_one(series_dir: Path, ep: Path, pid: str, timeout=60) -> dict:
    f = image_of(ep, pid)
    if not f:
        return {"id": pid, "error": "그림 없음"}
    img = Image.open(f).convert("RGB")
    img.thumbnail((1280, 1280))  # 896 에선 턱의 눈물 자국을 목걸이로 읽었다(2026-10-15 p078)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
               {"type": "text", "text": ASK + spec_text(series_dir, ep, pid)}]
    try:  # 주소는 judge_ep 가 고른다(VLM_URL, 안 되면 VLM_FALLBACK)
        from judge_ep import chat
        text = chat(content, max_tokens=700, timeout=max(timeout, 90))
    except Exception as e:  # noqa: BLE001
        return {"id": pid, "error": f"검수 모델 호출 실패: {str(e)[:120]}"}
    try:
        from judge_ep import parse_json
        d = parse_json(text)  # 처음 '{' 부터 끝 '}' 까지 통째로 읽으면 뒤 설명·잘린 답에서 깨졌다(2026-10-19)
    except ValueError:
        d = {}
    return {"id": pid, **d} if d else {"id": pid, "raw": text[:600]}


CHECKS = ("인원", "좌우시선", "옷", "표정연기", "배경", "기타")


def _probs(r: dict) -> dict:
    return {k: v for k, v in r.items() if k in CHECKS and isinstance(v, str) and v.startswith("문제")}


def fmt(r: dict) -> str:
    if r.get("error"):
        return f"{r['id']}: {r['error']}"
    if r.get("raw"):
        return f"{r['id']}: (형식 밖 답) {r['raw']}"
    probs = [f"{k} {v}" for k, v in _probs(r).items()]
    head = f"{r['id']}: {r.get('판정', '?')} (사람 {r.get('사람수', '?')})"
    return head + ("\n  - " + "\n  - ".join(probs) if probs else "") + (f"\n  고칠 점: {r['고칠점']}" if r.get("고칠점") else "")


def _merge(a: dict, b: dict) -> dict:
    """같은 컷을 두 번 따로 본 결과를 합친다. 두 번 다 걸린 항목만 '다시', 한 번만 걸린 항목은 '애매'.
    (한 번만 보면 판정이 흔들렸다: 같은 호텔 컷을 처음엔 p079, 다음엔 p078 이 걸렸고 p078 은 없는 목걸이를 봤다)"""
    if a.get("error") or a.get("raw"):
        return b if not (b.get("error") or b.get("raw")) else a
    if b.get("error") or b.get("raw"):
        return a
    pa, pb = _probs(a), _probs(b)
    both = {k: pa[k] for k in pa if k in pb}
    once = {k: (pa.get(k) or pb.get(k)) for k in set(pa) ^ set(pb)}
    out = {"id": a["id"], "사람수": a.get("사람수") if a.get("사람수") == b.get("사람수") else f"{a.get('사람수')}~{b.get('사람수')}"}
    out.update(both)
    if both:
        out["판정"] = "다시"
        out["고칠점"] = a.get("고칠점") or b.get("고칠점") or ""
    elif once:
        out["판정"] = "애매"
        out["기타"] = "문제: 한 번만 걸림 → " + " / ".join(f"{k} {v}" for k, v in once.items())
        out["고칠점"] = ""
    else:
        out["판정"] = "통과"
    return out


def review(series_dir: Path, ep: Path, ids: list, workers=6) -> list:
    """컷마다 두 번 따로 보게 하고 합친다(위 _merge)."""
    jobs = [i for i in ids for _ in (0, 1)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(lambda i: ask_one(series_dir, ep, i), jobs))
    return [_merge(res[k], res[k + 1]) for k in range(0, len(res), 2)]


def pick_ids(ep: Path, ids: str = "", scene: str = "") -> list:
    board, pj = _load(ep)
    want = [x for x in re.split(r"[,\s]+", ids or "") if x]
    if want:
        known = {p["id"] for p in board["panels"]}
        missing = [x for x in want if x not in known]
        if missing:
            raise ValueError(f"콘티에 없는 컷 번호: {', '.join(missing)}")
        return want
    out = []
    for p in board["panels"]:
        if p.get("composite") or not p.get("characters"):
            continue
        if scene and p.get("scene") != scene and (pj.get(p["id"], {}).get("location") != scene):
            continue
        if not scene and p["id"] not in pj:  # 기본은 새로 그린 컷만
            continue
        out.append(p["id"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("series_dir")
    ap.add_argument("episode")
    ap.add_argument("--ids", default="")
    ap.add_argument("--scene", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--folder", default="", help="그림 폴더(예: panels_hf_vast). 비우면 panels_hf → panels")
    a = ap.parse_args()
    if a.folder:
        DIRS[:] = [a.folder]
    sd = Path(a.series_dir)
    ep = sd / "episodes" / a.episode
    ids = pick_ids(ep, a.ids, a.scene)
    res = review(sd, ep, ids)
    bad = [r for r in res if r.get("판정") == "다시" or r.get("error")]
    unsure = [r for r in res if r.get("판정") == "애매"]
    report = (f"# 그림 검수 {sd.name}/{a.episode} ({len(res)}컷, 다시 {len(bad)}컷)\n\n" + "\n".join(fmt(r) for r in res)
              + f"\n\n다시 그릴 후보: {', '.join(r['id'] for r in bad) or '없음'} · 애매(사람 눈으로 확인): {', '.join(r['id'] for r in unsure) or '없음'}\n")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(report, encoding="utf-8")
        (Path(a.out).with_suffix(".json")).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.write(report)
