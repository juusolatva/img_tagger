import argparse
import os
import tempfile
from pathlib import Path

import piexif
from PIL import Image, PngImagePlugin


def clear_tags(image_path):
    """Removes added tags and markers from various image formats."""
    ext = image_path.suffix.lower().lstrip(".")
    try:
        if ext in ["jpg", "jpeg"]:
            try:
                exif_dict = piexif.load(str(image_path))
            except Exception:
                # If there's no EXIF data, there are no tags to clear.
                print(f"  No EXIF metadata found for: {image_path.name} (skipping)")
                return

            # Clear the specific keys we added
            exif_dict["Exif"][piexif.ExifIFD.UserComment] = b""
            exif_dict["0th"][piexif.ImageIFD.XPKeywords] = b""
            exif_dict["0th"][piexif.ImageIFD.Software] = b""

            exif_bytes = piexif.dump(exif_dict)

            # Use the same temp-file pattern as other formats for consistency/safety
            with Image.open(image_path) as img:
                img.load()
                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)
                # Added format="JPEG" explicitly
                img.save(
                    temp_path, format="JPEG", exif=exif_bytes, quality=95, method=6
                )
                os.replace(temp_path, image_path)
            print(f"  Cleared EXIF for: {image_path.name}")

        elif ext == "webp":
            exif_dict = piexif.load(str(image_path))
            # Clear the specific keys we added
            exif_dict["Exif"][piexif.ExifIFD.UserComment] = b""
            exif_dict["0th"][piexif.ImageIFD.XPKeywords] = b""
            exif_dict["0th"][piexif.ImageIFD.Software] = b""

            exif_bytes = piexif.dump(exif_dict)
            with Image.open(image_path) as img:
                img.load()  # Ensure image is loaded into memory
                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)

                # Added format="WEBP" explicitly
                img.save(
                    temp_path, format="WEBP", exif=exif_bytes, quality=95, method=6
                )
                os.replace(temp_path, image_path)
            print(f"  Cleared WebP metadata: {image_path.name}")

        elif ext == "png":
            with Image.open(image_path) as img:
                img.load()  # Ensure image is loaded into memory
                metadata = PngImagePlugin.PngInfo()
                # Copy existing metadata except the ones we want to wipe
                for k, v in img.info.items():
                    if k not in ["Keywords", "Description"]:
                        if isinstance(v, str):
                            metadata.add_text(k, v)

                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)

                # Added format="PNG" explicitly
                img.save(temp_path, format="PNG", pnginfo=metadata, optimize=True)
                os.replace(temp_path, image_path)
            print(f"  Cleared PNG chunks: {image_path.name}")

        elif ext == "gif":
            with Image.open(image_path) as img:
                img.load()  # Ensure image is loaded into memory
                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)

                # Added format="GIF" explicitly
                img.save(temp_path, format="GIF", save_all=True, comment="")
                os.replace(temp_path, image_path)
            print(f"  Cleared GIF comment: {image_path.name}")

    except Exception as e:
        print(f"  Failed to clear {image_path.name}: {e}")


def main():
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


if __name__ == "__main__":
    main()
