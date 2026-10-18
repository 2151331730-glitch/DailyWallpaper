# tests/test_steamcmd.py
from pathlib import Path
from types import SimpleNamespace

import pytest

from dailywallpaper.steamcmd import SteamCmd, SteamCmdError

SUCCESS = "Success. Downloaded item 123 to \"D:/x/steamapps/workshop/content/431960/123\" (1 bytes)"


class FakeRunner:
    def __init__(self, returncode=0, stdout=SUCCESS, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def _downloaded(install_dir: Path, file_id="123"):
    folder = install_dir / "steamapps" / "workshop" / "content" / "431960" / file_id
    folder.mkdir(parents=True)
    project = folder / "project.json"
    project.write_text("{}", encoding="utf-8")
    return project


def test_download_command_targets_the_item(tmp_path):
    runner = FakeRunner()
    _downloaded(tmp_path)
    cmd = SteamCmd(Path("steamcmd.exe"), runner=runner)

    project = cmd.download_item("me", "123", tmp_path)

    assert project == tmp_path / "steamapps" / "workshop" / "content" / "431960" / "123" / "project.json"
    command = runner.calls[0]["command"]
    assert command[0] == "steamcmd.exe"
    assert "+force_install_dir" in command
    assert "+workshop_download_item" in command
    assert "431960" in command and "123" in command


def test_a_failed_download_raises(tmp_path):
    runner = FakeRunner(returncode=8, stdout="ERROR! Download item 123 failed (Failure).")
    cmd = SteamCmd(Path("steamcmd.exe"), runner=runner)

    with pytest.raises(SteamCmdError, match="123"):
        cmd.download_item("me", "123", tmp_path)


def test_success_without_files_raises(tmp_path):
    runner = FakeRunner()
    cmd = SteamCmd(Path("steamcmd.exe"), runner=runner)

    with pytest.raises(SteamCmdError, match="project.json"):
        cmd.download_item("me", "123", tmp_path)


def test_an_alternate_layout_is_accepted(tmp_path):
    folder = tmp_path / "steamapps" / "content" / "431960" / "123"
    folder.mkdir(parents=True)
    (folder / "project.json").write_text("{}", encoding="utf-8")
    runner = FakeRunner()
    cmd = SteamCmd(Path("steamcmd.exe"), runner=runner)

    project = cmd.download_item("me", "123", tmp_path)

    assert project == folder / "project.json"
