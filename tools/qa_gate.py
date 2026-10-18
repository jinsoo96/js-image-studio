"""넘기기 전 기계 검사(관문). 경고가 0 이 아니면 넘기지 않는다(LESSONS.md 0번).
(2026-10-13 사용자 "뭐 할 때마다 자꾸 작붕 일어나고 일관성 없고 하는 게 반복" → 적어 두기만 하던 교훈을 기계가 먼저 잡게)

    .venv/Scripts/python tools/qa_gate.py <episode_dir> [--skip-char]

검사:
1. 콘티·인물 설정(LESSONS 1·2·3·10): 가중치 1.3 초과, extreme_close, 남은 layout(얼굴 둘)·혼자 컷의 count, 금지 태그
   (large breasts·curvy·oversized·cute face·sparkling eyes·bishounen·petite·cinematic lighting·sharp features·anime male lead·defined jawline·wide-eyed 등).
2. 글(LESSONS 7·8): 생각 풍선에 되돌아보는 말(~었다/~줄 알았다로 끝나는 긴 문장), 표지판식 자막(서술어 없이 끝남), 회사 호칭(선배님·에이스·부사수).
3. 말풍선(LESSONS 7): 얼굴 가림(풍선과 이어 주는 띠)·이어 말하는 풍선 순서(check_bubbles 와 같은 계산).
4. 인물 동일인(LESSONS 0): char_check 의 "튐·기준과 다름".
"""
import argparse
import re
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import load_json, load_style  # noqa: E402

# oversized 는 주인공 체형을 부풀린 적이 있지만(태식) 여자 숙소 옷의 오버핏은 의도라 금지 목록에서 뺐다 → 체형은 눈으로 본다
BANNED_TAGS = ["large breasts", "huge breasts", "curvy", "voluptuous", "hourglass", "cute face", "sparkling eyes",
               "bishounen", "petite", "child-like", "cinematic lighting", "sharp features", "anime male lead", "defined jawline",
               "thick eyebrows", "wide-eyed", "school uniform"]
BANNED_WORDS = ["선배님", "에이스", "부사수"]
# 사용자가 지금 얼굴을 받아들인 인물의 기존 태그는 예외(바꾸면 얼굴이 달라진다)
ALLOW = {"jaemin": ["defined jawline"]}
WEIGHT = re.compile(r":\s*(\d+(?:\.\d+)?)\)")
PREDICATE = ("다", "요", "까", "지", "네", "야", "어", "아", "고", "데", "게", "라", "나", "죠", "냐", "자", "래", "걸", "군")


def lint_text(text):
    return [float(w) for w in WEIGHT.findall(text or "") if float(w) > 1.3]


def tags_hit(text):
    t = (text or "").lower()
    out = []
    for b in BANNED_TAGS:
        for m in re.finditer(re.escape(b), t):
            # 부정어 쪽 "(large breasts:1.2)" 같은 건 괜찮다 → 호출하는 쪽이 긍정 텍스트만 넘긴다
            out.append(b)
            break
    return out


def sentence_ok(text):
    """자막에 서술어로 끝나는 문장이 하나는 있는지(표지판식 명사구 막기). "…당했다. 그것도 비뇨기과에서." 처럼 덧붙인 말은 괜찮다."""
    for sent in re.split(r"(?<=[.!?…])\s+|\|", text.strip()):
        t = re.sub(r"[\s.!?…~\"'”’)\]]+$", "", sent)
        t = re.sub(r"\([A-Za-z ]+\)$", "", t).strip()
        if t.endswith(PREDICATE):
            return True
    return False


def retrospective(text):
    t = text.strip()
    return len(t) >= 18 and bool(re.search(r"(었다|았다|였다|했다|줄 알았다|거였다|뻔했다)[.…]*$", t.split("|")[-1].strip()))


