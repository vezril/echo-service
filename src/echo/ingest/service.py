"""The hand-off: upload the file, tag the post, file it in the channel's pool.

Four steps, in an order chosen so that a failure anywhere leaves a state a retry can fix:

1. ``POST /uploads`` — the file. Content-addressed, so re-uploading the same bytes returns the
   same post rather than a duplicate.
2. ``PATCH /posts/{id}/tags`` — the **complete** set: the uploader, the channel's
   ``extra_tags``, and the title's tokens.
3. ``POST /pools`` — create the channel's pool if it does not exist (``409`` is success).
4. ``POST /pools/{id}/posts`` — append the post. Idempotent in the pool aggregate.

Every step is idempotent, so :class:`IngestReport` only needs to say whether the whole
hand-off completed; a partial failure is simply retried from the top on the next cycle. The
caller marks the video ingested **only** on success, which keeps the store honest about what
is actually in the gallery.

**The join key is the post id, not ``source``.** Artemis has a ``SetSource`` domain command,
but no HTTP route exposes it and the fresh-upload path never applies the ``source`` its own
service method accepts — so the value cannot be stamped from outside today. The post id,
which the upload returns and the store already holds, carries the join instead. See
``docs/DESIGN.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from echo.follow.config import ChannelSettings
from echo.ingest.artemis import ArtemisClient, ArtemisError
from echo.ingest.tags import tags_for_video
from echo.state.store import VideoRecord

__all__ = ["IngestOutcome", "IngestReport", "IngestService", "pool_id_for_channel"]


class IngestOutcome(StrEnum):
    """What happened to one hand-off."""

    INGESTED = "ingested"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IngestReport:
    """The outcome of one hand-off — returned, never raised, like a fetch report."""

    video_id: str
    outcome: IngestOutcome
    reason: str
    post_id: str | None = None
    tags: tuple[str, ...] = ()
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome is IngestOutcome.INGESTED


def pool_id_for_channel(channel_id: str) -> str:
    """The pool id for a channel: the channel id itself.

    Artemis accepts a client-supplied pool id, so using the ``UC…`` id makes the mapping
    derivable rather than stored — Echo never has to remember which pool belongs to which
    channel, and two Echo instances would agree.
    """
    if not channel_id:
        raise ValueError("channel_id is required")
    return channel_id


class IngestService:
    """Hands downloaded videos to Artemis."""

    def __init__(self, client: ArtemisClient) -> None:
        self._client = client

    def ingest(
        self,
        record: VideoRecord,
        *,
        settings: ChannelSettings,
        channel_name: str | None = None,
    ) -> IngestReport:
        """Upload, tag and file one downloaded video.

        ``channel_name`` names the pool; it falls back to the channel id, because a pool must
        have a name and an unnamed channel is still better off in a pool than loose.
        """
        if record.file_path is None:
            return IngestReport(
                video_id=record.video_id,
                outcome=IngestOutcome.FAILED,
                reason="no downloaded file recorded",
            )

        # `uploader_as_artist` gates only the uploader tag; `extra_tags` is an unconditional
        # per-channel stamp.
        tags = tags_for_video(
            title=record.title,
            uploader=record.uploader if settings.catalog.uploader_as_artist else None,
            extra_tags=settings.catalog.extra_tags,
        )

        post_id: str | None = None
        try:
            post_id = self._upload(record.file_path)
            self._client.replace_tags(post_id, tags)
            if settings.catalog.pool_per_channel:
                pool_id = pool_id_for_channel(record.channel_id)
                self._client.ensure_pool(pool_id, channel_name or record.channel_id)
                self._client.add_to_pool(pool_id, post_id)
        except ArtemisError as exc:
            return IngestReport(
                video_id=record.video_id,
                outcome=IngestOutcome.FAILED,
                reason=str(exc),
                post_id=post_id,
                tags=tags,
                retryable=exc.retryable,
            )

        return IngestReport(
            video_id=record.video_id,
            outcome=IngestOutcome.INGESTED,
            reason="ingested",
            post_id=post_id,
            tags=tags,
        )

    def _upload(self, path: Path) -> str:
        return self._client.upload(path).post_id
