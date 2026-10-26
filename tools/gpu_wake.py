"""GPU 서버 켜기 버튼(미사일 스튜디오 MCP 서버의 wake_command): 마지막으로 'use' 한 서버(state/gpu_cloud.json)를 깨운다.

    python -u tools/gpu_wake.py

서버를 바꿔 끼울 때 스튜디오 설정을 고치지 않게 하려고 둔다: vast 로 가려면 tools/vast_cloud.py use(또는 wake) 한 번,
RunPod 로 돌아가려면 tools/runpod_cloud.py use 한 번. 파일이 없으면 예전처럼 RunPod(tools/runpod_wake.py).
vast 설정이 여럿이면("profiles": 그림 상자·LLM 상자) 함께 깨우고, 진행 줄('##progress 퍼센트 남은초 단계')은 퍼센트 평균·남은 초 최대로 합쳐
한 줄로 넘긴다. 나머지 줄은 '[설정] ' 을 붙여 그대로. 종료 코드는 실패한 것이 있으면 그것.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_merged(cmds: dict, env: dict) -> int:
    """이름→명령을 함께 돌리고 출력을 한 줄기로 합친다."""
    prog, lock, codes = {n: (0, 0, "") for n in cmds}, threading.Lock(), {}

    def pump(name, cmd):
        p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        for line in p.stdout:
            line = line.rstrip("\r\n")
            with lock:
                if line.startswith("##progress "):
                    parts = line.split(" ", 3)
                    try:
                        prog[name] = (int(parts[1]), int(parts[2]), parts[3] if len(parts) > 3 else "")
                    except (ValueError, IndexError):
                        pass
                    pct = sum(v[0] for v in prog.values()) / len(prog)
                    eta = max(v[1] for v in prog.values())
                    stage = " · ".join(f"{n}: {v[2]}" if len(prog) > 1 else v[2] for n, v in prog.items() if v[2])
                    print(f"##progress {int(pct)} {eta} {stage}", flush=True)
                else:
                    print(f"[{name}] {line}" if len(cmds) > 1 else line, flush=True)
        codes[name] = p.wait()

    threads = [threading.Thread(target=pump, args=(n, c), daemon=True) for n, c in cmds.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return max(codes.values(), default=1)


def main() -> int:
    try:
        cur = json.loads((ROOT / "state" / "gpu_cloud.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    tool = cur.get("tool", "tools/runpod_cloud.py")
    profiles = cur.get("profiles") or ([cur["profile"]] if cur.get("profile") else [])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if Path(tool).name == "vast_cloud.py":
        print(f"켤 서버: vast {' + '.join(profiles) or '(기본)'}", flush=True)
        vast = str(ROOT / "tools" / "vast_cloud.py")
        cmds = {p: [sys.executable, "-u", vast, "--profile", p, "wake"] for p in profiles} or {"vast": [sys.executable, "-u", vast, "wake"]}
        return run_merged(cmds, env)
    if profiles:
        env["RUNPOD_PROFILE"] = profiles[0]
    print(f"켤 서버: runpod {profiles[0] if profiles else '(기본)'}", flush=True)
    return subprocess.call([sys.executable, "-u", str(ROOT / "tools" / "runpod_wake.py")], cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
