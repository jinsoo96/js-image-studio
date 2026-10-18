"""휴대폰 화면·달력·제목 카드 같은 그래픽 컷을 직접 그린다(모델은 화면 속 글자를 못 쓴다).

    python tools/phone_ui.py <episode_dir> <컷 id>

콘티 컷의 "composite": {"tool": "phone_ui", "kind": ..., ...} 를 읽어 panels/<컷 id>.png 로 저장한다.
kind:
  title    : {"title": "D+18", "subtitle": "..."}                검은 바탕 제목 카드(가로)
  profile  : {"name": "서아", "status": "D+18", "photo": "parts/x.png"(선택)}   메신저 프로필 화면(세로)
  chat     : {"name": "서아", "messages": [{"me": true, "text": "..."}...], "unread": true, "note": "..."}
  calendar : {"month": 10, "year": 2019, "marks": {"14": ["헤어진 날", "#e24a5a"], "17": ["D+1", "#3a7be0"]}, "caption": "..."}
  insta    : {"user": "...", "photo": "parts/x.png", "caption": "...", "likes": 128}
실제 앱 로고·상표는 넣지 않는다(가상의 메신저·SNS 화면).
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "fonts" / "NanumBarunGothicBold.ttf"
FONT_R = ROOT / "fonts" / "NanumBarunGothic.ttf"
BLACK_HAN = ROOT / "fonts" / "BlackHanSans-Regular.ttf"


def f(size, bold=True, path=None):
    return ImageFont.truetype(str(path or (FONT if bold else FONT_R)), size)


def room_bg(W, H, tone=(34, 30, 38)):
    """폰을 든 어두운 방 배경(위 밝고 아래 어둡게)."""
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c * (1.15 - 0.45 * t)) for c in tone))
    return img


def phone_frame(img, box):
    """box(x0,y0,x1,y1) 에 폰 테두리를 그리고 화면 영역을 돌려준다."""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    shadow = Image.new("L", img.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle([x0 + 10, y0 + 18, x1 + 10, y1 + 18], radius=60, fill=150)
    img.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(24)))
    d.rounded_rectangle(box, radius=58, fill=(18, 18, 22))
    sx0, sy0, sx1, sy1 = x0 + 16, y0 + 16, x1 - 16, y1 - 16
    return (sx0, sy0, sx1, sy1)


def status_bar(d, sx0, sy0, sx1, dark=False):
    col = (240, 240, 240) if dark else (30, 30, 34)
    d.text((sx0 + 34, sy0 + 22), "9:41", font=f(24), fill=col)
    d.rounded_rectangle([sx1 - 80, sy0 + 26, sx1 - 36, sy0 + 44], radius=4, outline=col, width=2)
    d.rectangle([sx1 - 77, sy0 + 29, sx1 - 48, sy0 + 41], fill=col)


def paste_cover(img, src_path, box, ep):
    src = Image.open(ep / src_path).convert("RGB")
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    s = max(w / src.width, h / src.height)
    src = src.resize((round(src.width * s), round(src.height * s)), Image.LANCZOS)
    left, top = (src.width - w) // 2, (src.height - h) // 3
    img.paste(src.crop((left, top, left + w, top + h)), (x0, y0))


def kind_title(spec, ep):
    W, H = 1344, 768
    img = Image.new("RGB", (W, H), (10, 10, 14))
    d = ImageDraw.Draw(img)
    t = spec.get("title", "")
    size = 230  # 짧은 제목(D+18) 기준 크기. 긴 제목은 폭 안에 들어올 때까지 줄인다(2026-10-18 "옥상 카페(시험)" 양옆이 잘림)
    while size > 60 and d.textlength(t, font=f(size, path=BLACK_HAN)) > W * 0.9:
        size -= 10
    d.text((W / 2, H / 2 - 30), t, font=f(size, path=BLACK_HAN), fill=(245, 245, 245), anchor="mm")
    if spec.get("subtitle"):
        sub = 40
        while sub > 22 and d.textlength(spec["subtitle"], font=f(sub)) > W * 0.9:
            sub -= 2
        d.text((W / 2, H / 2 + 120), spec["subtitle"], font=f(sub), fill=(170, 170, 180), anchor="mm")
    if spec.get("credit"):  # 작가 표기(글·그림)
        d.text((W / 2, H - 70), spec["credit"], font=f(30, bold=False), fill=(150, 150, 160), anchor="mm")
    return img


def kind_profile(spec, ep):
    W, H = 832, 1216
    img = room_bg(W, H)
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    # 프로필 배경: 사진이 있으면 흐리게, 없으면 은은한 그라데이션
    if spec.get("photo"):
        paste_cover(img, spec["photo"], (sx0, sy0, sx1, sy1), ep)
        region = img.crop((sx0, sy0, sx1, sy1)).filter(ImageFilter.GaussianBlur(6))
        img.paste(region, (sx0, sy0))
        dim = Image.new("RGBA", (sx1 - sx0, sy1 - sy0), (0, 0, 0, 110))
        img.paste(dim, (sx0, sy0), dim)
    else:
        for y in range(sy0, sy1):
            t = (y - sy0) / (sy1 - sy0)
            d.line([(sx0, y), (sx1, y)], fill=(int(214 - 80 * t), int(170 - 60 * t), int(190 - 50 * t)))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=255)
    frame = Image.new("RGB", img.size, (18, 18, 22))
    img = Image.composite(img, frame, mask)
    d = ImageDraw.Draw(img)
    status_bar(d, sx0, sy0, sx1, dark=True)
    cx = (sx0 + sx1) // 2
    d.ellipse([cx - 90, sy1 - 520, cx + 90, sy1 - 340], fill=(236, 226, 230), outline=(255, 255, 255), width=4)
    d.text((cx, sy1 - 300), spec.get("name", ""), font=f(42), fill=(255, 255, 255), anchor="mm")
    d.text((cx, sy1 - 238), spec.get("status", ""), font=f(58), fill=(255, 236, 120), anchor="mm")
    for i, lab in enumerate(("1:1 채팅", "통화", "프로필 편집")):
        x = sx0 + (sx1 - sx0) * (i + 1) / 4
        d.ellipse([x - 26, sy1 - 150, x + 26, sy1 - 98], outline=(255, 255, 255), width=3)
        d.text((x, sy1 - 70), lab, font=f(20, bold=False), fill=(230, 230, 230), anchor="mm")
    return img


def kind_chat(spec, ep):
    W, H = 832, 1216
    img = room_bg(W, H)
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=(186, 206, 224))
    status_bar(d, sx0, sy0, sx1)
    d.rectangle([sx0, sy0 + 64, sx1, sy0 + 130], fill=(172, 192, 212))
    d.text((sx0 + 40, sy0 + 97), "<", font=f(34), fill=(30, 30, 34), anchor="lm")
    d.text(((sx0 + sx1) / 2, sy0 + 97), spec.get("name", ""), font=f(30), fill=(30, 30, 34), anchor="mm")
    y = sy0 + 170
    fm = f(28)
    maxw = (sx1 - sx0) * 0.64
    for m in spec.get("messages", []):
        if m.get("divider"):  # 날짜 구분선(예: 6년 전 마지막 대화와 오늘 사이, 2026-10-14 D+18 2화)
            tw = d.textlength(m["divider"], font=f(20, bold=False)) + 36
            cx = (sx0 + sx1) / 2
            d.rounded_rectangle([cx - tw / 2, y + 4, cx + tw / 2, y + 40], radius=18, fill=(160, 180, 200))
            d.text((cx, y + 22), m["divider"], font=f(20, bold=False), fill=(255, 255, 255), anchor="mm")
            y += 58
            continue
        words, lines, cur = m["text"].split(" "), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if fm.getlength(trial) > maxw and cur:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        bw = max(fm.getlength(ln) for ln in lines) + 40
        bh = len(lines) * 38 + 24
        if m.get("me", True):
            bx1 = sx1 - 30
            bx0 = bx1 - bw
            fill = (255, 226, 54)
        else:
            bx0 = sx0 + 30
            bx1 = bx0 + bw
            fill = (255, 255, 255)
        d.rounded_rectangle([bx0, y, bx1, y + bh], radius=18, fill=fill)
        for i, ln in enumerate(lines):
            d.text((bx0 + 20, y + 14 + i * 38), ln, font=fm, fill=(20, 20, 20))
        if m.get("me", True) and m.get("unread", spec.get("unread", True)):  # 메시지마다 안 읽음(차단 뒤에 보낸 것만 1) 가능
            d.text((bx0 - 10, y + bh - 6), "1", font=f(22), fill=(240, 196, 0), anchor="rb")
        y += bh + 18
    if spec.get("note"):
        d.rounded_rectangle([sx0 + 60, y + 16, sx1 - 60, y + 66], radius=25, fill=(120, 140, 160))
        d.text(((sx0 + sx1) / 2, y + 41), spec["note"], font=f(22), fill=(255, 255, 255), anchor="mm")
    d.rectangle([sx0, sy1 - 100, sx1, sy1], fill=(255, 255, 255))
    d.rounded_rectangle([sx0 + 30, sy1 - 78, sx1 - 110, sy1 - 28], radius=24, fill=(240, 240, 240))
    return img


def kind_calendar(spec, ep):
    import calendar as cal
    W, H = 1216, 832
    img = Image.new("RGB", (W, H), (246, 244, 240))
    d = ImageDraw.Draw(img)
    year, month = spec.get("year", 2019), spec.get("month", 10)
    d.text((80, 70), f"{year}. {month}", font=f(56), fill=(30, 30, 34))
    cw, ch = (W - 160) / 7, 108
    for i, wd in enumerate("일월화수목금토"):
        d.text((80 + cw * i + cw / 2, 170), wd, font=f(28), fill=(200, 60, 60) if i == 0 else (90, 90, 96), anchor="mm")
    first, days = cal.monthrange(year, month)
    start = (first + 1) % 7
    marks = spec.get("marks", {})
    for day in range(1, days + 1):
        idx = start + day - 1
        cx = 80 + cw * (idx % 7) + cw / 2
        cy = 240 + ch * (idx // 7)
        if str(day) in marks:
            lab, col = marks[str(day)]
            d.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], outline=col, width=6)
            d.text((cx, cy + 56), lab, font=f(26), fill=col, anchor="mm")
        d.text((cx, cy), str(day), font=f(34), fill=(30, 30, 34), anchor="mm")
    if spec.get("caption"):
        d.text((W - 80, 92), spec["caption"], font=f(32), fill=(200, 60, 60), anchor="rm")
    return img


def avatar_from_photo(src_path, ep, size, pick=-1):
    """프로필 동그라미: 사진 속 얼굴(face_detect, pick 번째. 기본은 오른쪽 사람)을 잘라 쓴다. 못 찾으면 None."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from face_detect import detect
        src = Image.open(ep / src_path).convert("RGB")
        faces = sorted(detect(src), key=lambda b: b[0])
        x0, y0, x1, y1, _ = faces[pick]
        c, r = ((x0 + x1) / 2, (y0 + y1) / 2), max(x1 - x0, y1 - y0) * 0.75
        face = src.crop((int(c[0] - r), int(c[1] - r), int(c[0] + r), int(c[1] + r))).resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        return face, mask
    except Exception:
        return None


