# tests/test_vision.py
import json
from pathlib import Path
import pytest
from dailywallpaper.analysis.vision import validate_scores, VisionError

def load(name):
    return json.loads(Path(f"tests/fixtures/{name}").read_text(encoding="utf-8"))

def test_valid_scores():
    s = validate_scores(load("vision_ok.json"))
    assert s.quality == 82
    assert s.style_tags[0] == "landscape"

def test_empty_style_tags_rejected():
    with pytest.raises(VisionError):
        validate_scores(load("vision_bad.json"))

def test_out_of_range_rejected():
    bad = load("vision_ok.json")
    bad["quality"] = 101
    with pytest.raises(VisionError):
        validate_scores(bad)

def test_missing_key_rejected():
    bad = load("vision_ok.json")
    del bad["reason"]
    with pytest.raises(VisionError):
        validate_scores(bad)

def test_bool_score_rejected():
    bad = load("vision_ok.json")
    bad["quality"] = True
    with pytest.raises(VisionError):
        validate_scores(bad)

def test_bad_palette_rejected():
    bad = load("vision_ok.json")
    bad["palette"] = ["red"]
    with pytest.raises(VisionError):
        validate_scores(bad)

def test_bad_text_overlay_rejected():
    bad = load("vision_ok.json")
    bad["text_overlay"] = "huge"
    with pytest.raises(VisionError):
        validate_scores(bad)

def test_unhashable_text_overlay_rejected():
    bad = load("vision_ok.json")
    bad["text_overlay"] = ["none"]
    with pytest.raises(VisionError):
        validate_scores(bad)

def test_non_string_palette_entry_rejected():
    bad = load("vision_ok.json")
    bad["palette"] = ["#223344", None]
    with pytest.raises(VisionError):
        validate_scores(bad)


class _FakeMessage:
    def __init__(self, content):
        self.content = content

class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)

class _FakeUsage:
    def __init__(self, prompt_tokens=123, completion_tokens=45):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

_DEFAULT = object()

class _FakeResponse:
    def __init__(self, choices, usage=_DEFAULT):
        self.choices = choices
        self.usage = _FakeUsage() if usage is _DEFAULT else usage

class _FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.last = None

    def create(self, **kwargs):
        self.last = kwargs
        return self.response

class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)

class _FakeClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)


def _ok_content():
    return json.dumps(load("vision_ok.json"))

def _provider_with(response):
    from dailywallpaper.analysis.vision import ZhipuVision

    provider = object.__new__(ZhipuVision)
    provider.client = _FakeClient(response)
    return provider

def test_zhipu_analyze_with_fake_client(tmp_path):
    from dailywallpaper.analysis.vision import ZhipuVision

    image = tmp_path / "preview.jpg"
    image.write_bytes(b"fake-image-bytes")
    client = _FakeClient(_FakeResponse([_FakeChoice(_ok_content())]))
    provider = object.__new__(ZhipuVision)
    provider.client = client

    result = provider.analyze(image, "glm-test")

    assert result.scores.quality == 82
    assert result.tokens_in == 123
    assert result.tokens_out == 45
    assert result.latency_ms >= 0
    sent = client.chat.completions.last
    assert sent["model"] == "glm-test"
    assert sent["messages"][1]["content"][0]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )

def test_zhipu_invalid_json_raises_vision_error(tmp_path):
    image = tmp_path / "preview.jpg"
    image.write_bytes(b"fake-image-bytes")
    provider = _provider_with(_FakeResponse([_FakeChoice("{not json")]))

    with pytest.raises(VisionError):
        provider.analyze(image, "glm-test")

def test_zhipu_empty_choices_raises_vision_error(tmp_path):
    image = tmp_path / "preview.jpg"
    image.write_bytes(b"fake-image-bytes")
    provider = _provider_with(_FakeResponse([]))

    with pytest.raises(VisionError):
        provider.analyze(image, "glm-test")

def test_zhipu_missing_usage_returns_zero_tokens(tmp_path):
    image = tmp_path / "preview.jpg"
    image.write_bytes(b"fake-image-bytes")
    provider = _provider_with(
        _FakeResponse([_FakeChoice(_ok_content())], usage=None)
    )

    result = provider.analyze(image, "glm-test")

    assert result.scores.quality == 82
    assert result.tokens_in == 0
    assert result.tokens_out == 0
