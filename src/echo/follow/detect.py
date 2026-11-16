"""New-upload detection, including the first-sync backfill decision.

Pure functions over a feed and the set of video ids already seen. Nothing here does I/O, so
the one part of discovery with real consequences — *what gets downloaded* — is fully testable.

The output separates two outcomes deliberately:

* ``fetch`` — hand to :mod:`echo.fetch`, oldest-first so a channel's uploads are processed in
  the order they were published.
* ``mark_seen`` — record as seen **without downloading**. This is what makes
  ``backfill: 0`` ("go-forward only") work and what stops an older entry still sitting in the
  feed window from being re-fetched on the next poll.

Both lists must be recorded by the caller once the work is done. Echo errs toward re-fetching
nothing: a video recorded as seen but never downloaded is a gap a human can notice, while a
video downloaded twice silently burns bandwidth and makes a duplicate post.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from echo.follow.config import ChannelSettings
from echo.follow.feed import FeedEntry

__all__ = ["Selection", "matches_title_filters", "select_new"]


@dataclass(frozen=True, slots=True)
class Selection:
    """What a poll decided about one channel's feed."""

    fetch: tuple[FeedEntry, ...]
    mark_seen: tuple[FeedEntry, ...]

    @property
    def is_empty(self) -> bool:
        """True when the poll changed nothing — the common case for a quiet channel."""
        return not self.fetch and not self.mark_seen

    @property
    def all_entries(self) -> tuple[FeedEntry, ...]:
        """Every entry this poll accounted for, in fetch-then-skip order."""
        return self.fetch + self.mark_seen


def matches_title_filters(title: str, settings: ChannelSettings) -> bool:
    """Apply the only filters the feed can decide: ``title_include`` / ``title_exclude``.

    ``title_exclude`` wins over ``title_include`` — an explicit "never this" should not be
    overridden by a broad include pattern. Both use :func:`re.search`, so an unanchored
    pattern like ``trailer`` matches anywhere in the title, which is what a human writing
    ``title_exclude: "(?i)#shorts|trailer"`` expects.
    """
    filters = settings.filters
    if filters.title_exclude is not None and filters.title_exclude.search(title):
        return False
    if filters.title_include is None:
        return True
    return filters.title_include.search(title) is not None


def select_new(
    entries: Iterable[FeedEntry],
    *,
    seen: Collection[str],
    settings: ChannelSettings,
    first_sync: bool = False,
) -> Selection:
    """Decide what to fetch from one feed read.

    ``seen`` is the set of video ids Echo has already accounted for, from its own state.

    ``first_sync`` means this channel has no history yet, and ``backfill`` applies: the N
    most recent uploads are fetched and everything older in the feed window is marked seen,
    so subscribing to a long-running channel does not pull its whole feed window at once.
    Afterwards every unseen upload is fetched.

    Title-filtered entries are marked seen, not dropped: without recording them, every poll
    would re-evaluate (and re-log) the same rejected videos forever.
    """
    unseen = [entry for entry in entries if entry.video_id not in seen]
    # The feed is newest-first, but that is YouTube's ordering, not a guarantee — sort so
    # backfill means "most recent N" regardless of how the document happened to be ordered.
    newest_first = sorted(unseen, key=lambda entry: (entry.published, entry.video_id), reverse=True)

    if first_sync:
        candidates = newest_first[: settings.backfill]
        skipped = list(newest_first[settings.backfill :])
    else:
        candidates = newest_first
        skipped = []

    wanted: list[FeedEntry] = []
    filtered_out: list[FeedEntry] = []
    for entry in candidates:
        target = wanted if matches_title_filters(entry.title, settings) else filtered_out
        target.append(entry)

    return Selection(
        fetch=tuple(reversed(wanted)),  # oldest-first: publish order
        mark_seen=tuple(filtered_out + skipped),
    )
