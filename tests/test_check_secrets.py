# tests/test_check_secrets.py
import importlib.util
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "check_secrets", Path("scripts/check_secrets.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_real_looking_key_is_reported(tmp_path):
    source = tmp_path / "leak.py"
    source.write_text('KEY = "sk-abcdefghijklmnopqrstuvwx"\n', encoding="utf-8")

    assert _module().scan([source])


def test_a_placeholder_is_ignored(tmp_path):
    source = tmp_path / "ok.py"
    source.write_text('KEY = "your-key-here-please-replace"\n', encoding="utf-8")

    assert _module().scan([source]) == []


def test_tests_and_docs_are_skipped(tmp_path):
    folder = tmp_path / "tests"
    folder.mkdir()
    source = folder / "sample.py"
    source.write_text('KEY = "sk-abcdefghijklmnopqrstuvwx"\n', encoding="utf-8")

    assert _module().scan([source]) == []


def test_the_real_repository_is_clean():
    module = _module()
    assert module.scan(module.tracked_files()) == []


def test_a_key_on_a_line_containing_angle_brackets_is_still_reported(tmp_path):
    source = tmp_path / "page.html"
    source.write_text('<meta content="sk-abcdefghijklmnopqrstuvwx">\n', encoding="utf-8")

    assert _module().scan([source])
