"""웹툰 식자: 말풍선·내레이션·효과음을 그리고 컷 위·사이 여백에 배치한다. compose.py 가 쓴다.

기준은 2026-10-07 웹툰 식자 조사(Clip Studio·토투스·코미코·LINE Webtoon 가이드 등, worklogs/2026-10-07-lettering.md):
- 말풍선 폭 200~270px(800px 캔버스), 가로:세로 약 3:2, 거의 둥근 모양. 2줄 기본 3줄까지, 한 줄 7~12자, 마름모꼴 줄바꿈(어절 단위).
- 글자 24~28px, 행간 130%, 안쪽 여백 글자 한 자 이상. 테두리 3.5px 안팎, 3배로 그려 줄여 가장자리를 깨끗하게.
- 배치: 위에서 아래로 지그재그(같은 높이에 나란히 두지 않음), 컷 위 가장자리에 1/3 정도 걸쳐 여백으로 나오게. 같은 사람의 연속 대사는 목으로 잇고 꼬리는 첫 풍선만.
- 꼬리: 짧게(머리까지의 55%, 최대 70px), 밑변 30px, 풍선 아래 1/3 에서 말하는 사람 머리 쪽으로.
- 외침은 가시 모양 + 굵은 글꼴, 생각은 구름 + 작은 원 꼬리, 내레이션은 명조 사각 상자, 효과음은 글자마다 기울이고 검은 안쪽선 + 흰 바깥선.
"""
import math
import random
from itertools import combinations
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SS = 3  # 말풍선 모양을 3배로 그려 줄인다

FONT_FILES = {
    "speech": ROOT / "fonts" / "NanumBarunGothicBold.ttf",
    "thought": ROOT / "fonts" / "GowunDodum-Regular.ttf",
    "shout": ROOT / "fonts" / "BlackHanSans-Regular.ttf",
    "narration": ROOT / "fonts" / "NanumMyeongjo-Bold.ttf",
    "monologue": ROOT / "fonts" / "NanumMyeongjo-Bold.ttf",
    "sfx": ROOT / "fonts" / "BlackHanSans-Regular.ttf",
    "hand": ROOT / "fonts" / "NanumPenScript-Regular.ttf",  # 손글씨 독백(화자가 바뀐 걸 글씨로, 2026-10-18 추몽 미선)
}
SIZES = {"speech": 25, "thought": 25, "shout": 30, "narration": 23, "monologue": 27, "sfx": 54, "hand": 34}
MONO_INK = (46, 38, 54)
INK, PAPER = (17, 17, 17), (255, 255, 255)
STROKE = 3.5
MAX_LINE_W = 236  # 한 줄 최대 폭(px). 글자 25px 기준 약 10~11자
SIDE_X = {"L": 0.27, "C": 0.5, "R": 0.73}
HEAD_Y = {"extreme_close": 0.42, "close": 0.33, "medium": 0.24, "full": 0.16, "wide": 0.22, "detail": 0.3, "insert": 0.3}

_font_cache = {}

# 말풍선 결(2026-10-10 사용자 "말풍선 별로" → 시안 셋). edge = 컷 위 가장자리에 1/3 걸침(2026-10-07 식자 조사),
# inside = 컷 안쪽에 넣고 글씨 크게·선 얇게, soft = 컷 안쪽 + 테두리 없는 흰 풍선에 옅은 그림자(요즘 로맨스 웹툰 결)
BUBBLE_STYLE = __import__("os").environ.get("BUBBLE_STYLE", "edge")


def set_style(name):
    global BUBBLE_STYLE
    BUBBLE_STYLE = name or "edge"
    _font_cache.clear()


def _scale():
    return 1.0 if BUBBLE_STYLE == "edge" else 1.12


def font(kind, scale=1.0):
    size = round(SIZES[kind] * scale * (_scale() if kind in ("speech", "thought", "shout") else 1.0))
    key = (kind, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONT_FILES[kind]), size)
    return _font_cache[key]


def syllables(s):
    return sum(1 for ch in s if not ch.isspace())


# ---------- 줄바꿈 ----------

def wrap_diamond(text, fnt, max_w=MAX_LINE_W):
    """어절 단위로 1~3줄로 나눈다. 넘치지 않고, 줄 길이가 고르고, 3줄이면 가운데가 가장 긴(마름모) 쪽을 고른다.
    대본의 \\n 은 그대로 줄을 나눈다."""
    if "\n" in text:
        return [ln.strip() for ln in text.split("\n") if ln.strip()]
    words = text.split()
    if not words:
        return [""]
    if fnt.getlength(text) <= max_w * 0.85 or len(words) == 1:
        return [text]
    best, best_cost = [text], float("inf")
    for n in (2, 3):
        for cuts in combinations(range(1, len(words)), n - 1):
            idx = (0, *cuts, len(words))
            lines = [" ".join(words[idx[k]:idx[k + 1]]) for k in range(n)]
            ws = [fnt.getlength(ln) for ln in lines]
            over = sum(max(0, w - max_w) for w in ws)
            mean = sum(ws) / n
            cost = over * 50 + sum((w - mean) ** 2 for w in ws) ** 0.5
            if n == 3 and ws[1] < max(ws[0], ws[2]):
                cost += 25  # 마름모꼴: 가운데 줄이 길게
            # 문장·쉼표가 끝나는 곳에서 줄을 바꾸면 덜 어색하다("...아닌데? / 일곱 시에 잤는데."), 아니면 벌점
            cost += sum(-45 if ln.rstrip()[-1:] in ".?!~" else (-20 if ln.rstrip()[-1:] == "," else 8) for ln in lines[:-1])
            if min(syllables(ln) for ln in lines) <= 1:
                cost += 60  # 한 글자만 남는 줄 금지
            cost += (n - 2) * 45  # 2줄이 기본(3줄은 정말 길 때만)
            if cost < best_cost:
                best, best_cost = lines, cost
    return best


