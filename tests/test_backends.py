"""Tests for the Ollama and LM Studio request helpers, using fake clients (no server needed)."""

import base64

import pytest
from conftest import FakeOllamaClient, FakeOpenAIClient, _Obj, make_gif, make_image
from PIL import Image

import img_tagger


class TestGetTagsOllama:
    def test_sends_prompt_and_image_path(self, tmp_path):
        image = make_image(tmp_path / "a.jpg")
        client = FakeOllamaClient()
        client.reply = "a, b, c"

        result = img_tagger.get_tags_ollama(client, "qwen3-vl:8b", image, "describe it")

        assert result == "a, b, c"
        assert client.calls == [
            {
                "model": "qwen3-vl:8b",
                "messages": [{"role": "user", "content": "describe it", "images": [str(image)]}],
            }
        ]

    def test_accepts_dict_style_response(self, tmp_path):
        class DictClient:
            def chat(self, model, messages):
                return {"message": {"content": "from a dict"}}

        image = make_image(tmp_path / "a.jpg")
        assert img_tagger.get_tags_ollama(DictClient(), "m", image, "p") == "from a dict"


class TestGetTagsLmStudio:
    @pytest.mark.parametrize(
        "name, mime",
        [("a.jpg", "image/jpeg"), ("a.png", "image/png"), ("a.webp", "image/webp")],
    )
    def test_sends_base64_data_url_with_mime_type(self, tmp_path, name, mime):
        image = make_image(tmp_path / name)
        client = FakeOpenAIClient()

        img_tagger.get_tags_lm_studio(client, "some-model", image, "describe it")

        call = client.calls[0]
        assert call["model"] == "some-model"
        text_part, image_part = call["messages"][0]["content"]
        assert text_part == {"type": "text", "text": "describe it"}
        assert image_part["type"] == "image_url"
        url = image_part["image_url"]["url"]
        prefix = f"data:{mime};base64,"
        assert url.startswith(prefix)
        assert base64.b64decode(url[len(prefix):]) == image.read_bytes()

    def test_gif_mime_type(self, tmp_path):
        image = make_gif(tmp_path / "a.gif")
        client = FakeOpenAIClient()
        img_tagger.get_tags_lm_studio(client, "m", image, "p")
        assert client.calls[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/gif;base64,")

    def test_mime_type_follows_content_not_extension(self, tmp_path):
        image = tmp_path / "really_png.jpg"
        Image.new("RGB", (4, 4)).save(image, format="PNG")
        client = FakeOpenAIClient()
        img_tagger.get_tags_lm_studio(client, "m", image, "p")
        assert client.calls[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_unknown_format_falls_back_to_jpeg(self, tmp_path):
        bogus = tmp_path / "bogus.png"
        bogus.write_bytes(b"not an image")
        client = FakeOpenAIClient()
        img_tagger.get_tags_lm_studio(client, "m", bogus, "p")
        assert client.calls[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_returns_message_content(self, tmp_path):
        image = make_image(tmp_path / "a.png")
        client = FakeOpenAIClient()
        client.chat = _Obj(completions=_Obj(create=lambda **kw: _Obj(choices=[_Obj(message=_Obj(content="x, y"))])))
        assert img_tagger.get_tags_lm_studio(client, "m", image, "p") == "x, y"
