# Suggested commands (Linux: use `python3`, `python` may be unaliased)
- Install: `pip3 install -r requirements.txt`
- Tag (Ollama default): `python3 img_tagger.py <dir>`
- Tag via LM Studio: `python3 img_tagger.py <dir> --backend lm-studio`
- Options: `-r` recursive, `--model <m>`, `--workers 1..4`, `--host <url>`, `--log <file>` (only way to see DEBUG/INFO diagnostics).
- Reset tags for re-testing: `python3 clear_tags.py <dir> [-r]`
- Help: `python3 img_tagger.py -h`
- Inspect written metadata: `exiftool -UserComment -XMP:Subject -XMP:Description <img>` / `exiftool -Comment <gif>` (if exiftool installed).
- Tagging run needs a live model server; press `Q` for graceful quit (needs a TTY).
