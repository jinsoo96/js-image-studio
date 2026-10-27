"""합성된 세로 스크롤 웹툰을 epub(EPUB 3)으로 만든다.

    python tools/to_epub.py <episode_dir> [--page-height 1800] [--quality 86] [--author 이름]

to_pdf.py 와 같은 방식으로 out/<ep>_full.png 를 컷 사이 여백에서 잘라 한 쪽씩 이미지로 넣는다(컷이 쪽 중간에서 안 잘림).
고정 레이아웃(pre-paginated) + 이어 스크롤(scrolled-continuous) 힌트라서, 지원하는 뷰어(애플 북스·리디 등)는 세로로 이어 보이고
나머지는 한 쪽씩 넘겨 본다. 책 제목은 SERIES.md 첫 줄(작품명) + 콘티 title_card(회차 제목).
결과: out/<ep>.epub
"""
import argparse
import io
import sys
import time
import uuid
import zipfile
from html import escape
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_json, load_style  # noqa: E402
from to_pdf import cut_points, gap_rows  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ko" xml:lang="ko">
<head><meta charset="UTF-8"/><title>{title}</title><meta name="viewport" content="width={w}, height={h}"/>
<style>html,body{{margin:0;padding:0;background:#fff}}img{{display:block;width:{w}px;height:{h}px}}</style></head>
<body><img src="images/{img}" alt="{alt}"/></body>
</html>"""


def series_name(series_dir):
    md = series_dir / "SERIES.md"
    if md.exists():
        first = md.read_text(encoding="utf-8").splitlines()[0]
        if first.startswith("#"):
            return first.lstrip("# ").strip()
    return series_dir.name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--page-height", type=int, default=1800)
    ap.add_argument("--quality", type=int, default=86)
    ap.add_argument("--author", help="dc:creator(작가 이름). 없으면 넣지 않는다")
    args = ap.parse_args()

    ep = Path(args.episode_dir)
    series = ep.parent.parent
    src = ep / "out" / f"{ep.name}_full.png"
    if not src.exists():
        sys.exit(f"합성본이 없다: {src} (먼저 compose.py)")
    board = load_json(ep / "storyboard.json")
    name = series_name(series)
    ep_title = board.get("title_card") or f"{board.get('episode', ep.name)}화"
    book_title = f"{name} {ep_title}"

    bg_hex = load_style()["lettering"]["background"].lstrip("#")
    bg = tuple(int(bg_hex[i:i + 2], 16) for i in (0, 2, 4))
    full = Image.open(src).convert("RGB")
    cuts = cut_points(gap_rows(full, bg), args.page_height, int(args.page_height * 1.45))
    pages = [full.crop((0, a, full.width, b)) for a, b in zip(cuts, cuts[1:]) if b - a > 40]

    out = ep / "out" / f"{ep.name}.epub"
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'webtoon-studio/{series.name}/{ep.name}')}"
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest, spine = [], []
    with zipfile.ZipFile(out, "w") as z:
        # mimetype 은 맨 앞, 압축 없이(EPUB 규칙)
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        for i, pg in enumerate(pages, 1):
            img, xhtml = f"p{i:03d}.jpg", f"p{i:03d}.xhtml"
            buf = io.BytesIO()
            pg.save(buf, "JPEG", quality=args.quality, optimize=True)
            z.writestr(f"OEBPS/images/{img}", buf.getvalue(), compress_type=zipfile.ZIP_STORED)
            z.writestr(f"OEBPS/{xhtml}", PAGE.format(title=escape(book_title), w=pg.width, h=pg.height, img=img,
                                                     alt=escape(f"{book_title} {i}쪽")), compress_type=zipfile.ZIP_DEFLATED)
            props = ' properties="cover-image"' if i == 1 else ""
            manifest.append(f'<item id="img{i}" href="images/{img}" media-type="image/jpeg"{props}/>')
            manifest.append(f'<item id="pg{i}" href="{xhtml}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="pg{i}"/>')
        nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ko" xml:lang="ko">
<head><meta charset="UTF-8"/><title>{escape(book_title)}</title></head>
<body><nav epub:type="toc" id="toc"><ol><li><a href="p001.xhtml">{escape(ep_title)}</a></li></ol></nav></body>
</html>"""
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        creator = f"<dc:creator>{escape(args.author)}</dc:creator>" if args.author else ""
        if not args.author and board.get("credits"):  # 콘티의 credits: [["aut", 이름], ["ill", 이름]] (같은 사람이면 역할 둘)
            people = {}
            for role, who in board["credits"]:
                people.setdefault(who, []).append(role)
            creator = "".join(f'<dc:creator id="c{i}">{escape(who)}</dc:creator>' + "".join(
                f'<meta refines="#c{i}" property="role" scheme="marc:relators">{r}</meta>' for r in roles)
                for i, (who, roles) in enumerate(people.items(), 1))
        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ko">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{escape(book_title)}</dc:title>
    <dc:language>ko</dc:language>{creator}
    <meta property="dcterms:modified">{modified}</meta>
    <meta property="rendition:layout">pre-paginated</meta>
    <meta property="rendition:spread">none</meta>
    <meta property="rendition:flow">scrolled-continuous</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    {chr(10).join('    ' + m for m in manifest).lstrip()}
  </manifest>
  <spine>
    {chr(10).join('    ' + s for s in spine).lstrip()}
  </spine>
</package>"""
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
    print(f"{out} {len(pages)}쪽, {out.stat().st_size / 2**20:.1f}MB, 제목 '{book_title}'")


if __name__ == "__main__":
    main()
