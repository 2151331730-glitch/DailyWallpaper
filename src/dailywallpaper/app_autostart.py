# src/dailywallpaper/app_autostart.py
"""Start-with-Windows via the per-user Run key (no admin rights needed)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from dailywallpaper import identity

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Both are named after the build, not the product as a whole: the uninstaller
# deletes the Run value by name, so a shared name meant uninstalling one build
# switched autostart off for the other.
VALUE_NAME = identity.name()
TASK_NAME = identity.name()

logger = logging.getLogger(__name__)


def default_command(config_path=None) -> str:
    """Prefer pythonw.exe so startup does not flash a console window.

    The Run key starts the process with an unrelated working directory, so
    the config path must be absolute or the app would look for ./config.toml.
    """
    if config_path is None:
        from dailywallpaper.config import default_config_path

        config_path = default_config_path()
    if getattr(sys, "frozen", False):
        # An installed build is its own launcher; there is no -m module.
        command = f'"{Path(sys.executable)}"'
    else:
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        launcher = pythonw if pythonw.exists() else executable
        # Start the tray GUI: the wallpaper must be visible at login, and the
        # tray is what the user interacts with. The single-instance lock keeps
        # a second launch from starting a second scheduler.
        command = f'"{launcher}" -m dailywallpaper'
    if config_path is not None:
        command += f' --config "{Path(config_path).absolute()}"'
    return command


class _WinRegistry:
    def __init__(self):
        import winreg

        self.winreg = winreg

    def get(self, name):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return self.winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name, value) -> None:
        with self.winreg.OpenKey(
            self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_SET_VALUE
        ) as key:
            self.winreg.SetValueEx(key, name, 0, self.winreg.REG_SZ, value)

    def delete(self, name) -> None:
        try:
            with self.winreg.OpenKey(
                self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_SET_VALUE
            ) as key:
                self.winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class AutostartService:
    def __init__(self, registry=None, command=None):
        self.registry = registry if registry is not None else _WinRegistry()
        self.command = command or default_command()

    def enabled(self) -> bool:
        return bool(self.registry.get(VALUE_NAME))

    def sync(self, wanted: bool) -> None:
        """Make the Run key match the config; safe to call on every launch.

        The settings page is not the only thing that can change the config --
        a lost Run value (cleanup tool, deleted profile entry) otherwise leaves
        the toggle ticked while nothing starts at login. A stale command (moved
        venv, config path) is repaired the same way.
        """
        if not wanted:
            self.disable()
        elif self.registry.get(VALUE_NAME) != self.command:
            self.enable()

    def enable(self) -> None:
        self.registry.set(VALUE_NAME, self.command)

    def disable(self) -> None:
        self.registry.delete(VALUE_NAME)


# -- logon task -----------------------------------------------------------
# Windows starts Run-key entries a few seconds after the shell is up, and on
# a machine with ten other login items that delay measured ~30 s: the boot
# then shows yesterday's wallpaper for half a minute. A logon task is fired
# by the Task Scheduler with the session instead of behind Explorer's queue.
# The Run key stays as the fallback; the single-instance mutex makes whichever
# copy loses the race exit before it touches anything.


def _schtasks(*args: str, text: bool = True):
    import subprocess

    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _powershell(script: str):
    """Run a script through PowerShell.

    ``schtasks /SC ONLOGON`` needs elevation; the ScheduledTasks cmdlets
    register the same trigger for the current user without it.
    """
    import subprocess

    return subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", script,
        ],
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def split_command(command: str) -> tuple[str, str]:
    """Split ``"exe" args`` into the two halves the task action wants."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end], command[end + 1:].strip()
    exe, _, args = command.partition(" ")
    return exe, args.strip()


def _quote(text: str) -> str:
    """A single-quoted PowerShell literal."""
    return text.replace("'", "''")


def logon_task_exists(name: str = TASK_NAME) -> bool:
    try:
        return _schtasks("/Query", "/TN", name).returncode == 0
    except Exception as exc:  # missing schtasks, or a hung one
        logger.warning(f"logon task query failed: {exc}")
        return False


_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def logon_task_action(name: str = TASK_NAME) -> tuple[str, str] | None:
    """The (command, arguments) the task actually runs, or None.

    The task XML is used rather than the human-readable listing: field names
    there are localised, ``<Command>``/``<Arguments>`` are not.
    """
    try:
        result = _schtasks("/Query", "/TN", name, "/XML", text=False)
    except Exception as exc:
        logger.warning(f"logon task read failed: {exc}")
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    raw = result.stdout or b""
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, AttributeError):
            continue
    else:
        return None
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    command = root.find(f".//{_TASK_NS}Actions/{_TASK_NS}Exec/{_TASK_NS}Command")
    arguments = root.find(f".//{_TASK_NS}Actions/{_TASK_NS}Exec/{_TASK_NS}Arguments")
    if command is None or not command.text:
        return None
    return command.text.strip(), (arguments.text or "").strip() if arguments is not None else ""


def _same_action(current: tuple[str, str] | None, wanted: tuple[str, str]) -> bool:
    if current is None:
        return False
    current_exe, current_args = current
    wanted_exe, wanted_args = wanted
    return (
        current_exe.strip('"').lower() == wanted_exe.strip('"').lower()
        and current_args.strip() == wanted_args.strip()
    )


def ensure_logon_task(command: str | None = None, name: str = TASK_NAME) -> bool:
    """Create the at-logon task, and keep its command current.

    Existence is not enough: a moved venv or a new config path would leave a
    task that starts a stale copy, and that copy would win the single-instance
    mutex and run on the old data directory. The registered action is compared
    with what this launch would use and the task is re-registered when they
    differ.
    """
    wanted = split_command(command or default_command())
    current = logon_task_action(name)
    if _same_action(current, wanted):
        return True
    if current is None and logon_task_exists(name):
        # The task exists but its XML was unreadable: leave it alone rather
        # than re-registering on every launch.
        return True
    exe, arguments = wanted
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        f"  $a = New-ScheduledTaskAction -Execute '{_quote(exe)}'"
        f" -Argument '{_quote(arguments)}'"
        f" -WorkingDirectory '{_quote(str(Path(exe).parent))}'\n"
        "  $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME\n"
        f"  Register-ScheduledTask -TaskName '{_quote(name)}'"
        " -Action $a -Trigger $t -Force | Out-Null\n"
        "} catch { exit 1 }\n"
    )
    try:
        result = _powershell(script)
    except Exception as exc:  # no PowerShell: the Run key still works
        logger.warning(f"logon task not created: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        logger.warning(f"logon task not created: {detail[:300]}")
        return False
    logger.info("logon task created" if current is None else "logon task updated")
    return True


def remove_logon_task(name: str = TASK_NAME) -> None:
    if not logon_task_exists(name):
        return
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        f"  Unregister-ScheduledTask -TaskName '{_quote(name)}' -Confirm:$false\n"
        "} catch { exit 1 }\n"
    )
    try:
        result = _powershell(script)
    except Exception as exc:
        logger.warning(f"logon task not removed: {exc}")
        return
    if result.returncode != 0:
        logger.warning(f"logon task not removed: exit {result.returncode}")
