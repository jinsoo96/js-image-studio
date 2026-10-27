"""작업 실행기: 단계의 print 가 끝나기 전에 로그에 보여야 멈춤 지킴이가 일하는 단계를 끊지 않는다."""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_step_output_reaches_log_before_step_ends(tmp_path):
    child = "import time; print('진행 1/2'); time.sleep(20); print('진행 2/2')"  # flush 없음
    job = tmp_path / "j.json"
    job.write_text(json.dumps({"steps": [{"label": "버퍼 시험", "cmd": [sys.executable, "-c", child]}]}), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(ROOT / "mcp" / "job_runner.py"), str(job)], cwd=ROOT)
    try:
        log = job.with_suffix(".log")
        deadline = time.time() + 10
        while time.time() < deadline and "진행 1/2" not in (log.read_text(encoding="utf-8") if log.exists() else ""):
            time.sleep(0.5)
        assert "진행 1/2" in log.read_text(encoding="utf-8")
        assert p.poll() is None  # 단계는 아직 자는 중
    finally:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True) if sys.platform == "win32" else p.kill()
