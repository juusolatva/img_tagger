# Image tagger using a vision-language model

Tags all the JPEG, WebP, PNG and GIF files in the directory (and possibly subdirectories) by sending them to a locally running vision-language model (either on ollama or LM Studio). The tags the model sends back and they are added as metadata to the files.

## Features

- **Tags** are:
  - stored into EXIF (`UserComment`, `XPKeywords`, and `Software` fields) for **JPEG / WebP**
  - stored as native `Keywords` and `Description` text chunks for **PNG**
  - embedded directly inside the image comment block for **GIF**
- Skips already processed files that have the marker: [PROCESSED BY AI]
- Runs 1 to 4 workers


---


## Installation
Clone the repository and run img_tagger.py with Python.

### System prerequisites
You must have either **Ollama** or **LM Studio** with a Vision-capable model for processing the images.

### Installing Python dependencies
pip install -r requirements.txt


---


### Notes
The script doesn't respect preexisting tags so keep in mind that they may get overwritten.
If you need to you can clear all the tags in a folder using the clear_tags.py script.

### Known issues
Animated WebPs are not supported. Pillow may have issues with some PNGs or GIFs. Large animated GIFs are not recommended.