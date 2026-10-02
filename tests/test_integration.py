"""Opt-in integration tests against a real model server and real images.

These are NOT run by default: they need a running Ollama or LM Studio instance
with a vision-capable model, plus images in test_images/ (git-ignored). They
are meant for manually checking tagging quality, so each image's generated
tags are printed — run with -s to see them.

Images are copied to a temporary directory first, so the originals in
test_images/ are never modified.

Examples:
    pytest -m integration --run-integration -s
    pytest -m integration --run-integration -s --tagger-backend lm-studio --tagger-model qwen/qwen3-vl-8b
    pytest -m integration --run-integration -s --images-dir ~/Pictures/samples --tagger-host http://192.168.1.10:11434
"""

import shutil
import threading
from pathlib import Path

import pytest

import img_tagger

pytestmark = pytest.mark.integration

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _source_images(images_dir: Path) -> list[Path]:
    if not images_dir.is_dir():
        return []
    return sorted(p for p in images_dir.rglob("*") if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "source_image" not in metafunc.fixturenames:
        return
    images_dir = Path(metafunc.config.getoption("--images-dir")).expanduser()
    images = _source_images(images_dir)
    if images:
        metafunc.parametrize("source_image", images, ids=[str(p.relative_to(images_dir)) for p in images])
    else:
        metafunc.parametrize(
            "source_image",
            [pytest.param(None, marks=pytest.mark.skip(reason=f"no images found in {images_dir}"))],
        )


@pytest.fixture(scope="session")
def backend_settings(pytestconfig: pytest.Config) -> dict:
    backend = pytestconfig.getoption("--tagger-backend")
    host = pytestconfig.getoption("--tagger-host") or (
        "http://localhost:11434" if backend == "ollama" else "http://localhost:1234"
    )
    return {"backend": backend, "host": host, "model": pytestconfig.getoption("--tagger-model")}


@pytest.fixture(scope="session")
def client(backend_settings: dict):
    if backend_settings["backend"] == "ollama":
        if img_tagger.OllamaClient is None:
            pytest.skip("ollama library not installed")
        return img_tagger.OllamaClient(host=backend_settings["host"])
    if img_tagger.OpenAI is None:
        pytest.skip("openai library not installed")
    return img_tagger.OpenAI(base_url=f"{backend_settings['host']}/v1", api_key="lm-studio")


@pytest.fixture(scope="session")
def prompt() -> str:
    prompt_file = Path(img_tagger.__file__).parent / "prompt.txt"
    return prompt_file.read_text(encoding="utf-8").strip() if prompt_file.exists() else img_tagger.DEFAULT_PROMPT


def test_tag_real_image(source_image: Path, tmp_path: Path, client, backend_settings: dict, prompt: str) -> None:
    image = tmp_path / source_image.name
    shutil.copy2(source_image, image)

    # Start from a clean slate even if the source image was tagged earlier.
    if img_tagger.is_already_processed(image):
        import clear_tags

        clear_tags.clear_tags(image)
        if img_tagger.is_already_processed(image):
            pytest.skip("source image is already tagged and its tags could not be cleared")

    status, _, message, duration = img_tagger.process_single_image(
        image, client, backend_settings["backend"], backend_settings["model"], prompt, threading.Event()
    )
    print(f"\n[{status}] {source_image.name} ({duration:.1f}s) -> {message}")

    assert status == "SUCCESS", message
    assert img_tagger.is_already_processed(image)