def kind_insta(spec, ep):
    """인스타 게시물: 프로필(사진 속 얼굴)·아이디·사진(한 장)·아이콘 줄·좋아요·아이디+글·댓글 보기·올린 때.
    처음 판은 아이디만 있고 글이 따로 떠서 진짜 인스타 같지 않았다(2026-10-13)."""
    W, H = 832, 1216
    img = room_bg(W, H, (40, 36, 44))
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=(255, 255, 255))
    status_bar(d, sx0, sy0, sx1)
    user = spec.get("user", "")
    av = avatar_from_photo(spec["photo"], ep, 54) if spec.get("photo") else None
    d.ellipse([sx0 + 28, sy0 + 82, sx0 + 86, sy0 + 140], outline=(214, 41, 118), width=3)  # 스토리 테두리
    if av:
        img.paste(av[0], (sx0 + 30, sy0 + 84), av[1])
    else:
        d.ellipse([sx0 + 30, sy0 + 84, sx0 + 84, sy0 + 138], fill=(220, 200, 210))
    d.text((sx0 + 100, sy0 + 111), user, font=f(26), fill=(20, 20, 20), anchor="lm")
    d.text((sx1 - 34, sy0 + 108), "···", font=f(28), fill=(20, 20, 20), anchor="rm")
    px0, py0, px1 = sx0, sy0 + 160, sx1
    py1 = py0 + (px1 - px0)
    if spec.get("photo"):
        paste_cover(img, spec["photo"], (px0, py0, px1, py1), ep)
    d = ImageDraw.Draw(img)
    iy = py1 + 46
    ink = (20, 20, 20)
    d.text((sx0 + 30, iy), "♡", font=f(42), fill=ink, anchor="lm")  # 좋아요
    cx = sx0 + 112  # 댓글: 말풍선 테두리
    d.ellipse([cx - 17, iy - 17, cx + 17, iy + 17], outline=ink, width=3)
    d.polygon([(cx - 14, iy + 8), (cx - 20, iy + 20), (cx - 4, iy + 15)], fill=ink)
    px = sx0 + 168  # 보내기: 종이비행기
    d.polygon([(px - 16, iy - 14), (px + 18, iy - 16), (px - 2, iy + 18)], outline=ink, width=3)
    bx = sx1 - 44  # 저장: 책갈피
    d.line([(bx - 12, iy - 16), (bx + 12, iy - 16), (bx + 12, iy + 17), (bx, iy + 7), (bx - 12, iy + 17), (bx - 12, iy - 16)], fill=ink, width=3)
    d.text((sx0 + 30, iy + 52), f"좋아요 {spec.get('likes', 0)}개", font=f(24), fill=ink, anchor="lm")
    cy = iy + 92
    d.text((sx0 + 30, cy), user, font=f(24), fill=ink, anchor="lm")
    uw = d.textlength(user, font=f(24))
    d.text((sx0 + 30 + uw + 10, cy), spec.get("caption", ""), font=f(24, bold=False), fill=(40, 40, 40), anchor="lm")
    d.text((sx0 + 30, cy + 40), f"댓글 {spec.get('comments', 12)}개 모두 보기", font=f(22, bold=False), fill=(140, 140, 140), anchor="lm")
    d.text((sx0 + 30, cy + 76), spec.get("when", "3일 전"), font=f(20, bold=False), fill=(160, 160, 160), anchor="lm")
    return img


