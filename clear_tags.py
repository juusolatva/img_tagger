import argparse
import os
import tempfile
import shutil
import piexif
from pathlib import Path
from PIL import Image, ImageSequence, PngImagePlugin


def clear_tags(image_path):
    """Removes added tags and markers from various image formats without breaking animations or metadata profiles."""
    ext = image_path.suffix.lower().lstrip(".")
    try:
        if ext in ["jpg", "jpeg"]:
            try:
                exif_dict = piexif.load(str(image_path))
            except Exception:
                print(f"  No EXIF metadata found for: {image_path.name} (skipping)")
                return

            for ifd in ["Exif", "0th"]:
                if ifd not in exif_dict:
                    exif_dict[ifd] = {}

            exif_dict["Exif"][piexif.ExifIFD.UserComment] = b""
            exif_dict["0th"][piexif.ImageIFD.XPKeywords] = b""
            exif_dict["0th"][piexif.ImageIFD.Software] = b""

            exif_bytes = piexif.dump(exif_dict)
            fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
            os.close(fd)

            try:
                shutil.copy2(image_path, temp_path)
                piexif.insert(exif_bytes, temp_path)
                os.replace(temp_path, image_path)
                print(f"  Cleared EXIF tags for: {image_path.name}")
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e

        elif ext == "webp":
            with Image.open(image_path) as img:
                img.load()
                exif_bytes = img.info.get("exif", b"")
                
                try:
                    exif_dict = piexif.load(exif_bytes) if exif_bytes else {"Exif": {}, "0th": {}}
                except Exception:
                    exif_dict = {"Exif": {}, "0th": {}}

                for ifd in ["Exif", "0th"]:
                    if ifd not in exif_dict:
                        exif_dict[ifd] = {}

                exif_dict["Exif"][piexif.ExifIFD.UserComment] = b""
                exif_dict["0th"][piexif.ImageIFD.XPKeywords] = b""
                exif_dict["0th"][piexif.ImageIFD.Software] = b""

                cleaned_exif_bytes = piexif.dump(exif_dict)
                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)

                try:
                    img.save(
                        temp_path, format="WEBP", exif=cleaned_exif_bytes, quality=95, method=6
                    )
                    os.replace(temp_path, image_path)
                    print(f"  Cleared WebP metadata: {image_path.name}")
                except Exception as e:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise e

        elif ext == "png":
            with Image.open(image_path) as img:
                img.load()
                metadata = PngImagePlugin.PngInfo()
                
                new_info = {k: v for k, v in img.info.items() if k not in ["Keywords", "Description"]}
                for k, v in new_info.items():
                    if isinstance(k, str) and isinstance(v, str):
                        metadata.add_text(k, v)

                icc_profile = new_info.get("icc_profile")
                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)

                try:
                    save_params = {"format": "PNG", "pnginfo": metadata, "optimize": True}
                    if icc_profile:
                        save_params["icc_profile"] = icc_profile

                    img.save(temp_path, **save_params)
                    os.replace(temp_path, image_path)
                    print(f"  Cleared PNG chunks: {image_path.name}")
                except Exception as e:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise e

        elif ext == "gif":
            with Image.open(image_path) as img:
                img.load()
                # Map out all frames and animation configuration settings to avoid flattening
                frames = [f.copy() for f in ImageSequence.Iterator(img)]
                duration = img.info.get("duration", 100)
                loop = img.info.get("loop", 0)

                fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
                os.close(fd)

                try:
                    frames[0].save(
                        temp_path, 
                        format="GIF", 
                        save_all=True, 
                        append_images=frames[1:], 
                        duration=duration, 
                        loop=loop, 
                        comment=""
                    )
                    os.replace(temp_path, image_path)
                    print(f"  Cleared GIF comment: {image_path.name}")
                except Exception as e:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise e

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
