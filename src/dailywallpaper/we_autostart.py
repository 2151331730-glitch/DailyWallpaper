# src/dailywallpaper/we_autostart.py
"""Turn Wallpaper Engine's own start-with-Windows off.

WE does not start from the Run key. It installs an auto-start Windows service
("Wallpaper Engine Service", running ``C:\\Windows\\System32\\wallpaperservice64.exe``)
and that is why its live wallpaper can be on the desktop *before* Explorer is
-- long before this app gets a chance to replace it. The installer offers to
take that away; the same entry point (``--disable-we-autostart``) can be run
by hand.

Removing the service needs administrator rights. Nothing else here does, so
the elevated re-launch happens only when a service is actually present, and a
declined UAC prompt is reported instead of being retried in a loop.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "Wallpaper Engine Service"
HELPER_NAME = "wallpaperservice64.exe"
# Names WE has used for its Run value over the years; neither is present on a
# machine that starts through the service, but an old install may still have
# one and it would undo the whole point.
RUN_VALUES = ("WallpaperEngine", "Wallpaper Engine")
# WE ships a 32-bit and a 64-bit engine in the same folder, and either can be
# the one running (its own setting picks the build), so both names are probed
# and both are stopped before config.json is edited.
EXE_NAMES = ("wallpaper64.exe", "wallpaper32.exe")
EXE_NAME = EXE_NAMES[0]
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


@dataclass
class WeAutostartResult:
    """What was changed, in the order it happened, plus anything refused."""

    changed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # non-Windows, or a locked-down shell
        return False


def _run(runner, *args):
    return runner(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        # pythonw has no console: without this every tasklist/sc query flashes
        # a black window on the desktop.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def service_present(runner=subprocess.run) -> bool:
    """True when WE's auto-start service is installed.

    ``sc query`` prints localised text, so only the exit status is used.
    """
    try:
        return _run(runner, "sc", "query", SERVICE_NAME).returncode == 0
    except Exception as exc:
        logger.warning(f"service query failed: {exc}")
        return False


def we_processes_running(runner=subprocess.run) -> bool:
    for name in EXE_NAMES:
        try:
            out = _run(runner, "tasklist", "/FI", f"IMAGENAME eq {name}", "/NH")
        except Exception:
            continue
        if name in (out.stdout or "").lower():
            return True
    return False


def we_install_dir(cfg=None) -> Path | None:
    """Where WE lives, via the app config or Steam's own detection."""
    install = getattr(getattr(cfg, "wallpaper_engine", None), "install_dir", "")
    if not install:
        try:
            from dailywallpaper.steam_env import detect

            found = detect().we_install_dir
            install = str(found) if found is not None else ""
        except Exception:
            install = ""
    if not install:
        return None
    path = Path(install)
    return path if path.is_dir() else None


def we_config_path(install: Path | None) -> Path | None:
    if install is None:
        return None
    path = install / "config.json"
    return path if path.is_file() else None