def kind_calls(spec, ep):
    """통화 기록(최근 기록) 화면. 군 시절처럼 폰 대신 공중전화로 걸려 온 전화가 점점 짧아지는 걸 보여 줄 때(2026-10-14 D+18 p014e).
    spec: {"title": "최근 기록", "entries": [{"name": "수신자 부담 전화", "when": "3월 4일 오후 9:02", "dur": "42분 10초", "missed": false}]}"""
    W, H = 832, 1216
    img = room_bg(W, H)
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=(250, 250, 252))
    status_bar(d, sx0, sy0, sx1)
    d.text((sx0 + 36, sy0 + 110), spec.get("title", "최근 기록"), font=f(40), fill=(20, 20, 24), anchor="lm")
    y = sy0 + 170
    for e in spec.get("entries", []):
        missed = e.get("missed", False)
        d.ellipse([sx0 + 30, y + 14, sx0 + 82, y + 66], fill=(232, 236, 242))
        d.text((sx0 + 56, y + 40), "☎", font=f(26, path="C:/Windows/Fonts/seguisym.ttf"), fill=(80, 96, 120), anchor="mm")
        d.text((sx0 + 100, y + 26), e["name"], font=f(27), fill=(214, 56, 64) if missed else (24, 24, 28), anchor="lm")
        d.text((sx0 + 100, y + 60), e.get("when", ""), font=f(21, bold=False), fill=(140, 140, 148), anchor="lm")
        d.text((sx1 - 34, y + 40), "부재중" if missed else e.get("dur", ""), font=f(24, bold=False), fill=(214, 56, 64) if missed else (90, 90, 98), anchor="rm")
        y += 92
        d.line([sx0 + 100, y - 6, sx1 - 30, y - 6], fill=(232, 232, 236), width=1)
    return img


