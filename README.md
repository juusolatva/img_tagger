# img tagger

A python script that tags all the **JPEG**, **WebP**, **PNG** and **GIF** files in the directory (and possibly subdirectories) by sending them to a local vision-language model (either **ollama** or **LM Studio** running on the same computer or the local network). The tags the model sends back are added as metadata to the images. The accuracy of the tags depends on the model.

## Features

- Creates tags for **JPEGs**, **WebPs**, **PNGs** and **GIFs**
- Skips already processed files that have the marker: ***[PROCESSED BY AI]***
- Runs **1 to 4** workers concurrently
- Collects simple performance metrics
- Optional logging for troubleshooting


---


## Installation
Clone the repository and run *img_tagger.py* with Python.

### System prerequisites
You must have [Python](https://www.python.org/downloads/) and either **[ollama](https://ollama.com/download)** or **[LM Studio](https://lmstudio.ai/download)** with a vision-capable model for processing the images.

### Installing dependencies
*pip install -r requirements.txt*


---


## Usage
Just give it the folder with your images:

*python img_tagger.py path/to/images*

### Arguments
- *directory* - the folder with your images
- *-r* or *--recursive* - go through the subfolders too
- *--backend* - *ollama* (default) or *lm-studio*
- *--host* - address of the model server if it's not running on the default local address
- *--model* - the model to use (*qwen3-vl:8b* by default)
- *--workers* - how many images to process at the same time, from 1 to 4 (1 by default)
- *--log* - save a log file for troubleshooting, e.g. *--log logs/run.log*
- *--clear* - remove all metadata (EXIF/XMP/IPTC) and comments from images instead of tagging

### Examples
Tag a folder and all of its subfolders with ollama:

*python img_tagger.py ~/Pictures/memes -r*

Use LM Studio with 4 workers and save a log:

*python img_tagger.py ~/Pictures/memes --backend lm-studio --workers 4 --log run.log*

Clear all tags and metadata from a folder recursively:

*python img_tagger.py ~/Pictures/memes -r --clear*


---


### Notes
- Run *img_tagger.py -h* or *img_tagger.py --help* to see the arguments and options
- Press `Q` to quit and then wait for it to finish all the work in progress
- The script doesn't respect preexisting tags so keep in mind that they will be overwritten
- The default model is **Qwen3 VL 8B** but any model capable of processing image inputs should work
- Allows a maximum of 4 workers which is the default limit for both **ollama** and **LM Studio**
- In case you need to you can clear all the tags in a folder using the *--clear* flag or the standalone *clear_tags.py* script

### Running tests
*pip install -r requirements-dev.txt* and then *python -m pytest*. The default tests run offline (no model server needed).

To check tagging quality against real images, put them in a *test_images/* folder and run *python -m pytest -m integration --run-integration -s* with ollama or LM Studio running. Use *--tagger-backend*, *--tagger-host*, *--tagger-model* and *--images-dir* to change the defaults. The images are copied to a temporary folder so the originals are not modified.

### Known issues
- Animated **WebPs** keep their animation but the model might only see the first frame
