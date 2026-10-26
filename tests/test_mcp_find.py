"""mcp/server.py find 회귀 테스트(네트워크·GPU 없이, 이 PC 의 series/ 데이터로). python tests/test_mcp_find.py 또는 pytest.

- 걸린 컷 번호 전부가 ids_by_episode 로 오고(hits 는 limit 까지만), by_episode 합 = total
  (2026-10-26 외출 1화 '이상수': find total 180 인데 모델이 list_panels 목록을 눈으로 세어 244 라 답함)
- series·episode 를 주면 그 회차만
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import server as S  # noqa: E402


def _some_character() -> tuple:
    """인물 이름이 있는 아무 작품 하나: (작품 폴더, 인물 한글 이름). 데이터 없으면 None."""
    for sd in sorted(ROOT.glob("series/*")):
        cast = sd / "cast.json"
        if sd.name.startswith("_") or not cast.exists():
            continue
        for cid, v in json.loads(cast.read_text(encoding="utf-8")).items():
            ko = (v or {}).get("ko") if isinstance(v, dict) else None
            if ko and (sd / "episodes").is_dir():
                return sd, ko.split("(")[0].strip()
    return None


def test_find_gives_all_ids_per_episode_not_only_limit():
    pick = _some_character()
    if not pick:
        return
    sd, name = pick
    r = json.loads(S.find(name, series=sd.name, limit=3))
    if r.get("error"):
        return  # 이 인물이 컷 글에 없으면 시험할 것이 없다
    assert len(r["hits"]) <= 3 and r["total"] >= len(r["hits"])
    assert sum(r["by_episode"].values()) == r["total"]
    ids = {k: v.split(",") for k, v in r["ids_by_episode"].items()}
    assert {k: len(v) for k, v in ids.items()} == r["by_episode"]
    assert all(h["panel"] in ids[f"{h['series']}/{h['episode']}"] for h in r["hits"])
    if r["total"] > 3:
        assert "ids_by_episode" in r.get("note", "")
    # 회차를 주면 그 회차만
    ep = next(iter(r["by_episode"])).split("/")[1]
    r2 = json.loads(S.find(name, series=sd.name, episode=ep, limit=3))
    assert set(r2["by_episode"]) == {f"{sd.name}/{ep}"} and r2["total"] == r["by_episode"][f"{sd.name}/{ep}"]


if __name__ == "__main__":
    test_find_gives_all_ids_per_episode_not_only_limit()
    print("OK")
