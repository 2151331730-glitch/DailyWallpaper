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


class _FakeMessage:
    content = json.dumps(load("vision_ok.json"))

class _FakeChoice:
    message = _FakeMessage()

class _FakeUsage:
    prompt_tokens = 123
    completion_tokens = 45

class _FakeResponse:
    choices = [_FakeChoice()]
    usage = _FakeUsage()

class _FakeCompletions:
    def __init__(self):
        self.last = None

    def create(self, **kwargs):
        self.last = kwargs
        return _FakeResponse()

class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()

class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()

def test_zhipu_analyze_with_fake_client(tmp_path):
    from dailywallpaper.analysis.vision import ZhipuVision

    image = tmp_path / "preview.jpg"
    image.write_bytes(b"fake-image-bytes")
    provider = object.__new__(ZhipuVision)
    client = _FakeClient()
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
