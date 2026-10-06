import argparse
import base64
import contextlib
import os
import platform
import re
import select
import sys
import threading
import time
import logging
import json
import shutil
import tempfile
import pyexiv2
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Generator
from typing import Any, List, Optional
from PIL import Image, ImageSequence, PngImagePlugin

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

try:
    from ollama import Client as OllamaClient
except ImportError:
    OllamaClient = None
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

metadata_lock = threading.Lock()

PROCESSED_MARKER = "[PROCESSED BY AI]"
DEFAULT_MIME_TYPE = "image/jpeg"

DEFAULT_PROMPT = (
                "Analyze this image, which could be an internet meme, screenshot, artwork, or photograph. "
                "Extract 6 to 12 highly relevant keywords and return ONLY a comma-separated list of tags."
                "1. Always include the image type as the first tag (e.g., 'Meme', 'Screenshot', 'Artwork', 'Photo'). "
                "2. If it is a meme, identify the meme template/format, the main subjects, the core vibe/emotion, and 1-3 key words from the text. "
                "3. If it is a screenshot, summarize the main topic or software shown. "
                "4. All tags must be strictly in English. Do not use any other languages or alphabets. "
                "Return ONLY the comma-separated list of tags. No introductory text, bullet points, or quotes."
                )


def setup_logging(log_path: str | None) -> None:
    """
    Configures the logging system to write to a specified file.

    Args:
        log_path: The file path where logs should be written. If None,
            logging remains at default settings (usually stdout).
    """

    if log_path is None:
        return

    path_obj = Path(log_path)
    # Create parent directory if it doesn't exist
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        filename=str(path_obj),
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(threadName)s: %(message)s',
        filemode='a'
    )


def listen_for_quit(stop_event: threading.Event) -> None:
    """
    A daemon thread that monitors standard input for a 'q' keypress to trigger
    a graceful shutdown across different operating systems without hogging the CPU.

    Args:
        stop_event: A threading.Event object that will be set when 'q' is pressed.
    """

    if platform.system() == "Windows" and msvcrt is not None:
        _wait_for_quit_windows(stop_event)
        return

    try:
        with _cbreak_stdin():
            _wait_for_quit_posix(stop_event)
    except Exception as e:
        logging.debug(f"Terminal input unavailable ({e}); basic fallback active")
        # The extra sleep prevents hot-spinning on endless non-tty input (e.g. `yes |`)
        _wait_for_quit_posix(stop_event, idle_sleep=0.1)


def _wait_for_quit_windows(stop_event: threading.Event) -> None:
    """Polls the Windows console for a 'q' keypress until stop_event is set."""

    if msvcrt is None:
        return
    while not stop_event.is_set():
        if msvcrt.kbhit() and msvcrt.getch().decode("utf-8", errors="ignore").lower() == "q":
            stop_event.set()
            return
        time.sleep(0.05)  # Prevents a 100% CPU hot-spin on Windows when idle


@contextlib.contextmanager
def _cbreak_stdin() -> Generator[None, None, None]:
    """
    Puts the stdin terminal into cbreak mode (keys arrive without Enter) and
    restores the previous settings on exit.

    Raises:
        ImportError: If termios/tty are unavailable.
        termios.error: If stdin is not a terminal.
    """

    if termios is None or tty is None:
        raise ImportError("termios/tty not available")

    fd = sys.stdin.fileno()
    old_settings = getattr(termios, "tcgetattr")(fd)
    try:
        getattr(tty, "setcbreak")(fd)
        yield
    finally:
        getattr(termios, "tcsetattr")(fd, getattr(termios, "TCSADRAIN"), old_settings)


def _wait_for_quit_posix(stop_event: threading.Event, idle_sleep: float = 0.0) -> None:
    """
    Reads stdin one character at a time until 'q' is pressed (sets stop_event),
    stdin reaches EOF, or stop_event is set elsewhere.

    Args:
        stop_event: Event to set when 'q' is pressed.
        idle_sleep: Extra delay per loop iteration, on top of the select() timeout.
    """

    while not stop_event.is_set():
        # select waits up to 0.1 seconds for input
        if select.select([sys.stdin], [], [], 0.1)[0]:
            char = sys.stdin.read(1)
            if char == "":  # EOF reached (detached terminal / closed stdin)
                logging.debug("Stdin EOF reached; exiting Q monitor thread.")
                return
            if char.lower() == "q":
                stop_event.set()
                return
        time.sleep(idle_sleep)