def lint_board(ep):
    series = ep.parent.parent
    board = load_json(ep / "storyboard.json")
    chars = load_json(series / "characters.json")
    warns = []
    for cid, c in chars.items():
        for b in tags_hit(c.get("tags", "")):
            if b not in ALLOW.get(cid, []):
                warns.append(("설정", cid, f"인물 태그에 금지 태그 '{b}'"))
        for oname, o in (c.get("outfits") or {}).items():
            for b in tags_hit(o):
                warns.append(("설정", cid, f"옷 '{oname}' 에 금지 태그 '{b}'"))
    for p in board["panels"]:
        pid = p["id"]
        texts = [p.get("prompt", "")] + [r.get("prompt", "") for r in p.get("layout", [])]
        for w in lint_text(" ".join(texts)):
            warns.append(("콘티", pid, f"가중치 {w} > 1.3"))
        if p.get("shot") == "extreme_close":
            warns.append(("콘티", pid, "extreme_close(노이즈 위험) → close"))
        if p.get("layout") and not p.get("composite"):
            warns.append(("콘티", pid, "layout(얼굴 둘 한 컷) 남음"))
        if p.get("count") and len(p.get("characters") or []) == 1:
            warns.append(("콘티", pid, f"혼자 컷에 count '{p['count']}' 남음"))
        for b in tags_hit(" ".join(texts)):
            warns.append(("콘티", pid, f"컷 프롬프트에 금지 태그 '{b}'"))
        for d in p.get("dialogue", []):
            kind, text = d.get("type", "speech"), d.get("text", "")
            if kind == "thought" and retrospective(text):
                warns.append(("글", pid, f"생각 풍선에 되돌아보는 말 → 배경 글(monologue): {text[:30]}"))
            # authored = 사용자 글콘티 그대로인 줄(일부러 끊은 독백 "마흔여섯." 같은 것, 2026-10-18 추몽). 에이전트가 쓴 줄은 그대로 검사한다
            if (kind in ("narration", "monologue") and not d.get("box") and not text.startswith("[") and d.get("style") != "card"
                    and not d.get("authored")):
                if not sentence_ok(text):
                    warns.append(("글", pid, f"자막이 표지판식(서술어 없음): {text[:30]}"))
            for w in BANNED_WORDS:
                if w in text:
                    warns.append(("글", pid, f"호칭 '{w}' (LESSONS 8)"))
    return warns


def bubble_warns(ep):
    import lettering as lt
    style = load_style(ep.parent.parent)
    lt.set_style((style.get("lettering") or {}).get("bubble_style", "inside"))
    warns = []
    for p in load_json(ep / "storyboard.json")["panels"]:
        f = ep / "panels" / f"{p['id']}.png"
        dlg = [d for d in p.get("dialogue", []) if d.get("pos") not in ("above", "below") and d.get("type") not in ("sfx", "narration", "monologue")]
        if not f.exists() or not dlg:
            continue
        im = Image.open(f).convert("RGB")
        if im.height > im.width:
            continue
        im = im.resize((800, round(im.height * 800 / im.width)))
        faces = lt.face_boxes(im, 800, im.height) if (p.get("characters") or p.get("layout")) else []
        bs, _, _ = lt.layout_panel(p, dlg, 800, im.height, im)
        for b in bs:
            box = (b.cx - b.rx, b.cy - b.ry, b.cx + b.rx, b.cy + b.ry)
            if sum(lt.overlap(box, fb) for fb in faces) / (b.w * b.h) > 0.012:
                warns.append(("풍선", p["id"], "말풍선이 얼굴을 가림"))
                break
        if any(a_.neck_to and lt.neck_hits(a_, b_, faces) for a_, b_ in zip(bs, bs[1:])):
            warns.append(("풍선", p["id"], "이어 말하는 풍선을 잇는 띠가 얼굴을 지나감"))
        for a_, b_ in zip(bs, bs[1:]):
            same_row = abs(a_.cy - b_.cy) < min(a_.ry, b_.ry)
            if (same_row and b_.cx < a_.cx) or (not same_row and b_.cy < a_.cy - a_.ry * 0.5):
                warns.append(("풍선", p["id"], "이어 말하는 풍선 순서가 거꾸로"))
                break
    return warns


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--skip-char", action="store_true")
    args = ap.parse_args()
    ep = Path(args.episode_dir)
    warns = lint_board(ep) + bubble_warns(ep)
    for kind, pid, msg in warns:
        print(f"[{kind}] {pid}: {msg}")
    print(f"막는 경고 {len(warns)}개" + ("" if warns else " — 통과"))
    if not args.skip_char:
        # 인물 동일인 검사는 표정·각도 차이도 잡아서(2026-10-13 첫 실행: 20개 중 진짜 튐 3개) 막지 않고 "눈으로 꼭 볼 컷" 목록으로 낸다
        from char_check import check_episode
        look = [(pid, cid, verdict, refd, peer) for pid, cid, refd, peer, verdict in check_episode(ep, verbose=False) if verdict]
        for pid, cid, verdict, refd, peer in look:
            print(f"[눈으로 확인] {pid}: {cid} {verdict} (기준 {refd:.3f}, 동료 {peer:.3f})")
        print(f"눈으로 확인할 컷 {len(look)}개 — 같은 인물 기준 컷과 나란히 크게 보고, 머리·얼굴·체형이 다르면 다시 그린다")
    sys.exit(1 if warns else 0)


if __name__ == "__main__":
    main()
