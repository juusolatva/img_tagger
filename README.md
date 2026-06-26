# Image tagger using a vision language model

Tags all the **JPEG**, **WebP**, **PNG** and **GIF** files in the directory (and possibly subdirectories) by sending them to a local vision language model (either **ollama** or **LM Studio** running on the same computer or the local network). The tags the model sends back are added as metadata to the files and the quality of the tags depends on the model.

## Features

- **Tags** are:
  - stored into EXIF (`UserComment`, `XPKeywords`, and `Software` fields) for **JPEGs / WebPs**
  - stored as native `Keywords` and `Description` text chunks for **PNGs**
  - embedded directly inside the image comment block for **GIFs**
- Skips already processed files that have the marker: ***[PROCESSED BY AI]***
- Runs **1 to 4** workers concurrently
- Collects simple performance metrics
- Optional logging for troubleshooting


---


## Installation
Clone the repository and run *img_tagger.py* with Python.

### System prerequisites
You must have Python and either **ollama** or **LM Studio** with a vision-capable model for processing the images.

### Installing dependencies
*pip install -r requirements.txt*


---


### Notes
- Run *img_tagger.py -h* or *img_tagger.py --help* to see arguments and options
- Press `Q` to quit and then wait for it to finish all the work in progress
- The script doesn't respect preexisting tags so keep in mind that they will be overwritten
- The default model is Qwen3 VL 8B since it works well but any model capable of processing image inputs should work
- Allows a maximum of 4 workers which is the default limit for both **ollama** and **LM Studio**
- If you need to you can clear all the tags in a folder using the *clear_tags.py* script

### Known issues
- Animated **WebPs** are not supported
- May have issues with some **PNGs** or **GIFs**
- Large animated **GIFs** are not recommended