def get_image_format(img_path: Path) -> Optional[str]:
    """
    Attempts to identify the image format using Pillow.

    Args:
        img_path: The path to the image file.

    Returns:
        The PIL format string (e.g., 'JPEG', 'PNG') or None if the file is not a valid image.
    """

    try:
        with Image.open(img_path) as img:
            img.verify()
        # Re-open because verify() closes/clears the file handle in some versions
        with Image.open(img_path) as img:
            return img.format
    except Exception:
        return None


def robust_replace(src: Path, dst: Path) -> None:
    """Atomically replace dst with src, retrying on transient OSErrors like lingering file locks on Windows)."""
    for _ in range(10):
        try:
            src.replace(dst)
            return
        except OSError:
            time.sleep(0.5)
    raise OSError(f"Failed to replace {src} with {dst} after retries.")


def write_metadata(image_path: str, tags_list: list[str]) -> None:
    """
    Writes tags to image metadata (JPEG, WebP, PNG) using pyexiv2.

    This function uses a temporary file for safe writing and includes an
    auto-healing fallback that sanitizes corrupted EXIF data using Pillow
    before retrying the write operation.

    Args:
        image_path: The path to the target image file.
        tags_list: A list of strings representing the tags to embed.
    """

    marker = PROCESSED_MARKER
    tags_str = ", ".join(tags_list)

    fd, temp_path = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
    os.close(fd)

    try:
        shutil.copy2(image_path, temp_path)
        with metadata_lock:
            try:
                # Primary attempt using pyexiv2 (separated into EXIF and XMP)
                with pyexiv2.Image(temp_path, encoding='utf-8') as img:
                    img.modify_exif({
                        'Exif.Photo.UserComment': f"{tags_str} {marker}"
                    })
                    img.modify_xmp({
                    'Xmp.dc.subject': tags_list,
                    'Xmp.dc.description': f"Tags: {tags_str} | {marker}"
                })

            except RuntimeError as e:
                # Auto-healing fallback for corrupted EXIF data (IFD buffer errors)
                if "IFD" in str(e).upper() or "corrupt" in str(e).lower():
                    logging.warning(f"Corrupted metadata in {Path(image_path).name}, sanitizing via Pillow...")

                    # Open with Pillow (more forgiving) and save to temp_path to strip broken EXIF
                    with Image.open(image_path) as pil_img:
                        pil_img.save(temp_path, format=pil_img.format, quality=95)

                    # Retry pyexiv2 on the newly cleaned temporary file
                    with pyexiv2.Image(temp_path, encoding='utf-8') as img:
                        img.modify_exif({
                        'Exif.Photo.UserComment': f"{tags_str} {marker}"
                        })
                        img.modify_xmp({
                        'Xmp.dc.subject': tags_list,
                        'Xmp.dc.description': f"Tags: {tags_str} | {marker}"
                        })

                else:
                    raise e # Re-raise if it's a different pyexiv2 error

            robust_replace(Path(temp_path), Path(image_path))

    finally:
        Path(temp_path).unlink(missing_ok=True)


