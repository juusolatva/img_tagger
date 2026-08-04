# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Overview
- `img_tagger.py` — main CLI: tags JPEG/WebP/PNG/GIF files via a local vision-language model (Ollama or LM Studio), writing tags into image metadata.
- `clear_tags.py` — standalone helper that strips tags/metadata back out (used to reset images for testing).
- `prompt.txt` — externalized LLM prompt; takes precedence over the `DEFAULT_PROMPT` fallback hardcoded in `img_tagger.py`.
- Flat repo layout: no package structure, no `pyproject.toml`/`setup.py`, no automated tests, no CI.

## Build/Lint/Test Commands
No lint or test tooling is configured yet.
- Run tagger (Ollama, default): `python img_tagger.py <directory>`
- Run tagger (LM Studio): `python img_tagger.py <directory> --backend lm-studio`
- Recursive, custom model/workers: `python img_tagger.py <directory> -r --model <model> --workers 4`
- Clear all tags: `python clear_tags.py <directory> [-r]`
- Help: `python img_tagger.py -h`
- Logging: `python img_tagger.py <dir> --log output.log` (without `--log`, most diagnostic messages never surface — see Known Limitations)

## Backends
- `--backend ollama` (default): `ollama` Python client, default host `http://localhost:11434`, default model `qwen3-vl:8b`.
- `--backend lm-studio`: `openai` client against LM Studio's OpenAI-compatible API, default host `http://localhost:1234`, images sent as base64 data URLs. `api_key="lm-studio"` is a required placeholder string, not a real credential.
- Neither client is constructed with a request timeout — a hung local model server can block a worker thread indefinitely.

## Core Rules
- **Worker limit**: Max 4 concurrent workers (matches both backends' default concurrency limits). Default is 1. Enforced in CLI arg parsing only, not inside `process_directory()` itself.
- **Skip marker**: Files with `[PROCESSED BY AI]` in EXIF/XMP/GIF comment are skipped automatically (`is_already_processed`).
- **Prompt source**: `prompt.txt` takes precedence over the in-code `DEFAULT_PROMPT` fallback.
- **Tag count**: model output is normalized to 6–12 tags (`normalize_tags`); fewer than 6 valid tags after parsing fails that image.
- **Tag parsing**: `parse_model_output` tries several patterns in order — JSON array, bullet list, numbered list, "tags:"/"keywords:" intro text, then plain comma-separated (the expected default per the prompt).
- **Concurrency**: images are processed in parallel via `ThreadPoolExecutor`; all `pyexiv2` metadata reads/writes are serialized through one global `metadata_lock` (pyexiv2 isn't thread-safe). GIF writes (Pillow-only) aren't covered by that lock.
- **Graceful quit**: press `Q` during a run to stop after in-flight tasks finish; only checked between images, not mid-request.

## Metadata Schema
- **JPEG/WebP/PNG**:
- EXIF: `Exif.Photo.UserComment` (tags joined with `, ` + space + marker)
- XMP: `Xmp.dc.subject` (tag list), `Xmp.dc.description` (`Tags: <list> | <marker>`)
- **GIF**: Comment field only (no EXIF/XMP support); tags stored in the comment string.

## Windows File Handling
- `robust_replace()` retries file replacement up to 10× with 0.5s delay, but **only on Windows**; on Linux/macOS it does a single unguarded replace.
- Note: `clear_tags.py` has its own separate copy of `robust_replace()` that retries on *all* platforms — the two implementations are not currently kept in sync.

## GIF Processing
- Frames are streamed via a generator during `Image.save()` (each frame copied and yielded one at a time) to avoid holding the whole animation in memory at once — a memory-profile characteristic, not a size-gated code path.
- Per-frame durations captured and preserved; default 100ms if missing.
- Animated WebPs are not properly supported (see README known issues).

## Metadata Auto-Healing
If `pyexiv2` raises RuntimeError containing "IFD" or "corrupt":
1. Re-save image with Pillow (strips broken headers, quality=95).
2. Retry metadata write with pyexiv2 on the sanitized file.

## Known Limitations (for agents making changes)
- No automated test suite and no CI — verify changes manually against sample images.
- Logging is silent by default: without `--log`, `DEBUG`-level messages (including metadata-read failures) never surface, so failures can look like silent no-ops.
- `robust_replace()` is duplicated, not shared, between `img_tagger.py` and `clear_tags.py` (see Windows File Handling above).
