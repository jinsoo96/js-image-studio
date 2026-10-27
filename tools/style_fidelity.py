"""그림체 닮음: 새로 그린 컷이 그 작품 원작 컷의 그림체를 얼마나 닮았는지 잰다(CSD 그림체 특징, 이 PC 의 CPU).
(2026-10-27 사용자 "결과 그림이 원작 그림체를 닮았는지 재는 로직을 먼저". 조사·설계 worklogs/2026-10-27-style-fidelity-research.md)

    python tools/style_fidelity.py diagnose [--per 80]                         작품마다 '같은 작품끼리 vs 다른 작품끼리' 구별 진단 → state/style/diagnosis.json
    python tools/style_fidelity.py score <작품 폴더> <그림·폴더·글로브 …> [--floor 10] [--json-out f] [--review-exit]
    python tools/style_fidelity.py compare <작품 폴더> <이름=폴더> … [--floor 10]   설정끼리(LoRA 끔·켬 등) 묶음 비교
    python tools/style_fidelity.py ab make <작품 폴더> <A 폴더> <B 폴더> [--n 20]   사람 기준점: 어느 쪽이 원작 같은지 고르는 그림
    python tools/style_fidelity.py ab grade <key.json> <답(예 1221…)>             사람 답과 지표가 같은 쪽을 고른 비율

읽는 법(작품 이름·작품별 문턱 숫자를 박지 않는다):
- 원작 = 그 작품 회차 폴더들의 본편 그림(panels_hf). 점수 = 가장 닮은 원작 K 장과의 평균 닮음.
- 날 코사인은 절대 점수가 아니다(arXiv 2605.09030) → '원작끼리 닮음 분포(원작 하나하나를 자기 자신·같은 컷 번호를 빼고 잰 점수)의 몇 백분위'(pct)로 읽는다.
  floor 10 = 원작 10장 중 가장 덜 닮은 1장만큼은 닮아야 통과.
- 같은 컷 번호의 원작과는 견주지 않는다: 내용이 같아 그림체가 아니라 내용이 닮아 점수가 오른다.
- 진단에서 그 작품을 다른 작품과 구별하지 못하면(구별 간격의 부트스트랩 하한 ≤ 0, 날 점수·CSLS 둘 다) trusted=false: 그 작품에선 이 점수로 판정하지 않는다.
"""
import argparse
import glob
import hashlib
import json
import random
import sys
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "style"
CSD_DIR = ROOT / "models" / "style" / "csd"
K = 5            # 점수 = 가장 닮은 원작 K 장의 평균 닮음
FLOOR = 10.0     # 기본 통과선(백분위). 작품마다 그 작품 원작 분포로 읽으므로 작품별 숫자가 아니다
DUP_COS = 0.995  # 사실상 같은 그림(재사용 컷을 다른 파일로 둔 것)은 이웃에서 뺀다
BOOT = 300       # 부트스트랩 반복 수
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


# ── 원작·그림 목록 ──────────────────────────────────────────────────────────────

def bank(series: Path) -> list:
    """원작 컷: 회차 폴더들의 본편 그림(episodes/*/panels_hf/*.png). _rejected 같은 하위 폴더는 넣지 않는다."""
    return sorted(series.glob("episodes/*/panels_hf/*.png"))


def kinds(series: Path, files: list) -> list:
    """그림마다 컷 종류: 'people'(인물이 있는 컷) · 'none'(사람 없는 소품·배경 컷) · None(모름). 그림이 든 회차의 panels.json 의 chars 로,
    회차를 모르면(내보낸 사본 등) 그 작품 모든 회차에서 같은 컷 번호로 찾는다."""
    def table(pj):
        d = json.loads(pj.read_text(encoding="utf-8"))
        return {p["id"]: ("people" if p.get("chars") else "none") for p in (d["panels"] if isinstance(d, dict) else d)}
    every = {}
    for pj in sorted(series.glob("episodes/*/panels.json")):
        for pid, kd in table(pj).items():
            every.setdefault(pid, kd)
    out, cache = [], {}
    for f in files:
        pj = Path(f).parents[1] / "panels.json"
        if pj.exists() and pj not in cache:
            cache[pj] = table(pj)
        out.append((cache.get(pj) or {}).get(Path(f).stem) or every.get(Path(f).stem))
    return out


