"""한 화 마무리: 합성 재료 컷(phone_ui·repeat·세피아 등) → 합성(compose) → PDF → 점검표. 힉스필드 판은 이것만 한다(컷은 hf_panel 로 미리 그려 hf_place 로 놓는다).

    python tools/run_episode.py <episode_dir> [--preview-every 10]

- (옛 ComfyUI 판) 콘티에 prompt 가 있는 빈 컷이 있을 때만 그린다: COMFY_URL 이 있으면 그 ComfyUI, 없으면 RunPod 파드를 켜고 끝나면 내린다.
- 아직 없는 컷(직접 작성 칸·합성 컷 제외)을 순서대로 그린다. 이미 있는 컷은 건너뛴다(중간에 끊겨도 다시 돌리면 이어서).
- N컷마다 compose.py 로 out/ 미리보기를 갱신하고, 끝나면 최종 합성 + PDF + 점검표(review.py, 새로 그린 컷).
- 진행 상황: <episode_dir>/render_run.log, render_status.json
"""
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy import ROOT, Comfy, load_json, load_style, stable_seed  # noqa: E402
from render import read_log, render_one, write_log  # noqa: E402
import runpod_cloud  # noqa: E402


class Runner:
    def __init__(self, ep, preview_every):
        self.ep = ep
        self.series = ep.parent.parent
        self.preview_every = preview_every
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

    def render_all(self):
        style = load_style(self.series)
        board = load_json(self.ep / "storyboard.json")
        characters = load_json(self.series / "characters.json")
        panels_dir = self.ep / "panels"
        log_path = panels_dir / "render_log.json"
        log = read_log(log_path)
        # 합성으로 만드는 컷(composite: 8분할·세피아 회상·그래픽)과 비어 있는 직접 작성 칸은 그리지 않는다
        todo = [p for p in board["panels"] if p.get("author") != "user" and p.get("prompt") and not p.get("composite")]
        left = [p for p in todo if not (panels_dir / f"{p['id']}.png").exists()]
        self.log(f"그릴 컷 {len(left)}/{len(todo)} (ComfyUI {os.environ.get('COMFY_URL')})")
        done_now, t0 = 0, time.time()
        for p in left:
            out = panels_dir / f"{p['id']}.png"
            seed = p["seed"] if "seed" in p else stable_seed(self.series.name, self.ep.name, p["id"], 1)
            for attempt in range(1, 4):
                try:
                    render_one(Comfy(), style, p, characters, out, seed, log, p["id"])
                    write_log(log_path, log)
                    break
                except Exception as e:
                    self.log(f"{p['id']} 실패 {attempt}회: {str(e)[:200]}")
                    time.sleep(10)
            else:
                self.log(f"{p['id']} 건너뜀(3회 실패)")
                continue
            done_now += 1
            per = (time.time() - t0) / done_now
            status = {"done": done_now, "total": len(left), "last": p["id"], "sec_per_panel": round(per, 1),
                      "eta_min": round(per * (len(left) - done_now) / 60), "updated": time.strftime("%H:%M:%S")}
            (self.ep / "render_status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
            self.log(f"{p['id']} 완료 ({done_now}/{len(left)}, 남은 시간 약 {status['eta_min']}분)")
            if done_now % self.preview_every == 0:
                self.log("미리보기: " + self.tool("compose.py"))

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
                # 3인 컷: 개인 컷(parts/*.png, render.py panel 로 그림)을 세로 띠로 나란히. 인자 순서가 달라 바로 실행한다
                r = subprocess.run([sys.executable, str(ROOT / "tools" / "strip_panel.py"), str(self.ep / "panels" / f"{p['id']}.png"),
                                    *[str(self.ep / x) for x in comp["parts"]]], capture_output=True, text=True, encoding="utf-8")
                self.log(f"합성 컷 {p['id']}: {(r.stdout or r.stderr).strip()[-200:]}")
                continue
            else:
                continue
            r = subprocess.run([sys.executable, str(ROOT / "tools" / f"{comp['tool']}.py"), str(self.ep), *args],
                               capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
            self.log(f"합성 컷 {p['id']}: {(r.stdout or r.stderr).strip()[-200:]}")

    def pending(self):
        board = load_json(self.ep / "storyboard.json")
        return [p for p in board["panels"] if p.get("author") != "user" and p.get("prompt") and not p.get("composite")
                and not (self.ep / "panels" / f"{p['id']}.png").exists()]

    def run(self):
        # 그릴 컷이 있을 때만 그린다. 힉스필드 판은 콘티에 prompt 가 없어 늘 0 이라 합성만 한다.
        # (2026-10-14 전엔 COMFY_URL 이 없으면 그릴 컷이 없어도 RunPod 파드부터 켰다. 남아 있던 옛 COMFY_URL 덕에 안 켜졌을 뿐)
        if self.pending():
            # COMFY_URL 이 이미 있으면 그대로 쓰고, 없으면 이 실행 동안만 RunPod 파드를 켠다
            ctx = contextlib.nullcontext(os.environ["COMFY_URL"]) if os.environ.get("COMFY_URL") else runpod_cloud.session()
            with ctx as url:
                os.environ["COMFY_URL"] = url
                self.render_all()
        else:
            self.log("그릴 컷 0(합성만)")
        self.make_composites()
        self.log("합성: " + self.tool("compose.py"))
        self.log("PDF: " + self.tool("to_pdf.py"))
        self.log("점검표: " + self.tool("review.py"))
        (self.ep / "render_status.json").write_text(json.dumps({"done": "finished", "updated": time.strftime("%H:%M:%S")}), encoding="utf-8")
        self.log("끝")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir")
    ap.add_argument("--preview-every", type=int, default=10)
    args = ap.parse_args()
    Runner(Path(args.episode_dir).resolve(), args.preview_every).run()


if __name__ == "__main__":
    main()
