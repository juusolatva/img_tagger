# img_tagger core

Authoritative detailed spec lives in `AGENTS.md` (CLAUDE.md just `@`-includes it): parsing order, metadata schema, locking, auto-healing, GIF streaming. Read it before touching tagging/metadata logic; keep it in sync when behavior changes.

## Source map (flat, no package)
- `img_tagger.py` — single-file CLI. Pipeline: `process_directory` (`_find_images` → `_load_prompt` → `_create_client` → quit listener + `_run_tasks` [ThreadPoolExecutor + tqdm, totals in `_RunStats`] → `_print_report`) → `process_single_image` (format check → `is_already_processed` → `get_tags_ollama`/`get_tags_lm_studio` → `parse_model_output` → `normalize_tags` → `tag_image`) → `write_metadata` (JPEG/WebP/PNG via pyexiv2) or `write_gif_tags` (Pillow comment).
- `clear_tags.py` — standalone reset tool; duplicates `robust_replace`, Pillow auto-heal, GIF frame-streaming from `img_tagger.py`. Change both together.
- `prompt.txt` — overrides `DEFAULT_PROMPT`; unreadable file → `sys.exit(1)`.
- `tests/` — offline pytest suite (fake backends, generated images); `test_integration.py` is opt-in and uses `test_images/`.
- `test_images/` — gitignored local manual-test corpus (may not exist).

## Invariants
- `process_single_image` returns `(status, filename, message, duration)`; status ∈ SUCCESS/FAILED/SKIPPED/CANCELLED. Never raises — exceptions → FAILED.
- All pyexiv2 calls under module-level `metadata_lock`; model calls and Pillow work stay outside it.
- Writes go to `tempfile.mkstemp(dir=<image dir>)` copy, then `robust_replace`, cleanup in `finally`.
- Skip marker string `[PROCESSED BY AI]` (constant `PROCESSED_MARKER`) is the idempotency contract between tagger, clear tool and already-tagged user files — don't change its text.
- Worker bound (1–4) checked in `__main__` arg handling only.
- Optional imports (`msvcrt`, `termios`/`tty`, `ollama`, `openai`) fall back to `None`; code checks for `None` rather than importing lazily.

## Further memories
- Dependencies, Python version, available tools: `mem:tech_stack`
- Run/clear/log commands: `mem:suggested_commands`
- Code style (docstrings, typing mix, logging): `mem:conventions`
- What to verify before declaring done (pytest, no CI; dirty lint baseline; SonarQube): `mem:task_completion`
