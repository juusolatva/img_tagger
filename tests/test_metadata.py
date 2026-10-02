"""Tests for writing/reading tag metadata and the file-replacement helpers in img_tagger.py."""

from pathlib import Path

import pytest
from conftest import (
    GIF_DURATIONS,
    MARKER,
    SAMPLE_TAGS,
    gif_info,
    leftover_temp_files,
    make_gif,
    make_image,
    read_metadata,
)
from PIL import Image

import img_tagger


def pixels(path: Path) -> bytes:
    with Image.open(path) as img:
        return img.convert("RGB").tobytes()


# --- get_image_format -----------------------------------------------------

class TestGetImageFormat:
    @pytest.mark.parametrize("ext, expected", [("jpg", "JPEG"), ("jpeg", "JPEG"), ("png", "PNG"), ("webp", "WEBP")])
    def test_static_formats(self, tmp_path, ext, expected):
        assert img_tagger.get_image_format(make_image(tmp_path / f"a.{ext}")) == expected

    def test_gif(self, gif_image):
        assert img_tagger.get_image_format(gif_image) == "GIF"

    def test_detects_content_not_extension(self, tmp_path):
        mislabeled = tmp_path / "actually_png.jpg"
        Image.new("RGB", (4, 4)).save(mislabeled, format="PNG")
        assert img_tagger.get_image_format(mislabeled) == "PNG"

    def test_garbage_file_returns_none(self, tmp_path):
        bogus = tmp_path / "bogus.jpg"
        bogus.write_bytes(b"definitely not an image")
        assert img_tagger.get_image_format(bogus) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert img_tagger.get_image_format(tmp_path / "missing.png") is None


# --- write_metadata (JPEG / PNG / WebP) -----------------------------------

class TestWriteMetadata:
    def test_writes_exif_and_xmp_schema(self, static_image):
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)

        exif, xmp = read_metadata(static_image)
        tags_str = ", ".join(SAMPLE_TAGS)
        assert exif["Exif.Photo.UserComment"] == f"{tags_str} {MARKER}"
        assert xmp["Xmp.dc.subject"] == SAMPLE_TAGS
        assert xmp["Xmp.dc.description"] == {'lang="x-default"': f"Tags: {tags_str} | {MARKER}"}

    def test_marks_image_as_processed(self, static_image):
        assert not img_tagger.is_already_processed(static_image)
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        assert img_tagger.is_already_processed(static_image)

    def test_non_ascii_tags_and_filename(self, tmp_path):
        path = make_image(tmp_path / "kesä_ö.jpg")
        tags = ["café", "kesä", "日本", "naïve", "über", "smörgåsbord"]

        img_tagger.write_metadata(str(path), tags)

        exif, xmp = read_metadata(path)
        assert xmp["Xmp.dc.subject"] == tags
        assert exif["Exif.Photo.UserComment"].startswith("café, kesä, 日本")
        assert img_tagger.is_already_processed(path)

    def test_overwrites_previous_tags(self, static_image):
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        new_tags = ["cat", "meme", "funny", "reaction", "pet", "animal"]
        img_tagger.write_metadata(str(static_image), new_tags)

        _, xmp = read_metadata(static_image)
        assert xmp["Xmp.dc.subject"] == new_tags

    def test_image_pixels_unchanged(self, static_image):
        before = pixels(static_image)
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        assert pixels(static_image) == before

    def test_no_temp_files_left_behind(self, static_image):
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        assert leftover_temp_files(static_image.parent) == []

    def test_temp_file_created_next_to_image(self, static_image, monkeypatch):
        seen_dirs = []
        real_mkstemp = img_tagger.tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            seen_dirs.append(Path(kwargs["dir"]))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(img_tagger.tempfile, "mkstemp", spy_mkstemp)
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        assert seen_dirs == [static_image.parent]

    def test_failure_leaves_original_untouched_and_cleans_up(self, static_image, monkeypatch):
        original = static_image.read_bytes()

        class BrokenImage:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("some unrelated pyexiv2 failure")

        monkeypatch.setattr(img_tagger.pyexiv2, "Image", BrokenImage)

        with pytest.raises(RuntimeError, match="unrelated"):
            img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)

        assert static_image.read_bytes() == original
        assert leftover_temp_files(static_image.parent) == []

    @pytest.mark.parametrize("error", ["Invalid IFD structure", "Image data is corrupt"])
    def test_auto_heals_corrupt_metadata_via_pillow(self, static_image, monkeypatch, error):
        real_image = img_tagger.pyexiv2.Image
        attempts = []

        def flaky_image(*args, **kwargs):
            attempts.append(args[0])
            if len(attempts) == 1:
                raise RuntimeError(error)
            return real_image(*args, **kwargs)

        monkeypatch.setattr(img_tagger.pyexiv2, "Image", flaky_image)
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        monkeypatch.setattr(img_tagger.pyexiv2, "Image", real_image)

        assert len(attempts) == 2, "pyexiv2 should be retried once on the sanitized file"
        assert img_tagger.is_already_processed(static_image)
        expected_format = {".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP"}[static_image.suffix]
        assert img_tagger.get_image_format(static_image) == expected_format
        assert leftover_temp_files(static_image.parent) == []


