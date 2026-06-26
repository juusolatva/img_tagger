import argparse
import base64
import os
import platform
import select
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import piexif
from PIL import Image, PngImagePlugin

# Platform-specific imports for key detection
if platform.system() == "Windows":
    import msvcrt

# Try importing backends safely
try:
    from ollama import Client as OllamaClient
except ImportError:
    OllamaClient = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def listen_for_quit(stop_event):
    """Background thread to watch for 'q' keypress on Windows and Linux."""
    while not stop_event.is_set():
        if platform.system() == "Windows":
            if msvcrt.kbhit():
                key = msvcrt.getch().decode("utf-8").lower()
                if key == "q":
                    stop_event.set()
                    break
        else:
            # Linux and macOS logic
            # select.select([file], [outputs], [exceptions], timeout)
            # We check if there is data available to read from stdin (fileno 0)
            if select.select([sys.stdin], [], [], 0.1)[0]:
                char = sys.stdin.read(1).lower()
                if char == "q":
                    stop_event.set()
                    break


def is_valid_image(img_path):
    """Checks if the file is a valid image type that Pillow can process."""
    try:
        with Image.open(img_path) as img:
            # Pillow verifies the file is a valid image by its header
            return img.format in ["JPEG", "PNG", "WEBP", "GIF"]
    except:
        return False


def tag_image(image_path, tags_list):
    """Embeds tags into the image metadata across Windows and Linux platforms."""
    ext = image_path.lower().split(".")[-1]
    marker = "[PROCESSED_BY_AI]"

    # 1. Helper for EXIF dictionary (shared by JPEG and WEBP)
    def get_exif_dict():
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

    tags_str = ", ".join(tags_list)

    # 2. Handle JPEG/WEBP logic (using EXIF)
    if ext in ["jpg", "jpeg"]:
        exif_dict = get_exif_dict()
        user_comment = b"ASCII\x00\x00\x00" + (tags_str + " " + marker).encode(
            "ascii", errors="ignore"
        )
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = user_comment
        win_tags_str = ";".join(tags_list) + "\x00"
        exif_dict["0th"][piexif.ImageIFD.XPKeywords] = win_tags_str.encode("utf-16le")
        exif_dict["0th"][piexif.ImageIFD.Software] = marker.encode("ascii")

        try:
            exif_bytes = piexif.dump(exif_dict)
            # piexif.insert modifies the file in place without re-encoding pixels,
            # ensuring zero quality loss for JPEGs.
            piexif.insert(exif_bytes, image_path)
        except Exception as e:
            raise RuntimeError(f"EXIF injection failed: {e}")

    elif ext == "webp":
        exif_dict = get_exif_dict()
        user_comment = b"ASCII\x00\x00\x00" + (tags_str + " " + marker).encode(
            "ascii", errors="ignore"
        )
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = user_comment
        win_tags_str = ";".join(tags_list) + "\x00"
        exif_dict["0th"][piexif.ImageIFD.XPKeywords] = win_tags_str.encode("utf-16le")
        exif_dict["0th"][piexif.ImageIFD.Software] = marker.encode("ascii")

        try:
            exif_bytes = piexif.dump(exif_dict)
            with Image.open(image_path) as img:
                # Added quality=95 and method=6 for high-quality preservation
                img.save(image_path, exif=exif_bytes, quality=95, method=6)
        except Exception as e:
            raise RuntimeError(f"WebP EXIF injection failed: {e}")

    elif ext == "png":
        try:
            with Image.open(image_path) as img:
                img.load()
                metadata = PngImagePlugin.PngInfo()
                for k, v in img.info.items():
                    if isinstance(v, str) and k not in ["Keywords", "Description"]:
                        metadata.add_text(k, v)

                metadata.add_text("Keywords", tags_str)
                metadata.add_text("Description", f"Tags: {tags_str} | {marker}")

            # Added optimize=True to ensure best compression without quality loss
            img.save(image_path, pnginfo=metadata, optimize=True)
        except Exception as e:
            raise RuntimeError(f"PNG chunk metadata write failed: {e}")

    elif ext == "gif":
        try:
            with Image.open(image_path) as img:
                comment = f"{tags_str} {marker}"
                if len(comment) > 254:
                    comment = comment[:253] + "\x00"
                else:
                    comment += "\x00"
                # save_all=True preserves animation frames
                img.save(image_path, save_all=True, comment=comment)
        except Exception as e:
            raise RuntimeError(f"GIF metadata write failed: {e}")


def get_tags_ollama(client, model, img_path, prompt):
    """Sends image payload to an Ollama server."""
    response = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [str(img_path)]}],
    )
    return (
        response.message.content
        if hasattr(response, "message")
        else response["message"]["content"]
    )


def get_tags_lm_studio(client, model, img_path, prompt):
    """Encodes image to base64 and sends payload to an LM Studio server."""
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
    return response.choices[0].message.content


def is_already_processed(img_path):
    """Checks if the image already contains the AI processed marker."""
    marker = "[PROCESSED_BY_AI]"
    # Convert to Path object to ensure .suffix works even if a string is passed
    p = Path(img_path)
    ext = p.suffix.lower().lstrip(".")

    try:
        if ext in ["jpg", "jpeg", "webp"]:
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


