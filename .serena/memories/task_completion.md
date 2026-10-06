# Task completion checklist
No lint/format tooling; pytest suite in `tests/`. Minimum:
1. `python3 -m pytest` (offline, ~10s; install via `requirements-dev.txt`). Strict xfails pin known bugs — an XPASS means a fix landed; drop the marker.
2. Optional `ruff check <changed file>` / `basedpyright <file>`: baseline already has ~50 ruff errors (mostly LOG015 root-logger, BLE001 blind-except) and many basedpyright warnings (user-global strict rules). Only ensure you introduced no new ones; don't mass-fix unrelated findings.
3. Behavior changes: manual run against sample images of each format (.jpg/.png/.webp/.gif, plus a corrupt-EXIF sample if touching auto-heal), then `clear_tags.py` to reset. Requires a running Ollama/LM Studio for the tagger; say so if not verifiable.
4. If file-handling helpers changed, mirror the change in the other script (see `mem:core`).
5. Update `AGENTS.md` (and README if user-visible) when behavior or invariants change.
6. New code should stay within SonarQube rules the project already fixed: cognitive complexity ≤ 15, no duplicated literals, `logging.exception()` in `except`.
