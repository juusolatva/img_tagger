import argparse
import base64
import os
import platform
import select
import sys
import threading
import time
import logging
import shutil
import tempfile
import piexif
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
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


def setup_logging(log_path: str | None) -> None:
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
    """Background thread to watch for 'q' keypress on Windows and Linux."""
    if platform.system() == "Windows" and msvcrt is not None:
        while not stop_event.is_set():
            if msvcrt.kbhit():
                key = msvcrt.getch().decode("utf-8").lower()
                if key == "q":
                    stop_event.set()
                    break
    else:
        try:
            # Ensure termios and tty are available (not None from top-level import)
            if termios is None or tty is None:
                raise ImportError("termios/tty not available")

            fd = sys.stdin.fileno()
            # Save original terminal settings using getattr to satisfy static analysis
            old_settings = getattr(termios, "tcgetattr")(fd)
            try:
                # Switch to cbreak mode (unbuffered, but handles Ctrl+C)
                getattr(tty, "setcbreak")(fd)
                while not stop_event.is_set():
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        char = sys.stdin.read(1).lower()
                        if char == "q":
                            stop_event.set()
                            break
            finally:
                # Always restore original settings
                getattr(termios, "tcsetattr")(fd, getattr(termios, "TCSADRAIN"), old_settings)
        except (ImportError, AttributeError, Exception):
            # Fallback for systems where termios/tty are not available or if in a non-interactive shell
            while not stop_event.is_set():
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    char = sys.stdin.read(1).lower()
                    if char == "q":
                        stop_event.set()
                        break


def is_valid_image(img_path: Path) -> bool:
    """Checks if the file is a valid image type that Pillow can process."""
    try:
        with Image.open(img_path) as img:
            # Pillow verifies the file is a valid image by its header
            return img.format in ["JPEG", "PNG", "WEBP", "GIF"]
    except Exception:
        return False


def _get_exif_dict(image_path: str) -> dict:
    """Helper to load or create an EXIF dictionary."""
    try:
        return piexif.load(image_path)
    except Exception:
        return {
            "0th": {},
            "Exif": {},
            "GPS": {},
            "Interop": {},
            "1st": {},
            "thumbnail": None,
        }


def _write_exif_tags(image_path: str, 
                     tags_list: list[str]
                     ) -> None:
    """Writes tags to JPEG/WEBP images using EXIF metadata."""
    marker = "[PROCESSED_BY_AI]"
    tags_str = ", ".join(tags_list)
    exif_dict = _get_exif_dict(image_path)

    user_comment = b"ASCII\x00\x00\x00" + (tags_str + " " + marker).encode(
        "ascii", errors="ignore"
    )
    exif_dict["Exif"][piexif.ExifIFD.UserComment] = user_comment
    win_tags_str = ";".join(tags_list) + "\x00"
    exif_dict["0th"][piexif.ImageIFD.XPKeywords] = win_tags_str.encode("utf-16le")
    exif_dict["0th"][piexif.ImageIFD.Software] = marker.encode("ascii")

    ext = Path(image_path).suffix.lower()
    exif_bytes = piexif.dump(exif_dict)

    if ext in ["jpg", "jpeg"]:
        fd, temp_path = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
        os.close(fd)

        try:
            shutil.copy2(image_path, temp_path)
            piexif.insert(exif_bytes, temp_path)
            os.replace(temp_path, image_path)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e
        
    elif ext == "webp":
        with Image.open(image_path) as img:
            fd, temp_path = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
            os.close(fd)

            try:
                img.save(temp_path, exif=exif_bytes, quality=95, method=6, format="WEBP")
                os.replace(temp_path, image_path)
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e


def _write_png_tags(image_path: str, 
                    tags_list: list[str]
                    ) -> None:
    """Writes tags to PNG images using PngInfo."""
    marker = "[PROCESSED_BY_AI]"
    tags_str = ", ".join(tags_list)
    with Image.open(image_path) as img:
        img.load()
        metadata = PngImagePlugin.PngInfo()
        for k, v in img.info.items():
            if isinstance(v, str) and isinstance(k, str) and k not in ["Keywords", "Description"]:
                metadata.add_text(k, v)

        metadata.add_text("Keywords", tags_str)
        metadata.add_text("Description", f"Tags: {tags_str} | {marker}")
        fd, temp_path = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
        os.close(fd)

        try:
            save_kwargs = {"pnginfo": metadata, "optimize": True, "format": "PNG"}
            if "icc_profile" in img.info:
                save_kwargs["icc_profile"] = img.info["icc_profile"]
            img.save(temp_path, **save_kwargs)
            os.replace(temp_path, image_path)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e


