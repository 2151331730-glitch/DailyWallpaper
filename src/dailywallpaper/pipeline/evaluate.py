# src/dailywallpaper/pipeline/evaluate.py
"""Screening, scoring and the paid review pass."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from dailywallpaper.analysis.calibration import calibrate_scores
from dailywallpaper.analysis.desktop_fit import measure as measure_desktop_fit
from dailywallpaper.analysis.images import canonicalize, download
from dailywallpaper.analysis.media import gif_first_frame, video_contact_sheet
from dailywallpaper.analysis.prefilter import prefilter
from dailywallpaper.analysis.prompts import PROMPT_VERSION
from dailywallpaper.analysis.scoring import final_score, hard_gate
from dailywallpaper.app_support import (
    apply_path_for,
    install_allows_seed,
    merge_tags,
    rate_limited,
)
from dailywallpaper.constants import (
    ESTIMATE_CNY,
    RATE_LIMIT_ABORT_AFTER,
    VIDEO_SUFFIXES,
)
from dailywallpaper.models import Decision
from dailywallpaper.recommend.profile import PreferenceProfile
from dailywallpaper.sources.repository import (
    set_local_path,
    set_phash,
    upsert_wallpaper,
)

logger = logging.getLogger(__name__)


class EvaluateMixin:
    def _sugar_score(self, scores, title: str = ""):
        """The learned sugar-water score, or None when there is no model.

        None is what tells ``final_score`` to fall back to the formulaic
        nudge, so a machine without ``data/sugar_model.json`` behaves exactly
        as before.

        Words the user named as sugar water (luxury cars, 哪吒) are matched
        against the title and the tags separately from the model: the model's
        vocabulary is English style tags and has no training support for them.
        """
        from dailywallpaper.recommend.sugar import (
            SUGAR_WORD_SCORE,
            sugar_word_hit,
        )

        model = getattr(self, "sugar", None)
        base = None
        if model is not None:
            base = model.score(list(scores.style_tags) + list(scores.mood_tags))
        if sugar_word_hit(
            title, " ".join(scores.style_tags), " ".join(scores.mood_tags)
        ):
            # A named word beats a silent model, and never lowers a loud one.
            return max(base if base is not None else 0.0, SUGAR_WORD_SCORE)
        return base

    def _desktop_fit(self, path):
        """Measure a candidate once per process.

        The review pass re-scores the same few files the screen pass just
        measured; a 320x180 analysis is cheap, but doing it twice for every
        top candidate is pure waste.
        """
        key = str(path)
        cache = getattr(self, "_fit_cache", None)
        if cache is None:
            cache = self._fit_cache = {}
        if key in cache:
            return cache[key]
        fit = None
        try:
            fit = measure_desktop_fit(path)
        except Exception as exc:
            logger.warning(f"desktop fit for {path} failed: {exc}")
        cache[key] = fit
        return fit

    def _recent_styles(self):
        rows = self.conn.execute(
            """
            select w.source, a.style_tags_json
            from decisions d
            join wallpapers w on w.id = d.wallpaper_id
            join wallpaper_analyses wa on wa.wallpaper_id = w.id
            join analyses a on a.id = wa.analysis_id
            where d.status = 'applied'
              and a.id = (
                select max(a2.id) from wallpaper_analyses wa2
                join analyses a2 on a2.id = wa2.analysis_id
                where wa2.wallpaper_id = w.id
              )
            order by d.id desc limit 30
            """
        ).fetchall()
        recent = []
        for row in rows:
            tags = json.loads(row["style_tags_json"] or "[]")
            recent.append((row["source"], tags[0] if tags else ""))
        return recent

    def _profile(self, now):
        """Ratings joined with the features the vision model saw in them.

        The old query only carried one genre tag, so a taste for fog, low
        angles or muted palettes could never be learned.
        """
        rows = self.conn.execute(
            """
            select r.rating, r.created_at, r.derived_tags_json, w.source,
                   a.style_tags_json, a.mood_tags_json
            from ratings r
            join wallpapers w on w.id = r.wallpaper_id
            left join analyses a on a.id = (
                select max(a2.id) from wallpaper_analyses wa2
                join analyses a2 on a2.id = wa2.analysis_id
                where wa2.wallpaper_id = w.id
            )
            """
        ).fetchall()
        profile = PreferenceProfile.from_ratings(rows, now)
        # The curator seed has two halves. Weights are only a cold start: the
        # user's own ratings replace them. The rules (avoid/boost) are not
        # personal ratings at all -- they must apply on every machine,
        # including the one that produced them.
        from dailywallpaper.curation.seed import load_seed, seed_profile

        seed = load_seed() if install_allows_seed() else None
        if seed is not None:
            rules = seed_profile(seed)
            if profile.rating_count == 0:
                logger.info(
                    f"cold start from curated seed"
                    f" ({seed.get('rating_count', 0)} ratings)"
                )
                return rules
            profile = replace(
                profile,
                avoid_tags=rules.avoid_tags,
                boost_tags=rules.boost_tags,
            )
        # Comments the user wrote become lasting tags (permanent until removed
        # from the today page); merge them after the seed rules.
        from dailywallpaper.recommend.intents import preference_terms

        boost, avoid, soft = preference_terms(self.conn)
        if boost or avoid or soft:
            profile = replace(
                profile,
                avoid_tags=merge_tags(profile.avoid_tags, avoid),
                boost_tags=merge_tags(profile.boost_tags, boost),
                soft_avoid_tags=merge_tags(profile.soft_avoid_tags, soft),
            )
        return profile

    def _evaluate(self, candidates, data_dir, profile, recent, weather_code,
                  weather_text, recent_hashes, counters, hour=None) -> list[Decision]:
        """Screen and score one group of candidates; returns that group."""
        found: list[Decision] = []
        for candidate in candidates:
            if counters["downloads"] >= self.cfg.app.max_downloads_per_day:
                break
            if counters["analyses"] >= self.cfg.app.max_analyses_per_day:
                break
            if self.should_stop():
                break
            # Check the budget before downloading: once it is gone the analysis
            # cannot happen, so the preview would be a wasted download (and a
            # fallback group would pay for exactly one before noticing).
            if not self.budget.check(ESTIMATE_CNY):
                break
            try:
                # A rebuilt candidate already has a row; upserting it would
                # write local paths over the remote URLs it came from.
                wallpaper_id = candidate.known_id or upsert_wallpaper(
                    self.conn, candidate
                )
                if wallpaper_id in self._banned:
                    # Rated 1-2: the user never wants to see it again.
                    continue
                if self._rejected_recently(wallpaper_id):
                    # Its full-size file failed inspection lately; scoring the
                    # preview again would only queue another doomed download.
                    continue
                if not candidate.preview_url:
                    continue
                asset = Path(candidate.file_url)
                # A contact sheet or an extracted GIF frame is the shape the
                # dynamic-score bias was measured on; the author's cover is
                # not, so only these previews get calibrated.
                contact_sheet = False
                if (
                    candidate.media_type == "video"
                    and asset.is_file()
                    and asset.suffix.lower() in VIDEO_SUFFIXES
                    and self._ffmpeg_available()
                ):
                    # Spec 4.3: score a video from three frames, not its poster.
                    preview = video_contact_sheet(
                        asset,
                        data_dir / "cache" / "sheets" / f"{wallpaper_id}.jpg",
                        ffmpeg=self.cfg.tools.ffmpeg_path,
                    )
                    contact_sheet = True
                elif asset.is_file() and asset.suffix.lower() == ".gif":
                    preview = gif_first_frame(
                        asset, data_dir / "cache" / "sheets" / f"{wallpaper_id}.jpg"
                    )
                    contact_sheet = True
                else:
                    local_preview = Path(candidate.preview_url)
                    if local_preview.is_file():
                        # Rebuilt from the cache: no download, and the
                        # screening step is free.
                        preview = local_preview
                    else:
                        preview = download(
                            self.session,
                            candidate.preview_url,
                            data_dir / "cache" / "preview" / f"{wallpaper_id}.jpg",
                        )
                counters["downloads"] += 1
                canonical, image_hash = canonicalize(
                    preview, data_dir / "cache" / "normalized"
                )
                check = prefilter(canonical, candidate, self.cfg.sources, recent_hashes)
                if not check.ok:
                    continue
                set_phash(self.conn, wallpaper_id, check.dhash)
                set_local_path(self.conn, wallpaper_id, canonical)
                record = None
                stale = None
                if candidate.reuse_analysis:
                    # An old rubric (v1 knew nothing about sugar water or
                    # clichés) must not decide today's pick; re-score when the
                    # provider answers, and only fall back to it if it cannot.
                    stale = self.vision.latest_for(
                        wallpaper_id, self.cfg.vision.screen_model
                    )
                    if stale is not None and (
                        stale.prompt_version == PROMPT_VERSION
                    ):
                        record = stale
                if record is None:
                    if (
                        self._reanalyze_budget is not None
                        and self._reanalyze_budget <= 0
                    ):
                        # A refill may not spend minutes re-scoring: use what
                        # the cache already knows, or skip the candidate.
                        if stale is None:
                            continue
                        record = stale
                    else:
                        try:
                            record = self.vision.analyze_or_cache(
                                wallpaper_id, image_hash, canonical,
                                self.cfg.vision.screen_model,
                            )
                            counters["analyses"] += 1
                            self._rate_limited_streak = 0
                            if (
                                self._reanalyze_budget is not None
                                and not record.cached
                            ):
                                # A cache hit costs nothing; only a real call
                                # counts against the refill's fresh-analysis
                                # budget, or cached dynamic candidates would
                                # starve the static pool of its fair half.
                                self._reanalyze_budget -= 1
                        except Exception:
                            if stale is None:
                                raise
                            logger.info(
                                f"using the stored analysis for {wallpaper_id}"
                                " after the provider failed"
                            )
                            record = stale
                if hard_gate(record.scores):
                    continue
                fit = self._desktop_fit(canonical)
                # The gate above judges what the model actually saw; the
                # ranking below needs the score on the single-picture scale,
                # or a live wallpaper loses a comparison it should win.
                scores_now = (
                    calibrate_scores(record.scores)
                    if contact_sheet
                    else record.scores
                )
                scores = final_score(
                    scores_now, candidate.source, profile, recent, weather_code,
                    desktop_fit=None if fit is None else fit.score,
                    hour=hour,
                    sugar=self._sugar_score(record.scores, candidate.title),
                )
                reason = record.scores.reason
                if weather_text:
                    reason = f"{reason}；天气：{weather_text}"
                found.append(
                    Decision(
                        wallpaper_id,
                        canonical,
                        scores_now,
                        scores.explore,
                        scores.personalize,
                        scores.final,
                        reason,
                        candidate.source,
                        # A remote original has no local file yet; the
                        # materializer fills this in after ranking, and a
                        # candidate it cannot materialize is dropped.
                        apply_path=apply_path_for(candidate, canonical),
                        ready=(
                            candidate.source != "steam_workshop"
                            or Path(candidate.file_url).is_file()
                        ),
                        media_type=candidate.media_type,
                        calibrated=contact_sheet,
                    )
                )
            except Exception as exc:
                # One broken preview or vision response must not end the run.
                logger.warning(f"candidate {candidate.source_id} skipped: {exc}")
                if not rate_limited(exc):
                    self._rate_limited_streak = 0
                    continue
                self._rate_limited_streak += 1
                if self._rate_limited_streak >= RATE_LIMIT_ABORT_AFTER:
                    logger.warning(
                        f"provider rate-limited {self._rate_limited_streak} "
                        "times in a row; ending this pass early"
                    )
                    break
                continue
        return found

    def _review_top(self, profile, recent, weather_code, hour=None) -> None:
        """Re-score the strongest candidates with the paid model.

        A failure or an exhausted budget stops the loop and keeps whatever
        was reviewed so far; the free screen scores stay valid for the rest.
        """
        reviewed: list = []
        reviewed_ids: set = set()
        rejected: set = set()
        for decision in self.shortlist[: self.cfg.vision.review_top_n]:
            if not self.budget.check(ESTIMATE_CNY):
                logger.warning("review stopped: budget exhausted")
                break
            image_hash = Path(decision.image_path).stem
            try:
                record = self.vision.analyze_or_cache(
                    decision.wallpaper_id,
                    image_hash,
                    decision.image_path,
                    self.cfg.vision.review_model,
                )
            except Exception as exc:
                logger.warning(f"review failed for {decision.wallpaper_id}: {exc}")
                break
            if hard_gate(record.scores):
                rejected.add(decision.wallpaper_id)
                continue
            fit = self._desktop_fit(decision.image_path)
            scores_now = (
                calibrate_scores(record.scores)
                if decision.calibrated
                else record.scores
            )
            scores = final_score(
                scores_now, decision.source, profile, recent, weather_code,
                desktop_fit=None if fit is None else fit.score,
                hour=hour,
                sugar=self._sugar_score(
                    record.scores, getattr(decision, "title", "")
                ),
            )
            reviewed.append(
                replace(
                    decision,
                    scores=scores_now,
                    explore=scores.explore,
                    personalize=scores.personalize,
                    final=scores.final,
                    reviewed=True,
                )
            )
            reviewed_ids.add(decision.wallpaper_id)
        rest = [
            decision
            for decision in self.shortlist
            if decision.wallpaper_id not in reviewed_ids
            and decision.wallpaper_id not in rejected
        ]
        self.shortlist = reviewed + rest
