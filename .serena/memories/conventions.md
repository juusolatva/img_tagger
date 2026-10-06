# Conventions
- Google-style docstrings (`Args:` / `Returns:`) on every function; summary sometimes on the opening line, sometimes the next — either is accepted.
- Type hints on all signatures. Mixed style exists: builtin generics (`list[str]`, `tuple[...]`) alongside `typing.Optional`/`List`/`Any`. Prefer builtin generics for new code; don't churn existing hints unprompted.
- Paths: functions take `Path` or `str` inconsistently (e.g. `tag_image`/`write_metadata` take `str`, pipeline uses `Path`); convert at the boundary, keep existing signatures.
- Logging via root `logging.*` with f-strings; `setup_logging` only attaches a file handler when `--log` given. In `except` blocks use `logging.exception()`, not `logging.error(..., exc_info=True)` (SonarQube S8572).
- User-facing CLI errors: `print(...)` + `sys.exit(1)` in `__main__`, not exceptions.
- Wrap low-level failures as `RuntimeError(f"... {path}: {e}")` at format-router level (`tag_image`).
- Split large functions into `_private` module-level helpers (SonarQube cognitive complexity ≤ 15). Repeated literals become module constants (`PROCESSED_MARKER`, `DEFAULT_MIME_TYPE`).
- Inline comments explain intent of non-obvious blocks (locking, auto-heal); keep similar density.
- Single quotes for pyexiv2 keys/kwargs, double quotes elsewhere (not enforced).
