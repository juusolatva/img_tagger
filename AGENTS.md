# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Overview
- `img_tagger.py` — main CLI: tags JPEG/WebP/PNG/GIF files via a local vision-language model (Ollama or LM Studio), writing tags into image metadata.
- `clear_tags.py` — standalone helper that strips tags/metadata back out (used to reset images for testing).
- `prompt.txt` — externalized LLM prompt; takes precedence over the `DEFAULT_PROMPT` fallback hardcoded in `img_tagger.py`.
- Flat repo layout: no package structure, no `pyproject.toml`/`setup.py`, no CI. Tests live in `tests/` (pytest, configured by `pytest.ini`).
- Requires **Python 3.10+** (uses modern type annotations like `str | None` and `list[str]`).

## Build/Lint/Test Commands
No lint tooling is configured.
- Dependencies: `pip install -r requirements.txt` (or `pip3 install -r requirements.txt`); test deps: `pip install -r requirements-dev.txt`
- Run tests: `python3 -m pytest` (offline: images generated on the fly, model backends faked; ~10s)
- Integration tests (opt-in, need a live backend + images in git-ignored `test_images/`; copies images to a temp dir, prints tags for manual quality review): `python3 -m pytest -m integration --run-integration -s [--tagger-backend lm-studio] [--tagger-host URL] [--tagger-model M] [--images-dir DIR]`
- Run tagger (Ollama, default): `python3 img_tagger.py <directory>`
- Run tagger (LM Studio): `python3 img_tagger.py <directory> --backend lm-studio`
- Recursive, custom model/workers: `python3 img_tagger.py <directory> -r --model <model> --workers 4`
- Clear all tags: `python3 clear_tags.py <directory> [-r]`
- Help: `python3 img_tagger.py -h`
- Logging: `python3 img_tagger.py <dir> --log output.log` (without `--log`, diagnostic `DEBUG` messages never surface — see Known Limitations)
- Note: On Linux environments, use `python3` if `python` is not aliased.

## Backends
- `--backend ollama` (default): `ollama` Python client, default host `http://localhost:11434`, default model `qwen3-vl:8b`.
- `--backend lm-studio`: `openai` client against LM Studio's OpenAI-compatible API, default host `http://localhost:1234`, images sent as base64 data URLs. `api_key="lm-studio"` is a required placeholder string, not a real credential.
- Neither client is constructed with a request timeout — a hung local model server can block a worker thread indefinitely.