def kind_blocked(spec, ep):
    """메신저 차단 친구 관리 화면. spec: {"title": "차단 친구 관리", "name": "재민이", "pressed": true}
    누른 순간(차단 해제 버튼이 눌린 모습)을 보여 준다(2026-10-14 D+18 2화 서아가 6년 만에 차단을 푸는 장면)."""
    W, H = 832, 1216
    img = room_bg(W, H)
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=(250, 250, 252))
    status_bar(d, sx0, sy0, sx1)
    d.text((sx0 + 40, sy0 + 97), "<", font=f(34), fill=(30, 30, 34), anchor="lm")
    d.text(((sx0 + sx1) / 2, sy0 + 97), spec.get("title", "차단 친구 관리"), font=f(30), fill=(30, 30, 34), anchor="mm")
    d.line([sx0, sy0 + 135, sx1, sy0 + 135], fill=(230, 230, 234), width=2)
    y = sy0 + 170
    d.text((sx0 + 34, y), "차단한 친구 1", font=f(22, bold=False), fill=(140, 140, 148), anchor="lm")
    y += 50
    d.rounded_rectangle([sx0 + 30, y, sx0 + 100, y + 70], radius=26, fill=(214, 220, 230))
    d.text((sx0 + 120, y + 35), spec.get("name", ""), font=f(28), fill=(24, 24, 28), anchor="lm")
    bx1, by0 = sx1 - 30, y + 12
    bx0 = bx1 - 150
    pressed = spec.get("pressed", True)
    d.rounded_rectangle([bx0, by0, bx1, by0 + 46], radius=12, fill=(255, 226, 54) if pressed else (240, 240, 242),
                        outline=(200, 170, 0) if pressed else (210, 210, 214), width=2)
    d.text(((bx0 + bx1) / 2, by0 + 23), "차단 해제", font=f(22), fill=(30, 30, 34), anchor="mm")
    if spec.get("note"):
        d.text(((sx0 + sx1) / 2, y + 140), spec["note"], font=f(21, bold=False), fill=(150, 150, 158), anchor="mm")
    return img