# --- write_gif_tags -------------------------------------------------------

class TestWriteGifTags:
    def test_writes_comment_with_marker(self, gif_image):
        img_tagger.write_gif_tags(str(gif_image), SAMPLE_TAGS)
        assert gif_info(gif_image)["comment"] == f"{', '.join(SAMPLE_TAGS)} {MARKER}"
        assert img_tagger.is_already_processed(gif_image)

    def test_preserves_frames_durations_and_loop(self, tmp_path):
        path = make_gif(tmp_path / "anim.gif", durations=[40, 250, 90, 500], loop=3)
        img_tagger.write_gif_tags(str(path), SAMPLE_TAGS)

        info = gif_info(path)
        assert info["n_frames"] == 4
        assert info["durations"] == [40, 250, 90, 500]
        assert info["loop"] == 3

    def test_preserves_frame_content(self, gif_image):
        def frame_pixels(path):
            with Image.open(path) as img:
                out = []
                for i in range(img.n_frames):
                    img.seek(i)
                    out.append(img.convert("RGB").tobytes())
                return out

        before = frame_pixels(gif_image)
        img_tagger.write_gif_tags(str(gif_image), SAMPLE_TAGS)
        assert frame_pixels(gif_image) == before

    def test_single_frame_gif(self, tmp_path):
        path = tmp_path / "still.gif"
        Image.new("RGB", (8, 8), (10, 20, 30)).convert("P").save(path)
        img_tagger.write_gif_tags(str(path), SAMPLE_TAGS)
        assert gif_info(path)["n_frames"] == 1
        assert img_tagger.is_already_processed(path)

    def test_non_ascii_tags(self, gif_image):
        tags = ["café", "kesä", "日本", "naïve", "über", "smörgåsbord"]
        img_tagger.write_gif_tags(str(gif_image), tags)
        assert gif_info(gif_image)["comment"].startswith("café, kesä, 日本")

    def test_no_temp_files_left_behind(self, gif_image):
        img_tagger.write_gif_tags(str(gif_image), SAMPLE_TAGS)
        assert leftover_temp_files(gif_image.parent) == []


# --- tag_image (format router) --------------------------------------------