def split_long(text, limit=15):
    """두 문장이 15자를 넘으면 문장(. ? !) 경계에서 둘로 나눠 이어진 풍선으로(한 풍선에 한 생각, 문장이 줄 가운데서 갈리지 않게)."""
    if syllables(text) <= limit:
        return [text]
    # 문장 끝(. ? !)에서만 나눈다. 쉼표에서 나누면 "태식 씨," 같은 토막 풍선이 생긴다. 양쪽 다 4자 이상일 때만
    seps = [i + 1 for i, ch in enumerate(text) if ch in ".?!" and i + 1 < len(text) and text[i + 1] == " "
            and syllables(text[:i + 1]) >= 4 and syllables(text[i + 1:]) >= 4]
    if not seps:
        return [text]
    mid = min(seps, key=lambda i: abs(syllables(text[:i]) - syllables(text) / 2))
    return [text[:mid].strip(), text[mid:].strip()]


# ---------- 모양 ----------

def superellipse(cx, cy, rx, ry, n=2.3, steps=96, jag=0.0, wobble=0.0):
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        x = math.copysign(abs(c) ** (2 / n), c)
        y = math.copysign(abs(s) ** (2 / n), s)
        r = 1 + (jag if i % 2 else -jag * 0.3) + wobble * math.sin(i * 1.7)
        pts.append((cx + rx * x * r, cy + ry * y * r))
    return pts


def bubble_size(lines, fnt, kind):
    asc, desc = fnt.getmetrics()
    lh = (asc + desc) * 1.3
    tw = max(fnt.getlength(ln) for ln in lines)
    th = lh * len(lines) - (asc + desc) * 0.3
    pad = fnt.size * (0.8 if kind != "shout" else 1.1)
    k = 1.22  # 글자 상자 모서리는 대개 비어 있어 초타원 꼭짓점(1.35)보다 조금 안쪽으로
    rx = (tw / 2 + pad) * k
    ry = (th / 2 + pad * 0.75) * k
    rx = max(rx, ry * 1.15, 58)
    ry = max(ry, rx / 2.1)
    if kind == "shout":
        rx, ry = rx * 1.08, ry * 1.12
    return rx, ry, lh


def tail_poly(cx, cy, rx, ry, target, base=30, max_len=70, frac=0.55):
    """풍선 아래쪽(아래 1/3)에서 target 쪽으로 휘어 가늘어지는 꼬리. target 이 위에 있으면 위쪽에서 나간다."""
    tx, ty = target
    ang = math.atan2(ty - cy, tx - cx)
    # 아래쪽으로 나가는 꼬리는 각도를 아래 반원 안으로 모은다(옆구리에서 나가면 어색하다)
    if ty > cy:
        ang = min(max(ang, math.radians(40)), math.radians(140))
    ex, ey = cx + rx * 0.92 * math.cos(ang), cy + ry * 0.92 * math.sin(ang)
    dist = math.hypot(tx - ex, ty - ey)
    length = min(max_len, max(26, dist * frac))
    dx, dy = (tx - ex) / (dist or 1), (ty - ey) / (dist or 1)
    tip = (ex + dx * length, ey + dy * length)
    nx, ny = -math.sin(ang), math.cos(ang)
    b1 = (ex + nx * base / 2 - math.cos(ang) * 6, ey + ny * base / 2 - math.sin(ang) * 6)
    b2 = (ex - nx * base / 2 - math.cos(ang) * 6, ey - ny * base / 2 - math.sin(ang) * 6)
    bend = 0.18 * length  # 살짝 휘게
    pts = []
    for side, b in ((1, b1), (-1, b2)):
        mx = (b[0] + tip[0]) / 2 + nx * bend * 0.5
        my = (b[1] + tip[1]) / 2 + ny * bend * 0.5
        curve = [((1 - t) ** 2 * b[0] + 2 * (1 - t) * t * mx + t * t * tip[0],
                  (1 - t) ** 2 * b[1] + 2 * (1 - t) * t * my + t * t * tip[1]) for t in [i / 10 for i in range(11)]]
        pts.append(curve if side == 1 else curve[::-1])
    return pts[0] + pts[1][1:]


