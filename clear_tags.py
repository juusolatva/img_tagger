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


def clear_tags(image_path):
    """
    Removes all metadata (EXIF, XMP, IPTC) from standard images and clears
    comments from GIFs to reset images for testing purposes.
    """
    ext = image_path.suffix.lower().lstrip(".")
    try:
        if ext in ["jpg", "jpeg", "webp", "png"]:
            fd, temp_path_str = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
            os.close(fd)
            temp_path = Path(temp_path_str)

            try:
                shutil.copy2(image_path, temp_path)
                try:
                    # Primary attempt using pyexiv2 to wipe EVERYTHING
                    with pyexiv2.Image(str(temp_path)) as img:
                        img.clear_exif()
                        img.clear_xmp()
                        img.clear_iptc()

                    # FIX: Use robust_replace instead of direct .replace()
                    robust_replace(temp_path, image_path)
                    print(f"  Cleared metadata (pyexiv2) for: {image_path.name}")

                except RuntimeError as e:
                    if "IFD" in str(e).upper() or "corrupt" in str(e).lower():
                        # Sanitize with Pillow to strip broken headers.
                        # NOTE: Keeping high quality (95) to prevent noticeable generation loss
                        with Image.open(image_path) as pil_img:
                            format_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
                            pil_img.save(temp_path, format=format_map.get(ext, "JPEG"), quality=95)

                        # FIX: Use robust_replace instead of direct .replace()
                        robust_replace(temp_path, image_path)
                        print(f"  Sanitized and cleared (Pillow) for: {image_path.name}")
                    else:
                        raise e

            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise e

        elif ext == "gif":
            file_size = image_path.stat().st_size
            MAX_MEMORY_SIZE = 64 * 1024 * 1024

            fd, temp_path_str = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
            os.close(fd)
            temp_path = Path(temp_path_str)

            try:
                with Image.open(image_path) as img:
                    loop = img.info.get("loop", 0)
                    # FIX: Capture per-frame duration to preserve variable frame rates
                    durations = [f.info.get("duration", 100) for f in ImageSequence.Iterator(img)]

                    if file_size > MAX_MEMORY_SIZE:
                        print(f"  Processing large GIF ({file_size / (1024*1024):.1f}MB) using disk-based approach...")
                        with tempfile.TemporaryDirectory(dir=image_path.parent) as temp_dir:
                            temp_dir_path = Path(temp_dir)
                            frame_files = []

                            # Save frames to disk as PNGs
                            for i, frame in enumerate(ImageSequence.Iterator(img)):
                                frame_path = temp_dir_path / f"frame_{i:05d}.png"
                                frame.save(frame_path, format="PNG")
                                frame_files.append(frame_path)

                            def disk_frame_generator():
                                for i in range(1, len(frame_files)):
                                    yield Image.open(frame_files[i])

                            first_frame = Image.open(frame_files[0])
                            first_frame.save(
                                temp_path,
                                format="GIF",
                                save_all=True,
                                append_images=disk_frame_generator(),
                                duration=durations,  # Pass the list of exact durations
                                loop=loop,
                                comment=""
                            )
                            first_frame.close()
                    else:
                        # Reset the image pointer back to the first frame
                        img.seek(0)
                        first_frame = img.copy()

                        # FIX: Stream frame copies in-memory to bypass heavy disk PNG writes
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
                            duration=durations,  # Pass the list of exact durations
                            loop=loop,
                            comment=""
                        )
                        first_frame.close()

                robust_replace(temp_path, image_path)
                print(f"  Cleared GIF comment: {image_path.name}")

            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise e

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

    # FIX: Explicit directory check
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
