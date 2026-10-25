# src/dailywallpaper/config.py
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class AppSection:
    data_dir: str = "data"
    daily_time: str = "08:30"
    latest_change_time: str = "22:00"
    timezone: str = "Asia/Shanghai"
    start_with_windows: bool = False
    manual_change_counts_toward_daily_limit: bool = True
    allow_manual_override_after_daily_change: bool = False
    max_analyses_per_day: int = 40
    max_downloads_per_day: int = 60
    log_level: str = "INFO"


@dataclass(frozen=True)
class SourcesSection:
    wallhaven_enabled: bool = True
    bing_enabled: bool = True
    we_workshop_enabled: bool = False
    allow_mature: bool = False
    wallhaven_categories: str = "111"
    min_width: int = 2560
    min_height: int = 1440
    aspect_min: float = 1.70
    aspect_max: float = 1.85
    max_image_mb: int = 40
    max_video_mb: int = 500
    wallhaven_pages: int = 6
    connect_timeout: int = 5
    # Explicit proxy for Wallhaven / Steam / downloads. Empty means "use the
    # environment", which on Windows ignores Pigeon/Clash's system proxy.
    proxy: str = ""


@dataclass(frozen=True)
class VisionPricing:
    input_cny_per_million: float
    output_cny_per_million: float


@dataclass(frozen=True)
class VisionSection:
    provider: str = "zhipu"
    screen_model: str = "glm-4.6v-flash"
    review_model: str = "glm-4.6v"
    review_top_n: int = 5
    prompt_version: str = "v1"
    max_output_tokens: int = 500
    temperature: float = 0.2
    pricing: dict[str, VisionPricing] = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetSection:
    monthly_cny: float = 10.0
    warn_ratio: float = 0.80


@dataclass(frozen=True)
class WeatherSection:
    enabled: bool = True
    city: str = "北京"
    latitude: float = 39.9075
    longitude: float = 116.39723
    cache_minutes: int = 60


@dataclass(frozen=True)
class WallpaperSection:
    style: str = "fill"
    monitor: int = 0
    # Every candidate competes in one pool; anything at or above this score is
    # good enough to go on the desktop. 0 keeps everything that passed
    # screening. 45 was picked from the real score spread (22 candidates on a
    # typical day: median 62, tenth percentile 39) -- 55 left so few that a
    # second click had to wait for a full refetch.
    min_score: float = 45.0
    # Battery is the only thing that still looks at the media type: a scene or
    # video drains a laptop, so it is skipped while unplugged.
    dynamic_requires_ac: bool = True
    # Kept so an older config file still parses. The static/dynamic day plan
    # they drove is gone -- nothing reads these any more.
    media_strategy: str = "mixed"
    dynamic_every_n_days: int = 3
    selection_margin: float = 0.0
    dynamic_max_streak: int = 2
    dynamic_quota_days: int = 10
    dynamic_quota_max: int = 5


@dataclass(frozen=True)
class WallpaperEngineSection:
    install_dir: str = ""
    config_path: str = ""
    # Read the Steam registry and library list at launch. Turning this off
    # makes the app use install_dir exactly as written, with no guessing.
    auto_detect: bool = True


@dataclass(frozen=True)
class ToolsSection:
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


@dataclass(frozen=True)
class RetentionSection:
    wallpapers_days: int = 90
    cache_days: int = 180
    log_mb: int = 5
    log_backups: int = 5


@dataclass(frozen=True)
class SteamWorkshopSection:
    enabled: bool = False
    query_type: int = 3          # IPublishedFileService: 3 = trend
    days: int = 30
    per_page: int = 50
    prefetch_top_n: int = 6
    max_cache_gb: float = 3.0
    steamcmd_path: str = ""      # empty -> <data_dir>/tools/steamcmd/steamcmd.exe
    steam_account: str = ""      # username only; the password is never stored
    download_timeout: int = 1800


@dataclass(frozen=True)
class Config:
    app: AppSection
    sources: SourcesSection
    vision: VisionSection
    budget: BudgetSection
    weather: WeatherSection
    wallpaper: WallpaperSection
    wallpaper_engine: WallpaperEngineSection
    tools: ToolsSection
    retention: RetentionSection
    steam_workshop: SteamWorkshopSection = field(default_factory=SteamWorkshopSection)


def _section(cls: type[T], raw: dict[str, Any]) -> T:
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in allowed})


