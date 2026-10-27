"""장면 옷 고정(outfit_lock) · 상자 부분 수정(panel_edit.region_edit) · 이어짐 고치기 계획(continuity_fix.plan): GPU·판정 모델 없이 로직만."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vast"))

import outfit_lock as OL  # noqa: E402

RULES = OL.load_rules(None)


def _p(pid, chars, outfit, scene="room", **kw):
    return {"id": pid, "chars": chars, "outfit": outfit, "location": scene, **kw}


# ── 장면 옷 고정 ──────────────────────────────────────────────────────────

def test_silent_cut_inherits_hat_state_from_earlier_cut_in_scene():
    ps = [_p("p1", ["dad"], {"dad": "dark silk coat, bare-headed with no hat"}),
          _p("p2", ["dad"], {"dad": "dark silk coat"})]
    OL.lock(ps, RULES)
    assert "bare-headed with no hat" in ps[1]["outfit"]["dad"]


def test_silent_scene_gets_positive_absence_phrase_and_not_negative_words():
    ps = [_p("p1", ["kid"], {"kid": "black student uniform"}), _p("p2", ["kid"], {"kid": "black student uniform"})]
    OL.lock(ps, RULES)
    for p in ps:
        assert p["outfit"]["kid"].endswith("bare-headed")
        assert "no glasses" not in p["outfit"]["kid"] and "no gloves" not in p["outfit"]["kid"]  # 부정문은 넣지 않는다


def test_glasses_from_the_characters_own_look_are_written_short():
    ps = [_p("p1", ["jh"], {"jh": "black uniform"})]
    OL.lock(ps, RULES, {"jh": "Korean man, a plain bookish face: small narrow eyes behind round wire-rimmed glasses, thin lips"})
    assert "round wire-rimmed glasses" in ps[0]["outfit"]["jh"]
    assert "small narrow eyes" not in ps[0]["outfit"]["jh"]


def test_default_outfit_cap_is_not_forced_on_cuts_that_list_clothes():
    look = OL.looks(None)  # 작품 없음 → 빈 표
    assert look == {}
    ps = [_p("p1", ["jh"], {"jh": "black student uniform"})]
    OL.lock(ps, RULES, {"jh": "Korean man, thin side-parted hair"})  # 생김새(who)에 모자가 없으면 '쓰지 않음'
    assert "bare-headed" in ps[0]["outfit"]["jh"]


def test_held_hat_is_not_carried_as_worn():
    ps = [_p("p1", ["gs"], {"gs": "suit, square cap in hand"}), _p("p2", ["gs"], {"gs": "suit"})]
    OL.lock(ps, RULES)
    assert "cap" not in ps[1]["outfit"]["gs"] and "bare-headed" in ps[1]["outfit"]["gs"]


def test_new_scene_starts_fresh():
    ps = [_p("p1", ["a"], {"a": "coat, black fedora"}, scene="street"), _p("p2", ["a"], {"a": "coat"}, scene="home")]
    OL.lock(ps, RULES)
    assert "fedora" not in ps[1]["outfit"]["a"]


def test_conflicts_flag_on_off_without_story_reason_only():
    ps = [_p("p1", ["a"], {"a": "coat, black fedora"}), _p("p2", ["a"], {"a": "coat, bare-headed"})]
    assert len(OL.conflicts(ps, RULES)) == 1
    ps[1]["action"] = "He takes off his fedora and bows."
    assert OL.conflicts(ps, RULES) == []
    held = [_p("p1", ["a"], {"a": "armor with helmet"}), _p("p2", ["a"], {"a": "helmet off under his arm"}), _p("p3", ["a"], {"a": "armor with helmet"})]
    assert OL.conflicts(held, RULES) == []  # 벗어 든 것 ↔ 쓴 것은 이야기


def test_series_rules_extend_the_shared_ones(tmp_path):
    (tmp_path / "series.json").write_text(json.dumps({"outfit_rules": {"categories": {"headwear": {"words": ["helm"]}, "mask": {"words": ["mask"], "none": "bare face"}}}}), encoding="utf-8")
    r = OL.load_rules(tmp_path)
    assert "helm" in r["categories"]["headwear"]["words"] and "hat" in r["categories"]["headwear"]["words"]
    assert r["categories"]["mask"]["none"] == "bare face"


def test_every_series_episode_locks_without_errors():
    for ep in sorted((ROOT / "series").glob("*-hf/episodes/ep*")):
        f = ep / "panels.json"
        if not f.exists():
            continue
        raw = json.loads(f.read_text(encoding="utf-8"))
        panels = raw["panels"] if isinstance(raw, dict) else raw
        ordered = OL.order_by_board(panels, ep)
        OL.lock(ordered, OL.load_rules(ep.parents[1]), OL.looks(ep.parents[1]))
        OL.conflicts(ordered, OL.load_rules(ep.parents[1]))


# ── 상자 부분 수정 ────────────────────────────────────────────────────────

@pytest.fixture
def pe(monkeypatch):
    import panel_edit as PE

    def fake_edit(series, src, refs, prompt, seed, dst, lift=True):  # 편집 모델 대신: 조각을 통째로 밝게 + 살짝 붉게
        im = Image.open(src).convert("RGB")
        im = Image.eval(im, lambda v: min(255, v + 60))
        im.save(dst)
    monkeypatch.setattr(PE, "_run_edit", fake_edit)
    return PE


def test_region_edit_changes_only_inside_the_box(pe, tmp_path):
    src = tmp_path / "p.png"
    Image.new("RGB", (400, 300), (40, 50, 60)).save(src)
    out = tmp_path / "out.png"
    pe.region_edit(tmp_path, src, [], "make it brighter", 1, out, "250,250,750,750")
    a, b = Image.open(src).convert("RGB"), Image.open(out).convert("RGB")
    x0, y0, x1, y1 = pe.box_px("250,250,750,750", 400, 300)
    for x, y in [(5, 5), (395, 295), (x0 - 2, y0 - 2), (x1 + 1, y1 + 1)]:
        assert a.getpixel((x, y)) == b.getpixel((x, y))  # 상자 밖은 픽셀 그대로
    assert b.getpixel((200, 150)) != a.getpixel((200, 150))  # 안쪽은 바뀜


def test_match_ring_pulls_the_edited_edge_back_to_the_original_colour(pe):
    orig = Image.new("RGB", (200, 200), (100, 100, 100))
    new = Image.new("RGB", (200, 200), (120, 120, 120))
    fixed = pe.match_ring(orig, new)
    assert abs(fixed.getpixel((2, 2))[0] - 100) <= 2


def test_box_strings(pe, tmp_path):
    assert pe.resolve_box(tmp_path, tmp_path / "x.png", "10,20,300,400") == "10,20,300,400"
    with pytest.raises(SystemExit):
        pe.box_px("500,10,100,20", 100, 100)
    with pytest.raises(SystemExit):
        pe.box_px("a,b,c,d", 100, 100)


def test_refs_accept_a_cut_id_of_the_same_episode(pe, tmp_path):
    ep = tmp_path / "episodes" / "ep001"
    (ep / "panels_hf").mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(ep / "panels_hf" / "p012.png")
    assert pe._refs(tmp_path, "p012", ep) == [ep / "panels_hf" / "p012.png"]


# ── 이어짐 고치기 계획 ────────────────────────────────────────────────────

def test_plan_keeps_only_valid_appearance_fixes(monkeypatch, tmp_path):
    import continuity_fix as CF
    answer = {"fixes": [{"image": 2, "who": "dad", "part": "head", "change": "remove the fedora; bare-headed"},
                        {"image": 3, "who": "dad", "change": "x"},          # 없는 그림
                        {"image": 1, "who": "stranger", "change": "x"},     # 두 컷에 다 나오지 않는 사람
                        {"image": 1, "who": "dad", "part": "weird", "change": "add the vest"}]}
    monkeypatch.setattr(CF.R, "_vlm", lambda imgs, text, max_tokens=300: answer)
    prev = {"id": "p1", "chars": ["dad", "son"], "outfit": {"dad": "coat, bare-headed"}}
    p = {"id": "p2", "chars": ["dad"], "outfit": {"dad": "coat"}}
    fixes = CF.plan(prev, tmp_path / "a.png", p, tmp_path / "b.png", ["the father now wears a hat"], {})
    assert fixes == [{"image": 2, "who": "dad", "part": "head", "change": "remove the fedora; bare-headed"},
                     {"image": 1, "who": "dad", "part": "upper", "change": "add the vest"}]


def test_plan_skips_pairs_with_no_shared_person(monkeypatch, tmp_path):
    import continuity_fix as CF
    monkeypatch.setattr(CF.R, "_vlm", lambda *a, **k: pytest.fail("물을 필요가 없다"))
    assert CF.plan({"id": "p1", "chars": ["a"]}, tmp_path / "a.png", {"id": "p2", "chars": ["b"]}, tmp_path / "b.png", ["x"], {}) == []