def write_gif_tags(image_path: str, tags_list: list[str]) -> None:
    """
    Writes tags to GIF images using comments, handling large GIFs memory-efficiently.

    Args:
        image_path: The path to the GIF file.
        tags_list: A list of strings representing the tags to embed.
    """

    marker = PROCESSED_MARKER
    tags_str = ", ".join(tags_list)

    with Image.open(image_path) as img:
        loop = img.info.get("loop", 0)
        # Capture per-frame duration to preserve variable frame rates
        durations = [f.info.get("duration", 100) for f in ImageSequence.Iterator(img)]

    def frame_generator():
        # This generator streams frame copies one-by-one into the file writer.
        # It handles files larger than 64MB flawlessly with O(1) memory overhead.
        with Image.open(image_path) as img:
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i == 0:
                    continue
                yield frame.copy()

    fd, temp_path_str = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
    os.close(fd)
    temp_path = Path(temp_path_str)

    try:
        # Get the first frame to use as the base for saving
        with Image.open(image_path) as img:
            first_frame = ImageSequence.Iterator(img).__next__()
            first_frame_copy = first_frame.copy()

        first_frame_copy.save(
            temp_path,
            format="GIF",
            save_all=True,
            append_images=frame_generator(),
            duration=durations,
            loop=loop,
            comment=f"{tags_str} {marker}"
        )

        robust_replace(Path(temp_path), Path(image_path))

    finally:
        temp_path.unlink(missing_ok=True)


def tag_image(image_path: str, tags_list: list[str], fmt: Optional[str] = None) -> None:
    """
    Embeds a list of tags into an image's metadata.

    This function identifies the file format and routes to the appropriate
    writer (pyexiv2 for standard images, or custom logic for GIFs).

    Args:
        image_path: The filesystem path to the image file.
        tags_list: A list of strings representing the tags to embed.
        fmt: The image format (e.g., 'JPEG'). If None, it will be determined from the file.
    """

    img_path = Path(image_path)
    if fmt is None:
        fmt = get_image_format(img_path)

    if fmt is None:
        raise RuntimeError(f"Could not determine format for {image_path}")

    try:
        if fmt.lower() in ["jpg", "jpeg", "webp", "png"]:
            write_metadata(image_path, tags_list)
        elif fmt.lower() == "gif":
            write_gif_tags(image_path, tags_list)
        else:
            raise ValueError(f"Unsupported file format: {fmt}")
    except Exception as e:
        raise RuntimeError(f"Tagging failed for {image_path}: {e}")


def get_tags_ollama(client: Any,
                    model: str,
                    img_path: Path,
                    prompt: str
                    ) -> str:
    """
    Sends an image and prompt to an Ollama server for tag generation.

    Args:
        client: The Ollama client instance.
        model: The identifier for the model to use (e.g., 'qwen3-vl:8b').
        img_path: The path to the image file.
        prompt: The prompt instructions for generating tags.

    Returns:
        A string containing the generated tags from the model response.
    """

    logging.debug(f"Requesting tags from Ollama ({model}) for {img_path}")
    response = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [str(img_path)]}],
    )
    content = (
        response.message.content
        if hasattr(response, "message")
        else response["message"]["content"]
    )
    logging.debug(f"Raw Ollama response for {img_path}: {content}")
    return content


def get_tags_lm_studio(client: Any, model: str, img_path: Path, prompt: str) -> str:
    """
    Encodes the image to base64 and sends a request to an LM Studio server.

    Args:
        client: The OpenAI-compatible client instance (e.g., LM Studio).
        model: The model identifier to use.
        img_path: The path to the image file.
        prompt: The text prompt for generating tags.

    Returns:
        A string containing the generated tags from the response content.
    """

    logging.debug(f"Requesting tags from LM Studio ({model}) for {img_path}")
    fmt = get_image_format(img_path)
    if fmt is None:
        logging.error(f"Pillow could not determine the format for {img_path}")
    mime_map = {
        "jpeg": DEFAULT_MIME_TYPE,
        "jpg": DEFAULT_MIME_TYPE,
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif"
    }
    mime_type = mime_map.get((fmt or "").lower(), DEFAULT_MIME_TYPE)

    with open(img_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode("utf-8")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
                    },
                ],
            }
        ],
    )
    content = response.choices[0].message.content
    # Removed logging of 'content' to prevent massive log files with base64 image data
    logging.debug(f"Raw LM Studio response received for {img_path}")
    return content


