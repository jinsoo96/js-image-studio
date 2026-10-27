# js-image-studio

**An image studio you drive by conversation.** Generate images from prompts and reference art, train
style and character LoRAs, and let a harness check every result, all on your own GPU server and exposed
to any LLM agent as one MCP server (`imagestudio`, 46 tools). Models, servers and projects are data,
not code: swap the LLM, the GPU host or the project and the same tools keep running.

**Webtoons are the first template.** On top of the image tools, the webtoon template adds storyboards,
panel descriptions, continuity across panels, speech-bubble lettering and PDF/EPUB/trailer export, so
an agent can write and draw a vertical-scroll episode in the art style of an existing work.

```
  MCP client (Missile Studio …) ──MCP──▶ imagestudio (mcp/server.py) ──jobs──▶ vast/ · tools/   (this PC)
                                                                         │
                                                                         │ SSH tunnel (tools/vast_cloud.py)
                                           ┌─────────────────────────────┴──────────────────────────┐
                                           ▼                                                        ▼
                               image box (cloud/vast_img.json)                          LLM box (cloud/vast_llm.json)
                               ComfyUI · Qwen-Image 2.1 · LoRAs                         vLLM · Qwen3.8-27B (chat + vision QA)
```

## Image studio

- **Generate** — `draw` builds the prompt from a structured description plus reference images
  (characters, locations, expressions) and the project's style LoRA, renders on ComfyUI and keeps every
  result beside the original. `draw_models` lists the model presets saved on the server.
- **Edit and compare** — `edit_image` changes one part of an image and keeps the rest, `preview_image`
  tries a change without touching the original, `compare_image` shows before and after, `adopt_image`
  promotes a version, `image_versions` rolls back. Nothing is ever deleted.
