# Tech stack
- Python ≥3.10 required (PEP 604/585 annotations). Dev machine: Fedora Linux, Python 3.14.
- Deps (`requirements.txt`, lower bounds only, no lockfile): `ollama`, `openai` (LM Studio OpenAI-compatible API), `pillow`, `pyexiv2` (Exiv2 binding; not thread-safe), `tqdm`.
- No packaging (`pyproject.toml`/`setup.py`), no venv convention, no tests, no CI.
- Backends: Ollama `http://localhost:11434`, LM Studio `http://localhost:1234`; default model `qwen3-vl:8b`. No request timeouts on either client.
- Installed locally but NOT configured in repo: `ruff`, `black`, `basedpyright` (Serena LSP is basedpyright). Their output reflects user-global defaults.
