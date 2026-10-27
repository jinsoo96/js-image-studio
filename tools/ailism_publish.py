"""만든 회차를 Ailism Original(play.ailism.team)에 API 로 올린다: 작품 만들기·고치기(작가 이름 포함), 회차 올리기·바꾸기.
(2026-10-22 사용자 "나중에 API 로도 게시하라면 게시할 수 있게", "작가명을 미사일 발사대로")

    .venv/Scripts/python tools/ailism_publish.py works
    .venv/Scripts/python tools/ailism_publish.py work <slug> [--title 제목] [--summary-file 줄거리.txt] [--genre 드라마] [--tags "a,b"]
        [--age 15] [--status ongoing] [--days mon,thu] [--cover 표지.jpg] [--author 이름] [--hidden | --visible]
    .venv/Scripts/python tools/ailism_publish.py episode <slug> <회차 폴더 | PDF | 그림 폴더> [--title 제목] [--thumb 목록그림]
        [--note 작가의 말] [--publish now|schedule|draft] [--at 2026-10-25T18:00:00+09:00] [--replace 회차번호] [--dry-run]

예) tools/ailism_publish.py episode d-plus-18 series/d-plus-18-hf/episodes/ep003 --thumb series/d-plus-18-hf/episodes/ep003/panels_hf/p010.png
- 회차 폴더를 주면 out/<회차>_full.png 를 to_pdf.py 와 같은 자리(컷 사이 여백)에서 잘라 올리고, 제목은 storyboard.json 의
  title_card 에서 "N화." 를 뗀 것(사이트가 화 번호를 붙인다). PDF 는 쪽마다 들어 있는 그림을 그대로 꺼낸다(PyMuPDF 필요).
- 키: 환경 변수 AILISM_API_KEY, 없으면 CREDENTIALS 의 ailism_sk_ 키(권한 original:read·original:webtoon:write).
- 게이트웨이는 키마다 업로드를 분당 20장으로 막는다 → 간격을 두고 올리고, 429 면 기다렸다 다시 올린다.
  (스튜디오 화면은 그림을 한꺼번에 동시에 보내다 30여 장부터 429 를 받는데도 저장이 눌려, 67장 중 37장만 올라간 적이 있다)
- 작가 이름(authorName)은 운영자 계정만 바뀐다. 스튜디오 화면은 '공식 작품'일 때만 이 칸을 보내서, 화면에서 작품 정보를
  저장하면 계정 이름으로 돌아간다 → 그때는 `work <slug>` 한 번(기본 작가 이름을 다시 넣는다).
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from common import load_style  # noqa: E402
from to_pdf import cut_points, gap_rows  # noqa: E402

API = "https://api.ailism.team/v1/original"
SITE = "https://play.ailism.team"
CREDENTIALS = Path(r"D:\.claude\CREDENTIALS.local.md")
DEFAULT_AUTHOR = "미사일 발사대"  # 사용자 필명(작품 정보의 '작가'). 계정 이름(미사일 진수)과 다르다
UPLOAD_GAP = 3.2  # 초. 업로드 분당 20장(게이트웨이 limits.uploads_per_minute)
Image.MAX_IMAGE_PIXELS = None


def api_key() -> str:
    key = os.environ.get("AILISM_API_KEY", "")
    if not key and CREDENTIALS.exists():
        m = re.search(r"ailism_sk_[A-Za-z0-9_-]{40,}", CREDENTIALS.read_text(encoding="utf-8"))
        key = m.group(0) if m else ""
    if not key:
        raise SystemExit("Ailism API 키가 없다: AILISM_API_KEY 를 두거나 ailism.team/account/api-keys 에서 만든 키를 CREDENTIALS 에 적는다")
    return key


class Client:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key()}", "User-Agent": "webtoon-studio/ailism_publish"})
        self.last_upload = 0.0

    def call(self, method: str, path: str, **kw):
        for attempt in range(6):
            r = self.s.request(method, API + path, timeout=300, **kw)
            if r.status_code == 429 and attempt < 5:
                wait = float(r.headers.get("Retry-After") or 20 * (attempt + 1))
                print(f"  429(요청 한도) {wait:.0f}초 기다렸다 다시", flush=True)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise SystemExit(f"{method} {path} → {r.status_code} {r.text[:400]}")
            return r.json() if r.content else {}

    def upload(self, path: Path, purpose: str) -> str:
        time.sleep(max(0.0, self.last_upload + UPLOAD_GAP - time.time()))
        self.last_upload = time.time()
        ctype = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
        with open(path, "rb") as f:
            # 다시 보낼 때 파일을 처음부터 읽게 매번 바이트로 넘긴다
            return self.call("POST", "/media", files={"file": (path.name, f.read(), ctype)}, data={"purpose": purpose})["id"]


def split_full(ep: Path, out: Path) -> list:
    """to_pdf.py 와 같은 자리에서 자른 쪽 그림(PDF 쪽과 같다)."""
    src = ep / "out" / f"{ep.name}_full.png"
    if not src.exists():
        raise SystemExit(f"합성본이 없다: {src} (먼저 compose·export)")
    bg_hex = load_style()["lettering"]["background"].lstrip("#")
    bg = tuple(int(bg_hex[i:i + 2], 16) for i in (0, 2, 4))
    full = Image.open(src).convert("RGB")
    cuts = cut_points(gap_rows(full, bg), 1800, int(1800 * 1.45))
    pages = []
    for k, (a, b) in enumerate(zip(cuts, cuts[1:])):
        if b - a > 40:
            p = out / f"{k + 1:03d}.jpg"
            full.crop((0, a, full.width, b)).save(p, quality=90)
            pages.append(p)
    return pages


def from_pdf(pdf: Path, out: Path) -> list:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise SystemExit("PDF 를 읽으려면 PyMuPDF 가 필요하다(pip install pymupdf). 회차 폴더나 그림 폴더를 주면 없어도 된다")
    d, pages = fitz.open(pdf), []
    for i, pg in enumerate(d):
        imgs = pg.get_images(full=True)
        if len(imgs) == 1:  # to_pdf 가 만든 PDF 는 쪽마다 그림 하나 → 다시 압축하지 않고 그대로
            x = d.extract_image(imgs[0][0])
            p = out / f"{i + 1:03d}.{x['ext']}"
            p.write_bytes(x["image"])
        else:
            p = out / f"{i + 1:03d}.png"
            pg.get_pixmap(dpi=144).save(p)
        pages.append(p)
    return pages


def pages_of(src: Path, out: Path) -> tuple:
    """(쪽 그림들, 제목 후보)."""
    if src.suffix.lower() == ".pdf":
        return from_pdf(src, out), ""
    if (src / "storyboard.json").exists():
        b = json.loads((src / "storyboard.json").read_text(encoding="utf-8"))
        title = re.sub(r"^\s*\d+\s*화\s*[.:·]?\s*", "", b.get("title_card") or "")
        return split_full(src, out), title
    pages = sorted(p for p in src.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not pages:
        raise SystemExit(f"올릴 그림이 없다: {src}")
    return pages, ""


def cmd_works(c: Client, a):
    for w in c.call("GET", "/studio/works").get("items", []):
        print(f"{w['slug']:<20} {w['title']:<16} 작가 {w.get('author', {}).get('name', ''):<10} 회차 {w.get('episodes', 0):<3} "
              f"{w.get('state', '')}  {SITE}/ko/webtoon/{w['slug']}")


def cmd_work(c: Client, a):
    cur = None
    try:
        cur = c.call("GET", f"/studio/works/{a.slug}")
    except SystemExit as e:
        if " 404 " not in str(e):
            raise
    if cur is None and not a.title:
        raise SystemExit(f"{a.slug} 작품이 없다. 새로 만들려면 --title 을 준다")
    cur = cur or {}
    summary = Path(a.summary_file).read_text(encoding="utf-8").strip() if a.summary_file else cur.get("summary", "")
    cover = c.upload(Path(a.cover), "cover") if a.cover else (cur.get("coverRef") or cur.get("cover") or {}).get("id")
    visible = True if a.visible else False if a.hidden else (cur.get("state", "published") == "published")
    body = {"title": a.title or cur["title"], "summary": summary,
            "genre": cur.get("genre", "") if a.genre is None else a.genre,
            "tags": cur.get("tags", []) if a.tags is None else [t.strip() for t in a.tags.split(",") if t.strip()][:8],
            "days": cur.get("days", []) if a.days is None else [d.strip() for d in a.days.split(",") if d.strip()],
            "status": a.status or cur.get("status", "ongoing"), "age": a.age or cur.get("age", "15"),
            "lang": cur.get("lang", "ko"), "coverId": cover, "visible": visible,
            # 공식 지정은 운영 판단이라 지금 값을 그대로 둔다(이 도구로 켜지 않는다)
            "official": cur.get("official", False), "authorName": a.author or DEFAULT_AUTHOR,
            "slug": "" if cur else a.slug}
    if cur:
        w = c.call("PATCH", f"/works/{a.slug}", json=body)
    else:
        w = c.call("POST", "/works", json=body)
    print(f"{'고침' if cur else '만듦'}: {w.get('title')} · 작가 {w.get('author', {}).get('name')} · {w.get('state')} · {SITE}/ko/webtoon/{w.get('slug')}")


def cmd_episode(c: Client, a):
    with tempfile.TemporaryDirectory() as tmp:
        pages, title = pages_of(Path(a.source).resolve(), Path(tmp))
        title = a.title or title
        if not title:
            raise SystemExit("회차 제목이 없다: --title 을 준다")
        if len(pages) > 200:
            raise SystemExit(f"한 회차는 200장까지다: {len(pages)}장")
        hs = [Image.open(p).height for p in pages]
        print(f"{a.slug} · '{title}' · {len(pages)}장(쪽 높이 {min(hs)}~{max(hs)}px) · 올리는 데 약 {len(pages) * UPLOAD_GAP / 60:.1f}분", flush=True)
        if a.dry_run:
            return
        ids = []
        for k, p in enumerate(pages, 1):
            ids.append(c.upload(p, "episode"))
            if k % 10 == 0 or k == len(pages):
                print(f"  올림 {k}/{len(pages)}", flush=True)
        body = {"title": title, "images": ids, "note": a.note, "publish": a.publish}
        if a.thumb:
            body["thumbId"] = c.upload(Path(a.thumb), "cover")
        if a.publish == "schedule":
            if not a.at:
                raise SystemExit("예약 공개는 --at 2026-10-25T18:00:00+09:00 처럼 시각을 준다")
            body["publishAt"] = a.at
        if a.replace:
            c.call("PATCH", f"/works/{a.slug}/episodes/{a.replace}", json=body)
            no = a.replace
        else:
            no = c.call("POST", f"/works/{a.slug}/episodes", json=body)["no"]
        got = c.call("GET", f"/studio/works/{a.slug}/episodes/{no}")
        n = len(got.get("images", []))
        print(f"{'바꿈' if a.replace else '올림'}: {no}화 '{title}' 그림 {n}장 · {a.publish} · {SITE}/ko/webtoon/{a.slug}/{no}")
        if n != len(pages):
            raise SystemExit(f"올라간 그림 수가 다르다: {n} / {len(pages)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("works", help="내 작품 목록")
    w = sub.add_parser("work", help="작품 만들기·고치기(주지 않은 칸은 지금 값 그대로, 작가 이름은 늘 넣는다)")
    w.add_argument("slug")
    w.add_argument("--title")
    w.add_argument("--summary-file")
    w.add_argument("--genre")
    w.add_argument("--tags")
    w.add_argument("--days", help="mon,tue,...  빈 문자열이면 비정기")
    w.add_argument("--status", choices=["ongoing", "completed", "hiatus"])
    w.add_argument("--age", choices=["all", "15"])
    w.add_argument("--cover")
    w.add_argument("--author", help=f"보이는 작가 이름(기본 {DEFAULT_AUTHOR})")
    w.add_argument("--hidden", action="store_true", help="비공개로")
    w.add_argument("--visible", action="store_true", help="공개로")
    e = sub.add_parser("episode", help="회차 올리기")
    e.add_argument("slug")
    e.add_argument("source", help="회차 폴더(storyboard.json·out/<회차>_full.png) · PDF · 그림 폴더")
    e.add_argument("--title")
    e.add_argument("--thumb", help="목록 그림(비우면 사이트가 첫 그림 윗부분을 쓴다)")
    e.add_argument("--note", default="", help="작가의 말")
    e.add_argument("--publish", choices=["now", "schedule", "draft"], default="now")
    e.add_argument("--at", default="", help="예약 시각(ISO, 시간대 포함)")
    e.add_argument("--replace", type=int, default=0, help="이 회차 번호의 그림·제목을 바꾼다(새 회차 대신)")
    e.add_argument("--dry-run", action="store_true", help="자르기·장수만 보고 올리지 않는다")
    a = ap.parse_args()
    c = Client()
    {"works": cmd_works, "work": cmd_work, "episode": cmd_episode}[a.cmd](c, a)


if __name__ == "__main__":
    main()
