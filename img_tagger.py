import argparse
import base64
import os
import platform
import re
import select
import sys
import threading
import time
import logging
import shutil
import tempfile
import pyexiv2
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
    If no path is provided, logging remains at default settings.
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
    """

    if platform.system() == "Windows" and msvcrt is not None:
        while not stop_event.is_set():
            if msvcrt.kbhit():
                try:
                    key = msvcrt.getch().decode("utf-8").lower()
                    if key == "q":
                        stop_event.set()
                        break
                except Exception:
                    pass
            time.sleep(0.05)  # Prevents a 100% CPU hot-spin on Windows when idle
    else:
        try:
            if termios is None or tty is None:
                raise ImportError("termios/tty not available")

            fd = sys.stdin.fileno()
            old_settings = getattr(termios, "tcgetattr")(fd)
            try:
                getattr(tty, "setcbreak")(fd)
                while not stop_event.is_set():
                    # select waits up to 0.1 seconds for input
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        char = sys.stdin.read(1)
                        if char == "":  # EOF reached (detached terminal / closed stdin)
                            logging.debug("Stdin EOF reached; exiting Q monitor thread.")
                            break
                        if char.lower() == "q":
                            stop_event.set()
                            break
            finally:
                getattr(termios, "tcsetattr")(fd, getattr(termios, "TCSADRAIN"), old_settings)
        except (ImportError, AttributeError, Exception) as e:
            logging.debug(f"Terminal input unavailable ({e}); basic fallback active")
            while not stop_event.is_set():
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    char = sys.stdin.read(1)
                    if char == "":
                        break
                    if char.lower() == "q":
                        stop_event.set()
                        break
                time.sleep(0.1)  # Prevents hot-spinning in the fallback loop


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

    marker = "[PROCESSED BY AI]"
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
                        pil_img.save(temp_path, format=pil_img.format)

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

            Path(temp_path).replace(image_path)
            logging.debug(f"Successfully wrote metadata using pyexiv2 for {image_path}")

    finally:
        Path(temp_path).unlink(missing_ok=True)


def write_gif_tags(image_path: str, tags_list: list[str]) -> None:
    """
    Writes tags to GIF images using comments, handling large GIFs memory-efficiently.
    Note: For files larger than 64MB, frames are temporarily saved as PNGs to maintain
    color quality, but variable frame durations are not supported (they default to a
    uniform duration).
    """

    marker = "[PROCESSED BY AI]"
    tags_str = ", ".join(tags_list)

    file_size = Path(image_path).stat().st_size
    MAX_MEMORY_SIZE = 64 * 1024 * 1024  # 64MB

    if file_size <= MAX_MEMORY_SIZE:
        # In-memory approach for smaller GIFs to avoid I/O thrashing
        with Image.open(image_path) as img:
            duration = img.info.get("duration", 100)
            loop = img.info.get("loop", 0)
            frames = [frame.copy() for frame in ImageSequence.Iterator(img)]

        fd, temp_path_str = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
        os.close(fd)
        temp_path = Path(temp_path_str)

        try:
            first_frame = frames[0]
            first_frame.save(
                temp_path,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=duration,
                loop=loop,
                comment=f"{tags_str} {marker}"
            )

            # Robustly replace the file using Path objects with retries for Windows handle release
            success = True
            if platform.system() == "Windows":
                success = False
                for _ in range(10):  # Increased retries and sleep duration
                    try:
                        temp_path.replace(image_path)
                        success = True
                        break
                    except OSError:
                        time.sleep(0.5)

            if not success:
                raise OSError(f"Failed to replace {temp_path} with {image_path} after retries.")
        finally:
            if temp_path.exists():
                temp_path.unlink()
    else:
        logging.info(f"Large GIF detected ({Path(image_path).stat().st_size} bytes). Using disk-based processing with uniform duration.")
        # Original disk-based approach for larger GIFs (> 64MB)
        duration = 100
        loop = 0
        frame_paths = []

        with tempfile.TemporaryDirectory(dir=Path(image_path).parent) as tmp_dir:
            with Image.open(image_path) as img:
                duration = img.info.get("duration", duration)
                loop = img.info.get("loop", loop)
                for i, frame in enumerate(ImageSequence.Iterator(img)):
                    p = Path(tmp_dir) / f"frame_{i:04d}.png"
                    frame.save(p, format="PNG")
                    frame_paths.append(p)

            # Source image handle is now closed. Create the new GIF from saved frames inside the temp dir.
            fd, temp_path_str = tempfile.mkstemp(dir=tmp_dir, suffix=".tmp")
            os.close(fd)
            temp_path = Path(temp_path_str)

            try:
                def frame_generator():
                    for p in frame_paths[1:]:
                        f = Image.open(p)
                        yield f
                        # Removed f.close() to prevent ValueError due to Pillow's lazy loading

                first_frame = Image.open(frame_paths[0])
                first_frame.save(
                    temp_path,
                    format="GIF",
                    save_all=True,
                    append_images=frame_generator(),
                    duration=duration,
                    loop=loop,
                    comment=f"{tags_str} {marker}"
                )
                first_frame.close()

                # Robustly replace the file using Path objects with retries for Windows handle release
                success = True
                if platform.system() == "Windows":
                    success = False
                    for _ in range(10):  # Increased retries and sleep duration
                        try:
                            temp_path.replace(image_path)
                            success = True
                            break
                        except OSError:
                            time.sleep(0.5)

                if not success:
                    raise OSError(f"Failed to replace {temp_path} with {image_path} after retries.")

            finally:
                if temp_path.exists():
                    temp_path.unlink()


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
        mime_type = "image/jpeg"
    else:
        fmt_lower = fmt.lower()
        mime_map = {
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif"
        }
        mime_type = mime_map.get(fmt_lower, "image/jpeg")

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
    """

    # Pattern 1: JSON array ["tag1", "tag2"] - most reliable
    json_match = re.search(r'\[\s*"?([^",\]]+(?:"[^",\]]*)?)+\s*\]', raw_output)
    if json_match:
        return normalize_tags(extract_json_tags(json_match.group()))

    # Pattern 2: Bullet points or numbered lists (minimum 6 items expected)
    bullets = re.findall(r'[-•★●]\s*(.+)', raw_output)
    if len(bullets) >= 6:
        return normalize_tags([b.strip().strip('"').lower() for b in bullets])

    # Pattern 3: Numbered lists like "1. tag" (minimum 6 items expected)
    numbered = re.findall(r'\d+\.\s+(.+)', raw_output)
    if len(numbered) >= 6:
        return normalize_tags([n.strip().strip('"').lower() for n in numbered])

    # Pattern 4: "Here are the tags:" style intro text extraction
    text_after_intro = re.search(r'(?:tags|keywords)\s*[:\-]? (.+)', raw_output, re.IGNORECASE)
    if text_after_intro:
        return normalize_tags(parse_text_tags(text_after_intro.group(1)))

    # Fallback: comma-separated (current behavior)
    return normalize_tags([tag.strip().strip('"\'').lower() for tag in raw_output.split(",") if tag.strip()])


