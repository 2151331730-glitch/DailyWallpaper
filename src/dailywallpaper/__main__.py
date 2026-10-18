# src/dailywallpaper/__main__.py
"""Command-line entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from dailywallpaper.app import SingleInstance
from dailywallpaper.config import default_config_path, load_config
from dailywallpaper.logging_setup import setup_logging


def main(argv=None, app_factory=None, run_gui=None) -> int:
    parser = argparse.ArgumentParser(prog="dailywallpaper")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the scheduler without the desktop shell",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.app.data_dir, cfg.app.log_level)

    if app_factory is None:
        # The composition root lands in Task 1.11; tests inject a fake factory.
        from dailywallpaper.app import build_application

        app_factory = build_application

    with SingleInstance("DailyWallpaper"):
        application = app_factory(cfg)
        if args.once:
            return application.run_once(dry_run=args.dry_run)
        if args.headless:
            return application.run_forever()
        if run_gui is None:
            from dailywallpaper.ui.shell import build_gui

            run_gui = build_gui
        return run_gui(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