def expand(args) -> list:
    """그림 파일·폴더·글로브(작업실 기준 또는 절대 경로)를 그림 목록으로."""
    out = []
    for a in args:
        p = Path(a) if Path(a).is_absolute() else ROOT / a
        if any(ch in a for ch in "*?["):
            out += sorted(Path(x) for x in glob.glob(str(p)) if Path(x).suffix.lower() in IMG_EXT)
        elif p.is_dir():
            out += sorted(x for x in p.iterdir() if x.suffix.lower() in IMG_EXT)
        elif p.exists():
            out.append(p)
    return out


def series_dirs() -> list:
    return sorted(d for d in (ROOT / "series").iterdir() if (d / "episodes").is_dir() and not d.name.startswith("_"))


def _rel(f: Path) -> str:
    try:
        return f.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(f)


# ── 특징 ────────────────────────────────────────────────────────────────────────

class Embedder:
    """그림 → L2 정규화된 특징. 파일(경로·크기·수정 시각)별로 state/style/<이름>.npz 에 캐시한다(원작 수백 장을 매번 다시 뽑지 않게)."""

    def __init__(self, name: str, fn):
        self.name, self.fn = name, fn
        self.path = STATE / f"{name}.npz"
        self.cache = dict(np.load(self.path)) if self.path.exists() else {}

    @staticmethod
    def key(f: Path) -> str:
        st = f.stat()
        return hashlib.sha1(f"{f.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()

    def __call__(self, files: list) -> np.ndarray:
        keys = [self.key(f) for f in files]
        miss = [f for f, k in zip(files, keys) if k not in self.cache]
        if miss:
            for f, v in zip(miss, self.fn(miss)):
                self.cache[self.key(f)] = np.asarray(v, np.float32)
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.stem + ".tmp.npz")
            np.savez(tmp, **self.cache)
            tmp.replace(self.path)
        return np.stack([self.cache[k] for k in keys]) if files else np.zeros((0, 0), np.float32)


def _csd_weights():
    """CSD 가중치(그림체에 필요한 텐서만). 받은 파일(tomg-group-umd/CSD-ViT-L pytorch_model.bin)은 옵티마이저·설정값까지 든 학습 체크포인트(2.4GB)라
    처음 한 번 텐서만 csd_style.pt(반정밀도 0.6GB)로 옮긴다. 그때도 설정값 형식(argparse.Namespace·numpy 스칼라)만 허용해, 코드를 실행하는 피클은 막힌 채 읽는다."""
    import torch
    small = CSD_DIR / "csd_style.pt"
    if not small.exists():
        big = CSD_DIR / "pytorch_model.bin"
        if not big.exists():
            raise SystemExit(f"CSD 가중치가 없다: {big} (https://huggingface.co/tomg-group-umd/CSD-ViT-L 의 pytorch_model.bin 을 두면 처음 한 번 변환한다)")
        import argparse as _argparse
        import codecs
        from numpy._core import multiarray
        allow = [_argparse.Namespace, codecs.encode, (multiarray.scalar, "numpy.core.multiarray.scalar"), (np.dtype, "numpy.dtype"),
                 type(np.dtype("float64"))]
        with torch.serialization.safe_globals(allow):
            ck = torch.load(big, map_location="cpu", weights_only=True)
        sd = {k.removeprefix("module."): v.half() for k, v in ck["model_state_dict"].items() if not k.endswith("last_layer_content")}
        torch.save(sd, small)
    return {k: v.float() for k, v in torch.load(small, map_location="cpu", weights_only=True).items()}


