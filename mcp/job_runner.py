"""MCP 서버가 띄우는 백그라운드 작업 실행기. 작업 파일(json)의 단계를 차례로 돌리고 로그·상태를 남긴다.

    python mcp/job_runner.py state/mcp_jobs/<job_id>.json
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


def status(**kw):
    cur = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    cur.update(kw, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
    status_file.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")


steps = spec["steps"]
status(state="running", pid=os.getpid(), steps=len(steps), started=time.strftime("%Y-%m-%d %H:%M:%S"))
for i, step in enumerate(steps, 1):
    status(step=i, label=step["label"])
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n== [{i}/{len(steps)}] {step['label']} ({time.strftime('%H:%M:%S')})\n")
        f.flush()
        p = subprocess.run(step["cmd"], cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    if p.returncode in step.get("review_codes", []):  # 실패가 아니라 사람 확인 대기(make_episode 종료 3 = 새 에셋 시안 확인)
        status(state="needs_review", stopped_step=i, returncode=p.returncode, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.exit(0)
    if p.returncode != 0 and not step.get("allow_fail"):
        status(state="failed", failed_step=i, returncode=p.returncode)
        sys.exit(p.returncode)
status(state="done", finished=time.strftime("%Y-%m-%d %H:%M:%S"))