def process_single_image(img_path, client, backend, model, prompt, stop_event):
    """Handles the full pipeline for a single image: validation -> AI -> tagging."""
    if not is_valid_image(img_path):
        return "FAILED", img_path.name, "Skipping unsupported or corrupted file"

    # Skip check
    if is_already_processed(img_path):
        return "SKIPPED", img_path.name, "Skipped: Already Tagged"

    if stop_event.is_set():
        return "CANCELLED", img_path.name, "Cancelled by user"

    try:
        if backend == "ollama":
            raw_output = get_tags_ollama(client, model, img_path, prompt)
        else:
            raw_output = get_tags_lm_studio(client, model, img_path, prompt)

        # Check again after the network call before doing disk I/O
        if stop_event.is_set():
            return "CANCELLED", img_path.name, "Cancelled by user"

        # Clean the tags and check if we actually got anything
        tags = [tag.strip(" \"'") for tag in raw_output.split(",") if tag.strip()]

        if tags:
            tag_image(str(img_path), tags)
            return "SUCCESS", img_path.name, f"Generated Tags: {tags}"
        else:
            return "FAILED", img_path.name, "Empty tags list returned from model"

    except Exception as e:
        # Catching the specific error to show it in the report
        return "FAILED", img_path.name, str(e)


def process_directory(directory, recursive, backend, host, model, max_workers):
    base_path = Path(directory)
    files = base_path.rglob("*") if recursive else base_path.iterdir()
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    image_files = [
        f for f in files if f.is_file() and f.suffix.lower() in valid_extensions
    ]

    if not image_files:
        print(f"No valid images found in '{directory}'.")
        return

    print(f"Initialized backend: {backend.upper()} | Target: {host}")
    print(f"Found {len(image_files)} images to process. Let's begin...\n")

    # Optimized prompt targeting internet culture, memes, and UI screenshots
    prompt = (
        "Analyze this image, which could be an internet meme, screenshot, artwork, or photograph. "
        "Extract 6 to 12 highly relevant keywords. "
        "1. Always include the image type as the first tag (e.g., 'Meme', 'Screenshot', 'Artwork', 'Photo'). "
        "2. If it is a meme, identify the meme template/format, the main subjects, the core vibe/emotion, and 1-3 key words from the text. "
        "3. If it is a screenshot, summarize the main topic or software shown. "
        "4. All tags must be strictly in English. Do not use any other languages or alphabets. "
        "Return ONLY the comma-separated list of tags. No introductory text, bullet points, or quotes."
    )

    if backend == "ollama":
        client = OllamaClient(host=host)
    else:
        client = OpenAI(base_url=f"{host}/v1", api_key="lm-studio")

    # Performance Metrics Initializers
    success_count = 0
    fail_count = 0
    skip_count = 0
    failed_log = []
    start_time = time.time()

    # Concurrency & Quit Logic
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

        for future in as_completed(future_to_image):
            if stop_event.is_set():
                print("\n[!] Stop signal received (Q pressed). Stopping new tasks...")
                break

            status, name, message = future.result()
            if status == "SUCCESS":
                print(f"  [✓] {name} -> {message}")
                success_count += 1
            elif status == "SKIPPED":
                print(f"  [-] {name} -> {message}")
                skip_count += 1
            else:  # FAILED
                print(f"  [!] {name} -> {message}")
                fail_count += 1
                failed_log.append((name, message))

    # Calculate metrics
    end_time = time.time()
    total_seconds = end_time - start_time
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Output End Report
    print("\n" + "=" * 50)
    print("                PROCESSING REPORT")
    print("=" * 50)
    print(f" Successfully Processed : {success_count}")
    print(f" Skipped (Already Tagged): {skip_count}")
    print(f" Failed Images          : {fail_count}")
    print(f" Total Time Elapsed     : {int(hours)}h {int(minutes)}m {seconds:.2f}s")

    if success_count > 0:
        avg_speed = total_seconds / success_count
        print(f" Average Processing Speed: {avg_speed:.2f} seconds per image")

    if failed_log:
        print("\n--- Failed Files Details ---")
        for filename, error_msg in failed_log:
            print(f" * {filename} -> {error_msg}")

    print("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AI Media Library Tagger with Execution Reports"
    )
    parser.add_argument("directory", help="Path to your image folder")
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Process subdirectories recursively",
    )
    parser.add_argument(
        "--backend",
        choices=["ollama", "lm-studio"],
        default="ollama",
        help="Local AI provider backend",
    )
    parser.add_argument(
        "--host", help="Custom backend endpoint URL (Overrides defaults)"
    )
    parser.add_argument(
        "--model",
        default="qwen3-vl:8b",
        help="Model identification tag (Mainly for Ollama)",
    )
    # New worker argument
    parser.add_argument(
        "--worker",
        type=int,
        default=1,
        help="Number of concurrent workers (Max 4 recommended)",
    )

    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        print(f"Error: The folder '{args.directory}' could not be located.")
        exit(1)

    # Logic to enforce your rule of max 4 workers
    if args.worker > 4:
        print("Warning: Max workers set higher than 4.")
        args.worker = 4
    elif args.worker < 1:
        print("Error: Worker count must be at least 1.")
        exit(1)

    if not args.host:
        args.host = (
            "http://localhost:11434"
            if args.backend == "ollama"
            else "http://localhost:1234"
        )

    process_directory(
        args.directory, args.recursive, args.backend, args.host, args.model, args.worker
    )