class TestTagImage:
    def test_routes_static_images_to_write_metadata(self, static_image):
        img_tagger.tag_image(str(static_image), SAMPLE_TAGS)
        _, xmp = read_metadata(static_image)
        assert xmp["Xmp.dc.subject"] == SAMPLE_TAGS

    def test_routes_gif_to_write_gif_tags(self, gif_image):
        img_tagger.tag_image(str(gif_image), SAMPLE_TAGS)
        assert MARKER in gif_info(gif_image)["comment"]
        assert gif_info(gif_image)["durations"] == GIF_DURATIONS

    def test_uses_detected_format_over_extension(self, tmp_path):
        # A PNG named .gif must be written with pyexiv2, not the GIF writer.
        path = tmp_path / "png_in_disguise.gif"
        Image.new("RGB", (4, 4)).save(path, format="PNG")
        img_tagger.tag_image(str(path), SAMPLE_TAGS)
        _, xmp = read_metadata(path)
        assert xmp["Xmp.dc.subject"] == SAMPLE_TAGS

    def test_invalid_file_raises(self, tmp_path):
        bogus = tmp_path / "bogus.png"
        bogus.write_bytes(b"nope")
        with pytest.raises(RuntimeError, match="Could not determine format"):
            img_tagger.tag_image(str(bogus), SAMPLE_TAGS)

    def test_unsupported_format_raises(self, tmp_path):
        bmp = tmp_path / "image.bmp"
        Image.new("RGB", (4, 4)).save(bmp, format="BMP")
        with pytest.raises(RuntimeError, match="Unsupported file format: BMP"):
            img_tagger.tag_image(str(bmp), SAMPLE_TAGS)

    def test_writer_errors_are_wrapped(self, static_image, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(img_tagger, "write_metadata", boom)
        with pytest.raises(RuntimeError, match=r"Tagging failed for .*disk full"):
            img_tagger.tag_image(str(static_image), SAMPLE_TAGS)


# --- is_already_processed -------------------------------------------------

class TestIsAlreadyProcessed:
    def test_fresh_images_are_not_processed(self, static_image, gif_image):
        assert not img_tagger.is_already_processed(static_image)
        assert not img_tagger.is_already_processed(gif_image)

    def test_gif_with_unrelated_comment(self, tmp_path):
        path = make_gif(tmp_path / "c.gif", comment="made with GIMP")
        assert not img_tagger.is_already_processed(path)

    def test_gif_with_marker_comment(self, tmp_path):
        path = make_gif(tmp_path / "c.gif", comment=f"a, b {MARKER}")
        assert img_tagger.is_already_processed(path)

    def test_marker_only_in_xmp_description(self, tmp_path):
        import pyexiv2

        path = make_image(tmp_path / "x.jpg")
        with pyexiv2.Image(str(path)) as img:
            img.modify_xmp({"Xmp.dc.description": f"tagged elsewhere {MARKER}"})
        assert img_tagger.is_already_processed(path)

    def test_marker_only_in_png_text_chunk_uses_pillow_fallback(self, tmp_path):
        from PIL import PngImagePlugin

        path = tmp_path / "text.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", f"tags {MARKER}")
        Image.new("RGB", (4, 4)).save(path, pnginfo=info)
        assert img_tagger.is_already_processed(path)

    def test_unreadable_file_is_not_processed(self, tmp_path):
        bogus = tmp_path / "bogus.jpg"
        bogus.write_bytes(b"garbage")
        assert not img_tagger.is_already_processed(bogus)

    def test_unknown_extension_is_not_processed(self, tmp_path):
        path = tmp_path / "file.bmp"
        Image.new("RGB", (4, 4)).save(path, format="BMP")
        assert not img_tagger.is_already_processed(path)


# --- robust_replace -------------------------------------------------------

class TestRobustReplace:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(img_tagger.time, "sleep", lambda s: None)

    def test_replaces_file(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_text("new")
        dst.write_text("old")
        img_tagger.robust_replace(src, dst)
        assert dst.read_text() == "new"
        assert not src.exists()

    def test_retries_transient_errors(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_text("new")
        real_replace = Path.replace
        calls = []

        def flaky_replace(self, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("file is locked")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", flaky_replace)
        img_tagger.robust_replace(src, dst)
        assert len(calls) == 3
        assert dst.read_text() == "new"

    def test_gives_up_after_ten_attempts(self, tmp_path, monkeypatch):
        calls = []

        def always_locked(self, target):
            calls.append(1)
            raise PermissionError("file is locked")

        monkeypatch.setattr(Path, "replace", always_locked)
        with pytest.raises(OSError, match="after retries"):
            img_tagger.robust_replace(tmp_path / "a", tmp_path / "b")
        assert len(calls) == 10
