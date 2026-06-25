import os
import argparse
import base64
import time
from pathlib import Path
from PIL import Image, PngImagePlugin
import piexif

# Try importing backends safely
try:
    from ollama import Client as OllamaClient
except ImportError:
    OllamaClient = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def is_valid_image(img_path):
    """Checks if the file is a valid image type that Pillow can process."""
    try:
        with Image.open(img_path) as img:
            # Pillow verifies the file is a valid image by its header
            return img.format in ['JPEG', 'PNG', 'WEBP', 'GIF']
    except:
        return False


def tag_image(image_path, tags_list):
    """Embeds tags into the image metadata across Windows and Linux platforms."""
    ext = image_path.lower().split('.')[-1]
    marker = "[PROCESSED_BY_AI]"
    
    # 0. Check if already processed to allow for resuming/skipping
    try:
        if ext in ['jpg', 'jpeg', 'webp']:
            exif_dict = piexif.load(image_path)
            user_comment = exif_dict["Exif"].get(piexif.ExifIFD.UserComment, b"")
            if marker.encode('ascii') in user_comment:
                return 
        elif ext == 'png':
            with Image.open(image_path) as img:
                if marker in img.info.get("Description", ""):
                    return 
        elif ext == 'gif':
            with Image.open(image_path) as img:
                if marker in img.info.get("comment", ""):
                    return 
    except Exception:
        pass

    # 1. Helper for EXIF dictionary (shared by JPEG and WEBP)
    def get_exif_dict():
        try:
            return piexif.load(image_path)
        except Exception:
            return {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}

    tags_str = ", ".join(tags_list)

    # 2. Handle JPEG/WEBP logic (using EXIF)
    if ext in ['jpg', 'jpeg']:
        exif_dict = get_exif_dict()
        user_comment = b'ASCII\x00\x00\x00' + (tags_str + " " + marker).encode('ascii', errors='ignore')
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = user_comment
        win_tags_str = ";".join(tags_list) + "\x00"
        exif_dict["0th"][piexif.ImageIFD.XPKeywords] = win_tags_str.encode("utf-16le")
        exif_dict["0th"][piexif.ImageIFD.Software] = marker.encode('ascii')

        try:
            exif_bytes = piexif.dump(exif_dict)
            # piexif.insert modifies the file in place without re-encoding pixels, 
            # ensuring zero quality loss for JPEGs.
            piexif.insert(exif_bytes, image_path)
        except Exception as e:
            raise RuntimeError(f"EXIF injection failed: {e}")
            
    elif ext == 'webp':
        exif_dict = get_exif_dict()
        user_comment = b'ASCII\x00\x00\x00' + (tags_str + " " + marker).encode('ascii', errors='ignore')
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = user_comment
        win_tags_str = ";".join(tags_list) + "\x00"
        exif_dict["0th"][piexif.ImageIFD.XPKeywords] = win_tags_str.encode("utf-16le")
        exif_dict["0th"][piexif.ImageIFD.Software] = marker.encode('ascii')

        try:
            exif_bytes = piexif.dump(exif_dict)
            with Image.open(image_path) as img:
                # Added quality=95 and method=6 for high-quality preservation
                img.save(image_path, exif=exif_bytes, quality=95, method=6)
        except Exception as e:
            raise RuntimeError(f"WebP EXIF injection failed: {e}")

    elif ext == 'png':
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

    elif ext == 'gif':
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
        messages=[{
            'role': 'user',
            'content': prompt,
            'images': [str(img_path)]
        }]
    )
    return response.message.content if hasattr(response, 'message') else response['message']['content']


def get_tags_lm_studio(client, model, img_path, prompt):
    """Encodes image to base64 and sends payload to an LM Studio server."""
    with open(img_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')
        
    response = client.chat.completions.create(
        model=model, 
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        }]
    )
    return response.choices[0].message.content


def process_directory(directory, recursive, backend, host, model):
    base_path = Path(directory)
    files = base_path.rglob('*') if recursive else base_path.iterdir()
    valid_extensions = {'.jpg', '.jpeg', '.png','.webp', '.gif'}
    image_files = [f for f in files if f.is_file() and f.suffix.lower() in valid_extensions]

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

    if backend == 'ollama':
        client = OllamaClient(host=host)
    else:
        client = OpenAI(base_url=f"{host}/v1", api_key="lm-studio")

    # Performance Metrics Initializers
    success_count = 0
    fail_count = 0
    failed_log = []
    start_time = time.time()

    for img_path in image_files:

        # 1. Sanity Check (Lightweight Header Verification)
        if not is_valid_image(img_path):
            print(f"  [!] Skipping unsupported or corrupted file: {img_path.name}")
            continue

        print(f"Processing: {img_path.name}")
        try:
            if backend == 'ollama':
                raw_output = get_tags_ollama(client, model, img_path, prompt)
            else:
                raw_output = get_tags_lm_studio(client, model, img_path, prompt)
            
            tags = [tag.strip() for tag in raw_output.split(',') if tag.strip()]
            
            if tags:
                tag_image(str(img_path), tags)
                print(f"  -> Generated Tags: {tags}")
                print("  -> Metadata saved successfully.")
                success_count += 1
            else:
                print("  [!] Received an empty tag output list from the model.")
                fail_count += 1
                failed_log.append((img_path.name, "Empty tags list returned from model"))
                
        except Exception as e:
            print(f"  [!] Error processing {img_path.name}: {e}")
            fail_count += 1
            failed_log.append((img_path.name, str(e)))

    # Calculate metrics
    end_time = time.time()
    total_seconds = end_time - start_time
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Output End Report
    print("\n" + "="*50)
    print("                PROCESSING REPORT")
    print("="*50)
    print(f" Successfully Processed : {success_count}")
    print(f" Failed Images          : {fail_count}")
    print(f" Total Time Elapsed     : {int(hours)}h {int(minutes)}m {seconds:.2f}s")
    
    if success_count > 0:
        avg_speed = total_seconds / success_count
        print(f" Average Processing Speed: {avg_speed:.2f} seconds per image")
        
    if failed_log:
        print("\n--- Failed Files Details ---")
        for filename, error_msg in failed_log:
            print(f" * {filename} -> {error_msg}")
            
    print("="*50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Media Library Tagger with Execution Reports")
    parser.add_argument("directory", help="Path to your image folder")
    parser.add_argument("-r", "--recursive", action="store_true", help="Process subdirectories recursively")
    parser.add_argument("--backend", choices=['ollama', 'lm-studio'], default='ollama', help="Local AI provider backend")
    parser.add_argument("--host", help="Custom backend endpoint URL (Overrides defaults)")
    parser.add_argument("--model", default="qwen3-vl:8b", help="Model identification tag (Mainly for Ollama)")
    
    args = parser.parse_args()
    
    if not os.path.isdir(args.directory):
        print(f"Error: The folder '{args.directory}' could not be located.")
        exit(1)
        
    if not args.host:
        args.host = "http://localhost:11434" if args.backend == 'ollama' else "http://localhost:1234"
        
    process_directory(args.directory, args.recursive, args.backend, args.host, args.model)