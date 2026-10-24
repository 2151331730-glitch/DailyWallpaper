# src/dailywallpaper/__main__.py
"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dailywallpaper.app import AlreadyRunning, SingleInstance
from dailywallpaper.config import default_config_path, load_config
from dailywallpaper.logging_setup import setup_logging
from dailywallpaper.steam_env import apply_to_config


INSTANCE_NAME = "DailyWallpaper"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("dailywallpaper")
    except Exception:  # a frozen build may not ship the metadata
        return "unknown"


def main(argv=None, app_factory=None, run_gui=None, instance_name=None) -> int:
    parser = argparse.ArgumentParser(prog="dailywallpaper")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_version()}"
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the scheduler without the desktop shell",
    )
    parser.add_argument(
        "--curate",
        type=int,
        nargs="?",
        const=300,
        default=None,
        metavar="N",
        help="open the curation wall: rate N candidates and build a seed",
    )
    parser.add_argument(
        "--forget",
        action="store_true",
        help=argparse.SUPPRESS,  # the uninstaller uses it
    )
    parser.add_argument(
        "--disable-we-autostart",
        action="store_true",
        help="关闭 Wallpaper Engine 的开机自启动后退出（安装程序会调用它）",
    )
    args = parser.parse_args(argv)
    if args.forget:
        # Uninstall must not leave the Zhipu/Steam keys in the Windows
        # credential manager, where a reinstall would silently reuse them.
        import keyring

        from dailywallpaper.analysis.vision import (
            KEYRING_ACCOUNT,
            KEYRING_SERVICE,
        )

        for account in (KEYRING_ACCOUNT, "steam_api_key"):
            try:
                keyring.delete_password(KEYRING_SERVICE, account)
            except Exception:  # missing entry, or a locked credential store
                pass
        # Windows must not keep trying to start what is gone: the installer
        # removes the Run key itself, the logon task is ours to remove. And
        # giving Wallpaper Engine its own switches back is the other half of
        # "this uninstalls cleanly" -- the installer took them away.
        try:
            from dailywallpaper.app_autostart import remove_logon_task

            remove_logon_task()
        except Exception:
            pass
        try:
            from dailywallpaper.we_autostart import restore

            restore(load_config(default_config_path()))
        except Exception:
            pass
        return 0
    if args.dry_run:
        # A dry run that is silently ignored (GUI) or that loops real applies
        # (--headless) is worse than useless; make it exactly one safe tick.
        args.once = True

    cfg = load_config(args.config)
    # A new install has no wallpaper_engine path yet; find Steam's own copy
    # of Wallpaper Engine before anything reads the config.
    cfg = apply_to_config(cfg)
    if getattr(sys, "frozen", False):
        # The bundled ffmpeg is not on PATH; point the config at it.
        from dataclasses import replace as _replace

        from dailywallpaper.resources import resource

        bundled_ffmpeg = resource("ffmpeg", "ffmpeg.exe")
        if bundled_ffmpeg.is_file():
            cfg = _replace(
                cfg,
                tools=_replace(cfg.tools, ffmpeg_path=str(bundled_ffmpeg)),
            )
    setup_logging(
        cfg.app.data_dir,
        cfg.app.log_level,
        max_mb=cfg.retention.log_mb,
        backups=cfg.retention.log_backups,
    )

    if args.disable_we_autostart:
        # Before the single-instance lock: this runs as its own short-lived
        # process (the installer calls it) and may re-launch itself elevated,
        # which would otherwise be refused as a second instance.
        from dailywallpaper.we_autostart import disable_with_elevation

        rerun = ["--disable-we-autostart"]
        if args.config:
            rerun += ["--config", str(args.config)]
        return disable_with_elevation(cfg, rerun)

    if app_factory is None:
        # The composition root lands in Task 1.11; tests inject a fake factory.
        from dailywallpaper.app import build_application

        app_factory = build_application

    def run_inside():
        # Build the application only for the modes that use it: the GUI shell
        # builds its own (and the worker builds a third in its own thread).
        if args.once or args.headless:
            application = app_factory(cfg)
            if args.once:
                return application.run_once(dry_run=args.dry_run)
            return application.run_forever()
        gui = run_gui
        if gui is None:
            from dailywallpaper.ui.shell import build_gui

            gui = build_gui
        return gui(cfg, args.config)

    if args.curate:
        # Curation is a separate window and must open while the resident
        # scheduler is already running, so it skips the single-instance lock.
        from dailywallpaper.ui.curation_window import run_curation

        return run_curation(cfg, args.config, count=args.curate)

    try:
        with SingleInstance(instance_name or INSTANCE_NAME):
            return run_inside()
    except AlreadyRunning:
        print("另一个 DailyWallpaper 实例已经在运行，本次启动已退出。")
        return 1
    except RuntimeError as exc:
        # The headless path has no first-run dialog; say what is missing
        # instead of dying with a traceback in a console nobody watches.
        print(f"启动失败：{exc}")
        print("提示：先运行 scripts/set_key_gui.py 或 scripts/set_key.py 填入智谱 key。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
