"""학습한 LoRA 를 켜 둔 GPU 서버와 이 PC 보관함(models/loras) 사이에서 맞춘다. 서버 종류를 모르는 쪽(MCP 의 학습 작업 끝, 사람)이 부른다.

    python tools/gpu_loras.py pull     서버에 있고 PC 에 없는 것을 내려받는다(학습 작업의 마지막 단계)
    python tools/gpu_loras.py push     PC 에 있고 서버에 없는 것을 올린다(켤 때는 서버 도구가 스스로 한다)
    python tools/gpu_loras.py status

state/gpu_cloud.json(켜기 버튼이 쓰는 것)의 서버 도구와 설정 중 그림(comfy) 역할인 것을 골라 그 도구의 `loras` 를 부른다.
학습한 LoRA 가 서버 디스크에만 남아 서버를 옮기며 사라졌다(2026-10-26 무대 뒤·D+18). 대상 파일은 series/*/lora/loras.json 이 정한다(작품 무관).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def comfy_profiles(tool: str, profiles: list) -> list:
    """그림(comfy) 역할인 설정만(설정의 roles, 없으면 둘 다). 설정 파일이 없으면 뺀다."""
    out = []
    for p in profiles:
        f = ROOT / "cloud" / f"{p}.json"
        try:
            roles = json.loads(f.read_text(encoding="utf-8")).get("roles") or ["comfy", "llm"]
        except (OSError, ValueError):
            continue
        if "comfy" in roles:
            out.append(p)
    return out


def main() -> int:
    direction = sys.argv[1] if len(sys.argv) > 1 else "status"
    if direction not in ("push", "pull", "status"):
        sys.exit(__doc__)
    try:
        cur = json.loads((ROOT / "state" / "gpu_cloud.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("켜 둔 GPU 서버 정보(state/gpu_cloud.json)가 없다: 서버 도구의 use 를 먼저", flush=True)
        return 2
    tool = Path(cur.get("tool", "")).name
    profiles = cur.get("profiles") or ([cur["profile"]] if cur.get("profile") else [])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if tool == "vast_cloud.py":
        picks = comfy_profiles(tool, profiles) or ([] if profiles else [""])
        if not picks:
            print(f"그림 역할 설정이 없다: {profiles}", flush=True)
            return 2
        rc = 0
        for p in picks:
            cmd = [sys.executable, "-u", str(ROOT / "tools" / "vast_cloud.py"), *(["--profile", p] if p else []), "loras", direction]
            rc = max(rc, subprocess.call(cmd, cwd=ROOT, env=env))
        return rc
    print(f"{tool or '(없음)'} 은 LoRA 맞추기를 아직 지원하지 않는다(서버의 models/loras 에서 손으로): 대상 파일은 series/*/lora/loras.json", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