def parse_model_output(raw_output: str) -> Optional[List[str]]:
    """Parse model output and extract tags robustly with normalization.

    Tries multiple patterns (JSON, bullet points, intro text, comma-separated)
    to handle various LLM output styles. Returns None if parsing fails or
    insufficient tags are extracted.

    Args:
        raw_output: The raw string output from the LLM.

    Returns:
        A list of normalized tags, or None if no valid tags could be extracted.
    """

    # Thinking models (e.g. qwen3, deepseek-r1) may emit <think>...</think> reasoning before the answer
    if "</think>" in raw_output:
        raw_output = raw_output.rsplit("</think>", 1)[1]

    # Pattern 1: JSON array ["tag1", "tag2"] - most reliable
    parsed = None

    # Fast path: whole output is JSON
    try:
        parsed = json.loads(raw_output)
    except (ValueError, RecursionError):
        parsed = None

    # Fallback: find first bracketed segment and parse that
    if not isinstance(parsed, list):
        start = raw_output.find("[")
        end = raw_output.rfind("]")
        if start != -1 and end != -1 and end > start:
            candidate = raw_output[start:end + 1]
            try:
                parsed = json.loads(candidate)
            except (ValueError, RecursionError):
                parsed = None

    if isinstance(parsed, list):
        json_tags = [str(x).strip().lower() for x in parsed if isinstance(x, (str, int, float))]
        normalized = normalize_tags(json_tags)
        if normalized:
            return normalized
    # Pattern 2: Bullet points at line start (minimum 6 items expected); hyphens inside tags are not bullets
    bullets = re.findall(r'^[ \t]*[-•★●][ \t]*(\S.*)', raw_output, re.MULTILINE)
    if len(bullets) >= 6:
        return normalize_tags([t.strip().strip('"').lower() for b in bullets for t in b.split(",")])

    # Pattern 3: Numbered lists like "1. tag" (minimum 6 items expected)
    numbered = re.findall(r'(?<!\d)\d+\.\s+(.+)', raw_output)
    if len(numbered) >= 6:
        return normalize_tags([t.strip().strip('"').lower() for n in numbered for t in n.split(",")])

    # Pattern 4: "Here are the tags:" style intro text extraction. Needs a colon later on the same line
    # ("tags for this image:") or a dash right after, so a list item like "text tags" doesn't count.
    text_after_intro = re.match(
        r'.{0,40}?\b(?:tags|keywords)\b(?:[^:\n]{0,40}:|\s*-)\s*(.+)', raw_output, re.IGNORECASE | re.DOTALL
    )
    if text_after_intro:
        return normalize_tags(parse_text_tags(text_after_intro.group(1)))

    # Fallback: comma- and/or newline-separated
    return normalize_tags([tag.strip().strip('"\'').lower() for tag in re.split(r'[,\n]', raw_output) if tag.strip()])


def parse_text_tags(text: str) -> List[str]:
    """Parse tags from text that follows intro patterns.

    Args:
        text: The raw text output from the model.

    Returns:
        A list of extracted and cleaned tag strings.
    """

    # Split by commas or newlines
    parts = re.split(r'[,\n]+', text)
    tags = []
    for part in parts:
        part = part.strip().strip('"\'').lower()
        # Skip common non-tag words (short real tags like '3d' or 'ui' are kept)
        if part and part not in ['and', 'or', 'the', 'a', 'an', 'is', 'are', 'of']:
            tags.append(part)
    return tags


def normalize_tags(tags: List[str], min_count: int = 6, max_count: int = 12) -> Optional[List[str]]:
    """Normalize tags and enforce count constraints.

    Normalizes each tag (lowercase, strip whitespace), removes duplicates while
    preserving order, then enforces the 6-12 tag minimum from your prompt.
    Returns None if fewer than min_count valid tags remain.

    Args:
        tags: A list of raw tags extracted from the model output.
        min_count: The minimum number of tags required to be considered valid.
        max_count: The maximum number of tags to keep.

    Returns:
        A list of normalized tags, or None if the count is below min_count.
    """

    # Strip, lowercase, remove duplicates while preserving order
    seen = set()
    normalized = []
    for tag in tags:
        tag = tag.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            normalized.append(tag)

    # Enforce max_count (12) by trimming excess
    if len(normalized) > max_count:
        return normalized[:max_count]

    # Return None if we don't meet minimum, let caller decide how to handle it
    return normalized if len(normalized) >= min_count else None