def csd(crops: int = 1) -> Embedder:
    """CSD(Somepalli 외, ECCV 2024): OpenAI CLIP ViT-L/14 의 그림 쪽 + 그림체 투영(1024→768). 원 코드의 clip 패키지 대신 같은 구조를 여기서 짓고
    가중치를 strict 로 읽는다(층 이름이 하나라도 다르면 멈춤). 입력은 원 코드와 같은 224 bicubic 중앙 자르기·CLIP 정규화.
    crops=3 이면 긴 쪽 양끝 두 장을 더 잘라 평균(세로로 긴 컷의 위아래 그림체까지)."""
    import torch
    from torch import nn

    class QuickGELU(nn.Module):
        def forward(self, x):
            return x * torch.sigmoid(1.702 * x)

    class Block(nn.Module):
        def __init__(self, d, heads):
            super().__init__()
            self.ln_1, self.attn, self.ln_2 = nn.LayerNorm(d), nn.MultiheadAttention(d, heads), nn.LayerNorm(d)
            self.mlp = nn.Sequential(OrderedDict(c_fc=nn.Linear(d, 4 * d), gelu=QuickGELU(), c_proj=nn.Linear(4 * d, d)))

        def forward(self, x):
            y = self.ln_1(x)
            x = x + self.attn(y, y, y, need_weights=False)[0]
            return x + self.mlp(self.ln_2(x))

    class Visual(nn.Module):
        def __init__(self, d=1024, layers=24, heads=16, patch=14, res=224):
            super().__init__()
            self.conv1 = nn.Conv2d(3, d, patch, patch, bias=False)
            self.class_embedding = nn.Parameter(torch.zeros(d))
            self.positional_embedding = nn.Parameter(torch.zeros((res // patch) ** 2 + 1, d))
            self.ln_pre, self.ln_post = nn.LayerNorm(d), nn.LayerNorm(d)
            self.transformer = nn.Module()
            self.transformer.resblocks = nn.Sequential(*[Block(d, heads) for _ in range(layers)])

        def forward(self, x):
            x = self.conv1(x).flatten(2).transpose(1, 2)
            x = torch.cat([self.class_embedding.expand(x.shape[0], 1, -1), x], 1) + self.positional_embedding
            x = self.transformer.resblocks(self.ln_pre(x).transpose(0, 1)).transpose(0, 1)
            return self.ln_post(x[:, 0])

    model = nn.Module()
    model.backbone = Visual()
    model.last_layer_style = nn.Parameter(torch.zeros(1024, 768))
    model.load_state_dict(_csd_weights(), strict=True)
    model.eval()
    mean = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)

    def views(f):
        im = Image.open(f).convert("RGB")
        w, h = im.size
        s = 224 / min(w, h)
        im = im.resize((max(224, int(w * s)), max(224, int(h * s))), Image.BICUBIC)
        W, H = im.size
        offs = [((W - 224) // 2, (H - 224) // 2)] + ([(0, 0), (W - 224, H - 224)] if crops > 1 and W != H else [])
        return [((np.asarray(im.crop((x, y, x + 224, y + 224)), np.float32) / 255 - mean) / std).transpose(2, 0, 1) for x, y in offs]

    def embed(files):
        vecs = []
        with torch.inference_mode():
            for i in range(0, len(files), 8):
                vs = [views(f) for f in files[i:i + 8]]
                y = nn.functional.normalize(model.backbone(torch.from_numpy(np.stack([v for vv in vs for v in vv]))) @ model.last_layer_style,
                                            dim=1).numpy()
                j = 0
                for vv in vs:
                    m = y[j:j + len(vv)].mean(0)
                    vecs.append(m / np.linalg.norm(m))
                    j += len(vv)
                if len(files) > 16 and (i // 8) % 5 == 4:
                    print(f"  그림체 특징 {min(i + 8, len(files))}/{len(files)}", flush=True)
        return np.stack(vecs)

    return Embedder("csd" if crops == 1 else f"csd_c{crops}", embed)


# ── 점수 계산(특징만 다루는 순수 함수: 모델 없이 시험한다) ─────────────────────────────

def _masked(Q, D, qn=None, dn=None):
    """Q·D 코사인. 사실상 같은 그림(자기 자신 포함)과 같은 컷 번호는 -inf(이웃에서 뺌)."""
    S = Q @ D.T
    bad = S >= DUP_COS
    if qn is not None and dn is not None:
        bad |= np.asarray(qn)[:, None] == np.asarray(dn)[None, :]
    return np.where(bad, -np.inf, S)


def _topk(S, k=K):
    """행마다 가장 닮은 k 개의 평균(뺀 칸은 세지 않는다). 남은 칸이 없으면 nan."""
    if S.shape[1] == 0:
        return np.full(S.shape[0], np.nan)
    k = min(k, S.shape[1])
    top = -np.partition(-S, k - 1, axis=1)[:, :k]
    ok = np.isfinite(top)
    return np.where(ok.any(1), np.where(ok, top, 0).sum(1) / np.maximum(ok.sum(1), 1), np.nan)


def _hub(D, dn, k=K):
    """원작마다 이웃이 얼마나 붐비나(다른 원작들과의 상위 k 평균). CSLS 에서 빼서, 모든 그림과 두루 닮은 '허브' 원작 덕에 점수가 부푸는 것을 막는다."""
    return _topk(_masked(D, D, dn, dn), k)


def scores(Q, qn, D, dn, mode="raw", hub=None, qhub=None, k=K):
    """질의 그림마다 원작 묶음 D 에 대한 점수. raw = 상위 k 평균 코사인.
    csls = 상위 k 평균(2·cos − 원작 허브) − 질의 허브(Conneau 외 2018, 그림체 점수 보정은 arXiv 2605.09030). qhub 를 주지 않으면 D 에 대한 질의의 상위 k 평균."""
    S = _masked(Q, D, qn, dn)
    raw = _topk(S, k)
    if mode != "csls":
        return raw
    hub = _hub(D, dn, k) if hub is None else hub
    return _topk(np.where(np.isfinite(S), 2 * S - hub[None, :], -np.inf), k) - (raw if qhub is None else qhub)


def percentile(x, dist) -> float:
    """x 가 분포 dist 의 몇 백분위인가(dist 중 x 이하의 비율 × 100)."""
    d = np.asarray(dist, float)
    d = d[np.isfinite(d)]
    return float(100 * np.mean(d <= x)) if len(d) and np.isfinite(x) else float("nan")


def bootstrap(vals, stat=np.mean, n=BOOT, seed=0):
    """stat 의 90% 부트스트랩 구간 (하한, 상한). 값이 둘 미만이면 (nan, nan)."""
    v = np.asarray([x for x in vals if np.isfinite(x)], float)
    if len(v) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    bs = [stat(rng.choice(v, len(v))) for _ in range(n)]
    return float(np.percentile(bs, 5)), float(np.percentile(bs, 95))


def diagnose(groups: dict, k=K) -> dict:
    """groups = {작품: (특징 E, 컷 번호 목록)}. 그 점수로 작품 s 를 다른 작품과 구별하는가를 양쪽에서 본다:
    ① s 의 원작이 다른 어느 작품 t 보다 s 를 더 닮았나(간격 s→t) ② t 의 원작도 s 보다 자기 작품을 더 닮았나(간격 t→t).
    모든 t 에 대해 두 간격의 평균이 90% 부트스트랩 하한 > 0 이면 구별한다(raw 먼저, 안 되면 csls). 둘 다 안 되면 trusted=false.
    ①만 보면 원작이 유난히 촘촘한 작품이 '구별된다'로 나왔다(같은 분포에서 뽑은 두 작품 시험, 2026-10-27): 촘촘한 묶음은 남의 원작까지 끌어당긴다."""
    names = list(groups)
    if len(names) < 2:
        return {s: {"n": len(groups[s][1]), "mode": None, "trusted": None, "why": "견줄 다른 작품이 없다"} for s in names}
    allE = np.concatenate([groups[s][0] for s in names])
    alln = [f"{s}/{x}" for s in names for x in groups[s][1]]  # 작품이 다르면 같은 컷 번호라도 다른 컷
    pooled = _hub(allE, alln, k)
    span, at = {}, 0
    for s in names:
        span[s] = slice(at, at + len(groups[s][1]))
        at += len(groups[s][1])
    out = {s: {"n": len(groups[s][1])} for s in names}
    for mode in ("raw", "csls"):
        M = {}
        for s in names:
            q = pooled[span[s]] if mode == "csls" else None
            for t in names:
                same = s == t
                M[s, t] = scores(groups[s][0], groups[s][1] if same else None, groups[t][0], groups[t][1] if same else None,
                                 mode, pooled[span[t]], q, k)
        for s in names:
            gap = M[s, s] - np.max([M[s, t] for t in names if t != s], axis=0)
            lo, hi = bootstrap(gap)
            # 가장 헷갈리는 상대: 양쪽 간격 하한 중 가장 낮은 것
            pair = {t: min(bootstrap(M[s, s] - M[s, t])[0], bootstrap(M[t, t] - M[t, s])[0]) for t in names if t != s}
            worst = min(pair, key=pair.get)
            out[s][mode] = {"gap": round(float(np.nanmean(gap)), 4), "lo": round(lo, 4), "hi": round(hi, 4),
                            "acc": round(float(np.nanmean(gap > 0)), 3), "worst": worst, "worst_lo": round(pair[worst], 4),
                            "ok": bool(pair[worst] > 0)}
    for s in names:
        r = out[s]
        r["mode"] = "raw" if r["raw"]["ok"] else "csls" if r["csls"]["ok"] else None
        r["trusted"] = r["mode"] is not None
    return out


# ── 점수·비교·사람 기준점 ────────────────────────────────────────────────────────────

def load_diag() -> dict:
    p = STATE / "diagnosis.json"
    return json.loads(p.read_text(encoding="utf-8")).get("series", {}) if p.exists() else {}


def score(series: Path, files: list, emb, floor=FLOOR, k=K, diag=None) -> dict:
    """그림마다 그 작품 원작 대비 그림체 백분위. 원작 분포 = 원작 하나하나를 자기 자신·같은 컷 번호를 빼고 잰 점수.
    (처음엔 LoRA 학습에 쓴 원작을 분포에서 뺐더니 무대 뒤는 131장 중 10장만 남아 백분위가 10 단위로 뭉개졌다, 2026-10-27.
    원작끼리의 닮음은 학습 여부와 상관없다. 학습·검증을 나눌 곳은 LoRA 를 채점할 컷을 고르는 쪽이다: vast/lora_eval.py held_out)
    컷 종류(인물 있음·없음)를 알면 같은 종류 원작의 분포로 읽는다: 원작 대부분이 인물 컷이라 사람 없는 소품 컷은 원작 p095 조차 4백분위로
    걸렸다(2026-10-27 무대 뒤). 같은 종류 원작이 K+2 장보다 적으면 전체 분포로."""
    refs = bank(series)
    if len(refs) < k + 2:
        return {"series": series.name, "error": f"원작 컷이 {len(refs)}장뿐이라 기준을 세울 수 없다(본편 panels_hf 가 {k + 2}장 이상 필요)"}
    E, rn = emb(refs), [f.stem for f in refs]
    d = (load_diag() if diag is None else diag).get(series.name) or {}
    mode = d.get("mode") or "raw"
    hub = _hub(E, rn, k) if mode == "csls" else None
    dist = scores(E, rn, E, rn, mode, hub, None, k)
    rk = np.array(kinds(series, refs), dtype=object)
    items = []
    if files:
        Q = emb(files)
        for f, kd, s in zip(files, kinds(series, files), scores(Q, [f.stem for f in files], E, rn, mode, hub, None, k)):
            same = dist[rk == kd] if kd else dist
            p = percentile(s, same if len(same) >= k + 2 else dist)
            items.append({"file": _rel(f), "kind": kd, "pct": round(p, 1), "score": round(float(s), 4), "pass": bool(np.isfinite(p) and p >= floor)})
    trusted = d.get("trusted")
    note = ("진단 전: style_fidelity.py diagnose 를 먼저" if trusted is None else
            "" if trusted else "진단에서 이 작품을 다른 작품과 구별하지 못했다: 이 점수로 판정하지 않는다")
    return {"series": series.name, "mode": mode, "trusted": trusted, "floor": floor, "refs": len(refs),
            "dist_median": round(float(np.nanmedian(dist)), 4), "note": note, "items": items}


def compare(series: Path, groups: dict, emb, floor=FLOOR) -> dict:
    """설정끼리 묶음 비교. 묶음마다 백분위 중앙값·90% 구간·통과율, 첫 묶음 대비 같은 컷끼리 차이의 중앙값·구간.
    best = 첫 묶음보다 확실히 나은(차이 구간 하한 > 0) 묶음 중 중앙값이 가장 높은 것(없으면 None)."""
    res, rows = {}, []
    for name, files in groups.items():
        r = score(series, files, emb, floor)
        if r.get("error"):
            return r
        res[name] = {Path(it["file"]).stem: it["pct"] for it in r["items"]}
    base = next(iter(res))
    best, best_med = None, -1.0
    for name, pcts in res.items():
        vals = list(pcts.values())
        med = float(np.nanmedian(vals)) if vals else float("nan")
        lo, hi = bootstrap(vals, np.median)
        row = {"group": name, "n": len(vals), "median": round(med, 1), "lo": round(lo, 1), "hi": round(hi, 1),
               "pass_rate": round(float(np.mean([v >= floor for v in vals])), 2) if vals else None}
        if name != base:
            common = sorted(set(pcts) & set(res[base]))
            diff = [pcts[c] - res[base][c] for c in common]
            dlo, dhi = bootstrap(diff, np.median)
            row.update({"vs_base": round(float(np.median(diff)), 1) if diff else None, "vs_lo": round(dlo, 1), "vs_hi": round(dhi, 1), "paired": len(common)})
            if diff and dlo > 0 and med > best_med:
                best, best_med = name, med
        rows.append(row)
    return {"series": series.name, "base": base, "floor": floor, "rows": rows, "best": best, "trusted": score(series, [], emb)["trusted"]}


def ab_make(series: Path, a: list, b: list, emb, n=20, seed=0) -> Path:
    """사람 기준점 그림: 위 줄 = 이 작품 원작 두 장(같은 컷 번호는 피함), 아래 줄 = A·B 를 무작위 좌우로. key.json 에 정답 쪽과 지표가 고른 쪽."""
    pa, pb = {f.stem: f for f in a}, {f.stem: f for f in b}
    common = sorted(set(pa) & set(pb))
    if not common:
        raise SystemExit("A·B 에 같은 이름 그림이 없다")
    rnd = random.Random(seed)
    pick = rnd.sample(common, min(n, len(common)))
    sa = {Path(it["file"]).stem: it for it in score(series, [pa[s] for s in pick], emb)["items"]}
    sb = {Path(it["file"]).stem: it for it in score(series, [pb[s] for s in pick], emb)["items"]}
    refs = bank(series)
    out = ROOT / "exports" / time.strftime("%Y-%m-%d") / "그림체AB" / f"{series.name}_{time.strftime('%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\malgunbd.ttf", 26)
    except OSError:
        font = ImageFont.load_default()

    def tile(f, label, w=560):
        im = Image.open(f).convert("RGB")
        im.thumbnail((w, w))
        cell = Image.new("RGB", (w, w + 40), "white")
        cell.paste(im, ((w - im.width) // 2, 40))
        ImageDraw.Draw(cell).text((8, 4), label, fill="black", font=font)
        return cell

    pairs = []
    for i, s in enumerate(pick, 1):
        left_a = rnd.random() < 0.5
        lf, rf = (pa[s], pb[s]) if left_a else (pb[s], pa[s])
        lp, rp = (sa[s]["pct"], sb[s]["pct"]) if left_a else (sb[s]["pct"], sa[s]["pct"])
        anchors = rnd.sample([f for f in refs if f.stem != s], 2)
        cells = [tile(anchors[0], "원작 예시"), tile(anchors[1], "원작 예시"), tile(lf, "1"), tile(rf, "2")]
        sheet = Image.new("RGB", (2 * 564, 2 * 604), (90, 90, 90))
        for j, c in enumerate(cells):
            sheet.paste(c, ((j % 2) * 564, (j // 2) * 604))
        sheet.save(out / f"pair_{i:02d}.jpg", quality=88)
        pairs.append({"n": i, "panel": s, "left": "A" if left_a else "B", "metric": "1" if lp >= rp else "2", "pct_left": lp, "pct_right": rp})
    (out / "key.json").write_text(json.dumps({"series": series.name, "a": _rel(a[0].parent), "b": _rel(b[0].parent), "pairs": pairs},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def ab_grade(key: dict, answers: str) -> dict:
    """answers = 쌍마다 사람이 고른 '1'·'2'(원작 그림체에 더 가까운 쪽). 지표 일치율과 A 를 고른 비율."""
    ans = [c for c in answers if c in "12"]
    pairs = key["pairs"][:len(ans)]
    if not pairs:
        raise SystemExit("답이 없다: 쌍마다 1 또는 2")
    agree = [p["metric"] == c for p, c in zip(pairs, ans)]
    chose_a = [(c == "1") == (p["left"] == "A") for p, c in zip(pairs, ans)]
    return {"pairs": len(pairs), "agree": round(sum(agree) / len(pairs), 3), "prefer_a": round(sum(chose_a) / len(pairs), 3)}


# ── 명령 ────────────────────────────────────────────────────────────────────────

def _print_score(r: dict) -> None:
    if r.get("error"):
        print(r["error"])
        return
    flag = {True: "구별됨", False: "구별 못 함", None: "진단 전"}[r["trusted"]]
    print(f"{r['series']}: 원작 {r['refs']}장 · 점수 방식 {r['mode']} · 진단 {flag} · 통과선 {r['floor']:g}백분위")
    for it in r["items"]:
        print(f"  {Path(it['file']).name}: {it['pct']:.0f}백분위 {'통과' if it['pass'] else '걸림'}  ({it['file']})")
    if r["note"]:
        print("  " + r["note"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose")
    d.add_argument("--per", type=int, default=80, help="작품마다 원작 몇 장으로(고르게 뽑음)")
    s = sub.add_parser("score")
    s.add_argument("series")
    s.add_argument("paths", nargs="+")
    s.add_argument("--floor", type=float, default=FLOOR)
    s.add_argument("--json-out")
    s.add_argument("--review-exit", action="store_true", help="진단에서 구별된 작품인데 걸린 그림이 있으면 종료 코드 2(작업은 needs_review)")
    c = sub.add_parser("compare")
    c.add_argument("series")
    c.add_argument("groups", nargs="+", help="이름=폴더(첫 묶음이 기준, 예 끔=…/panels_hf_off)")
    c.add_argument("--floor", type=float, default=FLOOR)
    ab = sub.add_parser("ab")
    ab.add_argument("action", choices=["make", "grade"])
    ab.add_argument("args", nargs="+")
    ab.add_argument("--n", type=int, default=20)
    for p in (d, s, c, ab):
        p.add_argument("--crops", type=int, default=1, help="1 = 중앙만(원 논문), 3 = 긴 쪽 양끝도")
    a = ap.parse_args()

    if a.cmd == "ab" and a.action == "grade":
        key = json.loads(Path(a.args[0]).read_text(encoding="utf-8"))
        print(json.dumps(ab_grade(key, "".join(a.args[1:])), ensure_ascii=False))
        return
    emb = csd(a.crops)
    if a.cmd == "diagnose":
        groups = {}
        for sd in series_dirs():
            refs = bank(sd)
            if len(refs) >= K + 2:
                step = max(1, len(refs) // a.per)
                pick = refs[::step][:a.per]
                groups[sd.name] = (emb(pick), [f.stem for f in pick])
                print(f"{sd.name}: 원작 {len(refs)}장 중 {len(pick)}장", flush=True)
        res = diagnose(groups)
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "diagnosis.json").write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"), "backend": emb.name, "k": K, "series": res},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
        for name, r in res.items():
            if "raw" in r:
                line = " · ".join(f"{m} 간격 {r[m]['gap']:+.3f} [{r[m]['lo']:+.3f}, {r[m]['hi']:+.3f}] 맞힘 {r[m]['acc']:.0%} "
                                  f"(가장 헷갈리는 {r[m]['worst']} {r[m]['worst_lo']:+.3f})" for m in ("raw", "csls"))
                print(f"{name}: {line} → {r['mode'] or '구별 못 함'}")
        print(f"→ {STATE / 'diagnosis.json'}")
        return
    series = Path(a.series) if Path(a.series).is_absolute() else ROOT / a.series
    if a.cmd == "score":
        r = score(series, expand(a.paths), emb, a.floor)
        _print_score(r)
        if a.json_out:
            Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.json_out).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        if a.review_exit and r.get("trusted") and any(not it["pass"] for it in r.get("items", [])):
            sys.exit(2)
        return
    if a.cmd == "compare":
        groups = {g.split("=", 1)[0]: expand([g.split("=", 1)[1]]) for g in a.groups}
        r = compare(series, groups, emb, a.floor)
        if r.get("error"):
            sys.exit(r["error"])
        for row in r["rows"]:
            extra = f" · {r['base']} 대비 같은 컷 차이 {row['vs_base']:+.0f} [{row['vs_lo']:+.0f}, {row['vs_hi']:+.0f}]" if "vs_base" in row and row["vs_base"] is not None else ""
            print(f"{row['group']}: {row['n']}장 중앙값 {row['median']:.0f}백분위 [{row['lo']:.0f}, {row['hi']:.0f}] 통과 {row['pass_rate']:.0%}{extra}")
        print(f"가장 나은 설정: {r['best'] or '없음(기준보다 확실히 나은 설정이 없다)'}" + ("" if r["trusted"] else " · 진단에서 구별 못 한 작품이라 참고만"))
        return
    if a.action == "make":
        out = ab_make(series, expand([a.args[0]]), expand([a.args[1]]), emb, a.n)
        print(f"{out} (쌍마다 원작 그림체에 더 가까운 쪽을 1·2 로 → ab grade {out / 'key.json'} 1221…)")


if __name__ == "__main__":
    main()