def kind_search(spec, ep):
    """검색 화면. spec: {"query": "삐삐 음성사서함 확인", "results": [{"title": "...", "text": "..."}]}
    첫 결과가 답을 말하는 장면(2026-10-18 추몽 p072 '서비스 종료')."""
    W, H = 832, 1216
    img = room_bg(W, H)
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=(250, 250, 252))
    status_bar(d, sx0, sy0, sx1)
    d.rounded_rectangle([sx0 + 26, sy0 + 76, sx1 - 26, sy0 + 130], radius=27, fill=(238, 240, 244), outline=(210, 214, 222), width=2)
    d.text((sx0 + 52, sy0 + 103), spec.get("query", ""), font=f(24), fill=(30, 30, 34), anchor="lm")
    y = sy0 + 170
    for k, r in enumerate(spec.get("results", [])):
        d.text((sx0 + 34, y), r.get("title", ""), font=f(27 if k == 0 else 24), fill=(26, 70, 160), anchor="lm")
        y += 40
        for ln in wrap_px(d, r.get("text", ""), f(21, bold=False), sx1 - sx0 - 68):
            d.text((sx0 + 34, y), ln, font=f(21, bold=False), fill=(90, 90, 98), anchor="lm")
            y += 30
        y += 26
        d.line([sx0 + 30, y - 12, sx1 - 30, y - 12], fill=(230, 230, 236), width=1)
    return img