def extract_json_tags(json_string: str) -> List[str]:
    """Extract tag strings from a JSON array string."""
    # Simple parser for ["tag1", "tag2"] style arrays
    tags = []
    current = ""
    in_quotes = False

    for char in json_string:
        if char == '"' and (not current or current[-1] != '\\'):
            in_quotes = not in_quotes
            if in_quotes:
                current += char
            else:
                # Check for trailing comma before closing bracket
                if char == ']' and not current.strip():
                    break
        elif not in_quotes:
            if char.isalpha():
                current += char
            elif char in ',]':
                if current.strip() and current.strip().lower() not in ['and', 'or']:
                    tags.append(current.strip().lower())
                current = ""

    return tags


def parse_text_tags(text: str) -> List[str]:
    """Parse tags from text that follows intro patterns."""
    # Split by commas or newlines
    parts = re.split(r'[,\\n]+', text)
    tags = []
    for part in parts:
        part = part.strip().strip('"\'').lower()
        # Skip common non-tag words
        if part and len(part) > 2 and part not in ['and', 'or', 'the', 'a', 'an', 'is', 'are', 'of']:
            tags.append(part)
    return tags


def normalize_tags(tags: List[str], min_count: int = 6, max_count: int = 12) -> Optional[List[str]]:
    """Normalize tags and enforce count constraints.

    Normalizes each tag (lowercase, strip whitespace), removes duplicates while
    preserving order, then enforces the 6-12 tag minimum from your prompt.
    Returns None if fewer than min_count valid tags remain.
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


def is_already_processed(img_path: Path) -> bool:
    """Checks if the image already contains the AI processed marker."""

    marker = "[PROCESSED BY AI]"
    p = Path(img_path)
    ext = p.suffix.lower().lstrip(".")

    if ext in ["jpg", "jpeg", "webp", "png"]:
        # 1. Primary metadata inspection using pyexiv2
        try:
            with metadata_lock:
                # Explicitly pass encoding='utf-8' to handle non-ASCII path characters like 'ö'
                with pyexiv2.Image(str(p), encoding='utf-8') as img:
                    exif_dict, xmp_dict = {}, {}

                    # Read EXIF and XMP independently so one failing doesn't kill both
                    try:
                        exif_dict = img.read_exif()
                    except Exception:
                        pass
                    try:
                        xmp_dict = img.read_xmp()
                    except Exception:
                        pass

                    keys_to_check = [
                        "Exif.Photo.UserComment",
                        "Exif.UserComment",
                        "Xmp.dc.description",
                        "Xmp.dc.subject"
                    ]

                    # Check targeted keys
                    for key in keys_to_check:
                        data = exif_dict.get(key) if "Exif" in key else xmp_dict.get(key)
                        if data:
                            val = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
                            if marker in val:
                                return True

                    # Fallback lookup through all read metadata elements
                    for val in list(exif_dict.values()) + list(xmp_dict.values()):
                        if val and marker in (val.decode("utf-8", errors="ignore") if isinstance(val, bytes) else str(val)):
                            return True
        except Exception as e:
            logging.debug(f"pyexiv2 metadata read failed for {p.name} ({e}); trying Pillow fallback.")

        # 2. Fully isolated fallback check using Pillow
        try:
            with Image.open(p) as img:
                for key, value in img.info.items():
                    if isinstance(value, str) and marker in value:
                        return True
        except Exception as e:
            logging.debug(f"Pillow fallback failed for {p.name}: {e}")

    elif ext == "gif":
        try:
            with Image.open(p) as img:
                comment = img.info.get("comment", "")
                if comment and marker in str(comment):
                    return True
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
    """Handles the full pipeline for a single image: validation -> AI -> tagging."""

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

        # Apply 6-12 tag constraint if parsing succeeded but count was under minimum
        if len(parsed_tags) < 6:
            logging.warning(f"Model returned only {len(parsed_tags)} tags for {img_path.name}, using all available")

        tag_image(str(img_path), parsed_tags, fmt=fmt)
        logging.info(f"Successfully tagged {img_path.name} with {parsed_tags}")
        return (
            "SUCCESS",
            img_path.name,
            f"Generated Tags: {parsed_tags}",
            time.time() - start,
        )

    except Exception as e:
        logging.error(f"Error processing {img_path.name}: {e}", exc_info=True)
        return "FAILED", img_path.name, str(e), time.time() - start


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

    base_path = Path(directory)
    files = base_path.rglob("*") if recursive else base_path.iterdir()
    # We keep a broad filter for performance, but the final check is done in process_single_image via get_image_format
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    image_files = [
        f for f in files if f.is_file() and f.suffix.lower() in valid_extensions
    ]

    if not image_files:
        print(f"No valid images found in '{directory}'.")
        return

    print(f"Initialized backend: {backend} | Target: {host}")
    print(f"Found {len(image_files)} images to process. Starting...\n")

    # Load prompt from file
    prompt_file = Path(__file__).parent / "prompt.txt"
    if not prompt_file.exists():
        logging.warning(f"prompt.txt not found at {prompt_file}; using default")
        print(f"Warning: Could not read prompt.txt from {prompt_file}. Falling back to default.")
        prompt = DEFAULT_PROMPT
    else:
        try:
            prompt = prompt_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logging.error(f"Failed to read prompt.txt: {e}")
            sys.exit(1)

    if backend == "ollama":
        if OllamaClient is None:
            print("Error: 'ollama' library not found. Please install it using 'pip install ollama'.")
            return
        client = OllamaClient(host=host)
    else:
        if OpenAI is None:
            print("Error: 'openai' library not found. Please install it using 'pip install openai'.")
            return
        client = OpenAI(base_url=f"{host}/v1", api_key="lm-studio")

    # Initialize metrics
    success_count = 0
    fail_count = 0
    skip_count = 0
    total_success_duration = 0.0
    failed_log = []
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
                    status, name, message, duration = future.result()
                    if status == "SUCCESS":
                        tqdm.write(f"  [✓] {name} -> {message}")
                        success_count += 1
                        total_success_duration += duration
                    elif status == "SKIPPED":
                        tqdm.write(f"  [-] {name} -> {message}")
                        skip_count += 1
                    elif status == "CANCELLED":
                        # Do nothing for cancelled tasks to keep the console clean
                        pass
                    else:  # FAILED
                        tqdm.write(f"  [!] {name} -> {message}")
                        fail_count += 1
                        failed_log.append((name, message))
                except Exception as e:
                    img_path = future_to_image[future]
                    tqdm.write(f"  [!] {img_path} -> Unexpected Error: {e}")
                    fail_count += 1

    except Exception as e:
        print(f"An unexpected error occurred during processing: {e}")
        raise
    finally:
        if quit_thread.is_alive():
            quit_thread.join(timeout=2)

    # Calculate metrics
    end_time = time.time()
    total_seconds = end_time - start_time
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Report summary
    print("\n" + "=" * 50)
    print("                PROCESSING REPORT")
    print("=" * 50)
    print(f" Processed images: {success_count}")
    print(f" Skipped images: {skip_count}")
    print(f" Failed images: {fail_count}")
    print(f" Total time elapsed: {int(hours)}h {int(minutes)}m {seconds:.2f}s")

    if success_count > 0:
        avg_latency = total_success_duration / success_count
        print(f" Average processing time: {avg_latency:.2f} seconds")

    if failed_log:
        print("\n--- Failed files details ---")
        for filename, error_msg in failed_log:
            print(f" * {filename} -> {error_msg}")

    print("=" * 50)


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
