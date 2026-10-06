"""Tests for process_single_image(), process_directory() and listen_for_quit() with fake backends."""

import os
import sys
import threading
import time

import pytest

from conftest import (
    MARKER,
    SAMPLE_TAGS,
    FakeOllamaClient,
    FakeOpenAIClient,
    gif_info,
    make_gif,
    make_image,
    read_metadata,
)

import img_tagger


def run_single(path, client=None, backend="ollama", stop=False):
    client = client or FakeOllamaClient()
    event = threading.Event()
    if stop:
        event.set()
    return img_tagger.process_single_image(path, client, backend, "test-model", "prompt", event)


# --- process_single_image -------------------------------------------------

class TestProcessSingleImage:
    def test_success_tags_static_image(self, static_image):
        status, name, message, duration = run_single(static_image)

        assert status == "SUCCESS"
        assert name == static_image.name
        assert str(SAMPLE_TAGS) in message
        assert duration >= 0
        _, xmp = read_metadata(static_image)
        assert xmp["Xmp.dc.subject"] == SAMPLE_TAGS

    def test_success_tags_gif(self, gif_image):
        status, *_ = run_single(gif_image)
        assert status == "SUCCESS"
        assert MARKER in gif_info(gif_image)["comment"]

    def test_lm_studio_backend(self, static_image):
        client = FakeOpenAIClient()
        status, *_ = run_single(static_image, client=client, backend="lm-studio")
        assert status == "SUCCESS"
        assert len(client.calls) == 1

    def test_already_processed_is_skipped_without_calling_model(self, static_image):
        img_tagger.write_metadata(str(static_image), SAMPLE_TAGS)
        client = FakeOllamaClient()

        status, _, message, duration = run_single(static_image, client=client)

        assert (status, message, duration) == ("SKIPPED", "Skipped: Already Tagged", 0)
        assert client.calls == []

    def test_invalid_file_fails_without_calling_model(self, tmp_path):
        bogus = tmp_path / "bogus.jpg"
        bogus.write_bytes(b"not an image")
        client = FakeOllamaClient()

        status, _, message, _ = run_single(bogus, client=client)

        assert status == "FAILED"
        assert "unsupported or corrupted" in message
        assert client.calls == []

    def test_stop_event_cancels_before_model_call(self, static_image):
        client = FakeOllamaClient()
        status, *_ = run_single(static_image, client=client, stop=True)
        assert status == "CANCELLED"
        assert client.calls == []

    def test_unparseable_model_output_fails_and_leaves_image_untagged(self, static_image):
        client = FakeOllamaClient()
        client.reply = "I cannot help with that."

        status, _, message, _ = run_single(static_image, client=client)

        assert status == "FAILED"
        assert "insufficient tags" in message
        assert not img_tagger.is_already_processed(static_image)

    def test_backend_exception_is_reported_as_failure(self, static_image):
        class DownClient:
            def chat(self, model, messages):
                raise ConnectionError("connection refused")

        status, _, message, _ = run_single(static_image, client=DownClient())
        assert status == "FAILED"
        assert "connection refused" in message

    def test_tagging_exception_is_reported_as_failure(self, static_image, monkeypatch):
        def broken_tag_image(*args, **kwargs):
            raise RuntimeError("write failed")

        monkeypatch.setattr(img_tagger, "tag_image", broken_tag_image)
        status, _, message, _ = run_single(static_image)
        assert status == "FAILED"
        assert "write failed" in message


# --- process_directory ----------------------------------------------------