def draw_shape(canvas, shapes, ellipses=(), stroke=STROKE, fill=PAPER, outline=INK, dashed=False, shadow=False):
    """shapes(다각형 점 목록들)과 ellipses((cx,cy,r))의 합집합을 3배로 그려 테두리·채움을 얹는다."""
    xs = [x for s in shapes for x, _ in s] + [e[0] - e[2] for e in ellipses] + [e[0] + e[2] for e in ellipses]
    ys = [y for s in shapes for _, y in s] + [e[1] - e[2] for e in ellipses] + [e[1] + e[2] for e in ellipses]
    m = stroke + 4
    x0, y0 = int(min(xs) - m), int(min(ys) - m)
    x1, y1 = int(max(xs) + m) + 1, int(max(ys) + m) + 1
    w, h = (x1 - x0) * SS, (y1 - y0) * SS
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    for s in shapes:
        d.polygon([((x - x0) * SS, (y - y0) * SS) for x, y in s], fill=255)
    for ex, ey, r in ellipses:
        d.ellipse([(ex - r - x0) * SS, (ey - r - y0) * SS, (ex + r - x0) * SS, (ey + r - y0) * SS], fill=255)
    k = int(stroke * SS) * 2 + 1
    ring = mask.filter(ImageFilter.MaxFilter(k if k % 2 else k + 1))
    if dashed:
        # 생각 풍선: 테두리를 점선처럼 끊는다(바깥 둘레를 일정 간격으로 지운다)
        gaps = Image.new("L", (w, h), 255)
        gd = ImageDraw.Draw(gaps)
        cx, cy = w / 2, h / 2
        for i in range(0, 360, 9):
            a = math.radians(i)
            gd.line([(cx, cy), (cx + math.cos(a) * w, cy + math.sin(a) * w)], fill=0, width=SS * 3)
        ring = Image.composite(ring, mask, gaps)
    small = (x1 - x0, y1 - y0)
    ring_s = ring.resize(small, Image.LANCZOS)
    mask_s = mask.resize(small, Image.LANCZOS)
    if shadow:
        # 테두리 없는 풍선: 아래로 살짝 번진 옅은 그림자로 그림과 떼어 놓는다
        sh = mask_s.filter(ImageFilter.GaussianBlur(5)).point(lambda v: int(v * 0.35))
        canvas.paste(Image.new("RGB", small, (40, 30, 50)), (x0 + 2, y0 + 5), sh)
    canvas.paste(Image.new("RGB", small, outline), (x0, y0), ring_s)
    canvas.paste(Image.new("RGB", small, fill), (x0, y0), mask_s)


def draw_text(canvas, lines, fnt, cx, cy, lh, fill=INK):
    """가운데 정렬. 어절마다 따로 써서(일부 글꼴의 공백 글리프 문제 회피) 폭만 띄운다."""
    d = ImageDraw.Draw(canvas)
    space = fnt.getlength(" ")
    y = cy - lh * (len(lines) - 1) / 2
    for ln in lines:
        x = cx - fnt.getlength(ln) / 2
        for word in ln.split(" "):
            if word:
                d.text((x, y), word, font=fnt, fill=fill, anchor="lm")
            x += fnt.getlength(word) + space
        y += lh


# ---------- 하나씩 그리기 ----------

class Bubble:
    """배치 전에 크기를 알아야 해서 측정과 그리기를 나눈다."""

    def __init__(self, item, text=None):
        self.item = item
        self.kind = item.get("type", "speech")
        self.text = text if text is not None else item["text"]
        self.font = font(self.kind if self.kind in SIZES else "speech")
        self.lines = wrap_diamond(self.text, self.font)
        self.rx, self.ry, self.lh = bubble_size(self.lines, self.font, self.kind)
        self.cx = self.cy = 0
        self.tail = None   # (x, y) 머리 위치. None 이면 꼬리 없음
        self.neck_to = None  # 같은 사람 다음 풍선과 잇는 목

    @property
    def w(self):
        return self.rx * 2

    @property
    def h(self):
        return self.ry * 2

    def draw(self, canvas):
        cx, cy, rx, ry = self.cx, self.cy, self.rx, self.ry
        if self.kind == "shout":
            body = superellipse(cx, cy, rx, ry, n=2.0, steps=34, jag=0.16)
        else:
            body = superellipse(cx, cy, rx, ry, n=2.3)
        shapes, circles = [body], []
        if self.neck_to:
            ox, oy = self.neck_to
            nw = min(rx, ry) * 0.5
            shapes.append([(cx - nw / 2, cy), (cx + nw / 2, cy), (ox + nw / 2, oy), (ox - nw / 2, oy)])
        if self.tail:
            if self.kind == "thought":
                tx, ty = self.tail
                for i, f in enumerate((0.0, 0.42, 0.75)):
                    ex = cx + (tx - cx) * 0.0 + (tx - cx) * (0.18 + f * 0.25)
                    ey = cy + ry * 0.95 + (ty - cy - ry) * (0.12 + f * 0.25)
                    circles.append((ex, ey, 9 - i * 2.6))
            else:
                shapes.append(tail_poly(cx, cy, rx, ry, self.tail))
        if BUBBLE_STYLE == "soft" and self.kind != "shout":
            draw_shape(canvas, shapes, circles, stroke=1.0, outline=(225, 222, 228), shadow=True)
        else:
            stroke = STROKE if BUBBLE_STYLE == "edge" else 2.6
            draw_shape(canvas, shapes, circles, stroke=stroke if self.kind != "thought" else 2.4,
                       dashed=self.kind == "thought")
        draw_text(canvas, self.lines, self.font, cx, cy, self.lh)