## Core Rules & Architecture
- **Worker limit**: Max 4 concurrent workers (matches both backends' default concurrency limits). Default is 1. Enforced in CLI arg parsing (`1 <= workers <= 4`), not inside `process_directory()` itself.
- **Skip marker**: Files with `[PROCESSED BY AI]` in EXIF/XMP/GIF comment are skipped automatically (`is_already_processed()`).
- **Prompt source**: `prompt.txt` in the script directory takes precedence over the in-code `DEFAULT_PROMPT` fallback. If `prompt.txt` exists but cannot be read, execution aborts (`sys.exit(1)`).
- **Tag count & normalization**: Output is normalized via `normalize_tags()` (strip whitespace, lowercase, order-preserving deduplication, capped at 12 tags). Fewer than 6 valid tags after parsing marks that image as `FAILED`.
- **Tag parsing**: `parse_model_output()` tries several patterns in order:
  1. JSON array (`json.loads` on full string or bracketed `[...]` substring).
  2. Bullet points (`[-•★●]`, minimum 6 items expected; splits each line on commas).
  3. Numbered lists (`\d+\.`, minimum 6 items expected; splits each line on commas).
  4. Intro text matching `.{0,40}?\b(?:tags|keywords)\b\s*[:\-]?\s*(.+)` (parsed via `parse_text_tags()`, which strips common English stop words).
  5. Comma-separated list fallback.
- **Concurrency & Locks**: Images are processed in parallel via `ThreadPoolExecutor`. `pyexiv2` is not thread-safe: all `pyexiv2` reads (`is_already_processed()`) and writes (`write_metadata()`) are serialized through `metadata_lock = threading.Lock()`. Model API calls and Pillow operations (GIF read/write, format verification) are intentionally outside the lock to preserve concurrency.
- **Graceful quit**: Press `Q` during execution to trigger graceful shutdown. Monitored by a daemon thread (`listen_for_quit()`) using `msvcrt` on Windows or `termios`/`tty`/`select` on POSIX (with EOF detection for detached terminals). In-flight requests finish, but queued/unstarted tasks are marked `CANCELLED`.

## Metadata Schema
- **JPEG / WebP / PNG**:
  - EXIF: `Exif.Photo.UserComment` contains `"<tag1>, <tag2>, ... [PROCESSED BY AI]"`.
  - XMP: `Xmp.dc.subject` contains the raw tag list (`['tag1', 'tag2', ...]`); `Xmp.dc.description` contains `"Tags: <tag1>, <tag2>, ... | [PROCESSED BY AI]"`.
- **GIF**: Comment field only (no EXIF/XMP support). Stored as `"<tag1>, <tag2>, ... [PROCESSED BY AI]"`.
- **Metadata Clearing (`clear_tags.py`)**: Strips EXIF, XMP, and IPTC (`img.clear_iptc()`) for standard images, and empties `comment=""` for GIFs.

## File Replacement & Atomic Safety
- **Temp file placement**: Temporary files (`tempfile.mkstemp`) MUST always be created in the target image's directory (`dir=Path(image_path).parent`). This ensures replacements remain atomic on the same filesystem/mount point without cross-device link errors.
- **`robust_replace(src, dst)`**: Retries `src.replace(dst)` up to 10× with 0.5s delay upon `OSError` across **all platforms** (handles Windows file lock release and transient I/O locks). Both `img_tagger.py` and `clear_tags.py` implement this retry logic.
- **Temp cleanup**: Always wrapped in `try...finally` using `temp_path.unlink(missing_ok=True)`.

## GIF Processing
- Frames are streamed via a generator during `Image.save()` (each frame copied and yielded one at a time) to avoid loading all frames into RAM, maintaining O(1) memory overhead even for large animated GIFs.
- Per-frame durations are extracted from `img.info` and preserved (defaulting to 100ms if missing).
- Animated WebPs are not properly supported (see README known issues).

## Metadata Auto-Healing & Sanitization
If `pyexiv2` raises a `RuntimeError` containing `"IFD"` or `"corrupt"`:
1. Re-save image with Pillow (`quality=95` for lossy formats) to strip broken headers.
2. Retry metadata write/clear with pyexiv2 on the sanitized file.
*Caution*: Pillow re-saving can re-encode/compress lossy images (JPEG/WebP).

## Known Limitations & Maintenance Notes
- **Tests**: `python3 -m pytest` covers parsing, metadata round-trips for every format, GIF frame/duration preservation, auto-healing (simulated), temp-file cleanup, fake-backend pipeline, quit listener and CLI. No CI. Tagging *quality* still needs a manual run with `--run-integration` against real images.
- **Known bugs pinned as strict `xfail`** (fixing one makes it XPASS → remove the marker): parser misses newline-only lists, hyphenated tags in numbered lists, the word "tags" early in a plain list, ≤2-char tags after a `Tags:` intro, `<think>` blocks; `clear_tags.py` cannot clear XMP on WebP (pyexiv2 `clear_xmp()` no-op); `listen_for_quit()` misses a `q` arriving in the same read burst as another key.
- **Silent logging by default**: Without `--log`, `DEBUG` logs (including pyexiv2 read failures and fallback activations) do not display. Failures may look like silent no-ops.
- **No request timeouts**: Ollama and LM Studio HTTP clients lack request timeouts; hung local model servers can block worker threads indefinitely.
- **Duplicated utility logic**: `robust_replace()`, Pillow auto-healing fallback, and GIF streaming generator logic are duplicated between `img_tagger.py` and `clear_tags.py`. Keep both in sync when modifying file handling.
