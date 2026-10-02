"""Tests for model-output parsing: parse_model_output(), parse_text_tags(), normalize_tags()."""

import pytest

from img_tagger import normalize_tags, parse_model_output, parse_text_tags

SIX = ["photo", "sunset", "beach", "ocean", "sky", "clouds"]


# --- normalize_tags -------------------------------------------------------

class TestNormalizeTags:
    def test_strips_lowercases_and_keeps_order(self):
        assert normalize_tags(["  Photo ", "SUNSET", "Beach", "ocean", "Sky", "clouds"]) == SIX

    def test_deduplicates_case_insensitively_preserving_first_occurrence(self):
        tags = ["Photo", "photo", "sunset", "SUNSET", "beach", "ocean", "sky", "clouds"]
        assert normalize_tags(tags) == SIX

    def test_drops_empty_and_whitespace_only_tags(self):
        assert normalize_tags(["", "   ", *SIX]) == SIX

    def test_fewer_than_minimum_returns_none(self):
        assert normalize_tags(SIX[:5]) is None

    def test_duplicates_do_not_count_towards_minimum(self):
        assert normalize_tags(["a1", "a1", "b1", "c1", "d1", "e1"]) is None

    def test_exactly_minimum_is_accepted(self):
        assert normalize_tags(SIX) == SIX

    def test_capped_at_twelve(self):
        tags = [f"tag{i}" for i in range(20)]
        assert normalize_tags(tags) == tags[:12]

    def test_custom_bounds(self):
        assert normalize_tags(["a", "b", "c"], min_count=2, max_count=2) == ["a", "b"]
        assert normalize_tags(["a"], min_count=2) is None


# --- parse_text_tags ------------------------------------------------------

class TestParseTextTags:
    def test_splits_on_commas_and_newlines(self):
        assert parse_text_tags("photo, sunset\nbeach,ocean") == ["photo", "sunset", "beach", "ocean"]

    def test_strips_quotes_and_lowercases(self):
        assert parse_text_tags("\"Photo\", 'Sunset'") == ["photo", "sunset"]

    def test_skips_stop_words_and_very_short_parts(self):
        assert parse_text_tags("the, and, photo, ok, sunset, are") == ["photo", "sunset"]


# --- parse_model_output: supported output styles --------------------------

class TestParseModelOutput:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("photo, sunset, beach, ocean, sky, clouds", id="comma-separated"),
            pytest.param("Photo, Sunset, Beach, Ocean, Sky, Clouds", id="comma-separated-mixed-case"),
            pytest.param("photo,sunset,beach,ocean,sky,clouds\n", id="comma-no-spaces-trailing-newline"),
            pytest.param("'photo', \"sunset\", beach, ocean, sky, clouds", id="comma-quoted"),
            pytest.param('["photo", "sunset", "beach", "ocean", "sky", "clouds"]', id="json-array"),
            pytest.param('Sure! ["Photo","Sunset","beach","ocean","sky","clouds"] Hope it helps.', id="json-embedded"),
            pytest.param('```json\n["photo","sunset","beach","ocean","sky","clouds"]\n```', id="json-markdown-fence"),
            pytest.param("- photo\n- sunset\n- beach\n- ocean\n- sky\n- clouds", id="dash-bullets"),
            pytest.param("• photo\n• sunset\n• beach\n• ocean\n• sky\n• clouds", id="dot-bullets"),
            pytest.param("1. photo\n2. sunset\n3. beach\n4. ocean\n5. sky\n6. clouds", id="numbered"),
            pytest.param("Here are the tags: photo, sunset, beach, ocean, sky, clouds", id="intro-tags"),
            pytest.param("Keywords - photo, sunset, beach, ocean, sky, clouds", id="intro-keywords"),
        ],
    )
    def test_extracts_six_tags(self, raw):
        assert parse_model_output(raw) == SIX

    def test_json_with_non_string_items_keeps_numbers_and_drops_others(self):
        raw = '["photo", 2024, "sunset", null, {"x": 1}, "beach", "ocean", "sky"]'
        assert parse_model_output(raw) == ["photo", "2024", "sunset", "beach", "ocean", "sky"]

    def test_json_too_short_falls_through_to_failure(self):
        assert parse_model_output('["photo", "sunset"]') is None

    def test_bullets_with_comma_separated_items_are_split(self):
        raw = "- photo, sunset\n- beach\n- ocean\n- sky\n- clouds\n- sand"
        assert parse_model_output(raw) == ["photo", "sunset", "beach", "ocean", "sky", "clouds", "sand"]

    def test_output_capped_at_twelve(self):
        raw = ", ".join(f"tag{i}" for i in range(30))
        assert parse_model_output(raw) == [f"tag{i}" for i in range(12)]

    def test_duplicates_removed(self):
        assert parse_model_output("photo, Photo, PHOTO, sunset, beach, ocean, sky, clouds") == SIX

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("", id="empty"),
            pytest.param("   \n  ", id="whitespace"),
            pytest.param("photo, sunset, beach", id="too-few"),
            pytest.param("I'm sorry, I cannot analyze this image.", id="refusal"),
            pytest.param("A photo of a sunset over the ocean", id="sentence"),
        ],
    )
    def test_insufficient_output_returns_none(self, raw):
        assert parse_model_output(raw) is None


# --- Known parser weaknesses ----------------------------------------------
# These document real model-output styles that are currently mis-parsed.
# They are strict xfails: when the parser is fixed they will XPASS and fail
# the run, as a reminder to drop the xfail marker.

class TestParseModelOutputKnownIssues:
    @pytest.mark.xfail(strict=True, reason="plain newline-separated lists are not recognised")
    def test_newline_separated_list(self):
        assert parse_model_output("photo\nsunset\nbeach\nocean\nsky\nclouds") == SIX

    @pytest.mark.xfail(strict=True, reason="hyphens inside tags are treated as bullet markers")
    def test_numbered_list_with_hyphenated_tags(self):
        raw = "\n".join(f"{i}. tag-{i}" for i in range(1, 7))
        assert parse_model_output(raw) == [f"tag-{i}" for i in range(1, 7)]

    @pytest.mark.xfail(strict=True, reason="the word 'tags' early in a plain list triggers the intro-text path")
    def test_comma_list_containing_word_tags(self):
        raw = "meme, text tags, funny, cat, reaction, wholesome"
        assert parse_model_output(raw) == ["meme", "text tags", "funny", "cat", "reaction", "wholesome"]

    @pytest.mark.xfail(strict=True, reason="parse_text_tags drops tags of 2 chars or fewer (e.g. '3d', 'ui')")
    def test_short_tags_after_intro(self):
        raw = "Tags: photo, 3d, ui, beach, ocean, sky"
        assert parse_model_output(raw) == ["photo", "3d", "ui", "beach", "ocean", "sky"]

    @pytest.mark.xfail(strict=True, reason="<think> reasoning blocks from thinking models are not stripped")
    def test_reasoning_block_is_ignored(self):
        raw = "<think>The image shows a cat.</think>\nmeme, cat, funny, reaction, wholesome, pet"
        assert parse_model_output(raw) == ["meme", "cat", "funny", "reaction", "wholesome", "pet"]