@pytest.mark.usefixtures("no_quit_listener")
class TestProcessDirectory:
    def _populate(self, root):
        make_image(root / "a.jpg")
        make_image(root / "b.png")
        make_image(root / "c.webp")
        make_gif(root / "d.gif")
        (root / "notes.txt").write_text("not an image")
        sub = root / "sub"
        sub.mkdir()
        make_image(sub / "nested.jpg")

    def _run(self, root, recursive=False, backend="ollama", workers=1):
        img_tagger.process_directory(str(root), recursive, backend, "http://fake:1", "test-model", workers)

    def test_tags_top_level_images_only_when_not_recursive(self, tmp_path, fake_ollama, capsys):
        self._populate(tmp_path)
        self._run(tmp_path)

        out = capsys.readouterr().out
        assert "Found 4 images" in out
        assert "Processed images: 4" in out
        assert "Failed images: 0" in out
        assert not img_tagger.is_already_processed(tmp_path / "sub" / "nested.jpg")
        for name in ["a.jpg", "b.png", "c.webp", "d.gif"]:
            assert img_tagger.is_already_processed(tmp_path / name), name

    def test_recursive_includes_subdirectories(self, tmp_path, fake_ollama, capsys):
        self._populate(tmp_path)
        self._run(tmp_path, recursive=True)

        assert "Processed images: 5" in capsys.readouterr().out
        assert img_tagger.is_already_processed(tmp_path / "sub" / "nested.jpg")

    @pytest.mark.parametrize("workers", [1, 4])
    def test_worker_counts(self, tmp_path, fake_ollama, capsys, workers):
        for i in range(8):
            make_image(tmp_path / f"img{i}.jpg")
        self._run(tmp_path, workers=workers)
        assert "Processed images: 8" in capsys.readouterr().out
        assert list(tmp_path.glob("*.tmp")) == []

    def test_second_run_skips_everything(self, tmp_path, fake_ollama, capsys):
        self._populate(tmp_path)
        self._run(tmp_path)
        capsys.readouterr()
        fake_ollama.instances.clear()

        self._run(tmp_path)

        out = capsys.readouterr().out
        assert "Processed images: 0" in out
        assert "Skipped images: 4" in out
        assert fake_ollama.instances[0].calls == []

    def test_failures_are_listed_in_report(self, tmp_path, fake_ollama, capsys):
        make_image(tmp_path / "good.jpg")
        (tmp_path / "broken.png").write_bytes(b"corrupt")
        self._run(tmp_path)

        out = capsys.readouterr().out
        assert "Processed images: 1" in out
        assert "Failed images: 1" in out
        assert "Failed files details" in out
        assert "broken.png" in out

    def test_ollama_client_configuration(self, tmp_path, fake_ollama, capsys):
        make_image(tmp_path / "a.jpg")
        self._run(tmp_path)

        client = fake_ollama.instances[0]
        assert client.host == "http://fake:1"
        assert client.calls[0]["model"] == "test-model"

    def test_lm_studio_client_configuration(self, tmp_path, fake_openai, capsys):
        make_image(tmp_path / "a.jpg")
        self._run(tmp_path, backend="lm-studio")

        client = fake_openai.instances[0]
        assert client.base_url == "http://fake:1/v1"
        assert client.api_key == "lm-studio"
        assert "Processed images: 1" in capsys.readouterr().out

    def test_uses_prompt_txt(self, tmp_path, fake_ollama, capsys):
        make_image(tmp_path / "a.jpg")
        self._run(tmp_path)

        expected = (img_tagger.Path(img_tagger.__file__).parent / "prompt.txt").read_text(encoding="utf-8").strip()
        assert fake_ollama.instances[0].calls[0]["messages"][0]["content"] == expected

    def test_falls_back_to_default_prompt(self, tmp_path, fake_ollama, monkeypatch, capsys):
        script_dir = tmp_path / "script"
        script_dir.mkdir()
        monkeypatch.setattr(img_tagger, "__file__", str(script_dir / "img_tagger.py"))
        images = tmp_path / "images"
        images.mkdir()
        make_image(images / "a.jpg")

        self._run(images)

        assert "Falling back to default" in capsys.readouterr().out
        assert fake_ollama.instances[0].calls[0]["messages"][0]["content"] == img_tagger.DEFAULT_PROMPT

    def test_custom_prompt_file_is_stripped(self, tmp_path, fake_ollama, monkeypatch, capsys):
        script_dir = tmp_path / "script"
        script_dir.mkdir()
        (script_dir / "prompt.txt").write_text("\n  my custom prompt  \n", encoding="utf-8")
        monkeypatch.setattr(img_tagger, "__file__", str(script_dir / "img_tagger.py"))
        images = tmp_path / "images"
        images.mkdir()
        make_image(images / "a.jpg")

        self._run(images)

        assert fake_ollama.instances[0].calls[0]["messages"][0]["content"] == "my custom prompt"

    def test_empty_directory(self, tmp_path, fake_ollama, capsys):
        (tmp_path / "readme.txt").write_text("hi")
        self._run(tmp_path)
        assert "No valid images found" in capsys.readouterr().out
        assert fake_ollama.instances == []

    def test_missing_ollama_library(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(img_tagger, "OllamaClient", None)
        make_image(tmp_path / "a.jpg")
        self._run(tmp_path)
        assert "'ollama' library not found" in capsys.readouterr().out
        assert not img_tagger.is_already_processed(tmp_path / "a.jpg")

    def test_missing_openai_library(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(img_tagger, "OpenAI", None)
        make_image(tmp_path / "a.jpg")
        self._run(tmp_path, backend="lm-studio")
        assert "'openai' library not found" in capsys.readouterr().out

    def test_quit_cancels_queued_images(self, tmp_path, fake_ollama, monkeypatch, capsys):
        # Simulate 'Q' being pressed before any work starts: nothing should reach the model.
        monkeypatch.setattr(img_tagger, "listen_for_quit", lambda stop_event: stop_event.set())
        for i in range(5):
            make_image(tmp_path / f"img{i}.jpg")

        self._run(tmp_path, workers=2)

        out = capsys.readouterr().out
        assert "Processed images: 0" in out
        assert "Failed images: 0" in out
        assert fake_ollama.instances[0].calls == []


# --- listen_for_quit ------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX pipes as fake stdin")
class TestListenForQuit:
    @pytest.fixture
    def listener(self, monkeypatch):
        """Start listen_for_quit() on a pipe posing as stdin; yields (event, thread, writer)."""
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "r")
        writer = os.fdopen(write_fd, "w")
        monkeypatch.setattr(sys, "stdin", reader)
        event = threading.Event()
        thread = threading.Thread(target=img_tagger.listen_for_quit, args=(event,), daemon=True)
        thread.start()
        yield event, thread, writer
        # Stop the thread before monkeypatch restores the real stdin.
        event.set()
        thread.join(timeout=3)
        if not writer.closed:
            writer.close()
        reader.close()

    @staticmethod
    def press(writer, keys):
        for key in keys:
            writer.write(key)
            writer.flush()
            time.sleep(0.15)  # longer than the listener's select() timeout: one key per read

    @pytest.mark.parametrize("key", ["q", "Q"])
    def test_q_sets_stop_event(self, listener, key):
        event, thread, writer = listener
        self.press(writer, ["x", key])
        assert event.wait(timeout=3)
        thread.join(timeout=3)
        assert not thread.is_alive()

    def test_other_keys_are_ignored(self, listener):
        event, thread, writer = listener
        self.press(writer, ["a", "b", "c"])
        assert not event.wait(timeout=0.3)
        assert thread.is_alive()

    def test_eof_exits_without_stopping(self, listener):
        event, thread, writer = listener
        writer.close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not event.is_set()

    def test_exits_when_event_set_externally(self, listener):
        event, thread, _ = listener
        time.sleep(0.2)
        event.set()
        thread.join(timeout=3)
        assert not thread.is_alive()

    @pytest.mark.xfail(
        strict=True,
        reason="read(1) drains the whole OS buffer into Python's text buffer, so a 'q' arriving "
        "in the same burst as another key (paste, key repeat) is never seen by select()",
    )
    def test_q_in_same_burst_as_other_key(self, listener):
        event, _, writer = listener
        writer.write("xq")
        writer.flush()
        assert event.wait(timeout=1)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
class TestListenForQuitCbreak:
    """The cbreak path only runs on a real terminal, so drive it through a pty."""

    @pytest.fixture
    def tty_listener(self, monkeypatch):
        """Start listen_for_quit() with a pty as stdin.

        Yields (event, thread, master_fd, attrs, in_cbreak, original_attrs), where
        attrs() returns the pty's current termios settings.
        """
        import termios  # POSIX only

        master_fd, slave_fd = os.openpty()

        def attrs():
            return termios.tcgetattr(slave_fd)

        def in_cbreak():
            return not attrs()[3] & termios.ICANON

        original = attrs()
        reader = os.fdopen(slave_fd, "r")
        monkeypatch.setattr(sys, "stdin", reader)
        event = threading.Event()
        thread = threading.Thread(target=img_tagger.listen_for_quit, args=(event,), daemon=True)
        thread.start()
        yield event, thread, master_fd, attrs, in_cbreak, original
        event.set()
        thread.join(timeout=3)
        reader.close()
        os.close(master_fd)

    @staticmethod
    def wait_until(predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_switches_terminal_to_cbreak(self, tty_listener):
        *_, in_cbreak, _ = tty_listener
        assert self.wait_until(in_cbreak)

    @pytest.mark.parametrize("key", ["q", "Q"])
    def test_q_sets_stop_event_and_restores_terminal(self, tty_listener, key):
        event, thread, master_fd, attrs, in_cbreak, original = tty_listener
        assert self.wait_until(in_cbreak)
        # No newline: in cbreak mode a single key must be enough.
        os.write(master_fd, b"x")
        time.sleep(0.15)
        os.write(master_fd, key.encode())
        assert event.wait(timeout=3)
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert attrs() == original

    def test_restores_terminal_when_stopped_externally(self, tty_listener):
        event, thread, _, attrs, in_cbreak, original = tty_listener
        assert self.wait_until(in_cbreak)
        event.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert attrs() == original
