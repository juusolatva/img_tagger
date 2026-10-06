"""Shared fixtures and helpers for the img_tagger test suite.

All images used by the automated tests are generated on the fly with Pillow,
so the suite needs no fixture files and never talks to a model server.
The opt-in integration tests (see test_integration.py) are the only ones that
need a live Ollama / LM Studio backend and real images in test_images/.
"""

from pathlib import Path
from typing import ClassVar

import pyexiv2
import pytest
from PIL import Image, ImageSequence

MARKER = "[PROCESSED BY AI]"
SAMPLE_TAGS = ["photo", "sunset", "beach", "ocean", "sky", "clouds"]

# extension -> Pillow format name
STATIC_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


# --- Command-line options -------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("img_tagger integration")
    group.addoption(
        "--run-integration",
        action="store_true",
        help="run tests that need a live model server and images in test_images/",
    )
    group.addoption(
        "--tagger-backend",
        choices=["ollama", "lm-studio"],
        default="ollama",
        help="backend for integration tests (default: ollama)",
    )
    group.addoption("--tagger-host", default=None, help="backend URL (default: localhost for the backend)")
    group.addoption("--tagger-model", default="qwen3-vl:8b", help="model for integration tests")
    group.addoption(
        "--images-dir",
        default=str(Path(__file__).resolve().parent.parent / "test_images"),
        help="directory of real images for integration tests (default: ./test_images)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="integration test: pass --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


# --- Image factories ------------------------------------------------------

def make_image(path: Path, color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (32, 24)) -> Path:
    """Create a small solid-color static image; format is chosen from the extension."""
    fmt = STATIC_FORMATS[path.suffix.lower().lstrip(".")]
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


GIF_COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
GIF_DURATIONS = [50, 120, 200]


def make_gif(
    path: Path,
    durations: list[int] | None = None,
    loop: int = 0,
    comment: str | None = None,
) -> Path:
    """Create an animated GIF whose frames all differ (so Pillow cannot merge them)."""
    durations = durations or GIF_DURATIONS
    frames = [Image.new("RGB", (8, 8), GIF_COLORS[i % len(GIF_COLORS)]).convert("P") for i in range(len(durations))]
    kwargs = {"comment": comment} if comment is not None else {}
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=loop, **kwargs)
    return path


def read_metadata(path: Path) -> tuple[dict, dict]:
    """Return (exif, xmp) dicts as read by pyexiv2."""
    with pyexiv2.Image(str(path), encoding="utf-8") as img:
        return img.read_exif(), img.read_xmp()


def make_animated(path: Path, durations: list[int] | None = None, loop: int = 0) -> Path:
    """Create an animated WebP or PNG (APNG) whose frames all differ; format is chosen from the extension."""
    durations = durations or GIF_DURATIONS
    fmt = {"webp": "WEBP", "png": "PNG"}[path.suffix.lower().lstrip(".")]
    frames = [Image.new("RGB", (8, 8), GIF_COLORS[i % len(GIF_COLORS)]) for i in range(len(durations))]
    frames[0].save(path, format=fmt, save_all=True, append_images=frames[1:], duration=durations, loop=loop)
    return path


def animation_info(path: Path) -> dict:
    """Return frame count, per-frame durations and loop of an animated WebP/APNG."""
    with Image.open(path) as img:
        durations = []
        for frame in ImageSequence.Iterator(img):
            frame.load()  # WebP fills in the duration only after decoding the frame
            durations.append(round(frame.info.get("duration", 0)))  # APNG reports floats
        return {"n_frames": img.n_frames, "durations": durations, "loop": img.info.get("loop")}


def gif_info(path: Path) -> dict:
    """Return frame count, per-frame durations, loop and comment of a GIF."""
    with Image.open(path) as img:
        durations = [f.info.get("duration") for f in ImageSequence.Iterator(img)]
        comment = img.info.get("comment")
        return {
            "n_frames": img.n_frames,
            "durations": durations,
            "loop": img.info.get("loop"),
            "comment": comment.decode("utf-8") if isinstance(comment, bytes) else comment,
        }


def leftover_temp_files(directory: Path) -> list[Path]:
    return list(directory.glob("*.tmp"))


@pytest.fixture(params=["jpg", "png", "webp"])
def static_image(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A fresh untagged JPEG / PNG / WebP image."""
    return make_image(tmp_path / f"sample.{request.param}")


@pytest.fixture
def gif_image(tmp_path: Path) -> Path:
    """A fresh untagged 3-frame animated GIF with variable frame durations."""
    return make_gif(tmp_path / "anim.gif")


# --- Fake backend clients -------------------------------------------------

class _Obj:
    """Tiny attribute bag used to mimic SDK response objects."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeOllamaClient:
    """Stand-in for ollama.Client that returns a canned reply and records calls."""

    instances: ClassVar[list["FakeOllamaClient"]] = []
    reply: str = ", ".join(SAMPLE_TAGS)

    def __init__(self, host: str | None = None):
        self.host = host
        self.calls: list[dict] = []
        FakeOllamaClient.instances.append(self)

    def chat(self, model, messages):
        self.calls.append({"model": model, "messages": messages})
        return _Obj(message=_Obj(content=self.reply))


class FakeOpenAIClient:
    """Stand-in for openai.OpenAI exposing chat.completions.create()."""

    instances: ClassVar[list["FakeOpenAIClient"]] = []
    reply: str = ", ".join(SAMPLE_TAGS)

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = base_url
        self.api_key = api_key
        self.calls: list[dict] = []
        self.chat = _Obj(completions=_Obj(create=self._create))
        FakeOpenAIClient.instances.append(self)

    def _create(self, model, messages):
        self.calls.append({"model": model, "messages": messages})
        return _Obj(choices=[_Obj(message=_Obj(content=self.reply))])


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch):
    import img_tagger

    FakeOllamaClient.instances = []
    FakeOllamaClient.reply = ", ".join(SAMPLE_TAGS)
    monkeypatch.setattr(img_tagger, "OllamaClient", FakeOllamaClient)
    return FakeOllamaClient


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch):
    import img_tagger

    FakeOpenAIClient.instances = []
    FakeOpenAIClient.reply = ", ".join(SAMPLE_TAGS)
    monkeypatch.setattr(img_tagger, "OpenAI", FakeOpenAIClient)
    return FakeOpenAIClient


@pytest.fixture
def no_quit_listener(monkeypatch: pytest.MonkeyPatch):
    """Replace the stdin 'Q' listener so process_directory() never touches the real terminal."""
    import img_tagger

    monkeypatch.setattr(img_tagger, "listen_for_quit", lambda stop_event: None)
