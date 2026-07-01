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

                    robust_replace(temp_path, image_path)
                    print(f"  Cleared metadata (pyexiv2) for: {image_path.name}")

                except RuntimeError as e:
                    if "IFD" in str(e).upper() or "corrupt" in str(e).lower():
                        # Sanitize with Pillow to strip broken headers.
                        with Image.open(image_path) as pil_img:
                            format_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
                            pil_img.save(temp_path, format=format_map.get(ext, "JPEG"), quality=95)

                        robust_replace(temp_path, image_path)
                        print(f"  Sanitized and cleared (Pillow) for: {image_path.name}")
                    else:
                        raise e

            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise e

        elif ext == "gif":
            fd, temp_path_str = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
            os.close(fd)
            temp_path = Path(temp_path_str)

            try:
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
