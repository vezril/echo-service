"""Echo's own state — the memory the pipeline needs.

SQLite, single writer, file on the NAS volume. It holds what Echo alone authors: which video
ids a channel has already yielded, the `handle → channel_id` resolution, the feed's
conditional-GET validators, the **verbatim title** Artemis has no column for, and the
per-video queue/retry/error state ``echo-ui`` exists to display.

Rationale and the schema are in :mod:`echo.state.schema`; the queries the worker actually
runs are in :mod:`echo.state.store`.
"""

from __future__ import annotations

from echo.state.schema import (
    FETCH_STATES,
    INGEST_STATES,
    RETRY_BACKOFF,
    SCHEMA_VERSION,
    SEEN_STATES,
    VideoState,
)
from echo.state.store import (
    EchoStore,
    FeedValidators,
    VideoRecord,
    next_attempt_delay,
    open_store,
)

__all__ = [
    "FETCH_STATES",
    "INGEST_STATES",
    "RETRY_BACKOFF",
    "SCHEMA_VERSION",
    "SEEN_STATES",
    "EchoStore",
    "FeedValidators",
    "VideoRecord",
    "VideoState",
    "next_attempt_delay",
    "open_store",
]
