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
    for _ in range(10):  # Increased retries and sleep duration
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

    Args:
        image_path (Path): The path object of the image file to be processed.

    Raises:
        RuntimeError: If pyexiv2 encounters an error not related to corruption.
        OSError: If a file operation fails during processing.
    """

    ext = image_path.suffix.lower().lstrip(".")
    try:
        if ext in ["jpg", "jpeg", "webp", "png"]:
            fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
            os.close(fd)
            try:
                shutil.copy2(image_path, temp_path)
                try:
                    # Primary attempt using pyexiv2 to wipe EVERYTHING
                    with pyexiv2.Image(temp_path) as img:
                        img.clear_exif()
                        img.clear_xmp()
                        img.clear_iptc()

                    os.replace(temp_path, image_path)
                    print(f"  Cleared metadata (pyexiv2) for: {image_path.name}")

                except RuntimeError as e:
                    if "IFD" in str(e).upper() or "corrupt" in str(e).lower():
                        # The image is corrupted. Sanitize with Pillow to strip broken headers.
                        with Image.open(image_path) as pil_img:
                            format_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
                            pil_img.save(temp_path, format=format_map.get(ext, "JPEG"))

                        os.replace(temp_path, image_path)
                        print(f"  Sanitized and cleared (Pillow+pyexiv2) for: {image_path.name}")
                    else:
                        raise e

            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e

        elif ext == "gif":
            # 1. Extract frames to temporary files to avoid memory issues with large GIFs
            duration = 100
            loop = 0
            frame_paths = []

            with tempfile.TemporaryDirectory(dir=image_path.parent) as tmp_dir:
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
                            f.close()

                    first_frame = Image.open(frame_paths[0])
                    first_frame.save(
                        temp_path,
                        format="GIF",
                        save_all=True,
                        append_images=frame_generator(),
                        duration=duration,
                        loop=loop,
                        comment=""
                    )
                    first_frame.close()

                    # Robustly replace the file using Path objects with retries for Windows handle release
                    robust_replace(temp_path, image_path)

                    print(f"  Cleared GIF comment (memory-efficient): {image_path.name}")
                except Exception as e:
                    if temp_path.exists():
                        temp_path.unlink()
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
    valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    files = base_path.rglob("*") if args.recursive else base_path.iterdir()

    image_files = [
        f for f in files if f.is_file() and f.suffix.lower() in valid_extensions
    ]

    print(f"Resetting tags for {len(image_files)} images...")
    for img_path in image_files:
        clear_tags(img_path)
    print("Done.")
