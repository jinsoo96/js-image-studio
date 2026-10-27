"""tools/studio_test.py(미사일 스튜디오 시험 하네스) 회귀 테스트: 대화 모델·GPU 없이.

- DONE_WORDS: '거의 끝났습니다' 는 아직이라는 말(2026-10-26 무대 뒤 GPU 시나리오가 바르게 답한 차례를 '다 됐다'로 잡았다)
- wait_jobs: 죽은 프로세스가 running 으로 남긴 상태 파일을 기다리지 않는다(끊긴 LoRA 학습 하나로 차례마다 20분·15분을 꽉 채웠다)
- summarize/judge: 도는 작업을 job_status 가 done 으로 돌려주면 still_running 이 풀린다
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import studio_test as S  # noqa: E402


def test_done_words_skip_almost():
    assert not S.DONE_WORDS.search("합성이 거의 끝났습니다(133컷 합성·PDF·점검표까지 다 됐고, 마지막 기계 점검만 남음)")
    assert not S.DONE_WORDS.search("거의 다 됐어요")
    assert not S.DONE_WORDS.search("완료되면 알려 드리겠습니다")
    assert S.DONE_WORDS.search("다 됐습니다.")
    assert S.DONE_WORDS.search("합성 완료했습니다")


def test_wait_jobs_ignores_dead_process():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        jobs = root / "state" / "mcp_jobs"
        jobs.mkdir(parents=True)
        saved = (S.ROOT, S.time.sleep)
        slept = []
        S.ROOT, S.time.sleep = root, lambda s: slept.append(s)
        try:
            # 죽은 pid 가 running 으로 남아 있다 → 바로 돌아온다(기다리지 않는다)
            (jobs / "dead.status.json").write_text(json.dumps({"state": "running", "pid": 999999}), encoding="utf-8")
            S.wait_jobs(1)
            assert slept == []
            # 살아 있는 pid 가 running 이면 기다린다(한도까지)
            (jobs / "live.status.json").write_text(json.dumps({"state": "running", "pid": os.getpid()}), encoding="utf-8")
            clock = iter([0, 0, 30, 61])  # time.time: end 계산, 1번째 확인, 2번째 확인, 끝
            S.time.time, real_time = lambda: next(clock, 10**9), S.time.time
            try:
                S.wait_jobs(1)
            finally:
                S.time.time = real_time
            assert len(slept) >= 1
            # starting(아직 pid 없음)도 기다린다
            slept.clear()
            (jobs / "live.status.json").unlink()
            (jobs / "dead.status.json").write_text(json.dumps({"state": "starting"}), encoding="utf-8")
            clock = iter([0, 0, 61])
            S.time.time = lambda: next(clock, 10**9)
            try:
                S.wait_jobs(1)
            finally:
                S.time.time = real_time
            assert len(slept) >= 1
        finally:
            S.ROOT, S.time.sleep = saved


def test_summarize_clears_running_when_job_status_reports_done():
    ev = [
        {"type": "tool_call", "name": "webtoon-studio__compose", "arguments": {}},
        {"type": "tool_result", "name": "webtoon-studio__compose", "text": '{"state": "running", "job": "j1", "result_ready": false}'},
        {"type": "tool_result", "name": "webtoon-studio__job_status", "text": '{"job": "j1", "kind": "compose", "state": "running"}'},
    ]
    assert S.summarize({"events": ev})["still_running"] is True
    ev.append({"type": "tool_result", "name": "webtoon-studio__job_status", "text": '{"job": "j1", "kind": "compose", "state": "done", "x": "' + "y" * 400 + '"}'})
    assert S.summarize({"events": ev})["still_running"] is False
    # 도는 중에 '거의 끝났습니다' 는 거짓 완료가 아니다. '다 됐습니다' 는 맞다
    turns = [{"sum": {"calls": [], "errors": [], "still_running": True, "tokens": [0, 0]}, "text": "합성이 거의 끝났습니다. 기계 점검만 남음"}]
    assert "다 됐다" not in " ".join(S.judge({"checks": {"max_seconds": 999}}, turns, 1.0))
    turns[0]["text"] = "다 됐습니다. 합성 완료"
    assert any("다 됐다" in f for f in S.judge({"checks": {"max_seconds": 999}}, turns, 1.0))


def test_style_near_scores_only_new_drawings_and_respects_trust():
    import time as _t
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        drawn = root / "series" / "w" / "episodes" / "ep001_v9" / "panels_hf_vast"
        drawn.mkdir(parents=True)
        old = drawn / "p001.png"
        old.write_bytes(b"x")
        os.utime(old, (1, 1))  # 시나리오 전부터 있던 그림
        since = _t.time() - 1
        (drawn / "p002.png").write_bytes(b"x")
        sc = {"checks": {"style_near": {"floor": 10}}, "setup": {"copy": [["a", "series/w/episodes/ep001_v9"]]}}
        seen = {}

        def scorer(series, files, floor, trusted=True, pct=4):
            seen["series"], seen["files"] = series, [f.name for f in files]
            return {"trusted": trusted, "floor": floor, "items": [{"file": str(f), "pct": pct, "pass": pct >= floor} for f in files]}

        real = S.ROOT
        S.ROOT = root
        try:
            fails = S.style_near(sc, since, scorer)
            assert seen["files"] == ["p002.png"] and seen["series"] == root / "series" / "w"   # 새로 그린 것만, 사본의 작품 폴더로
            assert fails and "p002 4" in fails[0]
            assert S.style_near(sc, since, lambda s_, f_, fl: scorer(s_, f_, fl, pct=55)) == []           # 통과
            assert S.style_near(sc, since, lambda s_, f_, fl: scorer(s_, f_, fl, trusted=False)) == []   # 구별 못 한 작품은 채점 안 함
            assert S.style_near({"checks": {}}, since, scorer) == []                                       # 체크가 없으면 안 봄
            os.utime(drawn / "p002.png", (1, 1))
            assert "새로 그린 그림이 없다" in S.style_near(sc, since, scorer)[0]
        finally:
            S.ROOT = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK  ", name)
