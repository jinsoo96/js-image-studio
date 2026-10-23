"""MCP 서버가 띄우는 백그라운드 작업 실행기. 작업 파일(json)의 단계를 차례로 돌리고 로그·상태를 남긴다.

    python mcp/job_runner.py state/mcp_jobs/<job_id>.json

멈춤 지킴이: 단계의 로그가 stall_min 분(단계에 적거나 환경변수 WEBTOON_STALL_MIN, 기본 30) 동안 한 줄도 안 늘면 그 단계의 프로세스 나무를 끊고
'멈춤'으로 실패 처리한다. RunPod 프록시를 지나는 요청 하나가 끝나지 않아 한 화 만들기가 로그 없이 몇십 분 서 있었다(2026-10-24 야근의 온도 p008):
읽기 시간 제한은 한 번 받기마다라, 조금씩 흘러오는 연결은 끝없이 기다린다. 다시 부르면 빠진 컷만 다시 그린다.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
job = Path(sys.argv[1])
spec = json.loads(job.read_text(encoding="utf-8"))
log = job.with_suffix(".log")
status_file = job.with_name(job.stem + ".status.json")
env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
STALL_MIN = float(os.environ.get("WEBTOON_STALL_MIN", "30"))


def status(**kw):
    cur = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    cur.update(kw, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
    status_file.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")


def kill_tree(pid: int):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        os.killpg(os.getpgid(pid), signal.SIGKILL)


def run_step(step, f) -> int:
    """단계를 돌리며 로그가 자라는지 본다. 끝난 코드를 돌려주고, 멈춰서 끊었으면 -9."""
    stall = float(step.get("stall_min") or STALL_MIN) * 60
    p = subprocess.Popen(step["cmd"], cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT,
                         **({"start_new_session": True} if os.name != "nt" else {}))
    last_size, last_change = -1, time.time()
    while p.poll() is None:
        time.sleep(10)
        size = log.stat().st_size if log.exists() else 0
        if size != last_size:
            last_size, last_change = size, time.time()
        elif time.time() - last_change > stall:
            kill_tree(p.pid)
            p.wait()
            f.write(f"\n멈춤: 로그가 {stall / 60:.0f}분 동안 한 줄도 안 늘어 이 단계를 끊었다(서버 응답이 끝나지 않는 요청일 수 있다). "
                    "같은 도구를 다시 부르면 빠진 것만 다시 한다\n")
            f.flush()
            return -9
    return p.returncode


steps = spec["steps"]
status(state="running", pid=os.getpid(), steps=len(steps), started=time.strftime("%Y-%m-%d %H:%M:%S"))
for i, step in enumerate(steps, 1):
    status(step=i, label=step["label"])
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n== [{i}/{len(steps)}] {step['label']} ({time.strftime('%H:%M:%S')})\n")
        f.flush()
        code = run_step(step, f)
    if code in step.get("review_codes", []):  # 실패가 아니라 사람 확인 대기(make_episode 종료 3 = 새 에셋 시안 확인)
        status(state="needs_review", stopped_step=i, returncode=code, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.exit(0)
    if code == -9:
        status(state="failed", failed_step=i, returncode=code, stalled=True, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.exit(1)
    if code != 0 and not step.get("allow_fail"):
        status(state="failed", failed_step=i, returncode=code)
        sys.exit(code)
status(state="done", finished=time.strftime("%Y-%m-%d %H:%M:%S"))
