"""
Module to clear metadata (EXIF, XMP, IPTC) and comments from images.

This module provides utilities to sanitize image files by removing metadata
from common formats (JPG, JPEG, WebP, PNG) and clearing comments from GIFs.
It is primarily intended for resetting images for testing purposes.
"""

import argparse
import os
import tempfile
import shutil
import time
import pyexiv2
from pathlib import Path
from PIL import Image, ImageSequence


def robust_replace(src: Path, dst: Path):
    """
    Robustly replace the file using Path objects with retries for Windows handle release.

    Args:
        src (Path): The source Path object to be replaced.
        dst (Path): The destination Path object.

    Raises:
        OSError: If the replacement fails after multiple retries.
    """

    success = False
    for _ in range(10):
        try:
            src.replace(dst)
            success = True
            break
        except OSError:
            time.sleep(0.5)

    if not success:
        raise OSError(f"Failed to replace {src} with {dst} after retries.")


def _sanitize_with_pillow(src, dst, fmt=None) -> None:
    """
    Re-saves an image with Pillow to strip broken metadata headers, keeping every
    frame, per-frame duration and loop count of animated images (WebP, APNG).

    Args:
        src: The path of the image to read.
        dst: The path to write the sanitized copy to.
        fmt: Pillow format name to save as; defaults to the source image's format.
    """

    with Image.open(src) as img:
        fmt = fmt or img.format
        if not getattr(img, "is_animated", False):
            img.save(dst, format=fmt, quality=95)
            return

        durations = []
        for frame in ImageSequence.Iterator(img):
            frame.load()  # WebP only fills in the frame's duration once it's decoded
            durations.append(frame.info.get("duration", 100))
        img.seek(0)
        img.save(dst, format=fmt, quality=95, save_all=True, duration=durations, loop=img.info.get("loop", 0))


def _clear_standard(image_path, temp_path):
    """
    Wipes EXIF, XMP and IPTC from a JPEG/WebP/PNG via pyexiv2, falling back to
    a Pillow re-save when pyexiv2 reports corrupt metadata.

    Args:
        image_path (Path): The path to the image file.
        temp_path (Path): Temp file in the image's directory to build the result in.

    Raises:
        RuntimeError: If pyexiv2 fails for a reason other than corrupt metadata.
    """

    shutil.copy2(image_path, temp_path)
    try:
        # Primary attempt using pyexiv2 to wipe EVERYTHING
        with pyexiv2.Image(str(temp_path)) as img:
            img.clear_exif()
            img.clear_xmp()
            # On WebP, clear_xmp() alone leaves the XMP chunk in the saved file;
            # also emptying the raw packet removes it (harmless for JPEG/PNG).
            img.modify_raw_xmp("")
            img.clear_iptc()

        robust_replace(temp_path, image_path)
        print(f"  Cleared metadata (pyexiv2) for: {image_path.name}")

    except RuntimeError as e:
        if "IFD" not in str(e).upper() and "corrupt" not in str(e).lower():
            raise

        # Sanitize with Pillow to strip broken headers.
        ext = image_path.suffix.lower().lstrip(".")
        format_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
        _sanitize_with_pillow(image_path, temp_path, format_map.get(ext, "JPEG"))

        robust_replace(temp_path, image_path)
        print(f"  Sanitized and cleared (Pillow) for: {image_path.name}")


def _clear_gif(image_path, temp_path):
    """
    Re-saves a GIF with an empty comment, streaming frames to keep memory flat.

    Args:
        image_path (Path): The path to the GIF file.
        temp_path (Path): Temp file in the image's directory to build the result in.
    """

    with Image.open(image_path) as img:
        loop = img.info.get("loop", 0)
        # Capture per-frame duration to preserve variable frame rates
        durations = [f.info.get("duration", 100) for f in ImageSequence.Iterator(img)]

        # Reset the image pointer back to the first frame
        img.seek(0)
        first_frame = img.copy()

        # This generator streams frame copies one-by-one into the file writer.
        # It handles files larger than 64MB flawlessly with O(1) memory overhead.
        def frame_generator():
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i == 0:
                    continue
                yield frame.copy()

        first_frame.save(
            temp_path,
            format="GIF",
            save_all=True,
            append_images=frame_generator(),
            duration=durations,
            loop=loop,
            comment=""
        )
        first_frame.close()

    robust_replace(temp_path, image_path)
    print(f"  Cleared GIF comment (memory-efficient stream): {image_path.name}")


def clear_tags(image_path):
    """
    Removes all metadata (EXIF, XMP, IPTC) from standard images and clears
    comments from GIFs to reset images for testing purposes.

    Failures are printed rather than raised so one bad file doesn't stop a batch.

    Args:
        image_path (Path): The path to the image file.
    """

    ext = image_path.suffix.lower().lstrip(".")
    if ext in ["jpg", "jpeg", "webp", "png"]:
        clear_fn = _clear_standard
    elif ext == "gif":
        clear_fn = _clear_gif
    else:
        return

    try:
        fd, temp_path_str = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
        os.close(fd)
        temp_path = Path(temp_path_str)
        try:
            clear_fn(image_path, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"  Failed to clear {image_path.name}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset image tags for testing.")
    parser.add_argument("directory", help="Path to image folder")
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="Recursive search"
    )
    args = parser.parse_args()

    base_path = Path(args.directory)

    if not base_path.is_dir():
        print(f"Error: The path '{args.directory}' is not a valid directory.")
        exit(1)

    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    files = base_path.rglob("*") if args.recursive else base_path.iterdir()

    image_files = [
        f for f in files if f.is_file() and f.suffix.lower() in valid_extensions
    ]

    print(f"Resetting tags for {len(image_files)} images...")
    for img_path in image_files:
        clear_tags(img_path)
    print("Done.")