def draw_narration(canvas, item, x, y, max_w):
    """명조 사각 상자(어두운 바탕 + 흰 글자). (x, y) 는 상자 왼쪽 위."""
    fnt = font("narration")
    lines = [ln.strip() for ln in item["text"].replace(" / ", "\n").split("\n") if ln.strip()]
    lines = [w for ln in lines for w in (wrap_diamond(ln, fnt, max_w) if fnt.getlength(ln) > max_w else [ln])]
    asc, desc = fnt.getmetrics()
    lh = (asc + desc) * 1.35
    tw = max(fnt.getlength(ln) for ln in lines)
    bw, bh = tw + 44, lh * len(lines) + 22
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([x, y, x + bw, y + bh], radius=5, fill=(28, 26, 36))
    ty = y + 11 + lh / 2
    for ln in lines:
        d.text((x + 22, ty), ln, font=fnt, fill=(245, 242, 236), anchor="lm")
        ty += lh
    return bw, bh


def is_text_line(item):
    """풍선·상자 없이 칸 사이 배경에 글로만 쓰는 줄: 독백(monologue)과 자막(narration). "[2화에 계속]" 같은 꼬리표만 상자로.
    되돌아보는 말·시간 자막을 생각 풍선이나 검은 상자에 넣으면 시처럼·표지판처럼 읽혔다(2026-10-13 사용자 "저 내용은 보통 배경에 텍스트로 쓰지 않나")."""
    return item.get("type") in ("monologue", "narration") and not item.get("text", "").startswith("[") and not item.get("box")


def draw_monologue(canvas, item, W, y, max_w):
    """가운데 맞춤 명조 글(배경 위, 상자 없음). 돌려주는 것: 높이."""
    fnt = font(item["font"] if item.get("font") in FONT_FILES else "monologue")  # "font": "hand" 면 손글씨
    lines = [ln.strip() for ln in item["text"].replace(" / ", "\n").split("\n") if ln.strip()]
    lines = [w for ln in lines for w in (wrap_diamond(ln, fnt, max_w) if fnt.getlength(ln) > max_w else [ln])]
    asc, desc = fnt.getmetrics()
    lh = (asc + desc) * 1.55
    d = ImageDraw.Draw(canvas)
    ty = y + lh / 2
    for ln in lines:
        d.text((W / 2, ty), ln, font=fnt, fill=MONO_INK, anchor="mm")
        ty += lh
    return lh * len(lines)


CARD_FONT = ROOT / "fonts" / "NanumBarunGothicBold.ttf"
CARD_BG, CARD_INK, CARD_ACCENT = (10, 8, 12), (246, 244, 248), (232, 34, 40)


