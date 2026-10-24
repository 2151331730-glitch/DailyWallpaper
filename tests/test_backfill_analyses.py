# tests/test_backfill_analyses.py
import importlib.util
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "backfill_analyses", Path("scripts/backfill_analyses.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(**overrides):
    row = {"wallpaper_id": 1, "rating": 8, "source": "wallhaven",
           "media_type": "image", "local_path": None,
           "preview_url": None, "file_url": None}
    row.update(overrides)
    return row


def test_a_local_image_is_scored_as_is(tmp_path):
    picture = tmp_path / "apply.jpg"
    picture.write_bytes(b"x")
    kind, value = _module().plan_source(_row(local_path=str(picture)))
    assert kind == "local"
    assert value == picture


def test_a_gif_keeps_its_own_shape(tmp_path):
    clip = tmp_path / "loop.gif"
    clip.write_bytes(b"x")
    kind, value = _module().plan_source(_row(local_path=str(clip)))
    assert kind == "gif"
    assert value == clip


def test_a_video_on_disk_wins_over_the_poster(tmp_path):
    # The app scores a contact sheet during a real run; a still that still has
    # its video file must not be measured as a poster instead.
    video = tmp_path / "scene.mp4"
    video.write_bytes(b"x")
    kind, value = _module().plan_source(
        _row(media_type="video", file_url=str(video),
             preview_url="https://example.invalid/cover.jpg")
    )
    assert kind == "video"
    assert value == video


def test_a_deleted_local_file_falls_back_to_the_preview(tmp_path):
    kind, value = _module().plan_source(
        _row(local_path=str(tmp_path / "gone.jpg"),
             preview_url="https://th.wallhaven.cc/lg/gw/gw16m3.jpg")
    )
    assert kind == "remote"
    assert value.startswith("https://")


def test_a_missing_local_path_without_preview_is_reported_not_guessed():
    kind, reason = _module().plan_source(_row())
    assert kind is None
    assert reason


def test_scene_packages_are_never_read_as_video(tmp_path):
    # A .pkg is an archive, not a clip: ffmpeg would fail on it.
    package = tmp_path / "scene.pkg"
    package.write_bytes(b"x")
    kind, value = _module().plan_source(
        _row(media_type="scene", file_url=str(package),
             preview_url="https://example.invalid/cover.jpg")
    )
    assert kind == "remote"
