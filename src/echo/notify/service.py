"""Emitting the two notifications — and refusing to emit a misleading one.

**available** is unblocked and straightforward: a video finished ingesting, so say so.

**the stale/unwatched nudge is not.** It needs to know whether a post has been *watched*, and
Artemis has no watched-state: the shape was designed (an additive ``PostViewed`` event, a
``last_viewed_at`` projection column, ``POST /posts/{id}/view``) and surfaced to Calvin on
2026-09-09, but nothing was authorized and nothing was built (``docs/DESIGN.md`` §2–3).

So watched-state is a **port** here, not an assumption. :class:`WatchedState` has one method
that may answer "I cannot tell", and the default implementation always does. When it cannot
tell, the nudge is **suppressed rather than guessed**: a notification saying "4 videos you
haven't watched" about videos already watched is worse than no notification — it trains the
reader to ignore the channel. The projection, the window arithmetic and the payload are all
built and tested, so the day Artemis exposes a viewed flag this lights up by passing in an
implementation.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from echo.follow.config import ChannelSettings
from echo.notify.events import StaleUnwatched, VideoIngested
from echo.notify.hermes import HermesClient, HermesError
from echo.state.store import VideoRecord

__all__ = [
    "NotifyOutcome",
    "NotifyReport",
    "NotificationService",
    "UnknownWatchedState",
    "WatchedState",
    "stale_candidates",
]

#: A nudge naming more titles than this is a wall of text, not a prompt.
MAX_TITLES_IN_NUDGE = 5


class NotifyOutcome(StrEnum):
    """What happened to one notification."""

    #: Published to Hermes.
    SENT = "sent"
    #: The broker collapsed it onto an earlier publish with the same idempotency key.
    DEDUPLICATED = "deduplicated"
    #: Deliberately not sent — switched off in config, nothing to say, or unknowable.
    SUPPRESSED = "suppressed"
    #: The publish failed.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NotifyReport:
    """The outcome of one notification — returned, never raised."""

    outcome: NotifyOutcome
    reason: str
    message_id: str | None = None
    retryable: bool = False

    @property
    def delivered(self) -> bool:
        """True when Hermes holds the message, whether this call put it there or not."""
        return self.outcome in {NotifyOutcome.SENT, NotifyOutcome.DEDUPLICATED}


class WatchedState(Protocol):
    """Whether posts have been watched — Artemis's to answer, and it cannot yet.

    Returning ``None`` means "cannot tell", which is different from "none of them are
    watched" and must not be collapsed into it.
    """

    def unwatched(self, post_ids: Collection[str]) -> set[str] | None: ...


class UnknownWatchedState:
    """The only implementation available today: it never knows.

    Deliberately not a lie. Artemis exposes no viewed flag, so anything else here would be an
    assumption dressed as data.
    """

    def unwatched(self, post_ids: Collection[str]) -> set[str] | None:
        return None


def stale_candidates(
    records: Sequence[VideoRecord],
    *,
    settings: ChannelSettings,
    now: datetime | None = None,
) -> tuple[VideoRecord, ...]:
    """Ingested videos whose window has elapsed, oldest first.

    Uses each record's ``updated_at``, which for an ``ingested`` row is when it became
    ingested — nothing touches the row afterwards. ``stale_after = None`` disables the nudge
    for the channel and yields nothing.
    """
    window = settings.notify.stale_after
    if window is None:
        return ()

    cutoff = (now or datetime.now(UTC)) - window
    stale = [
        record
        for record in records
        if record.post_id and record.updated_at is not None and record.updated_at <= cutoff
    ]
    stale.sort(key=lambda record: (record.updated_at or cutoff, record.video_id))
    return tuple(stale)


class NotificationService:
    """Publishes Echo's two notifications over Hermes."""

    def __init__(
        self,
        client: HermesClient,
        *,
        watched_state: WatchedState | None = None,
        ensure_topics: bool = True,
    ) -> None:
        self._client = client
        self._watched = watched_state or UnknownWatchedState()
        self._ensure_topics = ensure_topics
        self._known_topics: set[str] = set()

    def video_ingested(
        self,
        record: VideoRecord,
        *,
        settings: ChannelSettings,
        channel_name: str | None = None,
        at: datetime | None = None,
    ) -> NotifyReport:
        """Announce a newly watchable video."""
        if not settings.notify.on_available:
            return NotifyReport(NotifyOutcome.SUPPRESSED, "on_available is false")
        if not record.post_id:
            return NotifyReport(
                NotifyOutcome.SUPPRESSED,
                "no Artemis post recorded — nothing to announce yet",
            )

        event = VideoIngested(
            video_id=record.video_id,
            post_id=record.post_id,
            channel_id=record.channel_id,
            channel_name=channel_name,
            title=record.title,
            url=record.url,
            uploader=record.uploader,
            duration_seconds=record.duration.total_seconds() if record.duration else None,
            file_size_bytes=record.filesize,
            ingested_at=record.updated_at or at or datetime.now(UTC),
        )
        return self._publish(event.event, event.to_json(), event.idempotency_key, event.attributes)

    def stale_unwatched(
        self,
        records: Sequence[VideoRecord],
        *,
        settings: ChannelSettings,
        channel_id: str,
        channel_name: str | None = None,
        now: datetime | None = None,
    ) -> NotifyReport:
        """Nudge about a channel's unwatched backlog, or explain why it cannot.

        Suppressed — never guessed — when watched-state is unavailable, which is the state of
        the world today.
        """
        window = settings.notify.stale_after
        if window is None:
            return NotifyReport(NotifyOutcome.SUPPRESSED, "stale_after is null for this channel")

        moment = now or datetime.now(UTC)
        candidates = stale_candidates(records, settings=settings, now=moment)
        if not candidates:
            return NotifyReport(NotifyOutcome.SUPPRESSED, "nothing past the stale window")

        unwatched_ids = self._watched.unwatched([r.post_id for r in candidates if r.post_id])
        if unwatched_ids is None:
            return NotifyReport(
                NotifyOutcome.SUPPRESSED,
                "no watched-state source: Artemis exposes no viewed flag, and a nudge about "
                "videos that may already be watched is worse than none",
            )

        unwatched = [record for record in candidates if record.post_id in unwatched_ids]
        if not unwatched:
            return NotifyReport(NotifyOutcome.SUPPRESSED, "everything past the window is watched")

        event = StaleUnwatched(
            channel_id=channel_id,
            channel_name=channel_name,
            video_count=len(unwatched),
            stale_after=window,
            video_ids=tuple(record.video_id for record in unwatched),
            titles=tuple(record.title for record in unwatched[:MAX_TITLES_IN_NUDGE]),
            oldest_ingested_at=unwatched[0].updated_at,
            as_of=moment,
        )
        return self._publish(event.event, event.to_json(), event.idempotency_key, event.attributes)

    def _publish(
        self,
        topic: str,
        payload: str,
        idempotency_key: str,
        attributes: dict[str, str],
    ) -> NotifyReport:
        try:
            if self._ensure_topics and topic not in self._known_topics:
                # Once per process: publishing to a missing topic is a 404, and creating it is
                # cheap, but doing it on every notification would double Echo's broker calls.
                self._client.ensure_topic(topic, labels={"producer": "echo-service"})
                self._known_topics.add(topic)

            published = self._client.publish(
                topic,
                payload,
                attributes=attributes,
                idempotency_key=idempotency_key,
            )
        except HermesError as exc:
            return NotifyReport(NotifyOutcome.FAILED, str(exc), retryable=exc.retryable)

        return NotifyReport(
            outcome=(NotifyOutcome.DEDUPLICATED if published.deduplicated else NotifyOutcome.SENT),
            reason="already published with this idempotency key"
            if published.deduplicated
            else "published",
            message_id=published.message_id,
        )
