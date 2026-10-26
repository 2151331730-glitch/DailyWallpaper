# tests/test_we_paths.py

from dailywallpaper.apply import we_paths


def _no_short_names(monkeypatch):
    """8.3 names are a volume setting; the tests must not depend on this one."""
    monkeypatch.setattr(we_paths, "_short_path", lambda path: None)


def test_an_ascii_path_is_passed_through(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    assert we_paths.ascii_path(video, tmp_path / "aliases") == video


def test_a_chinese_video_gets_an_ascii_link(tmp_path, monkeypatch):
    _no_short_names(monkeypatch)
    source = tmp_path / "1781466079" / "合成 1_1.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")

    alias = we_paths.ascii_path(source, tmp_path / "aliases")

    assert alias is not None
    assert str(alias).isascii()
    assert alias.read_bytes() == b"video"
    # Stable across calls, or WE's saved selection stops working next boot.
    assert we_paths.ascii_path(source, tmp_path / "aliases") == alias


def test_a_chinese_manifest_is_not_aliased(tmp_path, monkeypatch):
    # project.json resolves its real asset relative to itself, so a lone link
    # would point WE at an empty folder. Refusing is the honest answer.
    _no_short_names(monkeypatch)
    source = tmp_path / "中文工程" / "project.json"
    source.parent.mkdir()
    source.write_bytes(b"{}")

    assert we_paths.ascii_path(source, tmp_path / "aliases") is None


def test_no_alias_root_means_no_alias(tmp_path, monkeypatch):
    _no_short_names(monkeypatch)
    source = tmp_path / "1781466079" / "合成 1_1.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")

    assert we_paths.ascii_path(source, None) is None


def test_prune_drops_vanished_sources_but_never_the_one_we_use(tmp_path, monkeypatch):
    _no_short_names(monkeypatch)
    root = tmp_path / "aliases"
    live = tmp_path / "live" / "合成.mp4"
    live.parent.mkdir()
    live.write_bytes(b"a")
    gone = tmp_path / "gone" / "合成.mp4"
    gone.parent.mkdir()
    gone.write_bytes(b"b")

    we_paths.ascii_path(live, root)
    gone_alias = we_paths.ascii_path(gone, root)
    gone.unlink()

    we_paths.prune(root, keep=str(gone_alias))
    assert gone_alias.parent.is_dir(), "WE is still pointing at this alias"

    we_paths.prune(root)
    assert not gone_alias.parent.exists()


def test_a_volume_without_short_names_still_falls_back_to_the_alias(tmp_path, monkeypatch):
    # GetShortPathNameW answers with the long path when 8.3 is off; that is
    # "no answer", not an answer, and the alias has to take over.
    _no_short_names(monkeypatch)
    source = tmp_path / "1781466079" / "合成 1_1.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")

    alias = we_paths.ascii_path(source, tmp_path / "aliases")

    assert alias is not None and str(alias).isascii()
