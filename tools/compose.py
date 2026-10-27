"""패널 + 대사 → 세로 스크롤 웹툰 한 화.

    python tools/compose.py <episode_dir> [--no-user-slots]

--no-user-slots 는 직접 작성 칸(author: user)을 그림이 있어도 자리표시로 둔다(에이전트 점검용).
storyboard.json 순서대로 panels/<id>.png 를 폭 800px 로 맞추고, 대사(말풍선·내레이션·효과음)를 얹어
out/<ep>_full.png(미리보기 한 장)와 out/slices/<ep>_NNN.jpg(업로드용 조각)를 만든다.
아직 안 그린 컷은 회색 자리표시로 채워서 중간 점검에도 쓸 수 있다.

대사 항목: {"type": "speech|thought|shout|narration|sfx", "text": "...", "pos": "top-left" | [x, y], "tail": [x, y]}
  pos/tail 은 패널 기준 0~1 비율. pos 에 "above"/"below" 를 주면 컷 밖 여백(흰 바탕)에 띄운다.
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_json, load_style  # noqa: E402
import lettering  # noqa: E402

ANCHORS = {
    "top-left": (0.27, 0.11), "top": (0.5, 0.09), "top-right": (0.73, 0.11),
    "mid-left": (0.25, 0.5), "center": (0.5, 0.5), "mid-right": (0.75, 0.5),
    "bottom-left": (0.27, 0.89), "bottom": (0.5, 0.91), "bottom-right": (0.73, 0.89),
}
MARGIN = 16
NARRATION_FILL = (36, 36, 58)


class Fonts:
    def __init__(self, cfg, base):
        self.cfg, self.base, self.cache = cfg, base, {}

    def get(self, kind, scale=1.0):
        size = int(self.base * scale)
        key = (kind, size)
        if key not in self.cache:
            font = None
            for path in self.cfg.get(kind, []):
                if Path(path).exists():
                    font = ImageFont.truetype(path, size)
                    break
            self.cache[key] = font or ImageFont.load_default(size)
        return self.cache[key]


def wrap(text, font, max_w):
    """띄어쓰기 기준으로 줄을 나누고, 한 어절이 너무 길면 글자 단위로 끊는다. 대본의 \\n 은 그대로 줄바꿈."""
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}".strip()
            if font.getlength(trial) <= max_w:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            cur = ""
            for ch in word:
                if font.getlength(cur + ch) > max_w and cur:
                    lines.append(cur)
                    cur = ""
                cur += ch
        lines.append(cur)
    return lines


def text_block(lines, font):
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * 1.3)
    w = max((font.getlength(line) for line in lines), default=0)
    return int(w), lh * len(lines), lh


def draw_lines(draw, lines, font, cx, cy, fill, stroke=0, stroke_fill=None):
    """가운데 정렬로 여러 줄을 쓴다. 띄어쓰기는 그리지 않고 폭만 띄운다
    (설치된 NanumSquareAc 는 공백이 .notdef 네모로 찍힌다)."""
    w, h, lh = text_block(lines, font)
    space = font.getlength(" ")
    y = cy - h / 2
    for line in lines:
        words = line.split(" ")
        x = cx - font.getlength(line) / 2
        for word in words:
            if word:
                draw.text((x, y + lh / 2), word, font=font, fill=fill, anchor="lm", stroke_width=stroke, stroke_fill=stroke_fill)
            x += font.getlength(word) + space
        y += lh


def ellipse_pts(cx, cy, rx, ry, n=48, jag=0.0):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1 + (jag if i % 2 else 0)
        pts.append((cx + rx * r * math.cos(a), cy + ry * r * math.sin(a)))
    return pts


def bubble_mask(size, kind, cx, cy, rx, ry, tail):
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    if kind == "shout":
        d.polygon(ellipse_pts(cx, cy, rx * 0.95, ry * 0.95, n=36, jag=0.28), fill=255)
    elif kind == "thought":
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
        for x, y in ellipse_pts(cx, cy, rx * 0.92, ry * 0.88, n=14):
            r = min(rx, ry) * 0.32
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    else:
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    if tail:
        tx, ty = tail
        ang = math.atan2(ty - cy, tx - cx)
        if kind == "thought":
            for i, f in enumerate((0.45, 0.72, 0.92)):
                px, py = cx + (tx - cx) * f, cy + (ty - cy) * f
                r = max(4, min(rx, ry) * (0.16 - i * 0.04))
                d.ellipse([px - r, py - r, px + r, py + r], fill=255)
        else:
            # 꼬리 밑변은 말풍선 가장자리 안쪽에서 꼬리 방향에 수직으로, 폭은 짧은 반지름의 절반
            edge = rx * ry / math.hypot(ry * math.cos(ang), rx * math.sin(ang))
            bx, by = cx + math.cos(ang) * edge * 0.75, cy + math.sin(ang) * edge * 0.75
            half_w = min(rx, ry) * (0.28 if kind == "speech" else 0.22)
            px, py = -math.sin(ang) * half_w, math.cos(ang) * half_w
            d.polygon([(bx - px, by - py), (tx, ty), (bx + px, by + py)], fill=255)
    return mask


def paste_shape(img, mask, stroke, fill, outline):
    if stroke:
        ring = mask.filter(ImageFilter.MaxFilter(2 * stroke + 1))
        img.paste(Image.new("RGB", img.size, outline), mask=ring)
    img.paste(Image.new("RGB", img.size, fill), mask=mask)


def place(anchor, bw, bh, W, H):
    cx = min(max(anchor[0] * W, bw / 2 + MARGIN), W - bw / 2 - MARGIN)
    cy = min(max(anchor[1] * H, bh / 2 + MARGIN), H - bh / 2 - MARGIN)
    return cx, cy


def draw_item(img, item, fonts):
    """img(RGB) 위에 대사 하나를 그린다. pos 는 ANCHORS 이름이나 [x, y] 비율."""
    W, H = img.size
    kind = item.get("type", "speech")
    pos = item.get("pos", "top")
    anchor = ANCHORS.get(pos, (0.5, 0.5)) if isinstance(pos, str) else tuple(pos)
    draw = ImageDraw.Draw(img)
    if kind == "sfx":
        font = fonts.get("sfx", item.get("scale", 2.2))
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        lines = wrap(item["text"], font, W * 0.9)
        cx, cy = anchor[0] * W, anchor[1] * H
        draw_lines(ImageDraw.Draw(layer), lines, font, cx, cy, fill=item.get("color", "#ffffff"), stroke=5, stroke_fill="#111111")
        layer = layer.rotate(item.get("angle", 8), center=(cx, cy), resample=Image.BICUBIC)
        img.paste(layer, mask=layer)
        return
    if kind == "narration":
        font = fonts.get("narration")
        lines = wrap(item["text"], font, W * 0.78)
        tw, th, _ = text_block(lines, font)
        bw, bh = tw + 48, th + 32
        cx, cy = place(anchor, bw, bh, W, H)
        draw.rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], fill=NARRATION_FILL)
        draw_lines(draw, lines, font, cx, cy, fill="#ffffff")
        return
    font = fonts.get("shout" if kind == "shout" else ("thought" if kind == "thought" else "speech"), 1.15 if kind == "shout" else 1.0)
    lines = wrap(item["text"], font, W * 0.4)
    tw, th, _ = text_block(lines, font)
    grow = 1.75 if kind == "shout" else 1.45
    rx, ry = tw * grow / 2 + 18, th * grow / 2 + 14
    cx, cy = place(anchor, rx * 2, ry * 2, W, H)
    if "tail" in item:
        tail = None if item["tail"] is None else (item["tail"][0] * W, item["tail"][1] * H)
    else:
        down = cy < H * 0.5
        tail = (cx + (W / 2 - cx) * 0.15, cy + ry + 34 if down else cy - ry - 34)
    mask = bubble_mask((W, H), kind, cx, cy, rx, ry, tail)
    paste_shape(img, mask, 2 if kind == "thought" else 3, "#ffffff", "#111111")
    draw_lines(ImageDraw.Draw(img), lines, font, cx, cy, fill="#111111")


def float_strip(items, W, fonts, bg, below=False):
    """컷 밖 여백에 띄우는 대사들을 세로로 쌓은 흰 띠. 꼬리는 컷 쪽(위 띠는 아래, 아래 띠는 위)을 향한다."""
    strips = []
    for it in items:
        probe = Image.new("RGB", (W, 600), bg)
        draw_item(probe, {**it, "pos": [it.get("x", 0.5), 0.6 if below else 0.4]}, fonts)
        bbox = Image.eval(probe.convert("L"), lambda v: 255 if v < 250 else 0).getbbox()
        if bbox:
            strips.append(probe.crop((0, max(0, bbox[1] - 12), W, min(600, bbox[3] + 12))))
    if not strips:
        return None
    out = Image.new("RGB", (W, sum(s.height for s in strips)), bg)
    y = 0
    for s in strips:
        out.paste(s, (0, y))
        y += s.height
    return out


def harmonizer(paths, strength):
    """회차 전체 컷의 RGB 평균·표준편차를 구해, 컷마다 그 평균 쪽으로 strength 만큼 당기는 함수를 돌려준다.
    컷마다 조명·색감이 튀던 것(네온·핑크 캐스트)을 회차 톤으로 모은다."""
    try:
        import numpy as np
    except ImportError:
        return lambda im: im
    stats = []
    for p in paths:
        a = np.asarray(Image.open(p).convert("RGB").resize((128, 128)), dtype=np.float32)
        stats.append((a.reshape(-1, 3).mean(0), a.reshape(-1, 3).std(0) + 1e-3))
    if not stats:
        return lambda im: im
    ref_mean = np.mean([m for m, _ in stats], axis=0)
    ref_std = np.mean([s for _, s in stats], axis=0)

    def apply(im):
        a = np.asarray(im.convert("RGB"), dtype=np.float32)
        m, s = a.reshape(-1, 3).mean(0), a.reshape(-1, 3).std(0) + 1e-3
        moved = (a - m) / s * ref_std + ref_mean
        out = a * (1 - strength) + moved * strength
        return Image.fromarray(np.clip(out, 0, 255).astype("uint8"))
    return apply


def placeholder(W, panel, style):
    size = style["sizes"][panel.get("aspect", "portrait")]
    H = int(W * size[1] / size[0])
    img = Image.new("RGB", (W, H), (200, 200, 205))
    d = ImageDraw.Draw(img)
    label = "user slot" if panel.get("author") == "user" else "not rendered"
    d.text((W / 2, H / 2), f"{panel['id']} ({label})", fill=(90, 90, 100), anchor="mm", font=ImageFont.load_default(28))
    return img


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_user_slots = "--no-user-slots" in sys.argv
    only = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--only=")), None)
    if len(args) != 1:
        sys.exit(__doc__)
    ep = Path(args[0])
    board = load_json(ep / "storyboard.json")
    style = load_style()
    lt = style["lettering"]
    if "BUBBLE_STYLE" not in __import__("os").environ:  # 환경변수가 있으면 그게 먼저(시안 비교용)
        lettering.set_style(lt.get("bubble_style", "edge"))
    W, bg = board.get("width", lt["width"]), lt["background"]
    fonts = Fonts(lt["fonts"], lt["font_size"])
    missing = []
    panels = board["panels"]
    if only:  # 미리보기: 고른 컷만 묶어 out/preview.png 로(표지·조각 없이)
        panels = [p for p in panels if p["id"] in only]
        board = {k: v for k, v in board.items() if k not in ("cover", "title_card")}
    rendered = [ep / "panels" / f"{p['id']}.png" for p in panels if (ep / "panels" / f"{p['id']}.png").exists()]
    harmonize = harmonizer(rendered, board.get("color_harmonize", lt.get("color_harmonize", 0.0)))
    # 이미지는 세로로 쌓고(placed), 말풍선은 다 쌓은 뒤 전체 캔버스에 그린다(컷 위 여백으로 걸쳐 나가야 해서)
    placed, ops, y = [], [], 0
    if board.get("cover"):
        cover = Image.open((ep / board["cover"]).resolve()).convert("RGB")
        cover = cover.resize((W, round(cover.height * W / cover.width)), Image.LANCZOS)
        placed.append((cover, y))
        y += cover.height
    # 첫 컷이 표지 카드(phone_ui cover: 제목·화·작가가 들어 있음)면 흰 제목 카드는 겹쳐서 빼다(2026-10-18 추몽)
    first_cover = bool(panels) and (panels[0].get("composite") or {}).get("kind") == "cover"
    if board.get("title_card") and not first_cover:
        card = Image.new("RGB", (W, 360 if board.get("credit") else 300), bg)
        draw_lines(ImageDraw.Draw(card), [board["title_card"]], lettering.font("narration", 1.5), W / 2, 150, fill="#1a1024")
        if board.get("credit"):  # 작가 표기(글·그림), 2026-10-14
            draw_lines(ImageDraw.Draw(card), [board["credit"]], lettering.font("narration", 0.85), W / 2, 238, fill="#6b6273")
        placed.append((card, y))
        y += card.height
    gap_scale = lt.get("gap_after_scale", 1.0)
    pending_gap = lt["scene_gap"]
    for i, p in enumerate(panels):
        src = ep / "panels" / f"{p['id']}.png"
        if src.exists() and not (no_user_slots and p.get("author") == "user"):
            im = Image.open(src).convert("RGB")
            # 그래픽 컷(휴대폰 화면·달력·모니터)은 색감 맞추기에서 뺀다(흰 바탕이 회색으로 죽었다)
            if (p.get("composite") or {}).get("tool") not in ("phone_ui", "ecg_graphic"):
                im = harmonize(im)
            if im.height > im.width:
                # 세로 컷(전신 등장 등)은 폭을 줄여 가운데에. 꽉 채우면 한 컷이 화면을 다 먹는다(2026-10-07 "한 장면이 너무 크다")
                pw = lt.get("portrait_display_width", W)
                small = im.resize((pw, round(im.height * pw / im.width)), Image.LANCZOS)
                im = Image.new("RGB", (W, small.height), bg)
                im.paste(small, ((W - pw) // 2, 0))
            else:
                im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
        else:
            if p.get("author") != "user":
                missing.append(p["id"])
            im = placeholder(W, p, style)
        dlg = p.get("dialogue", [])
        # 배경 글(독백·자막)은 컷 안이 아니라 칸 사이에(pos below 만 아래, 나머지는 위)
        above = [d for d in dlg if d.get("pos") == "above" or (lettering.is_text_line(d) and d.get("pos") != "below")]
        below = [d for d in dlg if d.get("pos") == "below"]
        sfx = [d for d in dlg if d.get("type") == "sfx" and d.get("pos") not in ("above", "below")]
        inner = [d for d in dlg if d not in above and d not in below and d not in sfx]
        bubbles, narr, overflow = lettering.layout_panel(p, inner, W, im.height, im)
        top = lettering.gutter_block(above, W, bg)
        if top is not None:
            y += max(40, pending_gap // 3)
            placed.append((top, y))
            y += top.height
            pending_gap = 30
        y += max(pending_gap, int(overflow) + 30)
        panel_y = y
        placed.append((im, panel_y))
        y += im.height

        def op(canvas, bubbles=bubbles, narr=narr, sfx=sfx, panel_y=panel_y, H=im.height):
            for item, nx, ny in narr:
                lettering.draw_narration(canvas, item, nx, panel_y + ny, W * 0.6)
            for b in bubbles:
                b.cy += panel_y
                if b.tail:
                    b.tail = (b.tail[0], b.tail[1] + panel_y)
                if b.neck_to:
                    b.neck_to = (b.neck_to[0], b.neck_to[1] + panel_y)
            for b in bubbles:
                b.draw(canvas)
            for item in sfx:
                lettering.draw_sfx(canvas, item, 0, panel_y, W, H)
        ops.append(op)
        bot = lettering.gutter_block(below, W, bg, below=True)
        if bot is not None:
            y += 30
            placed.append((bot, y))
            y += bot.height
        if i < len(panels) - 1:
            nxt = panels[i + 1]
            # 장면 전환일 때만 넓게. 통화처럼 두 장소를 오가는 컷(최근 3컷에 있던 장소)은 같은 흐름으로 본다
            recent = {q.get("scene") for q in panels[max(0, i - 3):i + 1]}
            default = lt["scene_gap"] if nxt.get("scene") not in recent else lt["gap"]
            pending_gap = int(p["gap_after"] * gap_scale) if "gap_after" in p else default
    full = Image.new("RGB", (W, y), bg)
    for im, py in placed:
        full.paste(im, (0, py))
    for op in ops:
        op(full)
    out = ep / "out"
    if only:
        out.mkdir(exist_ok=True)
        full.save(out / "preview.png")
        print(f"preview {len(panels)}컷 {W}x{full.height} -> {out / 'preview.png'}")
        return
    (out / "slices").mkdir(parents=True, exist_ok=True)
    for old in (out / "slices").glob("*.jpg"):
        old.unlink()
    name = ep.name
    full.save(out / f"{name}_full.png", optimize=True)
    sh = lt["slice_height"]
    for n, top in enumerate(range(0, full.height, sh), start=1):
        full.crop((0, top, W, min(top + sh, full.height))).save(out / "slices" / f"{name}_{n:03d}.jpg", quality=92)
    print(f"{name}: {len(panels)}컷, {W}x{full.height}px, 조각 {math.ceil(full.height / sh)}장 -> {out}")
    if missing:
        print(f"  아직 안 그린 컷: {', '.join(missing)}")


if __name__ == "__main__":
    main()