def _write_config_autostart(path: Path, enabled: bool) -> bool:
    """Set every autostart switch WE keeps in config.json.

    WE rewrites this file when it exits, so it is only worth doing while WE
    is not running -- the caller stops it first for exactly that reason.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"WE config unreadable: {exc}")
        return False
    user = (
        raw.get("win11", {}).get("general", {}).get("user")
        if isinstance(raw, dict)
        else None
    )
    if not isinstance(user, dict):
        return False
    touched = False
    for key in ("autostart", "autostartx64", "autostartscheduler"):
        if key in user and user[key] is not enabled:
            user[key] = enabled
            touched = True
    if touched:
        if not enabled:
            # One backup, taken before the first change: later runs must not
            # overwrite it with the already-disabled file.
            backup = path.with_name(path.name + ".before-dailywallpaper")
            try:
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        path.write_text(
            json.dumps(raw, ensure_ascii=False, indent="\t"), encoding="utf-8"
        )
    return True


def _delete_run_values(registry) -> list[str]:
    removed = []
    for name in RUN_VALUES:
        try:
            if registry.get(name) is not None:
                registry.delete(name)
                removed.append(f"注册表启动项 {name}")
        except Exception as exc:
            logger.warning(f"Run value {name} not removed: {exc}")
    return removed


MARKER_NAME = "we_autostart.json"


def _marker_path(cfg) -> Path | None:
    data_dir = getattr(getattr(cfg, "app", None), "data_dir", "")
    if not data_dir:
        return None
    try:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path / MARKER_NAME
    except OSError:
        return None


def _write_marker(cfg, config_path: Path | None, run_values: dict[str, str]) -> None:
    """Remember what was turned off so uninstall can put it back.

    Changing another program's settings is only acceptable while it is
    reversible; the uninstaller reads this file (via ``--forget``) and undoes
    both the config switches and the Run values it cannot reconstruct.
    """
    path = _marker_path(cfg)
    if path is None:
        return
    try:
        path.write_text(
            json.dumps(
                {
                    "config": None if config_path is None else str(config_path),
                    "run_values": run_values,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(f"WE autostart marker not written: {exc}")


def restore(cfg=None, *, registry=None, runner=subprocess.run) -> WeAutostartResult:
    """Undo ``disable`` when the app is uninstalled.

    The switches go back to true; WE itself recreates the background service
    the next time it starts with that setting, which the uninstaller cannot do
    without administrator rights.
    """
    result = WeAutostartResult()
    path = _marker_path(cfg)
    if path is None or not path.is_file():
        return result
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"WE autostart marker unreadable: {exc}")
        return result
    if registry is None:
        from dailywallpaper.app_autostart import _WinRegistry

        registry = _WinRegistry()
    for name, value in (marker.get("run_values") or {}).items():
        try:
            if registry.get(name) is None:
                registry.set(name, value)
                result.changed.append(f"注册表启动项 {name}")
        except Exception as exc:
            result.failed.append(f"恢复 {name} 失败：{exc}")
    config = marker.get("config")
    if config:
        target = Path(config)
        if target.is_file():
            if _write_config_autostart(target, True):
                result.changed.append("WE 配置里的开机启动开关")
        else:
            result.failed.append(f"找不到 WE 配置：{target}")
    try:
        path.unlink()
    except OSError:
        pass
    return result


def _remove_service(runner, helper_path: Path | None = None) -> tuple[list[str], list[str]]:
    changed, failed = [], []
    try:
        _run(runner, "sc", "stop", SERVICE_NAME)
    except Exception as exc:
        logger.warning(f"service stop failed: {exc}")
    try:
        result = _run(runner, "sc", "delete", SERVICE_NAME)
    except Exception as exc:
        failed.append(f"删除服务失败：{exc}")
        return changed, failed
    if result.returncode == 0:
        changed.append(f"开机服务 {SERVICE_NAME}")
    else:
        failed.append(f"删除服务失败：{(result.stderr or result.stdout or '').strip()}")
    helper = helper_path or (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / HELPER_NAME
    )
    try:
        if helper.exists():
            helper.unlink()
            changed.append(f"辅助程序 {helper.name}")
    except OSError as exc:
        # The service is gone, which is what actually matters; a leftover
        # helper is inert and Windows deletes it on the next WE update.
        logger.warning(f"{helper} not removed: {exc}")
    return changed, failed


def disable(
    cfg=None,
    *,
    runner=subprocess.run,
    registry=None,
    admin: bool | None = None,
    running=None,
    helper_path: Path | None = None,
) -> WeAutostartResult:
    """Stop WE from starting with Windows. Idempotent and best effort."""
    result = WeAutostartResult()
    admin = is_admin() if admin is None else admin
    running = we_processes_running(runner) if running is None else running
    needs_service = service_present(runner)
    if needs_service and not admin:
        result.failed.append(
            "需要管理员权限才能删除 Wallpaper Engine 的后台服务"
        )
        return result

    if registry is None:
        from dailywallpaper.app_autostart import _WinRegistry

        registry = _WinRegistry()

    install = we_install_dir(cfg)
    config_path = we_config_path(install)
    if running and config_path is not None:
        # WE holds its settings in memory and rewrites config.json on exit, so
        # an edit made while it runs is silently undone.
        try:
            for name in EXE_NAMES:
                _run(runner, "taskkill", "/IM", name, "/F")
            result.changed.append("已结束正在运行的 Wallpaper Engine")
        except Exception as exc:
            logger.warning(f"WE not stopped: {exc}")
    if config_path is not None and _write_config_autostart(config_path, False):
        result.changed.append("WE 配置里的开机启动开关")

    # The Run values are read before they are deleted: uninstall can only put
    # back what was recorded here.
    previous: dict[str, str] = {}
    for name in RUN_VALUES:
        try:
            value = registry.get(name)
        except Exception:
            value = None
        if value is not None:
            previous[name] = value
    result.changed.extend(_delete_run_values(registry))
    if previous or config_path is not None:
        _write_marker(cfg, config_path, previous)

    if needs_service:
        changed, failed = _remove_service(runner, helper_path)
        result.changed.extend(changed)
        result.failed.extend(failed)
    return result


def relaunch_elevated(argv: list[str] | None = None) -> int:
    """Run this app again with a UAC prompt and return the child's code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    exe = sys.executable
    args = " ".join(a.replace("'", "''") for a in argv)
    script = (
        f"$p = Start-Process -FilePath '{exe.replace(chr(39), chr(39) * 2)}'"
        f" -ArgumentList '{args}' -Verb RunAs -Wait -PassThru;"
        " if ($p) { exit $p.ExitCode } else { exit 1 }"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            capture_output=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        logger.warning(f"elevation failed: {exc}")
        return 1
    return completed.returncode


def disable_with_elevation(cfg=None, argv: list[str] | None = None) -> int:
    """The ``--disable-we-autostart`` entry point.

    Returns 0 when WE no longer starts with Windows (including "it already
    did not"), 1 when something was refused.
    """
    result = disable(cfg)
    if result.ok:
        for step in result.changed:
            logger.info(f"WE autostart: {step}")
        if not result.changed:
            logger.info("WE autostart: already off")
        return 0
    if any("管理员权限" in item for item in result.failed):
        logger.info("WE autostart: retrying with administrator rights")
        code = relaunch_elevated(argv)
        if code != 0:
            logger.warning("WE autostart: administrator prompt was declined")
        return code
    for item in result.failed:
        logger.warning(f"WE autostart: {item}")
    return 1