def user_data_dir() -> Path:
    """Where an installed copy keeps config, data and logs.

    ``%LOCALAPPDATA%\\DailyWallpaper`` needs no admin rights, survives app
    updates and is not wiped by uninstall.
    """
    override = os.environ.get("DAILYWALLPAPER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "DailyWallpaper"
    return Path.home() / ".dailywallpaper"


def default_config_path() -> Path:
    override = os.environ.get("DAILYWALLPAPER_CONFIG")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return user_data_dir() / "config.toml"
    # Source runs keep the historical behaviour: config.toml in the cwd.
    return Path.cwd() / "config.toml"


def install_prefs() -> dict:
    """The installer's one-time choices (seed / autostart).

    A missing file means "no choice made": treat everything as the default.
    """
    path = user_data_dir() / "install_prefs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _create_default_config(path: Path) -> None:
    """First run of an installed copy: copy the shipped example."""
    from dailywallpaper.resources import resource

    example = resource("config.example.toml")
    if not example.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = example.read_text(encoding="utf-8")
    if install_prefs().get("autostart"):
        # The installer asked for autostart; the config is the source of
        # truth, so record the choice before the first launch syncs it.
        text = text.replace(
            "start_with_windows = false", "start_with_windows = true", 1
        )
    path.write_text(text, encoding="utf-8")


def load_config(path: Path) -> Config:
    if not path.exists():
        if getattr(sys, "frozen", False):
            _create_default_config(path)
        else:
            raise FileNotFoundError(f"config not found: {path}")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    vision_raw = dict(raw.get("vision", {}))
    pricing_raw = vision_raw.pop("pricing", {})
    pricing = {name: VisionPricing(**p) for name, p in pricing_raw.items()}
    app_section = _section(AppSection, raw.get("app", {}))
    if not Path(app_section.data_dir).is_absolute():
        # The Run key starts the process in an unrelated working directory,
        # so a relative data_dir must mean "next to the config file".
        app_section = replace(
            app_section,
            data_dir=str((Path(path).parent / app_section.data_dir).resolve()),
        )
    return Config(
        app=app_section,
        sources=_section(SourcesSection, raw.get("sources", {})),
        vision=VisionSection(**{**{k: v for k, v in vision_raw.items() if k in
                                  {f.name for f in fields(VisionSection)}},
                                "pricing": pricing}),
        budget=_section(BudgetSection, raw.get("budget", {})),
        weather=_section(WeatherSection, raw.get("weather", {})),
        wallpaper=_section(WallpaperSection, raw.get("wallpaper", {})),
        wallpaper_engine=_section(WallpaperEngineSection, raw.get("wallpaper_engine", {})),
        tools=_section(ToolsSection, raw.get("tools", {})),
        retention=_section(RetentionSection, raw.get("retention", {})),
        steam_workshop=_section(SteamWorkshopSection, raw.get("steam_workshop", {})),
    )



KNOWN_SECTIONS = (
    "app", "sources", "budget", "weather", "wallpaper",
    "wallpaper_engine", "tools", "retention", "vision", "steam_workshop",
)


def _toml_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    raise TypeError(f"unsupported config value: {value!r}")


NEWLINE = chr(10)


def save_config(path: Path, cfg: Config) -> None:
    """Rewrite config.toml from ``cfg``.

    Comments in the old file are not preserved: the settings page is the
    editor from now on, and the header says so.
    """
    lines = ["# Generated by the app; edit through the settings page.", ""]
    sections = (
        ("app", cfg.app),
        ("sources", cfg.sources),
        ("budget", cfg.budget),
        ("weather", cfg.weather),
        ("wallpaper", cfg.wallpaper),
        ("wallpaper_engine", cfg.wallpaper_engine),
        ("tools", cfg.tools),
        ("retention", cfg.retention),
        ("steam_workshop", cfg.steam_workshop),
    )
    for name, section in sections:
        lines.append(f"[{name}]")
        for spec in fields(section):
            lines.append(f"{spec.name} = {_toml_value(getattr(section, spec.name))}")
        lines.append("")

    lines.append("[vision]")
    for spec in fields(cfg.vision):
        if spec.name == "pricing":
            continue
        lines.append(f"{spec.name} = {_toml_value(getattr(cfg.vision, spec.name))}")
    for model, price in cfg.vision.pricing.items():
        lines.append("")
        lines.append(f'[vision.pricing."{model}"]')
        lines.append(
            f"input_cny_per_million = {_toml_value(price.input_cny_per_million)}"
        )
        lines.append(
            f"output_cny_per_million = {_toml_value(price.output_cny_per_million)}"
        )

    lines.extend(_unknown_sections(path))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(NEWLINE.join(lines) + NEWLINE, encoding="utf-8")
    os.replace(tmp, path)


def _unknown_sections(path: Path) -> list[str]:
    """Re-emit top-level tables this app does not model.

    Dropping them would silently destroy a user's hand-written section the
    first time they changed anything in the settings page.
    """
    if not path.exists():
        return []
    import tomllib

    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    extra = {key: value for key, value in raw.items() if key not in KNOWN_SECTIONS}
    if not extra:
        return []
    lines = ["", "# Preserved verbatim from the previous file."]
    for name, value in extra.items():
        lines.extend(_dump_value(name, value))
    return lines


def _toml_key(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name, ensure_ascii=False)


def _is_table_array(value) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) for item in value)
    )


def _dump_array(prefix: str, value: list) -> list[str]:
    lines = []
    for item in value:
        lines.append(f"[[{prefix}]]")
        nested = {}
        for key, sub in item.items():
            if isinstance(sub, (dict, list)) and (
                isinstance(sub, dict) or _is_table_array(sub)
            ):
                nested[key] = sub
            else:
                lines.append(f"{_toml_key(key)} = {_toml_value(sub)}")
        lines.append("")
        for key, sub in nested.items():
            child = f"{prefix}.{_toml_key(key)}"
            if isinstance(sub, dict):
                lines.extend(_dump_table(child, sub))
            else:
                lines.extend(_dump_array(child, sub))
    return lines


def _dump_value(prefix: str, value) -> list[str]:
    """Serialize one unknown top-level entry of any TOML shape."""
    if isinstance(value, dict):
        return _dump_table(prefix, value)
    if _is_table_array(value):
        return _dump_array(prefix, value)
    return [f"{_toml_key(prefix)} = {_toml_value(value)}", ""]


def _dump_table(prefix: str, table: dict) -> list[str]:
    lines = []
    scalars = {}
    nested = {}
    arrays = {}
    for key, value in table.items():
        if isinstance(value, dict):
            nested[key] = value
        elif _is_table_array(value):
            arrays[key] = value
        else:
            scalars[key] = value
    if scalars or (not nested and not arrays):
        lines.append(f"[{prefix}]")
        for key, value in scalars.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    for key, value in arrays.items():
        lines.extend(_dump_array(f"{prefix}.{_toml_key(key)}", value))
    for key, value in nested.items():
        lines.extend(_dump_table(f"{prefix}.{_toml_key(key)}", value))
    return lines
