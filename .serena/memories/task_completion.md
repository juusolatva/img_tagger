# Task completion checklist
No configured lint/format/test tooling. Minimum:
1. `python3 -m py_compile img_tagger.py clear_tags.py`
2. Optional `ruff check <changed file>` / `basedpyright <file>`: baseline already has ~59 ruff errors and many basedpyright warnings (user-global strict rules). Only ensure you introduced no new ones; don't mass-fix unrelated findings.
3. Behavior changes: manual run against sample images of each format (.jpg/.png/.webp/.gif, plus a corrupt-EXIF sample if touching auto-heal), then `clear_tags.py` to reset. Requires a running Ollama/LM Studio for the tagger; say so if not verifiable.
4. If file-handling helpers changed, mirror the change in the other script (see `mem:core`).
5. Update `AGENTS.md` (and README/TODO if user-visible) when behavior or invariants change.
