"""GPU 서버 켜기 버튼(미사일 스튜디오 MCP 서버의 wake_command): 마지막으로 'use' 한 서버(state/gpu_cloud.json)를 깨운다.

    python -u tools/gpu_wake.py

서버를 바꿔 끼울 때 스튜디오 설정을 고치지 않게 하려고 둔다: vast 로 가려면 tools/vast_cloud.py use(또는 wake) 한 번,
RunPod 로 돌아가려면 tools/runpod_cloud.py use 한 번. 파일이 없으면 예전처럼 RunPod(tools/runpod_wake.py).
vast 설정이 여럿이면("profiles": 그림 상자·LLM 상자) 함께 깨우고, 진행 줄('##progress 퍼센트 남은초 단계')은 퍼센트 평균·남은 초 최대로 합쳐
한 줄로 넘긴다. 나머지 줄은 '[설정] ' 을 붙여 그대로. 종료 코드는 실패한 것이 있으면 그것.
먼저 뜬 상자는 나머지가 다 뜰 때까지 붙든다(vast_cloud.py hold): 놀면 지우는 상자 안 지킴이가 다른 상자를 기다리는 시간을 '놂'으로 센다.
"""
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOLD_EVERY, HOLD_MIN = 240, 5  # 4분마다 5분씩 붙든다(겹치게). 다 뜨면 그만 붙들어 남는 건 마지막 몇 분


def run_merged(cmds: dict, env: dict, keep_alive=None, every: float = HOLD_EVERY) -> int:
    """이름→명령을 함께 돌리고 출력을 한 줄기로 합친다. keep_alive(이름)는 먼저 성공한 것을 나머지가 끝날 때까지 every 초마다 부른다."""
    prog, lock, codes = {n: (0, 0, "") for n in cmds}, threading.Lock(), {}

    def pump(name, cmd):
        p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        try:
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
        except Exception as e:  # noqa: BLE001  줄을 못 옮겨도 끝까지 받아 진짜 종료 코드를 남긴다(안 받으면 자식이 파이프에 막힌다)
            with contextlib.suppress(Exception):
                print(f"[{name}] 출력 옮기기 실패: {e!r}", flush=True)
            for _ in p.stdout:
                pass
        codes[name] = p.wait()

    threads = [threading.Thread(target=pump, args=(n, c), daemon=True) for n, c in cmds.items()]
    for t in threads:
        t.start()
    if keep_alive and len(cmds) > 1:
        # LLM 상자가 그림 상자를 기다리다 15분 '놂'으로 스스로 지워져 뒤 단계가 LLM 없이 돌았다(2026-10-27 이어짐 고치기 실서버 시험)
        held = {}
        while any(t.is_alive() for t in threads):
            for n in [n for n, c in list(codes.items()) if c == 0]:
                if time.time() - held.get(n, 0) >= every:
                    held[n] = time.time()
                    with contextlib.suppress(Exception):
                        keep_alive(n)
            time.sleep(min(5, every))
    for t in threads:
        t.join()
    # 코드를 못 남긴 설정은 실패로 친다: 출력 줄 하나(cp949 로 못 쓰는 글자)에 스레드가 죽어 LLM 상자 실패가 빠지고 0 이 나갔다
    # (2026-10-27 vast_llm 'SSH 호스트 키 없음'으로 지워졌는데 켜기 버튼은 성공, 뒤 이어짐 검사가 판정 모델 없이 '문제 없음')
    return max((codes.get(n, 1) for n in cmds), default=1)


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

        def hold(p):
            r = subprocess.run([sys.executable, vast, "--profile", p, "hold", str(HOLD_MIN)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=60)
            print(f"[{p}] 다른 상자가 뜰 때까지 붙듦({HOLD_MIN}분)" if r.returncode == 0 else f"[{p}] 붙들기 실패: {r.stdout.decode('utf-8', 'replace')[-200:]}",
                  flush=True)
        return run_merged(cmds, env, keep_alive=hold)
    if profiles:
        env["RUNPOD_PROFILE"] = profiles[0]
    print(f"켤 서버: runpod {profiles[0] if profiles else '(기본)'}", flush=True)
    return subprocess.call([sys.executable, "-u", str(ROOT / "tools" / "runpod_wake.py")], cwd=ROOT, env=env)


if __name__ == "__main__":
    with contextlib.suppress(Exception):  # 넘겨받는 줄은 utf-8(자식에 PYTHONIOENCODING): 콘솔이 cp949 여도 못 쓰는 글자에 죽지 않게
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.exit(main())