def kind_notice(spec, ep):
    """잠금 화면 알림 여러 개가 쌓인 장면. spec: {"time": "23:41", "notices": [{"app": "메신저", "title": "은성고 97졸 (43)", "text": "..."}]}
    (2026-10-18 추몽 p007·p009·p082: 단톡 공지·문자가 연달아 뜸)."""
    W, H = 832, 1216
    img = room_bg(W, H)
    sx0, sy0, sx1, sy1 = phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    for y in range(sy0, sy1):  # 어두운 잠금 화면
        t = (y - sy0) / (sy1 - sy0)
        d.line([(sx0, y), (sx1, y)], fill=(int(40 - 18 * t), int(48 - 20 * t), int(66 - 24 * t)))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([sx0, sy0, sx1, sy1], radius=44, fill=255)
    bg = img.copy()
    img.paste(room_bg(W, H), (0, 0))
    img.paste(bg, (0, 0), mask)
    phone_frame(img, (126, 70, 706, 1160))
    d = ImageDraw.Draw(img)
    d.text(((sx0 + sx1) / 2, sy0 + 150), spec.get("time", ""), font=f(84, bold=False), fill=(236, 240, 248), anchor="mm")
    y = sy0 + 260
    for n in spec.get("notices", []):
        lines = wrap_px(d, n.get("text", ""), f(22, bold=False), sx1 - sx0 - 90)
        h = 74 + 30 * len(lines)
        d.rounded_rectangle([sx0 + 22, y, sx1 - 22, y + h], radius=22, fill=(236, 238, 244))
        d.text((sx0 + 44, y + 24), n.get("app", ""), font=f(18, bold=False), fill=(110, 112, 122), anchor="lm")
        d.text((sx0 + 44, y + 52), n.get("title", ""), font=f(23), fill=(24, 24, 28), anchor="lm")
        ty = y + 84
        for ln in lines:
            d.text((sx0 + 44, ty), ln, font=f(22, bold=False), fill=(54, 54, 60), anchor="lm")
            ty += 30
        y += h + 14
    return img


