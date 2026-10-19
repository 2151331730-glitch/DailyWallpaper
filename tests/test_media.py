# tests/test_media.py
import subprocess

import pytest
from PIL import Image

from dailywallpaper.analysis.media import MediaError, gif_first_frame, video_contact_sheet


def test_gif_first_frame_writes_a_jpeg(tmp_path):
    gif = tmp_path / "a.gif"
    frames = [Image.new("RGB", (64, 48), colour) for colour in ("red", "blue")]
    frames[0].save(gif, save_all=True, append_images=frames[1:])
    dest = tmp_path / "out" / "first.jpg"

    gif_first_frame(gif, dest)

    with Image.open(dest) as im:
        assert im.size == (64, 48)


def test_video_contact_sheet_calls_ffmpeg_with_a_tile_filter(tmp_path):
    calls = []

    class _Result:
        returncode = 0
        stderr = ""

    def runner(args, **kwargs):
        calls.append(args)
        (tmp_path / "sheet.jpg").write_bytes(b"x")
        return _Result()

    import dailywallpaper.analysis.media as media

    original = media.subprocess.run
    media.subprocess.run = runner
    try:
        the_path = video_contact_sheet(tmp_path / "clip.mp4", tmp_path / "sheet.jpg")
    finally:
        media.subprocess.run = original

    assert the_path.exists()
    assert "-vf" in calls[0]
    assert any("tile=3x1" in arg for arg in calls[0])


def test_video_contact_sheet_reuses_an_existing_sheet(tmp_path):
    import os

    import dailywallpaper.analysis.media as media

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    sheet = tmp_path / "sheet.jpg"
    sheet.write_bytes(b"cached")
    # The sheet is newer than the video, so ffmpeg must not run again.
    now = sheet.stat().st_mtime
    os.utime(video, (now - 60, now - 60))

    def explode(*args, **kwargs):
        raise AssertionError("ffmpeg should not run for a fresh sheet")

    original = media.subprocess.run
    media.subprocess.run = explode
    try:
        assert video_contact_sheet(video, sheet) == sheet
    finally:
        media.subprocess.run = original
    assert sheet.read_bytes() == b"cached"


def test_video_contact_sheet_raises_on_ffmpeg_failure(tmp_path):
    class _Result:
        returncode = 1
        stderr = "boom"

    import dailywallpaper.analysis.media as media

    original = media.subprocess.run
    media.subprocess.run = lambda args, **kwargs: _Result()
    try:
        with pytest.raises(MediaError):
            video_contact_sheet(tmp_path / "clip.mp4", tmp_path / "sheet.jpg")
    finally:
        media.subprocess.run = original