def _write_gif_tags(image_path: str, 
                    tags_list: list[str]
                    ) -> None:
    """Writes tags to GIF images using comments."""
    marker = "[PROCESSED_BY_AI]"
    tags_str = ", ".join(tags_list)
    with Image.open(image_path) as img:
        frames = [f.copy() for f in ImageSequence.Iterator(img)]
        duration = img.info.get("duration", 100)
        loop = img.info.get("loop", 0)
    
    comment = f"{tags_str} {marker}\x00"
    fd, temp_path = tempfile.mkstemp(dir=Path(image_path).parent, suffix=".tmp")
    os.close(fd)

    try:
        frames[0].save(
            temp_path,
            save_all=True,
            append_images=frames[1:],
            comment=comment,
            duration=duration,
            loop=loop,
            format="GIF"
        )
        os.replace(temp_path, image_path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e


def tag_image(image_path: str, 
              tags_list: list[str]
              ) -> None:
    """Embeds tags into the image metadata across Windows and Linux platforms."""
    ext = Path(image_path).suffix.lower()
    try:
        if ext in ["jpg", "jpeg"]:
            _write_exif_tags(image_path, tags_list)
        elif ext == "webp":
            _write_exif_tags(image_path, tags_list)
        elif ext == "png":
            _write_png_tags(image_path, tags_list)
        elif ext == "gif":
            _write_gif_tags(image_path, tags_list)
        else:
            raise ValueError(f"Unsupported file extension: {ext}")
    except Exception as e:
        raise RuntimeError(f"Tagging failed for {image_path}: {e}")


def get_tags_ollama(client: Any, 
                    model: str, 
                    img_path: Path, 
                    prompt: str
                    ) -> str:
    """Sends image payload to an Ollama server."""
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


def get_tags_lm_studio(client: Any, 
                       model: str, 
                       img_path: Path, 
                       prompt: str
                       ) -> str:
    """Encodes image to base64 and sends payload to an LM Studio server."""
    logging.debug(f"Requesting tags from LM Studio ({model}) for {img_path}")
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
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ],
    )
    content = response.choices[0].message.content
    # Removed logging of 'content' to prevent massive log files with base64 image data
    logging.debug(f"Raw LM Studio response received for {img_path}")
    return content


def is_already_processed(img_path: Path) -> bool:
    """Checks if the image already contains the AI processed marker."""
    marker = "[PROCESSED_BY_AI]"
    p = Path(img_path)
    ext = p.suffix.lower().lstrip(".")

    try:
        if ext in ["jpg", "jpeg", "webp"]:
            if ext == "webp":
                with Image.open(p) as img:
                    exif_bytes = img.info.get("exif", b"")
                exif_dict = piexif.load(exif_bytes) if exif_bytes else {"Exif": {}}
            else:
                exif_dict = piexif.load(str(p))
            user_comment = exif_dict["Exif"].get(piexif.ExifIFD.UserComment, b"")
            return marker.encode("ascii") in user_comment
        elif ext == "png":
            with Image.open(p) as img:
                return marker in img.info.get("Description", "")
        elif ext == "gif":
            with Image.open(p) as img:
                return marker in img.info.get("comment", "")
    except Exception:
        pass
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
    if not is_valid_image(img_path):
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

        # Clean the tags and check if we actually got anything
        tags = [tag.strip(" \"'") for tag in raw_output.split(",") if tag.strip()]

        if tags:
            tag_image(str(img_path), tags)
            logging.info(f"Successfully tagged {img_path.name} with {tags}")
            return (
                "SUCCESS",
                img_path.name,
                f"Generated Tags: {tags}",
                time.time() - start,
            )
        else:
            logging.warning(f"Empty tags list returned from model for {img_path.name}")
            return (
                "FAILED",
                img_path.name,
                "Empty tags list returned from model",
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
    base_path = Path(directory)
    files = base_path.rglob("*") if recursive else base_path.iterdir()
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    image_files = [
        f for f in files if f.is_file() and f.suffix.lower() in valid_extensions
    ]

    if not image_files:
        print(f"No valid images found in '{directory}'.")
        return

    print(f"Initialized backend: {backend} | Target: {host}")
    print(f"Found {len(image_files)} images to process. Starting...\n")

    # The prompt works well enough for me but feel free to modify it.
    prompt = (
        "Analyze this image, which could be an internet meme, screenshot, artwork, or photograph. "
        "Extract 6 to 12 highly relevant keywords and return ONLY a comma-separated list of tags."
        "1. Always include the image type as the first tag (e.g., 'Meme', 'Screenshot', 'Artwork', 'Photo'). "
        "2. If it is a meme, identify the meme template/format, the main subjects, the core vibe/emotion, and 1-3 key words from the text. "
        "3. If it is a screenshot, summarize the main topic or software shown. "
        "4. All tags must be strictly in English. Do not use any other languages or alphabets. "
        "Return ONLY the comma-separated list of tags. No introductory text, bullet points, or quotes."
    )

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
        description="Image tagger using a vision language model"
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

    if not os.path.isdir(args.directory):
        print(f"Error: The folder '{args.directory}' could not be located.")
        sys.exit(1)

    # Max 4 workers as it's the default limit for both ollama and LM Studio.
    if args.workers > 4:
        print("Warning: Max workers set higher than 4.")
        args.workers = 4
    elif args.workers < 1:
        print("Error: Worker count must be at least 1.")
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
