import os
import argparse
from pathlib import Path
from PIL import Image, PngImagePlugin
import piexif

def clear_tags(image_path):
    """Removes added tags and markers from various image formats."""
    ext = image_path.suffix.lower().lstrip('.')
    try:
        if ext in ['jpg', 'jpeg']:
            exif_dict = piexif.load(str(image_path))
            # Clear the specific keys we added
            exif_dict["Exif"][piexif.ExifIFD.UserComment] = b""
            exif_dict["0th"][piexif.ImageIFD.XPKeywords] = b""
            exif_dict["0th"][piexif.ImageIFD.Software] = b""
            
            exif_bytes = piexif.dump(exif_dict)
            piexif.insert(exif_bytes, str(image_path))
            print(f"  Cleared EXIF for: {image_path.name}")

        elif ext == 'webp':
            exif_dict = piexif.load(str(image_path))
            exif_dict["Exif"][piexif.ExifIFD.UserComment] = b""
            exif_dict["0th"][piexif.ImageIFD.XPKeywords] = b""
            exif_dict["0th"][piexif.ImageIFD.Software] = b""
            
            exif_bytes = piexif.dump(exif_dict)
            with Image.open(image_path) as img:
                img.save(image_path, exif=exif_bytes, quality=95, method=6)
            print(f"  Cleared WebP metadata: {image_path.name}")

        elif ext == 'png':
            with Image.open(image_path) as img:
                metadata = PngImagePlugin.PngInfo()
                # Copy existing metadata except the ones we want to wipe
                for k, v in img.info.items():
                    if isinstance(v, str) and k not in ["Keywords", "Description"]:
                        metadata.add_text(k, v)
                
                img.save(image_path, pnginfo=metadata, optimize=True)
            print(f"  Cleared PNG chunks: {image_path.name}")

        elif ext == 'gif':
            with Image.open(image_path) as img:
                # Saving with comment=None or an empty string clears the chunk
                img.save(image_path, save_all=True, comment="")
            print(f" Cleared GIF comment: {image_path.name}")

    except Exception as e:
        print(f"  Failed to clear {image_path.name}: {e}")

def main():
    parser = argparse.ArgumentParser(description="Reset image tags for testing.")
    parser.add_argument("directory", help="Path to image folder")
    parser.add_argument("-r", "--recursive", action="store_true", help="Recursive search")
    args = parser.parse_args()

    base_path = Path(args.directory)
    valid_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    files = base_path.rglob('*') if args.recursive else base_path.iterdir()
    
    image_files = [f for f in files if f.is_file() and f.suffix.lower() in valid_extensions]

    print(f"Resetting tags for {len(image_files)} images...")
    for img_path in image_files:
        clear_tags(img_path)
    print("Done.")

if __name__ == "__main__":
    main()