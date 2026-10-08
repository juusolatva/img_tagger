"""End-to-end CLI tests that run the scripts as subprocesses (no model server needed)."""

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SAMPLE_TAGS, make_gif, make_image

import img_tagger

REPO = Path(__file__).resolve().parent.parent


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / script), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )


class TestImgTaggerCli:
    def test_help(self):
        result = run("img_tagger.py", "--help")
        assert result.returncode == 0
        for flag in ["--recursive", "--backend", "--host", "--model", "--workers", "--log", "--clear"]:
            assert flag in result.stdout

    def test_missing_directory(self, tmp_path):
        result = run("img_tagger.py", str(tmp_path / "nope"))
        assert result.returncode == 1
        assert "could not be located" in result.stdout

    @pytest.mark.parametrize("workers", ["0", "5", "-1"])
    def test_rejects_out_of_range_workers(self, tmp_path, workers):
        result = run("img_tagger.py", str(tmp_path), "--workers", workers)
        assert result.returncode == 1
        assert "Workers must be between 1 and 4" in result.stdout

    def test_rejects_unknown_backend(self, tmp_path):
        result = run("img_tagger.py", str(tmp_path), "--backend", "bogus")
        assert result.returncode == 2
        assert "invalid choice" in result.stderr

    def test_empty_directory_exits_cleanly(self, tmp_path):
        result = run("img_tagger.py", str(tmp_path))
        assert result.returncode == 0
        assert "No valid images found" in result.stdout

    def test_log_file_created(self, tmp_path):
        log = tmp_path / "logs" / "run.log"
        result = run("img_tagger.py", str(tmp_path), "--log", str(log))
        assert result.returncode == 0
        assert log.parent.is_dir()

    @pytest.mark.parametrize("recursive", [False, True])
    def test_clears_directory(self, tmp_path, recursive):
        top_jpg = make_image(tmp_path / "a.jpg")
        top_gif = make_gif(tmp_path / "b.gif")
        sub = tmp_path / "sub"
        sub.mkdir()
        nested = make_image(sub / "c.png")
        for path in (top_jpg, top_gif, nested):
            img_tagger.tag_image(str(path), SAMPLE_TAGS)

        args = [str(tmp_path), "--clear"] + (["-r"] if recursive else [])
        result = run("img_tagger.py", *args)

        assert result.returncode == 0
        assert f"Resetting tags for {3 if recursive else 2} images" in result.stdout
        assert not img_tagger.is_already_processed(top_jpg)
        assert not img_tagger.is_already_processed(top_gif)
        assert img_tagger.is_already_processed(nested) is not recursive

    def test_clears_with_multiple_workers(self, tmp_path):
        paths = [make_image(tmp_path / f"img_{i}.jpg") for i in range(4)]
        for p in paths:
            img_tagger.tag_image(str(p), SAMPLE_TAGS)

        result = run("img_tagger.py", str(tmp_path), "--clear", "--workers", "2")
        assert result.returncode == 0
        assert "Resetting tags for 4 images" in result.stdout
        for p in paths:
            assert not img_tagger.is_already_processed(p)
