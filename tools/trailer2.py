"""이야기가 보이는 세로 예고편(쇼츠·릴스용 mp4 + 소리 없는 gif). 콘티처럼 적은 trailer.json 을 따라 만든다.
처음 판(to_trailer.py)은 가로 컷을 세로 화면 가운데 작게 띄우고 말풍선 글씨가 폰에서 안 읽혔으며 모든 컷이 1.8초 같은 박자에 소리도 없었다
(2026-10-13 사용자 "예고편 다시 만든 거 맞냐? 아직 좀 부족한데").

    python tools/trailer2.py <episode_dir>            (episode_dir/trailer.json → out/trailer.mp4, out/trailer.gif)

trailer.json: {"beats": [
  {"text": ["입대 1년 차,", "*여자친구에게 차였다.*"], "sec": 1.6},          글자 카드(*...* 는 강조색). 한 줄씩 떠오른다
                                                                          대사 "line" 의 | 는 줄 끊기
  {"panel": "p054", "line": "D+18...?", "kind": "thought", "sec": 1.6,   컷(말풍선 없는 원본)을 세로로 크게 잘라 얼굴 쪽으로 다가간다
   "fx": "punch"|"flash"|"shake", "pan": true, "focus": [x, y]},          pan = 얼굴 없는 넓은 컷을 가로로 훑기, focus = 다가갈 곳(0~1)
  {"title": true, "sec": 2.6}],                                           끝 카드(작품명·부제·회차·지금 보기)
 "music": "tension"}
소리는 numpy 로 만든다(배경 패드·심장 박동·넘김 바람 소리·쿵). 저작권 걱정 없는 대신 단순하다.
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import ROOT, load_json  # noqa: E402
from to_epub import series_name  # noqa: E402

W, H, FPS = 720, 1280, 30
SR = 44100
FONTS = ROOT / "fonts"
BG = (14, 11, 18)
ACCENT = (255, 96, 150)
WIN = (40, 150, 680, 1050)  # 컷 창(x0, y0, x1, y1): 640x900


def fnt(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def ease(t):
    return t * t * (3 - 2 * t)


# ---------- 그림 ----------

def faces_center(im):
    try:
        from face_detect import detect
        f = detect(im)
        if f:
            x0, y0, x1, y1, _ = max(f, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            return ((x0 + x1) / 2 / im.width, (y0 + y1) / 2 / im.height), (x1 - x0) / im.width
    except Exception:
        pass
    return None, None


def crop_box(im, cx, cy, zoom, aspect):
    """그림에서 aspect(가로/세로) 비율 창을 (cx, cy) 가운데로, zoom 배 확대해 자를 상자."""
    iw, ih = im.size
    if iw / ih > aspect:
        bh = ih / zoom
        bw = bh * aspect
    else:
        bw = iw / zoom
        bh = bw / aspect
    x = min(max(cx * iw - bw / 2, 0), iw - bw)
    y = min(max(cy * ih - bh / 2, 0), ih - bh)
    return (x, y, x + bw, y + bh)


def backdrop(im):
    s = max(W / im.width, H / im.height)
    bg = im.resize((int(im.width * s) + 2, int(im.height * s) + 2)).filter(ImageFilter.GaussianBlur(30))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    return Image.blend(bg, Image.new("RGB", (W, H), BG), 0.6)


def wrap(text, font, max_w, d):
    if "|" in text:  # 콘티에서 직접 끊은 줄
        return [t.strip() for t in text.split("|")]
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_line(f, text, kind, alpha):
    """대사: 컷 창 아래쪽에 크게. speech = 흰 말풍선, thought = 점선 풍선, shout = 들쭉날쭉 큰 글씨, sfx = 그림 위 큰 효과음."""
    if not text or alpha <= 0:
        return f
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if kind == "sfx":
        font = fnt("BlackHanSans-Regular.ttf", 92)
        tw = d.textlength(text, font=font)
        x, y = (W - tw) / 2, WIN[3] - 190
        d.text((x, y), text, font=font, fill=(255, 255, 255, int(255 * alpha)), stroke_width=8, stroke_fill=(20, 10, 30, int(255 * alpha)))
    else:
        font = fnt("NanumBarunGothicBold.ttf" if kind != "thought" else "GowunDodum-Regular.ttf", 44 if kind != "shout" else 52)
        lines = wrap(text, font, W - 170, d)
        lh = font.size + 14
        tw = max(d.textlength(l, font=font) for l in lines)
        bw, bh = tw + 76, lh * len(lines) + 46
        bx, by = (W - bw) / 2, WIN[3] - bh * 0.55
        fill = (255, 255, 255, int(250 * alpha))
        ink = (24, 18, 30, int(255 * alpha))
        if kind == "thought":
            fill = (236, 232, 244, int(245 * alpha))
            d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh / 2, fill=fill)
            d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh / 2, outline=(120, 110, 140, int(255 * alpha)), width=3)
            for i, r in enumerate((14, 9)):
                cx, cy = bx + bw * 0.28 - i * 22, by - 18 - i * 22
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=(120, 110, 140, int(255 * alpha)), width=3)
        else:
            d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=34, fill=fill, outline=ink, width=4)
            tx = bx + bw * 0.3
            d.polygon([(tx - 18, by + 2), (tx + 18, by + 2), (tx - 4, by - 30)], fill=fill)
            d.line([(tx - 18, by + 2), (tx - 4, by - 30), (tx + 18, by + 2)], fill=ink, width=4)
        y = by + 23
        for l in lines:
            lw = d.textlength(l, font=font)
            d.text(((W - lw) / 2, y), l, font=font, fill=ink)
            y += lh
    out = f.convert("RGBA")
    out.alpha_composite(layer)
    return out.convert("RGB")


def shot_frames(ep, beat):
    src = ep / "panels" / f"{beat['panel']}.png"
    im = Image.open(src).convert("RGB")
    n = int(beat.get("sec", 1.6) * FPS)
    bg = backdrop(im)
    ww, wh = WIN[2] - WIN[0], WIN[3] - WIN[1]
    aspect = ww / wh
    face, fw = faces_center(im)
    graphic = im.height > im.width * 1.2  # 휴대폰 화면 같은 세로 그래픽은 통째로
    fx = beat.get("fx")
    for k in range(n):
        t = k / max(n - 1, 1)
        if graphic:  # 휴대폰 화면: 가운데를 크게(바깥 방 배경은 잘라 낸다)
            box = crop_box(im, 0.5, 0.5, 1.3 + 0.05 * t, aspect)
            fg = im.crop(tuple(int(v) for v in box)).resize((ww, wh), Image.LANCZOS)
        else:
            if beat.get("pan") or not face:
                cx = 0.3 + 0.4 * ease(t) if beat.get("pan") else 0.5
                cy, z = 0.5, 1.0 + 0.05 * t
            else:
                cx, cy = beat.get("focus", face)
                cy = min(max(cy + 0.05, 0.3), 0.7)
                z = 1.0 + 0.10 * ease(t)
            if fx == "punch":  # 처음 0.3초에 확 다가간다
                z *= 1.0 + 0.16 * ease(min(1.0, k / (0.3 * FPS)))
            box = crop_box(im, cx, cy, z, aspect)
            fg = im.crop(tuple(int(v) for v in box)).resize((ww, wh), Image.LANCZOS)
        f = bg.copy()
        dx = dy = 0
        if fx == "shake" or (fx == "punch" and k < 6):
            dx, dy = int(math.sin(k * 2.3) * 10), int(math.cos(k * 3.1) * 8)
        f.paste(fg, ((W - fg.width) // 2 + dx, WIN[1] + (wh - fg.height) // 2 + dy))
        # 컷 창 테두리
        ImageDraw.Draw(f).rectangle((WIN[0] - 2 + dx, WIN[1] - 2 + dy, WIN[2] + 1 + dx, WIN[3] + 1 + dy), outline=(255, 255, 255), width=2)
        if fx == "flash" and k < 5:
            f = Image.blend(f, Image.new("RGB", (W, H), (255, 255, 255)), 1 - k / 5)
        a = min(1.0, max(0.0, (k - 0.15 * FPS) / (0.18 * FPS)))  # 대사는 살짝 늦게
        yield draw_line(f, beat.get("line", ""), beat.get("kind", "speech"), a)


def text_frames(beat):
    n = int(beat.get("sec", 1.4) * FPS)
    lines = beat["text"]
    size = beat.get("size", 76)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    while size > 40 and max(probe.textlength(l.strip("*"), font=fnt("BlackHanSans-Regular.ttf", size)) for l in lines) > W - 90:
        size -= 2  # 화면 폭(양옆 45px)을 넘으면 줄인다
    big = fnt("BlackHanSans-Regular.ttf", size)
    per = max(1, int(0.35 * FPS))
    for k in range(n):
        f = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(f)
        total = len(lines) * (big.size + 26)
        y0 = (H - total) / 2
        for i, raw in enumerate(lines):
            a = min(1.0, max(0.0, (k - i * per) / (0.25 * FPS)))
            if a <= 0:
                continue
            accent = raw.startswith("*") and raw.endswith("*")
            text = raw.strip("*")
            tw = d.textlength(text, font=big)
            col = ACCENT if accent else (244, 238, 248)
            col = tuple(int(c * a + b * (1 - a)) for c, b in zip(col, BG))
            d.text(((W - tw) / 2, y0 + i * (big.size + 26) + (1 - a) * 24), text, font=big, fill=col)
        yield f


def title_frames(ep, beat):
    series = ep.parent.parent
    board = load_json(ep / "storyboard.json")
    sub = ""
    md = series / "SERIES.md"
    if md.exists():
        sub = next((l.split(":", 1)[1].strip() for l in md.read_text(encoding="utf-8").splitlines() if l.startswith("부제")), "")
    name = series_name(series)
    n = int(beat.get("sec", 2.6) * FPS)
    big, mid, small = fnt("BlackHanSans-Regular.ttf", 128), fnt("GowunDodum-Regular.ttf", 34), fnt("NanumBarunGothicBold.ttf", 32)
    for k in range(n):
        t = min(1.0, k / (0.5 * FPS))
        f = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(f)
        s = 1.25 - 0.25 * ease(t)
        bf = fnt("BlackHanSans-Regular.ttf", int(big.size * s))
        tw = d.textlength(name, font=bf)
        d.text(((W - tw) / 2, H / 2 - 190 - (bf.size - big.size) / 2), name, font=bf, fill=ACCENT)
        a = min(1.0, max(0.0, (k - 0.4 * FPS) / (0.4 * FPS)))
        for y, text, font, col in ((H / 2 - 20, sub, mid, (240, 234, 246)), (H / 2 + 50, board.get("title_card", ""), mid, (176, 162, 190)),
                                   (H / 2 + 280, board.get("credit", ""), small, (150, 140, 160))):
            if text:
                c = tuple(int(v * a + b * (1 - a)) for v, b in zip(col, BG))
                d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=c)
        b = min(1.0, max(0.0, (k - 0.9 * FPS) / (0.4 * FPS)))
        if b > 0:
            label = f"{int(ep.name[2:5]) if ep.name[2:5].isdigit() else 1}화 지금 보기"  # 회차 폴더 ep002 → 2화
            lw = d.textlength(label, font=small)
            bx, by = (W - lw) / 2 - 34, H / 2 + 170
            col = tuple(int(v * b + q * (1 - b)) for v, q in zip(ACCENT, BG))
            d.rounded_rectangle((bx, by, bx + lw + 68, by + 64), radius=32, outline=col, width=3)
            d.text((bx + 34, by + 14), label, font=small, fill=tuple(int(v * b + q * (1 - b)) for v, q in zip((255, 255, 255), BG)))
        yield f


# ---------- 소리 ----------

def tone(freq, sec, gain, attack=0.01, release=0.2):
    t = np.arange(int(sec * SR)) / SR
    env = np.minimum(1.0, t / max(attack, 1e-4)) * np.minimum(1.0, (sec - t) / max(release, 1e-4))
    return gain * np.sin(2 * np.pi * freq * t) * np.clip(env, 0, 1)


def thump(gain=0.6):
    t = np.arange(int(0.18 * SR)) / SR
    f = 70 * np.exp(-t * 8) + 38
    return gain * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 18)


def whoosh(sec=0.35, gain=0.18):
    n = int(sec * SR)
    noise = np.random.default_rng(1).standard_normal(n)
    k = np.linspace(0, 1, n)
    out, prev = np.zeros(n), 0.0
    for i in range(n):  # 갈수록 밝아지는 저역 통과
        a = 0.02 + 0.25 * k[i]
        prev = prev + a * (noise[i] - prev)
        out[i] = prev
    return gain * out / (np.abs(out).max() + 1e-9) * np.sin(np.pi * k) ** 1.5


def boom(gain=0.8):
    t = np.arange(int(1.4 * SR)) / SR
    f = 110 * np.exp(-t * 4) + 32
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.6)
    noise = np.random.default_rng(2).standard_normal(len(t)) * np.exp(-t * 14) * 0.35
    return gain * (body + noise)


def pad(total, gain=0.07):
    """낮게 깔리는 단조 화음(A 단조 → F → C → G 를 4초씩)."""
    out = np.zeros(int(total * SR))
    chords = [(110.0, 130.81, 164.81), (87.31, 110.0, 130.81), (130.81, 164.81, 196.0), (98.0, 123.47, 146.83)]
    seg = 4.0
    for i in range(int(math.ceil(total / seg))):
        for fq in chords[i % 4]:
            for det in (1.0, 1.004):
                s = tone(fq * det, seg + 0.6, gain / 3, attack=1.2, release=1.2)
                a = int(i * seg * SR)
                b = min(len(out), a + len(s))
                out[a:b] += s[: b - a]
    return out


def add(track, clip, at):
    a = int(at * SR)
    if a >= len(track):
        return
    b = min(len(track), a + len(clip))
    track[a:b] += clip[: b - a]


def soundtrack(events, total, heart):
    track = pad(total)
    for at, kind in events:
        if kind == "cut":
            add(track, whoosh(), max(0, at - 0.18))
        elif kind == "boom":
            add(track, boom(), at)
        elif kind == "flash":
            add(track, boom(0.55), at)
            add(track, whoosh(0.25, 0.25), max(0, at - 0.12))
    for a, b in heart:  # 심장 박동 구간
        t = a
        while t < b:
            add(track, thump(0.55), t)
            add(track, thump(0.35), t + 0.22)
            t += 0.85
    fade = int(0.8 * SR)
    track[-fade:] *= np.linspace(1, 0, fade)
    track = np.tanh(track * 1.2)
    return (track / (np.abs(track).max() + 1e-9) * 0.89 * 32767).astype(np.int16)


# ---------- 묶기 ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--gif-width", type=int, default=360)
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg 가 없다")
    ep = Path(args.episode_dir)
    spec = load_json(ep / "trailer.json")
    out = ep / "out"
    tmp = out / "_trailer2"
    tmp.mkdir(parents=True, exist_ok=True)
    silent = tmp / "video.mp4"
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                           "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", str(silent)], stdin=subprocess.PIPE)
    fade_n = int(0.2 * FPS)
    prev_tail, t_now, events, heart = [], 0.0, [], []
    for i, beat in enumerate(spec["beats"]):
        if beat.get("title"):
            clip = list(title_frames(ep, beat))
            events.append((t_now, "boom"))
        elif beat.get("text"):
            clip = list(text_frames(beat))
        else:
            clip = list(shot_frames(ep, beat))
            if beat.get("fx") in ("flash", "punch"):
                events.append((t_now, "flash"))
        if beat.get("heart"):
            heart.append((t_now, t_now + len(clip) / FPS))
        if i:
            events.append((t_now, "cut"))
        hard = beat.get("cut") == "hard" or beat.get("fx") == "flash"
        head = [] if hard else clip[:fade_n]
        for k, f in enumerate(head):
            ff.stdin.write((Image.blend(prev_tail[k], f, (k + 1) / (fade_n + 1)) if k < len(prev_tail) else f).tobytes())
        if hard:
            for f in prev_tail:
                ff.stdin.write(f.tobytes())
            t_now += len(prev_tail) / FPS
        body = clip[len(head):len(clip) - fade_n]
        for f in body:
            ff.stdin.write(f.tobytes())
        t_now += (len(head) + len(body)) / FPS
        prev_tail = clip[len(clip) - fade_n:]
    for f in prev_tail:
        ff.stdin.write(f.tobytes())
    t_now += len(prev_tail) / FPS
    ff.stdin.close()
    if ff.wait():
        sys.exit("ffmpeg 영상 실패")

    audio = tmp / "audio.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(soundtrack(events, t_now, heart).tobytes())
    mp4 = out / "trailer.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-shortest", "-movflags", "+faststart", str(mp4)], check=True)
    gif = out / "trailer.gif"
    vf = f"fps=12,scale={args.gif_width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-vf", vf, "-loop", "0", str(gif)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{mp4} {t_now:.1f}초 {mp4.stat().st_size / 2**20:.1f}MB / {gif} {gif.stat().st_size / 2**20:.1f}MB")


if __name__ == "__main__":
    main()
