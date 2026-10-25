"""GPU 서버 켜기 버튼(미사일 스튜디오 MCP 서버의 wake_command): 마지막으로 'use' 한 서버(state/gpu_cloud.json)를 깨운다.

    python -u tools/gpu_wake.py

서버를 바꿔 끼울 때 스튜디오 설정을 고치지 않게 하려고 둔다: vast 로 가려면 tools/vast_cloud.py use(또는 wake) 한 번,
RunPod 로 돌아가려면 tools/runpod_cloud.py use 한 번. 파일이 없으면 예전처럼 RunPod(tools/runpod_wake.py).
진행 줄('##progress ...')과 종료 코드는 부른 쪽 것을 그대로 넘긴다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        cur = json.loads((ROOT / "state" / "gpu_cloud.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    tool, profile = cur.get("tool", "tools/runpod_cloud.py"), cur.get("profile", "")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if Path(tool).name == "vast_cloud.py":
        cmd = [sys.executable, "-u", str(ROOT / "tools" / "vast_cloud.py"), *(["--profile", profile] if profile else []), "wake"]
    else:
        if profile:
            env["RUNPOD_PROFILE"] = profile
        cmd = [sys.executable, "-u", str(ROOT / "tools" / "runpod_wake.py")]
    print(f"켤 서버: {Path(tool).stem} {profile or '(기본)'}", flush=True)
    return subprocess.call(cmd, cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
