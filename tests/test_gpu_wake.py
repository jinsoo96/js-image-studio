"""켜기 버튼: 상자 하나라도 못 켜면 0 이 아닌 코드를 내야 뒤 단계가 모델 없이 돌지 않는다."""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = """
import os, sys
sys.path.insert(0, {tools!r})
import gpu_wake
ok = [sys.executable, "-c", "print('준비 완료')"]
bad = [sys.executable, "-c", "print('컨테이너 로그 \\u2717'); raise SystemExit('이미지에 SSH 호스트 키가 없다')"]
sys.exit(gpu_wake.run_merged({{"img": ok, "llm": bad}}, {{**os.environ, "PYTHONIOENCODING": "utf-8"}}))
"""


def test_first_ready_box_is_held_until_others_are_up():
    # 먼저 뜬 상자는 나머지를 기다리는 동안 붙들고(놀면 지우는 지킴이), 다 뜨면 더 붙들지 않는다. 늦게 뜬 상자는 붙들 일이 없다
    sys.path.insert(0, str(ROOT / "tools"))
    import gpu_wake
    held = []
    fast = [sys.executable, "-c", "print('준비 완료')"]
    slow = [sys.executable, "-c", "import time; time.sleep(3); print('준비 완료')"]
    code = gpu_wake.run_merged({"llm": fast, "img": slow}, {**os.environ, "PYTHONIOENCODING": "utf-8"},
                               keep_alive=lambda n: held.append((n, time.time())), every=1)
    assert code == 0 and held and {n for n, _ in held} == {"llm"}
    assert len(held) >= 2  # 기다리는 내내 다시 붙든다(한 번 걸고 끝나면 붙들기가 먼저 풀린다)


def test_one_failed_box_fails_wake_even_on_cp949_console():
    # 이 PC 콘솔·파이프 기본(cp949)에서 cp949 로 못 쓰는 글자(✗)가 오면 옮기던 스레드가 죽고 그 상자의 실패가 사라졌다
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8",)} | {"PYTHONIOENCODING": "cp949"}
    r = subprocess.run([sys.executable, "-c", RUN.format(tools=str(ROOT / "tools"))], env=env, capture_output=True, timeout=60)
    assert r.returncode == 1, r.stderr.decode("cp949", "replace")[-500:]
