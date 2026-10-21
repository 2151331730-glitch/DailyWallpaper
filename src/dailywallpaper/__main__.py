# src/dailywallpaper/__main__.py
"""Command-line entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from dailywallpaper.app import AlreadyRunning, SingleInstance
from dailywallpaper.config import default_config_path, load_config
from dailywallpaper.logging_setup import setup_logging


INSTANCE_NAME = "DailyWallpaper"


def main(argv=None, app_factory=None, run_gui=None, instance_name=None) -> int:
    parser = argparse.ArgumentParser(prog="dailywallpaper")
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
    args = parser.parse_args(argv)
    if args.dry_run:
        # A dry run that is silently ignored (GUI) or that loops real applies
        # (--headless) is worse than useless; make it exactly one safe tick.
        args.once = True

    cfg = load_config(args.config)
    setup_logging(
        cfg.app.data_dir,
        cfg.app.log_level,
        max_mb=cfg.retention.log_mb,
        backups=cfg.retention.log_backups,
    )

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


if __name__ == "__main__":
    raise SystemExit(main())
