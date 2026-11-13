"""Notifications: the manual-retention assist.

Retention is Calvin's and is never automated — Echo deletes nothing. It emits two
notifications over Hermes instead:

1. **available** — a new video is downloaded, ingested and watchable (``video.ingested``).
2. **stale / unwatched nudge** — a downloaded video has sat unwatched past a configurable
   window, prompting the watch-or-clear decision.

The stale nudge needs watched-state from Artemis, which does **not** exist and is not
authorized (``docs/DESIGN.md`` §2–3). The ``pool:`` metatag it would also want is currently
``UnsupportedMetatag``. Design against the ``GET /pools/{id}/posts`` fallback; do not plan
around either landing.
"""
