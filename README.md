# H3-Remake

Self-hosted AI video remake pipeline: **character swap** (replace the subject while preserving shots, camera, motion and the original soundtrack) and **scene edit** (remove or replace objects, or swap the background, while preserving the people), driven by an agent with a web UI.

This repository is the **product/orchestration layer only** — it contains no model weights and no vendored third-party code. Everything heavy is fetched from upstream projects at install time (see [Upstream projects](#upstream-projects)).

> Linux only for now. Windows support is planned for a later iteration.

## Upstream projects

This project stands on the shoulders of:

| Project | Role |
|---|---|
| [MiniMaxAI/MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) | Base video generation model (H3-Base FL2VA / Ref2VA) |
| [Viggle/Viggle-Remake](https://huggingface.co/Viggle/Viggle-Remake) | Open-source ComfyUI adaptation of H3: `remake_worker.py` execution entry, the ComfyUI patch set, workflow templates and `viggle-remake-mcp`. The installer fetches it from Hugging Face and applies its patches onto ComfyUI v0.42.0 |
| [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) | Repackaged text encoder / VAE weights consumed by ComfyUI |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) v0.42.0 | Inference engine (pinned; patched per Viggle-Remake) |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Agent runtime executing the remake skills via `viggle-remake-mcp` |

Model and code licenses follow the respective upstream repositories — review them before commercial use.

## Requirements (Linux)

| Item | Requirement |
|---|---|
| GPU | NVIDIA, **32 GB VRAM** (RTX 5090 class). The int8 profile streams weights into VRAM layer-by-layer and computes via INT8 quantized matmul (W8A8); 24 GB cards are not supported yet (a GGUF profile is reserved for a later iteration) |
| Driver | CUDA 13 capable (580+ series); PyTorch is installed from the cu130 index |
| System RAM | **≥ 64 GiB hard requirement** (weights stay mmap-resident), 96 GiB recommended. Enforced by `models.py` before download |
| Disk | ~20 GiB for the environment + ~60 GiB for weights; ≥ 150 GiB free recommended (outputs and intermediates) |
| Software | `git`, [uv](https://docs.astral.sh/uv/) (manages Python 3.12), Node.js ≥ 18 with `npm` (web UI build). ffmpeg is configured by the installer |
| API keys | An OpenAI-compatible multimodal LLM (default: Aliyun Bailian `qwen3.7-plus`) and MiniMax `H3-Image` for keyframe painting. Rendering itself runs on your local GPU; these two cloud APIs are pay-per-use |

## Install & run

Three separate steps — environment, models, launch:

```bash
git clone https://github.com/<your-username>/H3-Remake.git && cd H3-Remake

# 1. Environment: deps, torch cu130, ComfyUI v0.42.0 + patch set,
#    custom nodes, Hermes agent, web UI build
uv run install.py

# 2. Model weights (~59 GiB, resumable; --dry-run previews the manifest)
uv run models.py

# 3. Configure: copy the template and fill in the two api_key fields
cp config.example.toml config.toml

# 4. Launch (web UI + worker + agent watchdog; manual start, no autostart)
uv run start.py
```

Open **http://127.0.0.1:8000** and create a task: upload a source video, pick swap or edit, and describe the task in one sentence. Swap additionally needs a source-person id and a target character picked from the character library (upload character sheets there first); a reference photo is optional.

Useful flags:

- `uv run install.py --dry-run` — dry-run the web UI build only (stage 1); all other stages still execute
- `uv run install.py --stages 6,4` — re-run selected stages (idempotent; model download is a separate entry, `models.py`)
- `uv run models.py --dry-run` — verify remote file sizes against the pinned manifest, download nothing
- `uv run models.py --list` / `--profile <name>` — model profiles (currently `int8`)
- `uv run start.py --host 0.0.0.0 --port 8000` — expose the UI beyond localhost

An existing ComfyUI v0.42.0 installation can be reused instead of the bundled one: set `[comfyui] mode = "external"` and `path` in `config.toml` before installing.

## Model weights (int8 profile)

Downloaded by `models.py` from Hugging Face (default endpoint `hf-mirror.com`, override with `HF_ENDPOINT`). Files are verified byte-exact against a pinned manifest; existing valid files are skipped, so re-runs only fetch what is missing.

| Component | File | Source repo | Size |
|---|---|---|---|
| Diffusion — swap (character replacement) | `int8/viggle_remake_swap_pruned_int8_convrot.safetensors` | `Viggle/Viggle-Remake` | 19.5 GiB |
| Diffusion — edit (scene editing) | `int8/viggle_remake_edit_pruned_int8_convrot.safetensors` | `Viggle/Viggle-Remake` | 19.5 GiB |
| Text encoder (shared) | `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `Comfy-Org/MiniMax-H3` | 14.6 GiB |
| Video VAE (shared) | `vae/minimax_h3_video_vae_fp16.safetensors` | `Comfy-Org/MiniMax-H3` | 4.9 GiB |
| Audio VAE (shared) | `vae/minimax_h3_audio_vae_fp32.safetensors` | `Comfy-Org/MiniMax-H3` | 0.6 GiB |

Total ≈ **59.1 GiB**. The text encoder and VAEs are shared across profiles — switching profiles later only downloads the diffusion-model delta.

VRAM behavior: weights stay in int8 at rest (mmap-resident in host RAM) and stream into VRAM layer-by-layer under ComfyUI's dynamic VRAM management; compute runs through ComfyUI's INT8 quantized matmul path (W8A8 via `torch._int_mm`), which is what makes the 32 GB card feasible. The worker reserves headroom via upstream `--vram-headroom` (tunable through `[worker] comfy_flags`).

## Configuration & data

- `config.toml` (git-ignored) holds API keys, ComfyUI mode, worker limits and the model profile; `config.example.toml` documents every key.
- Sessions, queues and outputs live under `~/.viggle-remake` by default; point `[paths] remake_home` at a data disk on cloud machines.
- Per-window frame limits follow upstream H3 constraints (frame counts are 17n+5): 345 frames at 832×480, 175 frames at 1280×736.

## Repository layout

```
install.py / models.py / start.py   # three entry points: environment / weights / launch
scripts/                            # installer, model registry, worker launcher, tests
app/agent/                          # Hermes integration, session orchestration, watchdog & resume
app/server/                         # FastAPI backend (tasks, characters, settings, WS progress)
app/web/                            # React frontend (Vite + TypeScript)
```

## Status

M1–M5 complete (pipeline, agents, orchestration, real-render harness, web product) with a full offline test suite. Real-GPU validation on a 32 GB Linux machine is the next milestone.