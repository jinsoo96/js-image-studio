"""mcp/server.py contact_sheet 가 24개를 넘는 그림을 여러 장으로 나눠 전부 만드는지(GPU·대화 모델 없이).

2026-10-26 미사일 스튜디오 시험: 외출 1화 이상수 180컷을 앞 24개만 만들고 전체 수만 알리자, 작은 모델이 번호를 손으로 7번 쪼개 부르며
(겹치는 묶음까지) 도구 17번·입력 63만 토큰을 썼다. 장은 도구가 나눈다.
"""
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import server as W  # noqa: E402


def test_contact_sheet_pages_over_24():
    d = ROOT / "series" / "_tmp" / "_test_sheet_paging"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    made = []
    try:
        for i in range(30):
            Image.new("RGB", (64, 96), (i * 8 % 256, 80, 120)).save(d / f"img{i:02d}.png")
        r = W.contact_sheet(paths="series/_tmp/_test_sheet_paging/*.png")
        assert isinstance(r, list) and len(r) == 2, r
        info = json.loads(r[0])
        made = [pg["sheet"] for pg in info["pages"]]
        assert info["panels"] == 30 and info["sheets"] == 2 and info["per_sheet"] == 24
        assert [pg["panels"] for pg in info["pages"]] == [24, 6]
        assert info["pages"][0]["shown"][0] == "img00.png" and info["pages"][1]["shown"][-1] == "img29.png"
        assert all(Path(s).exists() for s in made)  # 장마다 파일이 실제로 있다
        # 24개 이하는 예전처럼 한 장(JSON 에 sheet·link·panels)
        r1 = W.contact_sheet(paths="series/_tmp/_test_sheet_paging/img0*.png")
        info1 = json.loads(r1[0])
        made.append(info1["sheet"])
        assert info1["panels"] == 10 and "pages" not in info1
    finally:
        shutil.rmtree(d, ignore_errors=True)
        for s in made:
            Path(s).unlink(missing_ok=True)


if __name__ == "__main__":
    test_contact_sheet_pages_over_24()
    print("OK")
