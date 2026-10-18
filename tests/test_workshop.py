# tests/test_workshop.py
import json

from dailywallpaper.sources.workshop import WorkshopSource


def _project(root, pid, **over):
    folder = root / pid
    folder.mkdir(parents=True)
    data = {"title": f"t{pid}", "type": "scene", "file": "scene.pkg", "preview": "preview.jpg"}
    data.update(over)
    (folder / "project.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "scene.pkg").write_bytes(b"x")
    (folder / "preview.jpg").write_bytes(b"x")
    return folder


def test_scan_maps_projects_to_candidates(tmp_path):
    _project(tmp_path, "111")

    items = WorkshopSource(tmp_path).scan()

    assert items[0].source == "we_workshop"
    assert items[0].source_id == "111"
    assert items[0].media_type == "scene"
    assert items[0].preview_url.endswith("preview.jpg")


def test_scan_skips_projects_without_preview(tmp_path):
    folder = _project(tmp_path, "111")
    (folder / "preview.jpg").unlink()

    assert WorkshopSource(tmp_path).scan() == []


def test_scan_skips_unreadable_project_json(tmp_path):
    folder = tmp_path / "222"
    folder.mkdir()
    (folder / "project.json").write_text("{not json", encoding="utf-8")

    assert WorkshopSource(tmp_path).scan() == []


def test_mature_content_is_skipped_unless_allowed(tmp_path):
    _project(tmp_path, "111", contentrating="Mature")

    assert WorkshopSource(tmp_path).scan() == []
    assert len(WorkshopSource(tmp_path, allow_mature=True).scan()) == 1


def test_video_projects_are_typed_as_video(tmp_path):
    _project(tmp_path, "333", type="video", file="clip.mp4")

    assert WorkshopSource(tmp_path).scan()[0].media_type == "video"


def test_missing_root_returns_empty(tmp_path):
    assert WorkshopSource(tmp_path / "nope").scan() == []


def test_fetch_is_the_pipeline_interface(tmp_path):
    _project(tmp_path, "111")

    assert len(WorkshopSource(tmp_path).fetch(10)) == 1


def test_fetch_respects_the_limit(tmp_path):
    for pid in ("1", "2", "3"):
        _project(tmp_path, pid)

    assert len(WorkshopSource(tmp_path).fetch(2)) == 2
