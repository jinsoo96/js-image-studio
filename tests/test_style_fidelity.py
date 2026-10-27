"""tools/style_fidelity.py 의 점수 로직 시험(모델 없이: 그림마다 정해 둔 가짜 특징으로).

- 백분위: 원작 무리 안의 그림은 높고, 동떨어진 그림은 0
- 같은 컷 번호·사실상 같은 그림은 이웃에서 빠진다(원작을 그대로 복사해 넣어도 점수가 부풀지 않는다)
- 기준 분포는 원작 전부를 하나씩 빼고 잰다(자기 자신과의 닮음은 세지 않는다)
- 진단: 떨어진 두 작품은 구별(trusted), 같은 분포의 두 작품은 구별 못 함
- CSLS 식, 설정 비교(같은 컷끼리 차이로 나은 쪽을 고름), 사람 기준점 채점
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import style_fidelity as S  # noqa: E402

RNG = np.random.default_rng(7)


def _unit(v):
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def _cluster(center, n, noise):
    return [_unit(center + noise * RNG.standard_normal(len(center))) for _ in range(n)]


class FakeEmb:
    """파일 이름 → 정해 둔 특징. Embedder 와 같은 꼴(호출하면 (n, d))."""
    name = "fake"

    def __init__(self, table):
        self.table = table

    def __call__(self, files):
        return np.stack([self.table[str(Path(f).resolve())] for f in files]) if files else np.zeros((0, 0))


def _series(root: Path, name: str, vecs: list, table: dict, ep="ep001", prefix="p"):
    """회차 panels_hf 에 작은 그림을 두고 그 그림의 가짜 특징을 table 에 적는다."""
    d = root / name / "episodes" / ep / "panels_hf"
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for i, v in enumerate(vecs):
        f = d / f"{prefix}{i:03d}.png"
        Image.new("RGB", (8, 8), (i % 255, 0, 0)).save(f)
        table[str(f.resolve())] = v
        files.append(f)
    return root / name, files


def _img(path: Path, v, table):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(path)
    table[str(path.resolve())] = v
    return path


def test_percentile_inside_vs_far():
    with tempfile.TemporaryDirectory() as t:
        t, table = Path(t), {}
        c = np.zeros(32)
        c[0] = 1
        sd, _ = _series(t, "w", _cluster(c, 30, 0.35), table)
        near = _img(t / "new" / "a001.png", _unit(c + 0.35 * RNG.standard_normal(32)), table)
        far = np.zeros(32)
        far[5] = 1
        away = _img(t / "new" / "a002.png", far, table)
        r = S.score(sd, [near, away], FakeEmb(table), floor=10, diag={})
        pct = {Path(it["file"]).name: it["pct"] for it in r["items"]}
        assert pct["a001.png"] >= 10 and pct["a002.png"] == 0
        assert r["trusted"] is None and "진단 전" in r["note"]  # 진단 없이는 판정하지 않는다고 알린다


def test_same_panel_number_and_duplicates_do_not_inflate():
    with tempfile.TemporaryDirectory() as t:
        t, table = Path(t), {}
        c = np.zeros(16)
        c[0] = 1
        sd, files = _series(t, "w", _cluster(c, 20, 0.5), table)
        emb = FakeEmb(table)
        # 원작 p003 을 그대로 다시 그린 것처럼(같은 특징, 같은 이름): 자기 원작과의 닮음 1.0 이 세지면 안 된다
        copy = _img(t / "draw" / "p003.png", table[str(files[3].resolve())], table)
        renamed = _img(t / "draw" / "x999.png", table[str(files[3].resolve())], table)  # 이름이 달라도 사실상 같은 그림이면 뺀다
        r = S.score(sd, [copy, renamed], emb, diag={})
        E = emb(files)
        own_loo = S.scores(E[3:4], ["p003"], E, [f.stem for f in files])[0]
        assert abs(r["items"][0]["score"] - round(float(own_loo), 4)) < 1e-4
        assert abs(r["items"][1]["score"] - round(float(own_loo), 4)) < 1e-4
        assert r["items"][0]["score"] < 0.999


def test_reference_distribution_uses_every_original_leave_one_out():
    with tempfile.TemporaryDirectory() as t:
        t, table = Path(t), {}
        c = np.zeros(16)
        c[1] = 1
        sd, files = _series(t, "w", _cluster(c, 20, 0.4), table)
        emb = FakeEmb(table)
        # 원작 하나를 새 그림처럼 채점하면(이름만 다르게) 원작 분포의 중간쯤: 자기 자신과의 1.0 은 빠지고 다른 원작과만 견준다
        probe = _img(t / "new" / "z001.png", table[str(files[7].resolve())], table)
        r = S.score(sd, [probe], emb, diag={})
        assert r["refs"] == 20 and 0 < r["items"][0]["pct"] <= 100
        E = emb(files)
        dist = S.scores(E, [f.stem for f in files], E, [f.stem for f in files])
        assert abs(r["items"][0]["score"] - round(float(dist[7]), 4)) < 1e-4


def test_diagnose_separated_vs_identical_series():
    a, b, c = np.zeros(24), np.zeros(24), np.zeros(24)
    a[0], b[1], c[0] = 1, 1, 1
    A, B, C = _cluster(a, 25, 0.4), _cluster(b, 25, 0.4), _cluster(c, 25, 0.4)
    names = [f"p{i:03d}" for i in range(25)]
    sep = S.diagnose({"A": (np.stack(A), names), "B": (np.stack(B), names)})
    assert sep["A"]["trusted"] and sep["B"]["trusted"] and sep["A"]["mode"] == "raw" and sep["A"]["raw"]["acc"] > 0.9
    same = S.diagnose({"A": (np.stack(A), names), "C": (np.stack(C), names)})  # 같은 분포: 구별할 수 없어야 한다
    assert not same["A"]["trusted"] and not same["C"]["trusted"]
    one = S.diagnose({"A": (np.stack(A), names)})
    assert one["A"]["trusted"] is None  # 견줄 작품이 없으면 판정하지 않는다


def test_csls_formula():
    D = np.stack([_unit([1, 0, 0]), _unit([0.8, 0.6, 0]), _unit([0, 0, 1])])
    Q = np.stack([_unit([1, 0.2, 0])])
    k = 2
    S_ = Q @ D.T
    DD = D @ D.T
    np.fill_diagonal(DD, -np.inf)
    hub = np.sort(DD, axis=1)[:, -k:].mean(1)
    top = np.sort(2 * S_[0] - hub)[-k:].mean()
    raw = np.sort(S_[0])[-k:].mean()
    got = S.scores(Q, None, D, None, mode="csls", k=k)[0]
    assert abs(got - (top - raw)) < 1e-9
    assert abs(S.scores(Q, None, D, None, mode="raw", k=k)[0] - raw) < 1e-9


def test_compare_picks_the_closer_setting_by_paired_difference():
    with tempfile.TemporaryDirectory() as t:
        t, table = Path(t), {}
        c = np.zeros(24)
        c[2] = 1
        sd, _ = _series(t, "w", _cluster(c, 30, 0.35), table)
        off, on = [], []
        for i in range(12):
            far = _unit(0.3 * c + RNG.standard_normal(24))
            off.append(_img(t / "off" / f"q{i:02d}.png", far, table))
            on.append(_img(t / "on" / f"q{i:02d}.png", _unit(c + 0.35 * RNG.standard_normal(24)), table))
        r = S.compare(sd, {"끔": off, "켬": on}, FakeEmb(table))
        rows = {row["group"]: row for row in r["rows"]}
        assert r["best"] == "켬" and rows["켬"]["vs_base"] > 0 and rows["켬"]["vs_lo"] > 0 and rows["켬"]["paired"] == 12
        r2 = S.compare(sd, {"켬": on, "끔": off}, FakeEmb(table))
        assert r2["best"] is None  # 기준보다 나쁜 설정은 고르지 않는다


def test_ab_make_and_grade():
    with tempfile.TemporaryDirectory() as t:
        t, table = Path(t), {}
        c = np.zeros(24)
        c[3] = 1
        sd, _ = _series(t, "w", _cluster(c, 20, 0.35), table)
        A = [_img(t / "A" / f"q{i:02d}.png", _unit(c + 0.3 * RNG.standard_normal(24)), table) for i in range(6)]
        B = [_img(t / "B" / f"q{i:02d}.png", _unit(RNG.standard_normal(24)), table) for i in range(6)]
        real = S.ROOT
        S.ROOT = t  # exports 를 임시 폴더에
        try:
            out = S.ab_make(sd, A, B, FakeEmb(table), n=6, seed=1)
        finally:
            S.ROOT = real
        key = json.loads((out / "key.json").read_text(encoding="utf-8"))
        assert len(key["pairs"]) == 6 and len(list(out.glob("pair_*.jpg"))) == 6
        truth = "".join("1" if p["left"] == "A" else "2" for p in key["pairs"])  # 사람이 늘 A(원작에 가까운 쪽)를 고른다면
        g = S.ab_grade(key, truth)
        assert g["prefer_a"] == 1.0 and g["agree"] >= 0.8  # 지표도 대부분 A 를 골라야 한다


def test_embedder_cache_reuses_vectors():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        f = _img(t / "x.png", np.ones(4), {})
        calls = []

        def fn(files):
            calls.append(len(files))
            return np.stack([_unit(np.arange(1, 5)) for _ in files])

        real = S.STATE
        S.STATE = t / "state"
        try:
            e = S.Embedder("t", fn)
            v1 = e([f])
            v2 = S.Embedder("t", fn)([f])  # 새 객체여도 파일 캐시에서
        finally:
            S.STATE = real
        assert calls == [1] and np.allclose(v1, v2)


def test_shot_kind_uses_same_kind_originals():
    # 원작 대부분은 인물 컷(한 무리), 사람 없는 컷은 소수(다른 무리). 사람 없는 새 컷은 사람 없는 원작들 기준으로 읽어야 헛경보가 없다
    with tempfile.TemporaryDirectory() as t:
        t, table = Path(t), {}
        a, b = np.zeros(24), np.zeros(24)
        a[0], b[0], b[1] = 1, 0.6, 0.8
        sd, files = _series(t, "w", _cluster(a, 30, 0.3) + _cluster(b, 10, 0.3), table)
        panels = [{"id": f.stem, "chars": ["x"] if i < 30 else []} for i, f in enumerate(files)]
        (sd / "episodes" / "ep001" / "panels.json").write_text(json.dumps({"panels": panels}), encoding="utf-8")
        new = sd / "episodes" / "ep001" / "panels_hf_vast"
        prop = _img(new / f"{files[33].stem}.png", _unit(b + 0.3 * RNG.standard_normal(24)), table)  # 사람 없는 컷을 다시 그린 것
        r = S.score(sd, [prop], FakeEmb(table), diag={})
        assert r["items"][0]["kind"] == "none" and r["items"][0]["pass"]
        assert S.kinds(sd, [files[0], files[35], t / "x" / "zz.png"]) == ["people", "none", None]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK  ", name)
