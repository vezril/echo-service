"""The SQLite schema, and the states a video moves through.

**Why SQLite:** Echo is a single-writer worker on the Argus pattern, and the store exists to
hold facts Echo alone authors — what it has seen, what it resolved, what it downloaded. A
server-class database would add a dependency, a Secret and a backup story for a few thousand
rows. The file lives on the NAS volume beside the downloads.

**Why a store at all** (``docs/DESIGN.md``): the feed is a ~15-entry window, so new-upload
detection needs memory; Artemis has no column that can hold a verbatim title, so Echo keeps
it and joins on ``source``; and ``echo-ui`` exists to show a queue, which has to be recorded
somewhere.

Two notions of "done" are deliberately separate, because conflating them either re-downloads
videos or loses them:

* :data:`SEEN_STATES` — do not pick this up from the feed again.
* the work queues — :data:`FETCH_STATES` still owe a download, :data:`INGEST_STATES` still
  owe an Artemis post.

A ``deferred`` or ``failed`` video is **not** seen: it is owed another attempt.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

__all__ = [
    "FETCH_STATES",
    "INGEST_STATES",
    "RETRY_BACKOFF",
    "SCHEMA_VERSION",
    "SEEN_STATES",
    "SCHEMA_STATEMENTS",
    "VideoState",
]

SCHEMA_VERSION: Final = 1


class VideoState(StrEnum):
    """Where a video is in Echo's pipeline."""

    #: Seen in the feed, not yet fetched.
    PENDING = "pending"
    #: Downloaded to the NAS; still owes an Artemis post.
    DOWNLOADED = "downloaded"
    #: Posted to Artemis — the terminal success.
    INGESTED = "ingested"
    #: Deliberately not downloaded (a filter, or a video that is permanently gone).
    FILTERED = "filtered"
    #: Not downloadable yet — a stream that is still live.
    DEFERRED = "deferred"
    #: An attempt failed for a reason that may not recur.
    FAILED = "failed"


#: States that mean "do not select this from the feed again".
SEEN_STATES: Final = frozenset({VideoState.DOWNLOADED, VideoState.INGESTED, VideoState.FILTERED})

#: States that still owe a download attempt.
FETCH_STATES: Final = frozenset({VideoState.PENDING, VideoState.DEFERRED, VideoState.FAILED})

#: States that still owe an Artemis post.
INGEST_STATES: Final = frozenset({VideoState.DOWNLOADED})

#: Backoff per attempt, capped at the last entry. A live stream waits roughly its own length
#: before being re-probed, and a bot-check backs off rather than hammering an endpoint that
#: is already refusing.
RETRY_BACKOFF: Final = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)

# Timestamps are ISO-8601 UTC text. SQLite has no date type, and text keeps the file
# readable with the sqlite3 CLI when something needs explaining at 2am.
SCHEMA_STATEMENTS: Final = (
    """
    CREATE TABLE IF NOT EXISTS channels (
        channel_id      TEXT PRIMARY KEY,
        handle          TEXT,
        url             TEXT,
        name            TEXT,
        -- NULL until a poll has completed: this is what makes backfill a first-sync-only
        -- decision rather than something that re-triggers on restart.
        first_synced_at TEXT,
        last_polled_at  TEXT,
        -- Conditional-GET validators, so a 30-minute poll usually costs YouTube a 304.
        etag            TEXT,
        last_modified   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
        -- The bare YouTube video id. Also the `source` stamped on the Artemis post, which
        -- is how a post is joined back to this row (docs/DESIGN.md, locked 2026-11-13).
        video_id         TEXT PRIMARY KEY,
        channel_id       TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
        -- VERBATIM, never normalized: Artemis has no field that can hold it, so this is the
        -- only place the human-readable title exists.
        title            TEXT NOT NULL,
        url              TEXT NOT NULL,
        uploader         TEXT,
        published_at     TEXT,
        state            TEXT NOT NULL,
        reason           TEXT,
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT,
        file_path        TEXT,
        filesize         INTEGER,
        duration_seconds REAL,
        post_id          TEXT,
        first_seen_at    TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS videos_by_channel ON videos (channel_id, state)",
    "CREATE INDEX IF NOT EXISTS videos_by_state ON videos (state, next_attempt_at)",
)
