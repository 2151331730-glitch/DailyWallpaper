# src/dailywallpaper/config.py
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
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
    wallhaven_pages: int = 4


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
    media_strategy: str = "mixed"
    dynamic_every_n_days: int = 3
    dynamic_requires_ac: bool = True


@dataclass(frozen=True)
class WallpaperEngineSection:
    install_dir: str = ""
    config_path: str = ""


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


def _section(cls: type[T], raw: dict[str, Any]) -> T:
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in allowed})


def default_config_path() -> Path:
    return Path(os.environ.get("DAILYWALLPAPER_CONFIG", Path.cwd() / "config.toml"))


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    vision_raw = dict(raw.get("vision", {}))
    pricing_raw = vision_raw.pop("pricing", {})
    pricing = {name: VisionPricing(**p) for name, p in pricing_raw.items()}
    return Config(
        app=_section(AppSection, raw.get("app", {})),
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
    )