def _contains_marker(value: object) -> bool:
    """Returns True if a metadata value (str, bytes or other) contains the processed marker."""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    return bool(value) and PROCESSED_MARKER in str(value)


def _pyexiv2_has_marker(p: Path) -> bool:
    """
    Checks every EXIF and XMP value for the processed marker.

    Raises:
        Exception: If pyexiv2 cannot open the file.
    """

    values = []
    with metadata_lock:
        # Explicitly pass encoding='utf-8' to handle non-ASCII path characters like 'ö'
        with pyexiv2.Image(str(p), encoding='utf-8') as img:
            # Read EXIF and XMP independently so one failing doesn't kill both
            for read in (img.read_exif, img.read_xmp):
                try:
                    values.extend(read().values())
                except Exception:
                    pass
    return any(_contains_marker(v) for v in values)


def _pillow_has_marker(p: Path) -> bool:
    """
    Checks Pillow's text metadata (e.g. PNG text chunks) for the processed marker.

    Raises:
        Exception: If Pillow cannot open the file.
    """

    with Image.open(p) as img:
        return any(isinstance(v, str) and PROCESSED_MARKER in v for v in img.info.values())


def is_already_processed(img_path: Path) -> bool:
    """Checks if the image already contains the AI processed marker.

    Args:
        img_path: The path to the image file to check.

    Returns:
        True if the marker is found in metadata or comments, False otherwise.
    """

    p = Path(img_path)
    ext = p.suffix.lower().lstrip(".")

    if ext in ["jpg", "jpeg", "webp", "png"]:
        # 1. Primary metadata inspection using pyexiv2
        try:
            if _pyexiv2_has_marker(p):
                return True
        except Exception as e:
            logging.debug(f"pyexiv2 metadata read failed for {p.name} ({e}); trying Pillow fallback.")

        # 2. Fully isolated fallback check using Pillow
        try:
            return _pillow_has_marker(p)
        except Exception as e:
            logging.debug(f"Pillow fallback failed for {p.name}: {e}")

    elif ext == "gif":
        try:
            with Image.open(p) as img:
                return _contains_marker(img.info.get("comment"))
        except Exception as e:
            logging.debug(f"Pillow failed to read GIF comments for {p.name}: {e}")

    return False


def process_single_image(
    img_path: Path,
    client: Any,
    backend: str,
    model: str,
    prompt: str,
    stop_event: threading.Event
    ) -> tuple[str, str, str, float]:
    """Handles the full pipeline for a single image: validation -> AI -> tagging.

    Args:
        img_path: The path to the image file to process.
        client: The client instance for the chosen backend (Ollama or OpenAI).
        backend: The backend type ('ollama' or 'lm-studio').
        model: The model identifier to use.
        prompt: The prompt instructions for tag generation.
        stop_event: A threading.Event to monitor for user cancellation.

    Returns:
        A tuple containing (status, filename, message, duration).
    """

    logging.info(f"Processing {img_path.name}")
    fmt = get_image_format(img_path)
    if fmt is None:
        logging.warning(f"Skipping invalid/unsupported file: {img_path.name}")
        return "FAILED", img_path.name, "Skipping unsupported or corrupted file", 0

    # Skip check
    if is_already_processed(img_path):
        logging.info(f"Skipping already processed image: {img_path.name}")
        return "SKIPPED", img_path.name, "Skipped: Already Tagged", 0

    if stop_event.is_set():
        return "CANCELLED", img_path.name, "Cancelled by user", 0

    start = time.time()  # Start timing the actual processing
    try:
        if backend == "ollama":
            raw_output = get_tags_ollama(client, model, img_path, prompt)
        else:
            raw_output = get_tags_lm_studio(client, model, img_path, prompt)

        # Parse model output robustly (handles JSON, bullets, intro text, or comma-separated)
        parsed_tags = parse_model_output(raw_output)

        if not parsed_tags:
            logging.warning(f"Failed to extract valid tags from model response for {img_path.name}")
            return (
                "FAILED",
                img_path.name,
                "Invalid or insufficient tags returned from model",
                time.time() - start,
            )

        tag_image(str(img_path), parsed_tags, fmt=fmt)
        logging.info(f"Successfully tagged {img_path.name} with {parsed_tags}")
        return (
            "SUCCESS",
            img_path.name,
            f"Generated Tags: {parsed_tags}",
            time.time() - start,
        )

    except Exception as e:
        logging.exception(f"Error processing {img_path.name}: {e}")
        return "FAILED", img_path.name, str(e), time.time() - start


