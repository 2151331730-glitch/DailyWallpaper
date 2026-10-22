# src/dailywallpaper/constants.py
"""Numbers that must agree across modules.

BANNED_MAX_RATING used to be written by hand in four modules and drifted;
the same for the sound key. One place now, imported everywhere.
"""

# A rating this low means "never show me this again".
BANNED_MAX_RATING = 2
# The tray sound switch, stored in the state table.
SOUND_KEY = "sound_enabled"
# WE forgets its mute state on restart; the guard re-asserts it this often.
SOUND_REASSERT_SECONDS = 60.0
# A day's shortlist can be refilled a handful of times.
MAX_FORCED_PREPARES = 6
# Only the head of the ranked list is downloaded up front.
MATERIALIZE_TOP_N = 3
# Stop a prepare pass after this many rate-limit refusals in a row.
RATE_LIMIT_ABORT_AFTER = 6
# At most this many search intents are honoured per prepare; each one costs a
# request per source, and the newest matter most.
MAX_SEARCH_INTENTS = 3
# A rough per-call estimate handed to the budget guard before paid work.
ESTIMATE_CNY = 0.003
# Video files that can be used directly from disk.
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