def draw_card(canvas, item, W, y):
    """글 칸: 컷 폭 전체의 검은 칸에 큰 글씨로 박는 독백(감정이 터지는 대목). text 의 | 는 줄 끊기, *...* 줄은 빨강.
    (2026-10-13 사용자가 보여 준 예: 검은 바탕에 흰 큰 글씨 + 강조 줄은 빨강) 돌려주는 것: 높이."""
    import random
    fnt = ImageFont.truetype(str(CARD_FONT), item.get("size", 46))
    lines = [t.strip() for t in item["text"].split("|") if t.strip()]
    asc, desc = fnt.getmetrics()
    lh = (asc + desc) * 1.35
    pad = 120
    h = int(pad * 2 + lh * len(lines) + 30 * (len(lines) - 1))
    card = Image.new("RGB", (W, h), CARD_BG)
    rnd = random.Random(len(item["text"]))
    blot = Image.new("L", (W // 8, h // 8), 0)
    bd = ImageDraw.Draw(blot)
    for _ in range(14):  # 옅은 붉은 얼룩(질감)
        cx, cy, r = rnd.uniform(0, W // 8), rnd.uniform(0, h // 8), rnd.uniform(6, 22)
        bd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rnd.randint(18, 40))
    blot = blot.resize((W, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(18))
    card = Image.composite(Image.new("RGB", (W, h), (70, 14, 18)), card, blot)
    d = ImageDraw.Draw(card)
    ty = pad + lh / 2
    for ln in lines:
        accent = ln.startswith("*") and ln.endswith("*")
        d.text((W / 2, ty), ln.strip("*"), font=fnt, fill=CARD_ACCENT if accent else CARD_INK, anchor="mm")
        ty += lh + 30
    canvas.paste(card, (0, int(y)))
    return h


def narration_size(item, max_w):
    fnt = font("narration")
    lines = [ln.strip() for ln in item["text"].replace(" / ", "\n").split("\n") if ln.strip()]
    lines = [w for ln in lines for w in (wrap_diamond(ln, fnt, max_w) if fnt.getlength(ln) > max_w else [ln])]
    asc, desc = fnt.getmetrics()
    return max(fnt.getlength(ln) for ln in lines) + 44, (asc + desc) * 1.35 * len(lines) + 22


SFX_ANCHORS = {"top-left": (0.22, 0.16), "top": (0.5, 0.14), "top-right": (0.78, 0.16), "mid-left": (0.2, 0.5), "center": (0.5, 0.5),
               "mid-right": (0.8, 0.5), "bottom-left": (0.22, 0.84), "bottom": (0.5, 0.86), "bottom-right": (0.78, 0.84)}


def draw_sfx(canvas, item, ox, oy, pw, ph):
    """효과음: 글자마다 조금씩 기울이고 들쭉날쭉하게, 검은 안쪽선 + 흰 바깥선. pos 는 컷 기준 비율.
    style "soft" = 신음·속삭임(분홍 "하아..." 등): 부드러운 글꼴, 흰 테두리만, 글자 폭대로 띄움
    (2026-10-13 굵은 효과음 글꼴로 쓰니 글자가 겹치고 점이 커서 깨져 보였다)."""
    text = item["text"]
    soft = item.get("style") == "soft"
    if soft:
        fnt = ImageFont.truetype(str(FONT_FILES["thought"]), round(SIZES["sfx"] * item.get("scale", 2.2) / 2.2))
    else:
        fnt = font("sfx", item.get("scale", 2.2) / 2.2)
    rnd = random.Random(text)
    fill = item.get("color", "#ffffff")
    glyphs, advs = [], []
    for ch in text:
        if ch == " ":
            glyphs.append(None)
            advs.append(fnt.size * (0.3 if soft else 0.37))
            continue
        size = int(fnt.size * 1.6)
        g = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        if soft:
            gd.text((size / 2, size / 2), ch, font=fnt, fill=fill, anchor="mm", stroke_width=4, stroke_fill="#ffffff")
            glyphs.append(g.rotate(rnd.uniform(-6, 6), resample=Image.BICUBIC))
            advs.append(fnt.getlength(ch) * 1.04 + 2)
        else:
            gd.text((size / 2, size / 2), ch, font=fnt, fill="#ffffff", anchor="mm", stroke_width=9, stroke_fill="#ffffff")
            gd.text((size / 2, size / 2), ch, font=fnt, fill=fill, anchor="mm", stroke_width=4, stroke_fill="#141414")
            glyphs.append(g.rotate(rnd.uniform(-12, 12), resample=Image.BICUBIC))
            advs.append(fnt.size * 0.92)
    total = sum(advs)
    pos = item.get("pos", [0.5, 0.5])
    if isinstance(pos, str):  # 말풍선처럼 이름으로도("bottom-right" 등)
        pos = SFX_ANCHORS.get(pos, (0.5, 0.5))
    cx, cy = ox + pos[0] * pw, oy + pos[1] * ph
    ang = math.radians(item.get("angle", 0))
    x = -total / 2
    for i, (g, adv) in enumerate(zip(glyphs, advs)):
        if g is not None:
            px = cx + (x + adv / 2) * math.cos(ang)
            py = cy + (x + adv / 2) * math.sin(ang) + rnd.uniform(-3, 3) * (1 if soft else 2) - i * (1 if soft else 2)
            canvas.paste(g, (int(px - g.width / 2), int(py - g.height / 2)), g)
        x += adv


# ---------- 배치 ----------

def side_of(item):
    pos = item.get("pos", "top")
    if isinstance(pos, (list, tuple)):
        x = pos[0]
    else:
        x = {"top-left": 0.2, "mid-left": 0.2, "bottom-left": 0.2, "top-right": 0.8, "mid-right": 0.8,
             "bottom-right": 0.8}.get(pos, 0.5)
    return "L" if x < 0.42 else ("R" if x > 0.58 else "C")


def speaker_heads(panel, W, H):
    """말하는 사람 머리 위치 추정(꼬리 끝). 좌·우·가운데 → (x, y). 인물이 없으면 빈 dict."""
    hy = HEAD_Y.get(panel.get("shot", "medium"), 0.25) * H
    if panel.get("layout"):
        xs = sorted((r.get("x", 0) + r.get("w", 0.5) / 2) * W for r in panel["layout"])
    elif panel.get("characters"):
        n = len(panel["characters"])
        xs = [W * (i + 1) / (n + 1) for i in range(n)]
    else:
        return {}
    return {"L": (xs[0], hy), "R": (xs[-1], hy), "C": (xs[len(xs) // 2], hy)}


def detail_map(img, cell=20):
    """그림의 복잡한 곳(얼굴·손·소품) 지도. 풍선은 덜 복잡한 곳(벽·하늘·어두운 구석)으로 민다."""
    g = img.convert("L").resize((max(1, img.width // cell), max(1, img.height // cell)), Image.BILINEAR)
    e = g.filter(ImageFilter.FIND_EDGES)
    return e, cell


def detail_cost(dmap, x0, y0, x1, y1):
    e, cell = dmap
    w, h = e.size
    xs, ys = max(0, int(x0 // cell)), max(0, int(y0 // cell))
    xe, ye = min(w, int(x1 // cell) + 1), min(h, int(y1 // cell) + 1)
    if xe <= xs or ye <= ys:
        return 0.0
    region = e.crop((xs, ys, xe, ye))
    return sum(region.getdata()) / (255 * max(1, (xe - xs) * (ye - ys))) * (xe - xs) * (ye - ys)


_cascade = None


def face_boxes(img, W, H):
    """그림 속 애니 얼굴 상자 [(x0,y0,x1,y1)] (W·H 좌표, 머리 윗부분·턱까지 넉넉히). 애니 얼굴 YOLO(face_detect)로 찾고,
    없으면 lbpcascade_animeface. lbpcascade 는 이 그림체 남자 얼굴을 거의 못 찾아 말풍선이 얼굴을 가렸다(2026-10-13 "처음 뵙겠습니다")."""
    try:
        from face_detect import detect
        sx, sy = W / img.width, H / img.height
        # 컷 밖(위 여백)으로 넘친 부분은 자른다. 위 여백으로 올린 풍선이 "얼굴과 겹친다"로 잡히지 않게
        return [(max(0.0, (x0 - (x1 - x0) * 0.12) * sx), max(0.0, (y0 - (y1 - y0) * 0.3) * sy),
                 min(W, (x1 + (x1 - x0) * 0.12) * sx), min(H, (y1 + (y1 - y0) * 0.1) * sy))
                for x0, y0, x1, y1, _ in detect(img)]
    except Exception:
        pass
    global _cascade
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    xml = ROOT / "models" / "detect" / "lbpcascade_animeface.xml"
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(str(xml)) if xml.exists() else False
    if not _cascade:
        return []
    g = cv2.equalizeHist(cv2.cvtColor(np.asarray(img.convert("RGB").resize((W, H))), cv2.COLOR_RGB2GRAY))
    found = _cascade.detectMultiScale(g, scaleFactor=1.08, minNeighbors=3, minSize=(40, 40))
    return [(x - w * 0.1, y - h * 0.15, x + w * 1.1, y + h * 1.05) for x, y, w, h in found]


def overlap(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def neck_hits(a, b, boxes):
    """같은 사람 두 풍선(a→b)을 잇는 목이 두 풍선 밖에서 얼굴 상자를 지나가면 True.
    (2026-10-13 D+18 힉스필드 판: 풍선 상자는 얼굴을 피했는데 둘을 잇는 흰 띠가 이마·눈을 가로질렀다)"""
    for k in range(1, 40):
        t = k / 40
        x, y = a.cx + (b.cx - a.cx) * t, a.cy + (b.cy - a.cy) * t
        if any(((x - q.cx) / q.rx) ** 2 + ((y - q.cy) / q.ry) ** 2 <= 1 for q in (a, b)):
            continue
        if any(f[0] <= x <= f[2] and f[1] <= y <= f[3] for f in boxes):
            return True
    return False


def layout_panel(panel, items, W, H, img):
    """컷 위에 얹을 풍선들을 배치한다. 좌표는 컷 왼쪽 위 기준(음수 y 는 위 여백으로 나간 부분).
    돌려주는 것: (풍선 목록, 내레이션 [(item, x, y)], 위로 튀어나간 높이)."""
    heads = speaker_heads(panel, W, H)
    dmap = detail_map(img)
    # 컷 안쪽에 넣는 결(inside·soft)은 얼굴을 피한다: 찾은 얼굴 + 머리 위치 추정
    avoid = []
    if BUBBLE_STYLE != "edge":
        # 찾은 얼굴이 있으면 그것만, 없을 때만 머리 위치 추정(추정 상자가 빈 벽을 막고 실제 얼굴은 놓쳤다)
        faces = face_boxes(img, W, H) if panel.get("characters") or panel.get("layout") else []
        avoid = faces or [(hx - W * 0.1, hy - H * 0.17, hx + W * 0.1, hy + H * 0.12) for hx, hy in heads.values()]
        if faces:  # 꼬리도 추정 자리 대신 실제 얼굴(가장 가까운 얼굴의 이마 쪽)을 가리키게
            heads = {k: min((((f[0] + f[2]) / 2, f[1] + (f[3] - f[1]) * 0.35) for f in faces), key=lambda c: abs(c[0] - hx))
                     for k, (hx, hy) in heads.items()}
    bubbles, narr = [], []
    y_cursor = None
    margin = 22
    for item in items:
        if item.get("type") == "narration":
            nw, nh = narration_size(item, W * 0.6)
            narr.append((item, margin, margin if y_cursor is None else y_cursor))
            y_cursor = (margin if y_cursor is None else y_cursor) + nh + 14
            continue
        parts = split_long(item["text"]) if item.get("type", "speech") != "shout" else [item["text"]]
        side = side_of(item)
        for k, part in enumerate(parts):
            b = Bubble(item, part)
            b.side = side
            b.first = k == 0
            bubbles.append(b)
    # 지그재그: 같은 쪽이 연달아 오면 같은 사람으로 보고 목으로 잇는다
    prev = None
    for b in bubbles:
        same = prev is not None and prev.side == b.side
        if prev is None:
            if BUBBLE_STYLE == "edge":
                top = -b.h * 0.36 if y_cursor is None else y_cursor  # 첫 풍선은 위 가장자리에 1/3 걸친다
            else:
                top = margin if y_cursor is None else y_cursor  # 컷 안쪽 위에서 시작
        elif same:
            # 같은 사람 이어 말하기: 앞 풍선 글자를 가리지 않게 거의 붙여서 아래로(2026-10-12 p048·p057·p109 두 풍선이 겹쳐 글자가 가려졌다)
            top = prev.cy + prev.ry * 0.92
        else:
            top = prev.cy + prev.ry * 0.62
        b.cy = top + b.ry
        base_x = SIDE_X[b.side] * W
        if same:
            base_x = prev.cx + (40 if b.side != "R" else -40)
        lo, hi = margin + b.rx, W - margin - b.rx
        if avoid and not same:
            # 컷 안쪽: 위쪽 띠에서 얼굴과 안 겹치는 자리를 찾고, 없으면 이 풍선만 위 가장자리에 걸친다
            area = b.w * b.h
            best = None
            # 말하는 사람 쪽 절반 안에서만 고른다. 어깨 너머 컷에서 단색인 앞사람 뒤통수 위가 "덜 복잡한 곳"으로 뽑혀
            # 듣는 사람이 말하는 것처럼 읽혔다(2026-10-14 D+18 2화 카페·호프집)
            slo, shi = (lo, min(hi, W * 0.6)) if b.side == "L" else ((max(lo, W * 0.4), hi) if b.side == "R" else (lo, hi))
            if slo > shi:
                slo, shi = lo, hi
            # 한쪽 사람 풍선은 위쪽에서만 찾고, 없으면 컷 위 여백으로(가슴께로 내려가면 꼬리 없이 몸에 붙은 글처럼 보였다)
            dys = (0, H * 0.07, H * 0.14, H * 0.22) if b.side in "LR" else (0, H * 0.07, H * 0.14, H * 0.22, H * 0.32, H * 0.45, H * 0.58)
            for dy in dys:
                cy = b.cy + dy
                if cy + b.ry > H - margin:
                    continue
                for i in range(9):
                    x = slo + (shi - slo) * i / 8
                    box = (x - b.rx, cy - b.ry, x + b.rx, cy + b.ry)
                    face = sum(overlap(box, a) for a in avoid) / area
                    cost = face * 400 + detail_cost(dmap, box[0], max(0, box[1]), box[2], max(0, box[3])) + abs(x - base_x) * 0.03 + dy * 0.05
                    if best is None or cost < best[0]:
                        best = (cost, x, cy, face)
            if best and best[3] < 0.012:
                b.cx, b.cy = best[1], best[2]
            else:
                # 얼굴을 안 가리는 자리가 컷 안에 없으면 컷 위 여백으로 완전히 올린다(위에 1/3 걸치면 위쪽 얼굴을 가렸다)
                b.cx = min(max(base_x, lo), hi)
                b.cy = -b.ry - 6
                b.gutter = True
        else:
            cands = [min(max(base_x + dx, lo), hi) for dx in (-60, -30, 0, 30, 60)]
            b.cx = min(cands, key=lambda x: detail_cost(dmap, x - b.rx, max(0, b.cy - b.ry), x + b.rx, max(0, b.cy + b.ry))
                       + abs(x - base_x) * 0.02)
        if avoid and same:
            # 같은 사람 이어 말하기도 얼굴을 가리면 앞 풍선 옆으로 나란히 둔다(D+18 p004 우빈 얼굴이 가려졌다)
            def face_of(x, y):
                box = (x - b.rx, y - b.ry, x + b.rx, y + b.ry)
                return sum(overlap(box, a) for a in avoid) / (b.w * b.h)
            if face_of(b.cx, b.cy) > 0.06:
                opts = [(b.cx, b.cy)]
                # 이어 말하기도 말하는 사람 쪽 절반 안에서(p091 둘째 풍선이 맞은편 사람 머리 위로 갔다)
                slo, shi = (lo, min(hi, W * 0.6)) if b.side == "L" else ((max(lo, W * 0.4), hi) if b.side == "R" else (lo, hi))
                if slo > shi:
                    slo, shi = lo, hi
                for i in range(9):
                    x = slo + (shi - slo) * i / 8
                    # 같은 높이에 나란히 둘 땐 앞 풍선 오른쪽에만(왼쪽에 두면 거꾸로 읽힌다, 2026-10-13 무대 뒤 p055)
                    if x >= prev.cx + (prev.rx + b.rx) * 0.85:
                        opts.append((x, prev.cy))
                    opts.append((x, prev.cy + prev.ry + b.ry + 6))
                done = bubbles[:bubbles.index(b)]

                def hits(o):
                    box = (o[0] - b.rx, o[1] - b.ry, o[0] + b.rx, o[1] + b.ry)
                    return sum(overlap(box, (q.cx - q.rx, q.cy - q.ry, q.cx + q.rx, q.cy + q.ry)) for q in done) / (b.w * b.h)
                # 얼굴·앞 풍선을 안 가리는 자리 중 앞 풍선에 가까운 곳(없으면 덜 가리는 곳)
                best = min(opts, key=lambda o: (face_of(*o) > 0.06 or hits(o) > 0.02, face_of(*o) + hits(o),
                                                abs(o[0] - prev.cx) + abs(o[1] - prev.cy)))
                if face_of(*best) > 0.06 or hits(best) > 0.02:
                    # 자리가 없으면 앞 풍선을 위 여백으로 더 올리고 그 자리에 이어 둔다(얼굴 위 머리까지만 걸친다)
                    best = (prev.cx + (28 if b.side != "R" else -28), prev.cy)
                    prev.cy -= (prev.ry + b.ry) * 1.04
                b.cx, b.cy = best
        # 앞 풍선과 겹치면(다른 사람) 안 겹칠 때까지 아래로 내린다(p090 “설마.”와 “역시 대표님.”이 포개졌다).
        # 위 여백으로 올린 풍선은 위로 쌓는다(아래로 내리면 다시 얼굴을 가린다)
        if not same:
            for q in bubbles[:bubbles.index(b)]:
                if abs(q.cx - b.cx) < (q.rx + b.rx) * 1.02 and abs(q.cy - b.cy) < (q.ry + b.ry) * 1.02:
                    if getattr(b, "gutter", False):
                        b.cy = q.cy - (q.ry + b.ry) * 1.04
                    else:
                        b.cy = q.cy + (q.ry + b.ry) * 1.04
        if same:
            prev.neck_to = (b.cx, b.cy)
        prev = b
    # 마지막 확인: 그래도 얼굴을 가리는 풍선은 위 여백으로(앞 풍선들보다 위에 쌓는다). 같은 사람 잇기 목도 다시 잇는다
    if avoid:
        # 위 여백으로 올라가는 풍선이 있으면 그 앞 풍선들도 같이 올려 읽는 순서대로 위에서부터 쌓는다
        # (뒤 풍선만 올리면 첫 풍선보다 위에 놓여 거꾸로 읽혔다, 2026-10-13 D+18 p006·무대 뒤 p039 등)
        def hits_face(q):
            box = (q.cx - q.rx, q.cy - q.ry, q.cx + q.rx, q.cy + q.ry)
            return sum(overlap(box, a) for a in avoid) / (q.w * q.h) > 0.012
        need = [i for i, q in enumerate(bubbles) if getattr(q, "gutter", False) or (q.cy - q.ry < 0 and q.cy < q.ry) or hits_face(q)]
        if need:
            group = bubbles[:max(need) + 1]
            y = -6.0
            for q in reversed(group):
                q.cy = y - q.ry
                y = q.cy - q.ry - 8
                q.gutter = True
        for i in range(1, len(bubbles)):
            if bubbles[i].side == bubbles[i - 1].side and bubbles[i - 1].neck_to:
                bubbles[i - 1].neck_to = (bubbles[i].cx, bubbles[i].cy)
    # 목이 얼굴을 지나가면: 뒤 풍선을 앞 풍선 바로 아래(같은 쪽)로 붙여 보고, 거기도 얼굴·다른 풍선에 걸리면 목을 빼고 따로 꼬리를 단다
    unchained = set()
    for i in range(1, len(bubbles)):
        a, b = bubbles[i - 1], bubbles[i]
        if not a.neck_to or not neck_hits(a, b, avoid):
            continue
        cx, cy = a.cx, a.cy + (a.ry + b.ry) * 1.04
        box = (cx - b.rx, cy - b.ry, cx + b.rx, cy + b.ry)
        free = box[3] <= H - 4 and not any(overlap(box, f) > 0 for f in avoid) and not any(
            overlap(box, (q.cx - q.rx, q.cy - q.ry, q.cx + q.rx, q.cy + q.ry)) > 0 for q in bubbles if q is not a and q is not b)
        if free:
            b.cx, b.cy = cx, cy
            a.neck_to = (b.cx, b.cy)
            if b.neck_to and i + 1 < len(bubbles):
                b.neck_to = (bubbles[i + 1].cx, bubbles[i + 1].cy)
        else:
            a.neck_to = None
            unchained.add(i)
    # 꼬리: 같은 사람 연속이면 첫 풍선만, 화면 밖 사람(머리 추정 없음)·tail: null 은 없음
    for b in bubbles:
        explicit = b.item.get("tail", "auto")
        if explicit is None:
            continue
        if isinstance(explicit, (list, tuple)):
            b.tail = (explicit[0] * W, explicit[1] * H)
        elif b.first and b.side in heads:
            hx, hy = heads[b.side]
            if hy > b.cy + b.ry:  # 머리가 풍선보다 아래에 있을 때만
                b.tail = (hx, hy)
    chained_tails = set()
    for i in range(1, len(bubbles)):
        if bubbles[i].side == bubbles[i - 1].side and i not in unchained:
            chained_tails.add(i)
    for i in chained_tails:
        bubbles[i].tail = None
    for i in unchained:  # 목을 뺀 풍선은 제 꼬리로 말하는 사람을 가리킨다(얼굴을 지나지 않을 때만)
        b = bubbles[i]
        if b.item.get("tail", "auto") == "auto" and b.side in heads:
            hx, hy = heads[b.side]
            tip = (hx + (b.cx - hx) * 0.45, hy + (b.cy - hy) * 0.45)
            b.tail = tip if not any(f[0] <= tip[0] <= f[2] and f[1] <= tip[1] <= f[3] for f in avoid) else None
    overflow = max([0.0] + [-(b.cy - b.ry) for b in bubbles])
    return bubbles, narr, overflow


def gutter_block(items, W, bg, panel=None, below=False):
    """컷 밖 여백(위·아래)에 띄우는 대사들. 내레이션은 왼쪽에, 말풍선은 지그재그로. 이미지를 돌려준다."""
    if not items:
        return None
    probe_h = 1400
    canvas = Image.new("RGB", (W, probe_h), bg)
    y = 26
    side_flip = "L"
    for item in items:
        if item.get("style") == "card":
            y += 10
            y += draw_card(canvas, item, W, y) + 10
            continue
        if is_text_line(item):
            y += 14
            y += draw_monologue(canvas, item, W, y, W * 0.8) + 18
            continue
        if item.get("type") == "narration":
            bw, bh = draw_narration(canvas, item, 26, y, W * 0.62)
            y += bh + 16
            continue
        for part in split_long(item["text"]):
            b = Bubble(item, part)
            side = side_of(item) if side_of(item) != "C" else side_flip
            side_flip = "R" if side == "L" else "L"
            b.cx = min(max(SIDE_X[side] * W, 22 + b.rx), W - 22 - b.rx)
            b.cy = y + b.ry
            if item.get("tail", "auto") is not None:
                b.tail = (b.cx + (W / 2 - b.cx) * 0.2, b.cy + b.ry + 60) if not below else (b.cx, b.cy - b.ry - 60)
            b.draw(canvas)
            y += b.h * 0.8
    return canvas.crop((0, 0, W, int(min(probe_h, y + 26))))
