import argparse
import os
import tempfile
import shutil
import pyexiv2
import piexif
from pathlib import Path
from PIL import Image, ImageSequence


def clear_tags(image_path):
    """Removes added tags and markers without breaking animations or metadata profiles."""
    ext = image_path.suffix.lower().lstrip(".")
    try:
        if ext in ["jpg", "jpeg", "webp", "png"]:
            fd, temp_path = tempfile.mkstemp(dir=image_path.parent, suffix=".tmp")
            os.close(fd)
            try:
                shutil.copy2(image_path, temp_path)
                try:
                    # Primary attempt using pyexiv2
                    with pyexiv2.Image(temp_path) as img:
                        img.modify_meta({
                            'Exif.Photo.UserComment': "",
                            'Exif.Image.XPKeywords': "",
                            'Xmp.dc.subject': "",
                            'Xmp.dc.description': ""
                        })
                    os.replace(temp_path, image_path)
                    print(f"  Cleared metadata (pyexiv2) for: {image_path.name}")
                    
                except RuntimeError as e:
                    if "IFD" in str(e).upper() or "corrupt" in str(e).lower():
                        # The image is ALREADY corrupted (likely from the old script).
                        # Let's use Pillow to strip the broken EXIF data entirely.
                        with Image.open(image_path) as pil_img:
                            pil_img.save(temp_path, format=pil_img.format)
                        os.replace(temp_path, image_path)
                        print(f"  Sanitized corrupted EXIF (Pillow) for: {image_path.name}")
                    else:
                        raise e
                        
                except Exception as pyexiv2_err:
                    # Fallback to piexif for other errors
                    try:
                        exif_dict = piexif.load(str(image_path))
                        
                        # SAFELY DELETE the keys instead of setting them to b""
                        if "Exif" in exif_dict:
                            exif_dict["Exif"].pop(piexif.ExifIFD.UserComment, None)
                        if "0th" in exif_dict:
                            exif_dict["0th"].pop(piexif.ImageIFD.XPKeywords, None)
                            exif_dict["0th"].pop(piexif.ImageIFD.Software, None)

                        exif_bytes = piexif.dump(exif_dict)
                        piexif.insert(exif_bytes, temp_path)
                        os.replace(temp_path, image_path)
                        print(f"  Cleared metadata (piexif fallback) for: {image_path.name}")
                    except Exception as fallback_err:
                        raise pyexiv2_err
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e

        elif ext == "gif":
            with Image.open(image_path) as img:
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
