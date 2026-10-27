# webtoon-studio

**Make vertical-scroll webtoons by conversation, in the art style of an existing work.** An LLM agent
(Missile Studio or a agent app agent) drives an MCP server of 46 tools; the tools draw on your own GPU server
with ComfyUI and open models, follow each work's character sheets and style, check every panel, letter
the speech bubbles and export PDF, EPUB and trailers. Works, models and servers are data, not code:
swap the LLM, the GPU host or the work and the same tools keep running.

```
  Missile Studio / agent app agent ──MCP (http)──▶ mcp/server.py ──jobs──▶ vast/ · tools/      (this PC)
                                              46 tools             │
                                                                   │ SSH tunnel (tools/vast_cloud.py)
                                          ┌────────────────────────┴────────────────────────┐
                                          ▼                                                 ▼
                              image box (cloud/vast_img.json)                    LLM box (cloud/vast_llm.json)
                              ComfyUI · Qwen-Image 2.1 · style LoRA              vLLM · Qwen3.8-27B (chat + vision QA)
```

## What it does

- **Drawing** — `vast_draw` turns each panel description (`panels.json`) into a prompt with the work's
  location art, character head references, expression cells and style LoRA, renders on ComfyUI and lets the
  vision model check staging, gaze and extra people; failed panels are redrawn or direction-edited.
- **Editing** — `edit_panel`, `adopt_panel`, `compare_panel`, `preview_panel`: fix one panel, compare it
  with the published one, swap it in. Originals are always kept.
- **Style fidelity** — `style_check` and every `vast_draw` job score each new panel against the work's
  published panels with CSD style descriptors, read as a percentile of how alike the originals are
  (same shot kind, never against the panel's own original). A diagnosis checks that the metric can tell
  the works apart before it is trusted.
- **Episodes** — `make_episode` runs storyboard → asset check → drawing → placement → lettering → QA →
  export in one job. `compose`/`export` run on this PC without a GPU.
- **Training** — `lora_train`, `restyle`, `lora_compare`: style and character LoRAs on the GPU server;
  checkpoints are scored and kept in `models/loras/`, pushed back to any new server automatically.
- **Swappable parts** — drawing reads only `state/comfy_url.txt`, text and vision read only
  `state/vlm_url.txt`. Waking a server rewrites both; any OpenAI-compatible LLM also works from Missile Studio.

## Requirements

- Windows, Python 3.12, [uv](https://github.com/astral-sh/uv)
- A GPU server: a vast.ai account (two-box setup in `cloud/`) or RunPod (`tools/runpod_cloud.py`)
- Optional: [Missile Studio](https://github.com/jinsoo96/missile-studio) or agent app as the agent front end

## Setup

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
uv pip install --python .venv\Scripts\python.exe "torch==2.14.1+cpu" --index-url https://download.pytorch.org/whl/cpu
```

Not in the repository (copy them from the studio PC):

| Path | What |
|---|---|
| `series/` | the works: storyboards, panel descriptions, cast, assets, published panels |
| `models/loras/` | trained LoRA weights (pushed to the GPU server on wake) |
| `models/detect/`, `models/u2net/` | face/character detection (CCIP) and background removal |
| `models/style/csd/` | CSD style weights (`pytorch_model.bin` from `tomg-group-umd/CSD-ViT-L`; converted on first use) |

Secrets (vast API key, SSH key `~/.ssh/vast_webtoon_ed25519`, Cloudflare Access keys) live outside the repository.

## Usage

**Through an agent (normal).** Run the MCP server (`mcp/serve_http.cmd`, port 7440; the scheduled task
`WEBTOON-MCP-http` starts it at boot) and register `http://127.0.0.1:7440/mcp` in Missile Studio or agent app.
Then ask: *"<work> episode 3 — draw it"*, *"redraw p043 and p082, put p082 in, make him look sterner,
compose"*. See `mcp/README.md` for the agent prompt and remote access.

**By hand.**

```powershell
$py = ".venv\Scripts\python.exe"
& $py -u tools\gpu_wake.py                                   # wake both GPU boxes (~15 min)
& $py tools\make_episode.py series\<work> ep003              # one episode end to end
& $py vast\comfy_panel.py series\<work> series\<work>\episodes\ep003\panels.json --episode ep003 --only p003,p008 --variant vast --turbo --expr --fix --check
& $py tools\style_fidelity.py score series\<work> series\<work>\episodes\ep003\panels_hf_vast
& $py tools\place_panels.py series\<work>\episodes\ep003      # published panels → compose size
& $py tools\run_episode.py series\<work>\episodes\ep003       # composite panels, lettering, PDF, QA sheets (no GPU)
& $py tools\vast_cloud.py --profile vast_img down; & $py tools\vast_cloud.py --profile vast_llm down
```

## GPU servers

`tools/gpu_wake.py` rents and boots both boxes in parallel (offers ranked by price × expected hours,
CUDA, reliability, region and download cost). Inside each box a watchdog deletes the instance after
15 idle minutes or 3 hours; on this PC the `WEBTOON-budget-guard` task deletes everything when the
balance reaches the floor set in `min_balance_usd`. Nothing is kept stopped, so idle cost is zero.
Swap the LLM on a running box with `tools/vast_cloud.py --profile vast_llm llm --model <name>`.

## Testing

```powershell
python -m pytest tests                                   # logic tests, no GPU, no LLM
.venv\Scripts\python.exe tools\mcp_smoke.py              # every read tool on every work, prompt dry run, hardcode scan
python tools\studio_test.py --list                       # Missile Studio scenarios (add --provider, --series, --gpu)
.venv\Scripts\python.exe tools\style_fidelity.py diagnose  # can the style metric tell the works apart?
```

Scenarios named `작품별_*` expand to every work. GPU scenarios also score style (`style_near`).

## Layout

```
mcp/        MCP server, job runner, agent prompts
vast/       drawing, editing, LoRA training and evaluation on ComfyUI
tools/      episode pipeline, lettering, QA, style fidelity, GPU server control, tests harness
cloud/      GPU box profiles and the in-box bootstrap (install, agent, watchdog)
skills/     how-to for each production step (storyboard, panel spec, staging review, lettering …)
tests/      pytest suites and Missile Studio scenarios
fonts/      lettering fonts (OFL)
worklogs/   dated engineering notes and OPEN-ISSUES.md
```

Session rules for coding agents are in `CLAUDE.md`; lessons from past mistakes in `LESSONS.md`.

## License

Proprietary — all rights reserved. No use, copying, modification or distribution without written
permission. See [LICENSE](LICENSE).
