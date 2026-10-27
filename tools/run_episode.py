"""한 화 마무리: 합성 재료 컷(phone_ui·repeat·세피아 등) → 합성(compose) → PDF → 점검표. 컷은 미리 그려져 있어야 한다
(draw·make_episode 로 그리고 place_panels.py 가 panels/ 에 놓음). 그림이 없는 컷은 알리고 자리만 비워 합성한다.

    python tools/run_episode.py <episode_dir>

진행 기록: <episode_dir>/render_run.log, 끝나면 render_status.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_json  # noqa: E402


class Runner:
    def __init__(self, ep):
        self.ep = ep
        self.series = ep.parent.parent
        self.log_file = open(ep / "render_run.log", "a", encoding="utf-8")

    def log(self, msg):
        line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def tool(self, name):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / name), str(self.ep)],
                           capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
        return (r.stdout.strip().splitlines() or [r.stderr.strip()[-200:] or "(출력 없음)"])[-1]

    def make_composites(self):
        # 원본 컷을 새로 그렸으면 그걸로 만든 합성 컷도 다시 만든다. 그래픽 컷(ecg_graphic 등)은 없을 때만 만든다
        for p in load_json(self.ep / "storyboard.json")["panels"]:
            comp = p.get("composite") or {}
            if comp.get("tool") == "sepia_flashback":
                args = [comp["from"], p["id"]]
            elif comp.get("tool") == "ecg_graphic" and not (self.ep / "panels" / f"{p['id']}.png").exists():
                args = [p["id"]]
            elif comp.get("tool") == "tv_frame" and (self.ep / comp["from"]).exists():
                args = [comp["from"], p["id"]]
            elif comp.get("tool") == "phone_ui" and (not comp.get("photo") or (self.ep / comp["photo"]).exists()):
                args = [p["id"]]  # 휴대폰 화면·달력·제목 카드(스펙은 콘티 composite 안)
            elif comp.get("tool") == "repeat" and (self.ep / "panels" / f"{comp['from']}.png").exists():
                # 같은 순간을 다시 보여 주는 컷(콜드 오픈 ↔ 본편): 따로 그리면 같은 장면이 다른 그림이 된다(2026-10-12 p004 ↔ p100)
                shutil.copyfile(self.ep / "panels" / f"{comp['from']}.png", self.ep / "panels" / f"{p['id']}.png")
                self.log(f"합성 컷 {p['id']}: {comp['from']} 그대로")
                continue
            elif comp.get("tool") == "strip_panel" and all((self.ep / x).exists() for x in comp["parts"]):
                # 3인 컷: 개인 컷(parts/*.png)을 세로 띠로 나란히. 인자 순서가 달라 바로 실행한다
                r = subprocess.run([sys.executable, str(ROOT / "tools" / "strip_panel.py"), str(self.ep / "panels" / f"{p['id']}.png"),
                                    *[str(self.ep / x) for x in comp["parts"]]], capture_output=True, text=True, encoding="utf-8")
                self.log(f"합성 컷 {p['id']}: {(r.stdout or r.stderr).strip()[-200:]}")
                continue
            else:
                continue
            r = subprocess.run([sys.executable, str(ROOT / "tools" / f"{comp['tool']}.py"), str(self.ep), *args],
                               capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
            self.log(f"합성 컷 {p['id']}: {(r.stdout or r.stderr).strip()[-200:]}")

    def missing(self):
        """그림이 아직 없는 컷(직접 작성 칸·합성 컷 제외). 여기서는 그리지 않는다."""
        board = load_json(self.ep / "storyboard.json")
        return [p["id"] for p in board["panels"] if p.get("author") != "user" and not p.get("composite")
                and not (self.ep / "panels" / f"{p['id']}.png").exists()]

    def run(self):
        # 예전엔 콘티에 옛 SDXL prompt 칸이 남은 컷(D+18·무대 뒤 1화)이 비면 RunPod 파드를 켜고 옛 ComfyUI 로 그리려 했다.
        # 그리기는 GPU 서버(draw)의 일이라 여기서는 알리기만 한다(2026-10-27 정리)
        left = self.missing()
        self.log(f"그림 없는 컷 {len(left)}" + (f": {', '.join(left[:20])}{' …' if len(left) > 20 else ''} (draw 로 그리고 place_panels.py 로 놓는다)"
                                                if left else "(합성만)"))
        self.make_composites()
        self.log("합성: " + self.tool("compose.py"))
        self.log("PDF: " + self.tool("to_pdf.py"))
        self.log("점검표: " + self.tool("review.py"))
        (self.ep / "render_status.json").write_text(json.dumps({"done": "finished", "updated": time.strftime("%H:%M:%S")}), encoding="utf-8")
        self.log("끝")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    args = ap.parse_args()
    Runner(Path(args.episode_dir).resolve()).run()


if __name__ == "__main__":
    main()