- **Train** — `lora_train` (style or character), `restyle` (an edit LoRA that pulls generated images toward
  the project's look), `lora_compare` (checkpoints × strengths on held-out images, scored). Trained weights
  are kept on this PC and pushed to any new server on wake.
- **Harness QA** — rules are enforced by isolated judges, not by the prompt:
  - gates score storyboards and descriptions before drawing (`prepare_episode`);
  - a vision model checks staging, gaze, extra people and props after drawing and fixes or redraws
    (`draw --check`, `review_panels`);
  - **style fidelity** scores each image against the project's originals with CSD style descriptors, read
    as a percentile of how alike the originals are, after a diagnosis confirms the metric can tell the
    projects apart (`style_check`, also attached to every `draw` job).
- **Swappable parts** — drawing reads only `state/comfy_url.txt`, text and vision read only
  `state/vlm_url.txt`. Waking a server rewrites both; any OpenAI-compatible LLM works as the agent.

## Webtoon template

`new_series` → `new_episode` → `set_storyboard` → `set_panels` → `prepare_episode` → `make_episode`
(asset check → drawing → placement → lettering → QA → export). `compose` and `export` run on this PC
without a GPU; `publish_ailism` posts a finished episode. The template's how-to lives in `skills/`.

## Tools

Each tool tells the agent what it does in one short paragraph; `tool_help <name>` returns the full
reference (arguments, result, next step, cautions). Long-running tools start a job and return its id;
`job_status` reports progress, links, style scores and the next step.

| Group | Tools | GPU |
|---|---|---|
| Image | `draw` `edit_image` `preview_image` `compare_image` `adopt_image` `image_versions` `image_info` `contact_sheet` `make_assets` `draw_candidates` `redo_candidate` `style_compare` `draw_models` | drawing and editing |
| Training | `lora_train` `restyle` `lora_compare` `loras` | yes |
| Harness QA | `style_check` `review_panels` `prepare_episode` | review only |
| Webtoon template | `new_series` `add_to_series` `new_episode` `set_storyboard` `set_panels` `list_panels` `find` `make_episode` `compose` `export` | drawing only |
| Workspace | `guide` `tool_help` `status` `job_status` `read_doc` `list_files` `read_file` `write_file` `edit_file` `copy_file` `move_file` `file_link` | – |
| Server | `comfy_server` | – |
| Publishing | `ailism_works` `ailism_work` `publish_ailism` | – |

## Requirements

- Windows, Python 3.12, [uv](https://github.com/astral-sh/uv)
- A GPU server: a vast.ai account (two-box setup in `cloud/`) or RunPod (`tools/runpod_cloud.py`)
- An MCP client for the agent, e.g. [Missile Studio](https://github.com/jinsoo96/missile-studio) (any client that speaks MCP over HTTP or stdio works)

## Setup

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
uv pip install --python .venv\Scripts\python.exe "torch==2.14.1+cpu" --index-url https://download.pytorch.org/whl/cpu
```

Not in the repository (kept on the studio PC):

| Path | What |
|---|---|
| `series/` | projects: storyboards, descriptions, cast, assets, published images |
| `models/loras/` | trained LoRA weights (pushed to the GPU server on wake) |
| `models/detect/`, `models/u2net/` | face/character detection (CCIP) and background removal |
| `models/style/csd/` | CSD style weights (`pytorch_model.bin` from `tomg-group-umd/CSD-ViT-L`; converted on first use) |

Secrets (GPU host API key, SSH key, Cloudflare Access keys) live outside the repository.

## Usage

**Through an agent (normal).** Run the MCP server (`mcp/serve_http.cmd`, port 7440; a scheduled task
starts it at boot) and register `http://127.0.0.1:7440/mcp` in your MCP client as `imagestudio`.
Then ask: *"redraw p043 and p082, put p082 in, make him look sterner"*, *"how close is this episode to
the original art style?"*, *"<project> episode 3 — draw it"*. See `mcp/README.md` for remote access and
agent prompts.

**By hand.**

```powershell
$py = ".venv\Scripts\python.exe"
& $py -u tools\gpu_wake.py                                   # wake both GPU boxes (~15 min)
& $py vast\comfy_panel.py series\<project> series\<project>\episodes\ep003\panels.json --episode ep003 --only p003,p008 --variant vast --turbo --expr --fix --check
& $py tools\style_fidelity.py score series\<project> series\<project>\episodes\ep003\panels_hf_vast
& $py vast\lora_eval.py series\<project> style --strengths 0.6,0.8,1.0   # compare LoRA checkpoints, scored
& $py tools\make_episode.py series\<project> ep003           # webtoon template: one episode end to end
& $py tools\vast_cloud.py --profile vast_img down; & $py tools\vast_cloud.py --profile vast_llm down
```

## GPU servers

`tools/gpu_wake.py` rents and boots both boxes in parallel (offers ranked by price × expected hours,
CUDA, reliability, region and download cost). Inside each box a watchdog deletes the instance after
15 idle minutes or 3 hours; on this PC a budget guard deletes everything when the balance reaches the
floor set in `min_balance_usd`. Nothing is kept stopped, so idle cost is zero. Swap the LLM on a running
box with `tools/vast_cloud.py --profile vast_llm llm --model <name>`.

## Testing

```powershell
python -m pytest tests                                     # logic tests, no GPU, no LLM
.venv\Scripts\python.exe tools\mcp_smoke.py                # every read tool on every project, prompt dry run, hardcode scan
python tools\studio_test.py --list                         # agent scenarios through Missile Studio (add --provider, --series, --gpu)
.venv\Scripts\python.exe tools\style_fidelity.py diagnose  # can the style metric tell the projects apart?
```

Scenarios named `작품별_*` expand to every project. GPU scenarios also score style (`style_near`).

## Layout

```
mcp/        MCP server, job runner, agent prompts
vast/       generation, editing, LoRA training and evaluation on ComfyUI
tools/      style fidelity, QA gates, GPU server control, webtoon pipeline and lettering, test harness
cloud/      GPU box profiles and the in-box bootstrap (install, agent, watchdog)
skills/     how-to for each step of the webtoon template
tests/      pytest suites and agent scenarios
fonts/      lettering fonts (OFL)
```

## License

Proprietary — all rights reserved. No use, copying, modification or distribution without written
permission. See [LICENSE](LICENSE).
