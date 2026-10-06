"""Tests for clear_tags.py: tags written by img_tagger must be removable again."""

from pathlib import Path

import pytest
from conftest import (
    SAMPLE_TAGS,
    animation_info,
    gif_info,
    leftover_temp_files,
    make_animated,
    make_gif,
    make_image,
    read_metadata,
)
from PIL import Image

import clear_tags
import img_tagger


def tag(path: Path) -> Path:
    img_tagger.tag_image(str(path), SAMPLE_TAGS)
    assert img_tagger.is_already_processed(path)
    return path


@pytest.mark.parametrize("ext", ["jpg", "jpeg", "png", "webp"])
def test_clears_static_image_tags(tmp_path, ext):
    path = tag(make_image(tmp_path / f"a.{ext}"))

    clear_tags.clear_tags(path)

    exif, xmp = read_metadata(path)
    assert "Exif.Photo.UserComment" not in exif
    assert "Xmp.dc.subject" not in xmp
    assert not img_tagger.is_already_processed(path)
    assert leftover_temp_files(tmp_path) == []


def test_clears_gif_comment_and_preserves_animation(tmp_path):
    path = tag(make_gif(tmp_path / "a.gif", durations=[40, 250, 90], loop=2))

    clear_tags.clear_tags(path)

    info = gif_info(path)
    assert not info["comment"]
    assert info["n_frames"] == 3
    assert info["durations"] == [40, 250, 90]
    assert info["loop"] == 2
    assert not img_tagger.is_already_processed(path)
    assert leftover_temp_files(tmp_path) == []


def test_clear_preserves_pixels(tmp_path):
    path = tag(make_image(tmp_path / "a.png"))
    with Image.open(path) as img:
        before = img.tobytes()
    clear_tags.clear_tags(path)
    with Image.open(path) as img:
        assert img.tobytes() == before


def test_tag_clear_tag_cycle(tmp_path):
    path = tag(make_image(tmp_path / "a.jpg"))
    clear_tags.clear_tags(path)
    tag(path)


def test_ignores_unsupported_extension(tmp_path):
    path = tmp_path / "a.bmp"
    Image.new("RGB", (4, 4)).save(path, format="BMP")
    original = path.read_bytes()
    clear_tags.clear_tags(path)
    assert path.read_bytes() == original


def test_failure_is_reported_and_cleans_up(tmp_path, capsys):
    bogus = tmp_path / "bogus.gif"
    bogus.write_bytes(b"not a gif")

    clear_tags.clear_tags(bogus)  # must not raise

    assert "Failed to clear bogus.gif" in capsys.readouterr().out
    assert bogus.read_bytes() == b"not a gif"
    assert leftover_temp_files(tmp_path) == []


@pytest.mark.parametrize("error", ["Invalid IFD structure", "Image data is corrupt"])
def test_auto_heals_corrupt_metadata_via_pillow(tmp_path, monkeypatch, capsys, error):
    path = make_image(tmp_path / "a.jpg")

    def corrupt(*args, **kwargs):
        raise RuntimeError(error)

    monkeypatch.setattr(clear_tags.pyexiv2, "Image", corrupt)
    clear_tags.clear_tags(path)

    assert "Sanitized and cleared (Pillow)" in capsys.readouterr().out
    with Image.open(path) as img:
        assert img.format == "JPEG"
    assert leftover_temp_files(tmp_path) == []


@pytest.mark.parametrize("ext", ["webp", "png"])
def test_clearing_keeps_animation(tmp_path, ext):
    path = tag(make_animated(tmp_path / f"anim.{ext}", durations=[40, 250, 90], loop=3))
    clear_tags.clear_tags(path)
    assert animation_info(path) == {"n_frames": 3, "durations": [40, 250, 90], "loop": 3}
    assert not img_tagger.is_already_processed(path)


@pytest.mark.parametrize("ext", ["webp", "png"])
def test_auto_heal_keeps_animation(tmp_path, monkeypatch, capsys, ext):
    path = make_animated(tmp_path / f"anim.{ext}", durations=[40, 250, 90], loop=3)

    def corrupt(*args, **kwargs):
        raise RuntimeError("Image data is corrupt")

    monkeypatch.setattr(clear_tags.pyexiv2, "Image", corrupt)
    clear_tags.clear_tags(path)

    assert "Sanitized and cleared (Pillow)" in capsys.readouterr().out
    assert animation_info(path) == {"n_frames": 3, "durations": [40, 250, 90], "loop": 3}
    assert leftover_temp_files(tmp_path) == []


def test_unrelated_pyexiv2_error_leaves_file_untouched(tmp_path, monkeypatch, capsys):
    path = tag(make_image(tmp_path / "a.png"))
    original = path.read_bytes()

    def broken(*args, **kwargs):
        raise RuntimeError("something else")

    monkeypatch.setattr(clear_tags.pyexiv2, "Image", broken)
    clear_tags.clear_tags(path)

    assert "Failed to clear a.png" in capsys.readouterr().out
    assert path.read_bytes() == original
    assert leftover_temp_files(tmp_path) == []


class TestRobustReplace:
    """clear_tags.py keeps its own copy of robust_replace(); it must behave like img_tagger's."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(clear_tags.time, "sleep", lambda s: None)

    def test_retries_transient_errors(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_text("new")
        real_replace = Path.replace
        calls = []

        def flaky_replace(self, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("locked")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", flaky_replace)
        clear_tags.robust_replace(src, dst)
        assert len(calls) == 3
        assert dst.read_text() == "new"

    def test_gives_up_after_ten_attempts(self, tmp_path, monkeypatch):
        calls = []

        def always_locked(self, target):
            calls.append(1)
            raise PermissionError("locked")

        monkeypatch.setattr(Path, "replace", always_locked)
        with pytest.raises(OSError, match="after retries"):
            clear_tags.robust_replace(tmp_path / "a", tmp_path / "b")
        assert len(calls) == 10
