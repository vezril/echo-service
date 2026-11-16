"""Notifications: the manual-retention assist.

Retention is Calvin's and is never automated — Echo deletes nothing. It emits two
notifications over Hermes instead:

1. **available** — a new video is downloaded, ingested and watchable (``video.ingested``).
   Unblocked and implemented.
2. **stale / unwatched nudge** — a downloaded video has sat unwatched past a configurable
   window, prompting the watch-or-clear decision.

The stale nudge needs watched-state from Artemis, which does **not** exist and is not
authorized (``docs/DESIGN.md`` §2–3). It is therefore built against a
:class:`~echo.notify.service.WatchedState` port whose only implementation today answers "I
cannot tell", and the nudge is **suppressed rather than guessed** in that case — a nudge about
videos that may already be watched is worse than none. The window arithmetic, the aggregation
and the payload are built and tested, so the day Artemis exposes a viewed flag this lights up
by passing in an implementation.

Pieces:

* :mod:`~echo.notify.hermes` — the broker client (publish + ensure-topic), verified against
  hermesmq's REST routes
* :mod:`~echo.notify.events` — the two payloads. **Not Lexicon-defined** — a recorded gap,
  shaped so promoting them into ``the-lexicon`` is rename-free
* :mod:`~echo.notify.service` — when to emit, when to stay quiet
"""

from __future__ import annotations

from echo.notify.events import (
    STALE_UNWATCHED,
    VIDEO_INGESTED,
    StaleUnwatched,
    VideoIngested,
)
from echo.notify.hermes import (
    CORRELATION_HEADER,
    HermesClient,
    HermesError,
    PublishedMessage,
)
from echo.notify.service import (
    NotificationService,
    NotifyOutcome,
    NotifyReport,
    UnknownWatchedState,
    WatchedState,
    stale_candidates,
)

__all__ = [
    "CORRELATION_HEADER",
    "STALE_UNWATCHED",
    "VIDEO_INGESTED",
    "HermesClient",
    "HermesError",
    "NotificationService",
    "NotifyOutcome",
    "NotifyReport",
    "PublishedMessage",
    "StaleUnwatched",
    "UnknownWatchedState",
    "VideoIngested",
    "WatchedState",
    "stale_candidates",
]
