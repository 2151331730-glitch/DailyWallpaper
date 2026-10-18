# src/dailywallpaper/db.py
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

# Spec 6.1, verbatim.
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS wallpapers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT NOT NULL,              -- wallhaven | bing | we_workshop
  source_id     TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  page_url      TEXT NOT NULL DEFAULT '',
  file_url      TEXT NOT NULL DEFAULT '',
  preview_url   TEXT NOT NULL DEFAULT '',
  local_path    TEXT NOT NULL DEFAULT '',
  media_type    TEXT NOT NULL DEFAULT 'image', -- image | video | scene
  width         INTEGER NOT NULL DEFAULT 0,
  height        INTEGER NOT NULL DEFAULT 0,
  file_size     INTEGER NOT NULL DEFAULT 0,
  phash         TEXT NOT NULL DEFAULT '',
  tags_json     TEXT NOT NULL DEFAULT '[]',
  raw_json      TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS analyses (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  image_hash      TEXT NOT NULL,
  scores_json     TEXT NOT NULL,
  quality         REAL NOT NULL,
  creativity      REAL NOT NULL,
  novelty         REAL NOT NULL,
  formulaic       REAL NOT NULL,
  nsfw            INTEGER NOT NULL DEFAULT 0,
  style_tags_json TEXT NOT NULL DEFAULT '[]',
  mood_tags_json  TEXT NOT NULL DEFAULT '[]',
  palette_json    TEXT NOT NULL DEFAULT '[]',
  tokens_in       INTEGER NOT NULL DEFAULT 0,
  tokens_out      INTEGER NOT NULL DEFAULT 0,
  cost_cny        REAL NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  UNIQUE(image_hash, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS wallpaper_analyses (
  wallpaper_id INTEGER NOT NULL REFERENCES wallpapers(id) ON DELETE CASCADE,
  analysis_id  INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (wallpaper_id, analysis_id)
);

CREATE TABLE IF NOT EXISTS decisions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_date     TEXT NOT NULL,          -- YYYY-MM-DD，本地时区
  wallpaper_id      INTEGER NOT NULL REFERENCES wallpapers(id),
  explore_score     REAL NOT NULL,
  personalize_score REAL NOT NULL,
  final_score       REAL NOT NULL,
  reason            TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL,           -- shortlisted | applied | rejected | kept | analysis_failed
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  change_date              TEXT NOT NULL,
  wallpaper_id             INTEGER NOT NULL REFERENCES wallpapers(id),
  previous_wallpaper       TEXT NOT NULL DEFAULT '',
  previous_we_config_json  TEXT NOT NULL DEFAULT '{}',
  method                   TEXT NOT NULL,     -- static | we | manual
  applied_at               TEXT NOT NULL,
  success                  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ratings (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  wallpaper_id     INTEGER NOT NULL REFERENCES wallpapers(id),
  rating           INTEGER NOT NULL,          -- -1 dislike | 0 neutral | 1 like
  comment          TEXT NOT NULL DEFAULT '',
  derived_tags_json TEXT NOT NULL DEFAULT '[]',
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,                  -- 本地时间 YYYY-MM-DDTHH:MM:SS
  run_id      TEXT NOT NULL DEFAULT '',
  provider    TEXT NOT NULL,
  model       TEXT NOT NULL,
  purpose     TEXT NOT NULL,                  -- screen | review | comment
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0,
  cost_cny    REAL NOT NULL DEFAULT 0,
  estimated   INTEGER NOT NULL DEFAULT 0,
  cache_hit   INTEGER NOT NULL DEFAULT 0,
  status_code INTEGER NOT NULL DEFAULT 0,
  latency_ms  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wallpapers_source ON wallpapers(source, source_id);
CREATE INDEX IF NOT EXISTS idx_analyses_hash ON analyses(image_hash);
CREATE INDEX IF NOT EXISTS idx_wallpaper_analyses_wallpaper ON wallpaper_analyses(wallpaper_id);
CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(decision_date);
CREATE INDEX IF NOT EXISTS idx_api_usage_ts ON api_usage(ts);
CREATE INDEX IF NOT EXISTS idx_api_usage_run ON api_usage(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_change_success_date
  ON change_log(change_date) WHERE success = 1 AND method <> 'manual';
CREATE UNIQUE INDEX IF NOT EXISTS ux_ratings_wallpaper
  ON ratings(wallpaper_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def backup(conn, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(dest) as target:
        conn.backup(target)
    return dest


def _as_aware(moment: datetime) -> datetime:
    """Attach the local offset to a naive datetime; leave aware ones alone."""
    return moment if moment.tzinfo else moment.astimezone()


def parse_ts(value: str) -> datetime:
    """Parse a stored timestamp into an aware datetime.

    ``now_iso`` writes no offset, so everything read back is naive until it
    is normalized here -- comparing it with ``SystemClock.now()`` (aware)
    would otherwise raise TypeError.
    """
    return _as_aware(datetime.fromisoformat(value))


def now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()
