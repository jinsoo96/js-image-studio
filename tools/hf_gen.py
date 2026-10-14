"""힉스필드(Higgsfield) CLI 로 그림을 만들고 내려받는다. 크레딧을 아껴 쓰도록 실행 전 예상 비용을 찍고, 결과·프롬프트·비용을 로그에 남긴다.
CLI: D:\\.claude\\tools\\higgsfield\\hf.exe (로그인돼 있음, 개인 결제 계정이라 아껴 쓸 것). 기본 모델 nano_banana_pro(장당 2크레딧, 참고 그림 최대 14장).

    .venv/Scripts/python tools/hf_gen.py --out <폴더> --name <이름> --prompt-file <txt> [--ref a.png --ref b.png] [--aspect 16:9] [--n 2]
        [--model nano_banana_pro] [--resolution 2k] [--dry]
    결과: <폴더>/<이름>_v1.png … , <폴더>/hf_log.json 에 기록
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 다른 PC 에선 환경변수 HF_CLI 로 힉스필드 CLI 위치를 준다(이 PC 기본값은 아래)
HF = os.environ.get("HF_CLI") or r"D:\.claude\tools\higgsfield\hf.exe"


def run(args, tries=4):
    """힉스필드가 가끔 520·503 을 낸다(2026-10-13) → 잠깐 쉬고 다시."""
    for k in range(tries):
        r = subprocess.run([HF, *args], capture_output=True, text=True, encoding="utf-8")
        if not r.returncode:
            return r.stdout
        err = (r.stderr or r.stdout)[-800:]
        if k == tries - 1 or not any(c in err for c in ("520", "502", "503", "504", "timeout", "Timeout")):
            raise RuntimeError(err)
        time.sleep(15 * (k + 1))


UPLOAD_DIR = Path(__file__).resolve().parents[1] / "state" / "hf_uploads"
_account = None


def account():
    """지금 로그인한 힉스필드 계정(올린 그림 id 는 계정마다 다르다)."""
    global _account
    if _account is None:
        _account = run(["account", "status"]).strip().split()[0]
    return _account


def upload_id(path):
    """같은 파일은 한 번만 올리고 그 id 를 다시 쓴다(파일 내용 해시로 기억).
    기억은 계정마다 따로(`state/hf_uploads/<계정>.json`): 계정을 바꾸면 새 계정에 처음부터 다시 올린다(2026-10-13 사용자 "나중에 계정 바꿔서도 해야 함").
    그림·시트·장소는 전부 D: 에 있고 힉스필드 쪽 id 는 다시 만들 수 있는 캐시일 뿐이다."""
    import hashlib
    p = Path(path)
    if not p.exists():
        return str(path)  # 이미 id 이거나 URL
    h = hashlib.sha1(p.read_bytes()).hexdigest()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS = UPLOAD_DIR / f"{account()}.json"
    def load():  # 작업 두 개가 동시에 쓰는 순간 읽으면 반쪽 파일일 수 있다 → 잠깐 뒤 다시
        for _ in range(5):
            try:
                return json.loads(UPLOADS.read_text(encoding="utf-8")) if UPLOADS.exists() else {}
            except json.JSONDecodeError:
                time.sleep(0.5)
        return {}
    cache = load()
    if h not in cache:
        new_id = json.loads(run(["upload", "create", str(p), "--json"]))["id"]
        cache = load()
        cache[h] = new_id
        tmp = UPLOADS.with_suffix(f".{h[:8]}.tmp")
        tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        tmp.replace(UPLOADS)
    return cache[h]


def credits():
    out = run(["account", "status"])
    try:
        return float(out.strip().split(",")[-1].split()[0])
    except Exception:
        return out.strip()


def generate(model, prompt, refs, aspect, resolution, out_png, extra=()):
    args = ["generate", "create", model, "--prompt", prompt, "--aspect_ratio", aspect, "--wait", "--json"]
    for kv in extra:  # --param quality=high 같은 모델별 값
        k, v = kv.split("=", 1)
        args += [f"--{k}", v]
    if resolution:
        args += ["--resolution", resolution]
    for r in refs:
        args += ["--image-references", upload_id(r)]
    data = json.loads(run(args))
    job = data[0] if isinstance(data, list) else data
    url = job.get("result_url")
    if not url:
        raise RuntimeError(f"결과 없음: {json.dumps(job)[:500]}")
    req = urllib.request.Request(url, headers={"User-Agent": "webtoon-studio/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(out_png, "wb") as f:
        f.write(r.read())
    return job.get("id"), url


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--model", default="nano_banana_pro")
    ap.add_argument("--resolution", default="2k")
    ap.add_argument("--param", action="append", default=[], help="모델별 값 k=v (예: quality=high)")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true", help="이미 있는 결과도 다시")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    cost = run(["generate", "cost", args.model, "--prompt", "x", *(["--resolution", args.resolution] if args.resolution else [])]).strip()
    print(f"{args.name}: {args.n}장 x {cost}  (남은 크레딧 {credits()})", flush=True)
    if args.dry:
        return
    log_dir = out / "hf_log"  # 이름마다 한 파일(여러 개를 동시에 돌려도 서로 안 덮인다, 2026-10-13)
    log_dir.mkdir(exist_ok=True)
    for v in range(1, args.n + 1):
        name = f"{args.name}_v{v}" if args.n > 1 else args.name
        if (out / f"{name}.png").exists() and not args.force:
            print(f"  {name}.png 있음, 건너뜀", flush=True)
            continue
        t0 = time.time()
        job, url = generate(args.model, prompt, args.ref, args.aspect, args.resolution, out / f"{name}.png", args.param)
        rec = {"model": args.model, "aspect": args.aspect, "resolution": args.resolution, "refs": args.ref, "prompt": prompt,
               "job": job, "url": url, "seconds": round(time.time() - t0)}
        (log_dir / f"{name}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {name}.png ({round(time.time() - t0)}초)", flush=True)
    print(f"남은 크레딧 {credits()}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