@dataclass
class _RunStats:
    """Running totals for one process_directory() call."""

    success: int = 0
    skipped: int = 0
    failed: int = 0
    success_duration: float = 0.0
    failed_log: list[tuple[str, str]] = field(default_factory=list)

    def record(self, status: str, name: str, message: str, duration: float) -> None:
        """Prints one process_single_image() result and adds it to the totals."""

        if status == "SUCCESS":
            tqdm.write(f"  [✓] {name} -> {message}")
            self.success += 1
            self.success_duration += duration
        elif status == "SKIPPED":
            tqdm.write(f"  [-] {name} -> {message}")
            self.skipped += 1
        elif status == "CANCELLED":
            # Do nothing for cancelled tasks to keep the console clean
            pass
        else:  # FAILED
            tqdm.write(f"  [!] {name} -> {message}")
            self.failed += 1
            self.failed_log.append((name, message))


def _find_images(directory: str, recursive: bool) -> list[Path]:
    """Lists files in directory (optionally recursive) with a supported image extension."""

    base_path = Path(directory)
    files = base_path.rglob("*") if recursive else base_path.iterdir()
    # We keep a broad filter for performance, but the final check is done in process_single_image via get_image_format
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    return [f for f in files if f.is_file() and f.suffix.lower() in valid_extensions]


def _load_prompt() -> str:
    """
    Loads prompt.txt from the script directory, falling back to DEFAULT_PROMPT
    if it doesn't exist. Exits the program if it exists but cannot be read.
    """

    prompt_file = Path(__file__).parent / "prompt.txt"
    if not prompt_file.exists():
        logging.warning(f"prompt.txt not found at {prompt_file}; using default")
        print(f"Warning: Could not read prompt.txt from {prompt_file}. Falling back to default.")
        return DEFAULT_PROMPT

    try:
        return prompt_file.read_text(encoding="utf-8").strip()
    except Exception as e:
        logging.exception(f"Failed to read prompt.txt: {e}")
        sys.exit(1)


def _create_client(backend: str, host: str) -> Any:
    """
    Builds the model client for the chosen backend.

    Returns:
        The client, or None (after printing an error) if its library is not installed.
    """

    if backend == "ollama":
        if OllamaClient is None:
            print("Error: 'ollama' library not found. Please install it using 'pip install ollama'.")
            return None
        return OllamaClient(host=host)

    if OpenAI is None:
        print("Error: 'openai' library not found. Please install it using 'pip install openai'.")
        return None
    return OpenAI(base_url=f"{host}/v1", api_key="lm-studio")


def _run_tasks(
    image_files: list[Path],
    client: Any,
    backend: str,
    model: str,
    prompt: str,
    max_workers: int,
    stop_event: threading.Event
    ) -> _RunStats:
    """Tags image_files concurrently with a progress bar and returns the totals."""

    stats = _RunStats()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_image = {
            executor.submit(
                process_single_image,
                img_path,
                client,
                backend,
                model,
                prompt,
                stop_event,
            ): img_path
            for img_path in image_files
        }

        stopped_notified = False
        for future in tqdm(as_completed(future_to_image), total=len(image_files), desc="Processing images"):
            if stop_event.is_set() and not stopped_notified:
                tqdm.write(
                    "  [!] Stop signal received (Q pressed). Finishing currently running tasks..."
                )
                stopped_notified = True

            try:
                stats.record(*future.result())
            except Exception as e:
                img_path = future_to_image[future]
                tqdm.write(f"  [!] {img_path} -> Unexpected Error: {e}")
                stats.failed += 1

    return stats


