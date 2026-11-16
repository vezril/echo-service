"""The two notification payloads.

**These are not Lexicon-defined, and that is a recorded gap rather than an oversight.** The
constellation's wire contracts live in ``the-lexicon`` as protobuf and generate typed stubs for
both languages precisely so producer and consumer cannot drift
(``codex.messages.v1`` — ``ProcessMediaJob``, ``MediaProcessed``, ``TagJob``…). There is **no
``video.ingested`` message there**, and adding one is a change to that repo, gated by its own
``buf lint`` / ``buf breaking`` checks and release process — not something Echo's tree can do.

So Echo publishes JSON it defines here, shaped to make promotion into the Lexicon a rename-free
move when it happens:

* **snake_case field names**, which is what protobuf canonical JSON emits;
* **flat, scalar fields** — no nested free-form objects;
* **durations in seconds, timestamps as RFC 3339 UTC**, matching ``MediaMetadata``'s
  ``duration_seconds`` and the lexicon's timestamp convention;
* an explicit ``event`` discriminator, so one topic could carry both payloads.

Nothing consumes these yet (Hera / the Mentor brief are the intended readers), which is why
defining them here is acceptable for now: the first consumer is also the moment the contract
needs to be real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

__all__ = [
    "STALE_UNWATCHED",
    "VIDEO_INGESTED",
    "StaleUnwatched",
    "VideoIngested",
]

#: The event names, which double as the Hermes topic ids (the constellation's topics are
#: dotted lowercase: ``media.process``, ``media.tag``, ``media.reprocess``).
VIDEO_INGESTED: Final = "video.ingested"
STALE_UNWATCHED: Final = "video.stale"


@dataclass(frozen=True, slots=True)
class VideoIngested:
    """A new video is downloaded, posted and watchable in Artemis.

    ``post_id`` is the join key to Artemis — ``source`` cannot be stamped from outside today
    (``docs/DESIGN.md``) — and ``video_id`` is the join key back to Echo's own record.
    """

    video_id: str
    post_id: str
    channel_id: str
    title: str
    url: str
    channel_name: str | None = None
    uploader: str | None = None
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    ingested_at: datetime | None = None

    @property
    def event(self) -> str:
        return VIDEO_INGESTED

    def to_payload(self) -> dict[str, Any]:
        """The JSON body, omitting absent optional fields as proto JSON does."""
        return _compact(
            {
                "event": self.event,
                "video_id": self.video_id,
                "post_id": self.post_id,
                "channel_id": self.channel_id,
                "channel_name": self.channel_name,
                "title": self.title,
                "url": self.url,
                "uploader": self.uploader,
                "duration_seconds": self.duration_seconds,
                "file_size_bytes": self.file_size_bytes,
                "ingested_at": _timestamp(self.ingested_at),
            }
        )

    def to_json(self) -> str:
        return _dump(self.to_payload())

    @property
    def idempotency_key(self) -> str:
        """One notification per video, however many times the publish is retried."""
        return f"{self.event}:{self.video_id}"

    @property
    def attributes(self) -> dict[str, str]:
        """Broker-level attributes a consumer can filter on without parsing the payload."""
        return {"event": self.event, "channel_id": self.channel_id, "video_id": self.video_id}


@dataclass(frozen=True, slots=True)
class StaleUnwatched:
    """Videos from one channel have sat unwatched past the configured window.

    Aggregated per channel, not per video — "4 videos from Veritasium you haven't watched" is
    one actionable nudge, where four notifications are noise. It deletes nothing: it prompts
    the watch-or-clear decision, which is the retention loop (retention stays manual).
    """

    channel_id: str
    video_count: int
    stale_after: timedelta
    channel_name: str | None = None
    video_ids: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    oldest_ingested_at: datetime | None = None
    as_of: datetime | None = None

    @property
    def event(self) -> str:
        return STALE_UNWATCHED

    def to_payload(self) -> dict[str, Any]:
        return _compact(
            {
                "event": self.event,
                "channel_id": self.channel_id,
                "channel_name": self.channel_name,
                "video_count": self.video_count,
                "stale_after_seconds": int(self.stale_after.total_seconds()),
                "video_ids": list(self.video_ids) or None,
                "titles": list(self.titles) or None,
                "oldest_ingested_at": _timestamp(self.oldest_ingested_at),
                "as_of": _timestamp(self.as_of),
            }
        )

    def to_json(self) -> str:
        return _dump(self.to_payload())

    @property
    def idempotency_key(self) -> str:
        """One nudge per channel per UTC day.

        The date in the key is what stops a worker loop that runs every 30 minutes from
        nudging 48 times about the same backlog, without Echo keeping nudge state. How often
        the nudge is *attempted* is the loop's cadence to decide; this is the floor.
        """
        day = (self.as_of or datetime.now(UTC)).astimezone(UTC).date().isoformat()
        return f"{self.event}:{self.channel_id}:{day}"

    @property
    def attributes(self) -> dict[str, str]:
        return {
            "event": self.event,
            "channel_id": self.channel_id,
            "video_count": str(self.video_count),
        }


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop absent optional fields, as protobuf canonical JSON does."""
    return {key: value for key, value in payload.items() if value is not None}


def _dump(payload: dict[str, Any]) -> str:
    # Compact separators and no ASCII escaping: the payload carries verbatim video titles,
    # which are routinely non-ASCII, and a consumer reading them should not see \uXXXX.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _timestamp(value: datetime | None) -> str | None:
    """RFC 3339 in UTC — the lexicon's timestamp convention."""
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()