def kind_band(spec, ep):
    """시대·화자가 바뀌는 곳의 색 띠(위 색 → 아래 색), 가운데 짧은 글. spec: {"top": "#5b6878", "bottom": "#a8865a", "text": "1996년 7월", "h": 900}
    (2026-10-18 추몽 p023: 블루그레이에서 세피아로 넘어가는 여백)."""
    W, H = 1344, int(spec.get("h", 900))
    a = tuple(int(spec.get("top", "#5b6878")[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(spec.get("bottom", "#a8865a")[i:i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3)))
    if spec.get("text"):
        d.text((W / 2, H / 2), spec["text"], font=f(46), fill=(250, 246, 238), anchor="mm")
    return img


SERIF = Path(__file__).resolve().parents[1] / "fonts" / "NotoSerifKR-Variable.ttf"  # 한자까지 있는 명조(나눔명조엔 한자가 없다)


def serif(size, weight="Regular"):
    fnt = ImageFont.truetype(str(SERIF), size)
    fnt.set_variation_by_name(weight)
    return fnt


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def kind_cover(spec, ep):
    """작품 표지·다음 화 카드(글자 디자인). 2026-10-18 사용자 "제목은 추몽(한자)로, 디자인도 알잘딱".
    spec: {"title": "추몽", "hanja": "追夢", "show_hanja": true, "label": "", "subtitle": "1화 「음성사서함」", "quote": "…",
           "credit": "글·그림: …", "top": "#2b333f", "bottom": "#12161d", "accent": "#c8b07c", "bg": "panels_hf/p019.png", "h": 1700}
    바탕은 위→아래 색 흐름 + bg 그림(아주 어둡고 흐리게, 위아래로 스며들게), 큰 한자를 은은하게 깔고,
    한글 제목·작은 한자·선·부제·한 줄을 한 덩어리로 세로 가운데에, 작가는 맨 아래."""
    W, H = 1344, int(spec.get("h", 1700))
    top, bottom, accent = _hex(spec.get("top", "#2b333f")), _hex(spec.get("bottom", "#12161d")), _hex(spec.get("accent", "#c8b07c"))
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(round(top[k] + (bottom[k] - top[k]) * t) for k in range(3)))
    if spec.get("bg") and (Path(ep) / spec["bg"]).exists():  # 작품의 상징 그림(삐삐 등)을 어둡고 흐리게, 위아래 경계 없이
        src = Image.open(Path(ep) / spec["bg"]).convert("L").filter(ImageFilter.GaussianBlur(4))
        bh = round(src.height * W / src.width)
        src = src.resize((W, bh), Image.LANCZOS)
        layer = Image.merge("RGB", [src.point(lambda v, c=c: int(v * c / 255)) for c in (150, 165, 185)])
        yy = np.linspace(0, 1, bh)[:, None]
        fade = np.minimum(np.clip(yy / 0.35, 0, 1), np.clip((1 - yy) / 0.35, 0, 1))  # 위·아래 35% 씩 스며듦
        alpha = (fade * float(spec.get("bg_strength", 0.2)) * 255).astype(np.uint8)
        mask = Image.fromarray(np.repeat(alpha, W, axis=1))
        img.paste(layer, (0, int((H - bh) * 0.4)), mask)
    title, hanja = spec.get("title", ""), spec.get("hanja", "")
    if hanja:  # 큰 한자를 배경 무늬처럼 은은하게
        over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(over)
        big = serif(int(min(W * 0.5, H * 0.55)), "ExtraLight")
        for k, ch in enumerate(hanja):
            od.text((W / 2 + (k - (len(hanja) - 1) / 2) * W * 0.44, H * 0.44), ch, font=big, fill=(255, 255, 255, 20), anchor="mm")
        img.paste(over, (0, 0), over)
    d = ImageDraw.Draw(img)
    size = 300 if H >= 1400 else 200
    while size > 100 and d.textlength(title, font=serif(size, "Bold")) > W * 0.8:
        size -= 10
    blocks = []  # (높이, 그리기)
    if spec.get("label"):
        blocks.append((80, lambda y: d.text((W / 2, y + 40), spec["label"], font=serif(44, "Light"), fill=accent, anchor="mm")))
    blocks.append((size * 1.05, lambda y: d.text((W / 2, y + size * 0.52), title, font=serif(size, "Bold"), fill=(242, 238, 228), anchor="mm")))
    if hanja and spec.get("show_hanja", True):
        blocks.append((100, lambda y: d.text((W / 2, y + 50), hanja, font=serif(72, "Medium"), fill=accent, anchor="mm")))
    blocks.append((70, lambda y: d.line([(W / 2 - 120, y + 35), (W / 2 + 120, y + 35)], fill=accent, width=2)))
    if spec.get("subtitle"):
        blocks.append((90, lambda y: d.text((W / 2, y + 45), spec["subtitle"], font=serif(56, "Medium"), fill=(232, 228, 220), anchor="mm")))
    if spec.get("quote"):
        lines = wrap_px(d, spec["quote"], serif(34, "Light"), W * 0.7)
        blocks.append((30 + 52 * len(lines), lambda y, lines=lines: [d.text((W / 2, y + 40 + 52 * k), ln, font=serif(34, "Light"), fill=(170, 176, 186), anchor="mm")
                                                                    for k, ln in enumerate(lines)]))
    total = sum(h for h, _ in blocks)
    y = (H - (150 if spec.get("credit") else 0) - total) / 2
    for h, draw in blocks:
        draw(y)
        y += h
    if spec.get("credit"):
        d.text((W / 2, H - 100), spec["credit"], font=serif(34, "Regular"), fill=(150, 158, 168), anchor="mm")
    return img


def wrap_px(d, text, fnt, max_w):
    """글자 폭 기준 줄 바꿈(한글은 띄어쓰기 단위, 넘치면 글자 단위)."""
    out, cur = [], ""
    for word in text.split(" "):
        cand = (cur + " " + word).strip()
        if d.textlength(cand, font=fnt) <= max_w:
            cur = cand
            continue
        if cur:
            out.append(cur)
        cur = word
        while d.textlength(cur, font=fnt) > max_w:
            k = len(cur)
            while k > 1 and d.textlength(cur[:k], font=fnt) > max_w:
                k -= 1
            out.append(cur[:k])
            cur = cur[k:]
    if cur:
        out.append(cur)
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    ep, pid = Path(sys.argv[1]), sys.argv[2]
    board = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    panel = next(p for p in board["panels"] if p["id"] == pid)
    spec = panel["composite"]
    img = {"title": kind_title, "profile": kind_profile, "chat": kind_chat, "calendar": kind_calendar,
           "insta": kind_insta, "calls": kind_calls, "blocked": kind_blocked, "search": kind_search,
           "notice": kind_notice, "band": kind_band, "cover": kind_cover}[spec["kind"]](spec, ep)
    out = ep / "panels" / f"{pid}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"{out.name} ({spec['kind']})")


if __name__ == "__main__":
    main()