def _print_report(stats: _RunStats, total_seconds: float) -> None:
    """Prints the end-of-run summary."""

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    print("\n" + "=" * 50)
    print("                PROCESSING REPORT")
    print("=" * 50)
    print(f" Processed images: {stats.success}")
    print(f" Skipped images: {stats.skipped}")
    print(f" Failed images: {stats.failed}")
    print(f" Total time elapsed: {int(hours)}h {int(minutes)}m {seconds:.2f}s")

    if stats.success > 0:
        avg_latency = stats.success_duration / stats.success
        print(f" Average processing time: {avg_latency:.2f} seconds")

    if stats.failed_log:
        print("\n--- Failed files details ---")
        for filename, error_msg in stats.failed_log:
            print(f" * {filename} -> {error_msg}")

    print("=" * 50)


def process_directory(
    directory: str,
    recursive: bool,
    backend: str,
    host: str,
    model: str,
    max_workers: int
    ) -> None:
    """
    Orchestrates the image tagging process for a directory or its subdirectories.

    This function handles prompt loading, backend initialization (Ollama or LM Studio),
    concurrent task distribution via ThreadPoolExecutor, and provides a real-time progress
    bar and final summary report.

    Args:
        directory: The root directory to scan for images.
        recursive: Whether to include subdirectories in the search.
        backend: The model provider backend ('ollama' or 'lm-studio').
        host: The endpoint URL for the chosen backend.
        model: The specific model name/ID to use.
        max_workers: The maximum number of concurrent threads (limited to 4).
    """

    image_files = _find_images(directory, recursive)
    if not image_files:
        print(f"No valid images found in '{directory}'.")
        return

    print(f"Initialized backend: {backend} | Target: {host}")
    print(f"Found {len(image_files)} images to process. Starting...\n")

    prompt = _load_prompt()
    client = _create_client(backend, host)
    if client is None:
        return

    start_time = time.time()

    # Concurrency and quitting
    stop_event = threading.Event()
    quit_thread = threading.Thread(
        target=listen_for_quit, args=(stop_event,), daemon=True
    )
    quit_thread.start()

    try:
        print(f"Starting concurrent processing (max_workers={max_workers}).")
        print("Press 'q' at any time to stop and see the current report.\n")
        stats = _run_tasks(image_files, client, backend, model, prompt, max_workers, stop_event)
    except Exception as e:
        print(f"An unexpected error occurred during processing: {e}")
        raise
    finally:
        if quit_thread.is_alive():
            quit_thread.join(timeout=2)

    _print_report(stats, time.time() - start_time)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Image tagger using a vision-language model"
    )
    parser.add_argument("directory", help="Path to your image folder")
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="process subdirectories recursively",
    )
    parser.add_argument(
        "--backend",
        choices=["ollama", "lm-studio"],
        default="ollama",
        help="local model provider backend (ollama by default)",
    )
    parser.add_argument(
        "--host", help="backend endpoint URL (localhost by default)"
    )
    parser.add_argument(
        "--model",
        default="qwen3-vl:8b",
        help="model identification tag (defaults to qwen3-vl:8b)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="number of concurrent workers (max 4, default 1)"
    )
    parser.add_argument(
        "--log",
        help="enable logging and create log file (e.g., --log logs/run.log)"
    )

    args = parser.parse_args()

    setup_logging(args.log)

    if not Path(args.directory).is_dir():
        print(f"Error: The folder '{args.directory}' could not be located.")
        sys.exit(1)

    # Max 4 workers as it's the default limit for both ollama and LM Studio.
    if not 1 <= args.workers <= 4:
        print(f"Error: Workers must be between 1 and 4, got {args.workers}")
        sys.exit(1)

    if not args.host:
        args.host = (
            "http://localhost:11434"
            if args.backend == "ollama"
            else "http://localhost:1234"
        )

    process_directory(
        args.directory,
        args.recursive,
        args.backend,
        args.host,
        args.model,
        args.workers,
    )
