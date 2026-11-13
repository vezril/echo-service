"""Echo's state store.

The tests that matter most are the ones about *not* losing or repeating work: what counts as
seen, what a retryable outcome does to the queue, and that re-recording a feed entry cannot
walk a downloaded video backwards.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from echo.fetch.download import DownloadedMedia, FetchOutcome, FetchReport
from echo.follow.config import Channel, ChannelSettings, parse_config
from echo.follow.detect import select_new
from echo.follow.feed import FeedEntry, parse_feed
from echo.state import (
    EchoStore,
    FeedValidators,
    VideoState,
    next_attempt_delay,
    open_store,
)
from echo.state.schema import RETRY_BACKOFF, SCHEMA_VERSION
from tests.conftest import CHANNEL_ID

EntryFactory = Callable[..., FeedEntry]
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> EchoStore:
    """An in-memory store with the channel already recorded."""
    store = EchoStore.open(":memory:")
    store.upsert_channel(channel_for(id=CHANNEL_ID, name="Linus Tech Tips"))
    return store


def channel_for(**overrides: object) -> Channel:
    entry: dict[str, object] = {**overrides}
    return parse_config({"version": 1, "channels": [entry]}).channels[0]


def downloaded(video_id: str, **overrides: object) -> FetchReport:
    media = DownloadedMedia(
        video_id=video_id,
        path=Path(f"/nas/echo/{CHANNEL_ID}/{video_id}.mp4"),
        title="A Normal Review",
        uploader="Linus Tech Tips",
        duration=timedelta(minutes=12),
        filesize=1024,
        **overrides,  # type: ignore[arg-type]
    )
    return FetchReport(
        video_id=video_id,
        outcome=FetchOutcome.DOWNLOADED,
        reason="downloaded",
        media=media,
    )


# --- lifecycle ------------------------------------------------------------------------


def test_opening_creates_a_migrated_store(tmp_path: Path) -> None:
    with open_store(tmp_path / "nested" / "echo.db") as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.counts_by_state()[VideoState.PENDING] == 0


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Starting an existing deployment must be a no-op, not an error."""
    path = tmp_path / "echo.db"
    with open_store(path) as first:
        first.upsert_channel(channel_for(id=CHANNEL_ID))
    with open_store(path) as second:
        second.migrate()
        assert second.is_first_sync(CHANNEL_ID) is True


# --- channels and resolution ----------------------------------------------------------


def test_a_resolved_handle_is_recorded_once(store: EchoStore) -> None:
    """This is what keeps resolution once-per-channel instead of once-per-poll."""
    resolved = "UC" + "a" * 22
    store.upsert_channel(channel_for(handle="@veritasium"), channel_id=resolved)

    assert store.resolved_channel_id(handle="@veritasium") == resolved
    assert store.resolved_channel_id(handle="@VERITASIUM") == resolved  # case-insensitive
    assert store.resolved_channel_id(handle="@unknown") is None


def test_a_url_identified_channel_resolves_too(store: EchoStore) -> None:
    resolved = "UC" + "b" * 22
    url = "https://www.youtube.com/@3blue1brown"
    store.upsert_channel(channel_for(url=url), channel_id=resolved)

    assert store.resolved_channel_id(url=url) == resolved


def test_resolution_needs_something_to_look_up(store: EchoStore) -> None:
    with pytest.raises(ValueError, match="handle or a url"):
        store.resolved_channel_id()


def test_a_channel_cannot_be_recorded_without_an_id(store: EchoStore) -> None:
    with pytest.raises(ValueError, match="resolved channel_id"):
        store.upsert_channel(channel_for(handle="@unresolved"))


def test_re_recording_a_channel_keeps_its_sync_history(store: EchoStore) -> None:
    """A renamed channel in the config must not reset what Echo has already seen."""
    store.mark_polled(CHANNEL_ID, at=NOW)
    store.upsert_channel(channel_for(id=CHANNEL_ID, name="Linus Tech Tips (renamed)"))

    assert store.is_first_sync(CHANNEL_ID) is False


# --- first sync and backfill ----------------------------------------------------------


def test_first_sync_is_true_until_a_poll_completes(store: EchoStore) -> None:
    assert store.is_first_sync(CHANNEL_ID) is True
    store.mark_polled(CHANNEL_ID, at=NOW)
    assert store.is_first_sync(CHANNEL_ID) is False


def test_first_sync_is_true_for_an_unknown_channel(store: EchoStore) -> None:
    assert store.is_first_sync("UC" + "z" * 22) is True


def test_a_later_poll_does_not_reopen_the_backfill_window(store: EchoStore) -> None:
    """backfill applies once; a restart or a second poll must not re-trigger it."""
    store.mark_polled(CHANNEL_ID, at=NOW)
    store.mark_polled(CHANNEL_ID, at=NOW + timedelta(hours=1))

    assert store.is_first_sync(CHANNEL_ID) is False


# --- conditional-GET validators -------------------------------------------------------


def test_validators_round_trip(store: EchoStore) -> None:
    store.mark_polled(
        CHANNEL_ID,
        validators=FeedValidators(etag='W/"abc"', last_modified="Thu, 12 Nov 2026 18:30:00 GMT"),
        at=NOW,
    )

    stored = store.feed_validators(CHANNEL_ID)
    assert stored.etag == 'W/"abc"'
    assert stored.last_modified == "Thu, 12 Nov 2026 18:30:00 GMT"
    assert stored.usable is True


def test_a_poll_without_new_validators_keeps_the_old_ones(store: EchoStore) -> None:
    """A 304 carries no new ETag, and discarding the old one would end conditional polling."""
    store.mark_polled(CHANNEL_ID, validators=FeedValidators(etag='W/"abc"'), at=NOW)
    store.mark_polled(CHANNEL_ID, at=NOW + timedelta(minutes=30))

    assert store.feed_validators(CHANNEL_ID).etag == 'W/"abc"'


def test_an_unknown_channel_has_no_validators(store: EchoStore) -> None:
    assert store.feed_validators("UC" + "y" * 22).usable is False


# --- recording feed entries -----------------------------------------------------------


def test_entries_are_recorded_pending_with_the_verbatim_title(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    entry = entry_factory("vid000000001", title="We Built a PC Inside a Fish Tank 🐟 (OFFICIAL)")

    assert store.record_entries(CHANNEL_ID, [entry]) == 1

    record = store.video("vid000000001")
    assert record is not None
    assert record.state is VideoState.PENDING
    assert record.title == "We Built a PC Inside a Fish Tank 🐟 (OFFICIAL)"
    assert record.source == "vid000000001"


def test_recording_nothing_is_allowed(store: EchoStore) -> None:
    assert store.record_entries(CHANNEL_ID, []) == 0


def test_re_recording_an_entry_inserts_nothing_new(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    entries = [entry_factory("vid000000001")]
    store.record_entries(CHANNEL_ID, entries)

    assert store.record_entries(CHANNEL_ID, entries) == 0


def test_re_recording_cannot_walk_a_video_backwards(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    """The feed window keeps showing a video Echo already downloaded; that must be inert."""
    entry = entry_factory("vid000000001")
    store.record_entries(CHANNEL_ID, [entry])
    store.record_report(downloaded("vid000000001"), at=NOW)

    store.record_entries(CHANNEL_ID, [entry])

    record = store.video("vid000000001")
    assert record is not None
    assert record.state is VideoState.DOWNLOADED
    assert record.file_path == Path(f"/nas/echo/{CHANNEL_ID}/vid000000001.mp4")


def test_entries_can_be_recorded_as_already_filtered(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    """follow/ hands back a mark_seen list — backfill-skipped and title-filtered entries."""
    store.record_entries(
        CHANNEL_ID,
        [entry_factory("vid000000002")],
        state=VideoState.FILTERED,
        reason="outside the backfill window",
    )

    record = store.video("vid000000002")
    assert record is not None
    assert record.state is VideoState.FILTERED
    assert record.reason == "outside the backfill window"


def test_an_unknown_video_is_none(store: EchoStore) -> None:
    assert store.video("nope00000001") is None


# --- what counts as seen --------------------------------------------------------------


def test_seen_covers_downloaded_ingested_and_filtered(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    for video_id, state in [
        ("downloaded01", VideoState.DOWNLOADED),
        ("ingested0001", VideoState.INGESTED),
        ("filtered0001", VideoState.FILTERED),
    ]:
        store.record_entries(CHANNEL_ID, [entry_factory(video_id)], state=state)

    assert store.seen_video_ids(CHANNEL_ID) == {
        "downloaded01",
        "ingested0001",
        "filtered0001",
    }


def test_deferred_and_failed_are_not_seen(store: EchoStore, entry_factory: EntryFactory) -> None:
    """Including them would strand a livestream's finished recording forever."""
    for video_id, state in [
        ("deferred0001", VideoState.DEFERRED),
        ("failed000001", VideoState.FAILED),
        ("pending00001", VideoState.PENDING),
    ]:
        store.record_entries(CHANNEL_ID, [entry_factory(video_id)], state=state)

    assert store.seen_video_ids(CHANNEL_ID) == frozenset()


def test_seen_ids_are_scoped_to_their_channel(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    other = "UC" + "c" * 22
    store.upsert_channel(channel_for(id=other))
    store.record_entries(CHANNEL_ID, [entry_factory("mine00000001")], state=VideoState.INGESTED)
    store.record_entries(other, [entry_factory("theirs000001")], state=VideoState.INGESTED)

    assert store.seen_video_ids(CHANNEL_ID) == {"mine00000001"}


# --- applying fetch reports -----------------------------------------------------------


def test_a_downloaded_report_stores_the_file_facts(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001", title="feed title")])

    assert store.record_report(downloaded("vid000000001"), at=NOW) is VideoState.DOWNLOADED

    record = store.video("vid000000001")
    assert record is not None
    assert record.filesize == 1024
    assert record.duration == timedelta(minutes=12)
    assert record.next_attempt_at is None
    # yt-dlp's title replaces the feed's when a download produced one — still verbatim.
    assert record.title == "A Normal Review"


def test_a_filtered_report_is_terminal(store: EchoStore, entry_factory: EntryFactory) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")])
    report = FetchReport(
        video_id="vid000000001",
        outcome=FetchOutcome.FILTERED,
        reason="shorter than min_duration (30s < 60s)",
    )

    assert store.record_report(report, at=NOW) is VideoState.FILTERED

    record = store.video("vid000000001")
    assert record is not None
    assert record.next_attempt_at is None
    assert record.attempts == 0  # a filter is not a failed attempt
    assert "min_duration" in (record.reason or "")
    assert "vid000000001" in store.seen_video_ids(CHANNEL_ID)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (FetchOutcome.DEFERRED, VideoState.DEFERRED),
        (FetchOutcome.FAILED, VideoState.FAILED),
    ],
)
def test_a_retryable_report_counts_the_attempt_and_backs_off(
    outcome: FetchOutcome, expected: VideoState, store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")])
    report = FetchReport(video_id="vid000000001", outcome=outcome, reason="live now")

    assert store.record_report(report, at=NOW) is expected

    record = store.video("vid000000001")
    assert record is not None
    assert record.attempts == 1
    assert record.next_attempt_at == NOW + RETRY_BACKOFF[0]
    assert "vid000000001" not in store.seen_video_ids(CHANNEL_ID)


def test_repeated_failures_back_off_further_each_time(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")])
    report = FetchReport(video_id="vid000000001", outcome=FetchOutcome.FAILED, reason="bot check")

    for attempt, expected_delay in enumerate(RETRY_BACKOFF, start=1):
        store.record_report(report, at=NOW)
        record = store.video("vid000000001")
        assert record is not None
        assert record.attempts == attempt
        assert record.next_attempt_at == NOW + expected_delay


def test_the_backoff_is_capped(store: EchoStore, entry_factory: EntryFactory) -> None:
    """A permanently-broken extractor must not schedule a retry a month out."""
    assert next_attempt_delay(99) == RETRY_BACKOFF[-1]
    assert next_attempt_delay(0) == RETRY_BACKOFF[0]


def test_a_recovered_video_clears_its_retry_schedule(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")])
    store.record_report(
        FetchReport(video_id="vid000000001", outcome=FetchOutcome.FAILED, reason="bot check"),
        at=NOW,
    )

    store.record_report(downloaded("vid000000001"), at=NOW + timedelta(hours=1))

    record = store.video("vid000000001")
    assert record is not None
    assert record.state is VideoState.DOWNLOADED
    assert record.next_attempt_at is None


def test_a_report_for_an_unrecorded_video_is_an_error(store: EchoStore) -> None:
    """Silently updating zero rows would lose the outcome of a download that happened."""
    with pytest.raises(LookupError, match="not in the store"):
        store.record_report(downloaded("neverseen001"), at=NOW)


def test_a_downloaded_report_must_carry_its_media(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")])
    report = FetchReport(
        video_id="vid000000001", outcome=FetchOutcome.DOWNLOADED, reason="downloaded"
    )

    with pytest.raises(ValueError, match="must carry its media"):
        store.record_report(report, at=NOW)


# --- the work queues ------------------------------------------------------------------


def test_the_fetch_queue_holds_what_still_owes_a_download(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(
        CHANNEL_ID,
        [
            entry_factory("pending00001", age_days=1),
            entry_factory("ingested0001", age_days=2),
        ],
    )
    store.record_report(downloaded("ingested0001"), at=NOW)
    store.mark_ingested("ingested0001", post_id="post-1", at=NOW)

    due = store.due_for_fetch(now=NOW)

    assert [record.video_id for record in due] == ["pending00001"]


def test_the_fetch_queue_is_ordered_oldest_published_first(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(
        CHANNEL_ID,
        [
            entry_factory("newest000001", age_days=0),
            entry_factory("oldest000003", age_days=3),
            entry_factory("middle000002", age_days=1),
        ],
    )

    assert [record.video_id for record in store.due_for_fetch(now=NOW)] == [
        "oldest000003",
        "middle000002",
        "newest000001",
    ]


def test_a_backed_off_video_is_held_back_until_its_time(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("deferred0001")])
    store.record_report(
        FetchReport(video_id="deferred0001", outcome=FetchOutcome.DEFERRED, reason="live now"),
        at=NOW,
    )

    assert store.due_for_fetch(now=NOW) == ()
    assert store.due_for_fetch(now=NOW + RETRY_BACKOFF[0]) != ()


def test_the_queue_can_be_scoped_and_limited(store: EchoStore, entry_factory: EntryFactory) -> None:
    other = "UC" + "d" * 22
    store.upsert_channel(channel_for(id=other))
    store.record_entries(CHANNEL_ID, [entry_factory(f"mine{i:08d}", age_days=i) for i in range(3)])
    store.record_entries(other, [entry_factory("theirs000001")])

    assert len(store.due_for_fetch(channel_id=CHANNEL_ID, now=NOW)) == 3
    assert len(store.due_for_fetch(channel_id=CHANNEL_ID, now=NOW, limit=2)) == 2
    assert len(store.due_for_fetch(now=NOW)) == 4


def test_the_ingest_queue_holds_downloaded_videos_only(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(
        CHANNEL_ID,
        [entry_factory("downloaded01", age_days=1), entry_factory("pending00001", age_days=0)],
    )
    store.record_report(downloaded("downloaded01"), at=NOW)

    due = store.due_for_ingest()

    assert [record.video_id for record in due] == ["downloaded01"]
    assert due[0].file_path is not None


def test_ingesting_records_the_post_and_empties_the_queue(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")])
    store.record_report(downloaded("vid000000001"), at=NOW)

    store.mark_ingested("vid000000001", post_id="post-42", at=NOW)

    record = store.video("vid000000001")
    assert record is not None
    assert record.state is VideoState.INGESTED
    assert record.post_id == "post-42"
    assert store.due_for_ingest() == ()


def test_ingesting_an_unknown_video_is_an_error(store: EchoStore) -> None:
    with pytest.raises(LookupError, match="not in the store"):
        store.mark_ingested("neverseen001", post_id="post-1")


# --- echo-ui's views ------------------------------------------------------------------


def test_counts_by_state_always_covers_every_state(store: EchoStore) -> None:
    counts = store.counts_by_state()

    assert set(counts) == set(VideoState)
    assert all(count == 0 for count in counts.values())


def test_counts_by_state_can_be_scoped_to_a_channel(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    other = "UC" + "e" * 22
    store.upsert_channel(channel_for(id=other))
    store.record_entries(CHANNEL_ID, [entry_factory("mine00000001")])
    store.record_entries(other, [entry_factory("theirs000001")])
    store.record_entries(other, [entry_factory("theirs000002")], state=VideoState.FILTERED)

    assert store.counts_by_state(channel_id=other)[VideoState.PENDING] == 1
    assert store.counts_by_state(channel_id=other)[VideoState.FILTERED] == 1
    assert store.counts_by_state()[VideoState.PENDING] == 2


def test_a_channels_videos_are_listed_newest_first(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(
        CHANNEL_ID,
        [
            entry_factory("oldest000003", age_days=3),
            entry_factory("newest000001", age_days=0),
        ],
    )

    listed = store.videos_for_channel(CHANNEL_ID)

    assert [record.video_id for record in listed] == ["newest000001", "oldest000003"]


def test_a_channels_videos_can_be_filtered_by_state(
    store: EchoStore, entry_factory: EntryFactory
) -> None:
    store.record_entries(CHANNEL_ID, [entry_factory("pending00001")])
    store.record_entries(CHANNEL_ID, [entry_factory("filtered0001")], state=VideoState.FILTERED)

    listed = store.videos_for_channel(CHANNEL_ID, states={VideoState.FILTERED})

    assert [record.video_id for record in listed] == ["filtered0001"]


# --- timestamps -----------------------------------------------------------------------


def test_timestamps_round_trip_as_utc_aware(store: EchoStore, entry_factory: EntryFactory) -> None:
    """A naive timestamp would compare wrong against the stale-nudge window."""
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")], at=NOW)

    record = store.video("vid000000001")
    assert record is not None
    assert record.first_seen_at == NOW
    assert record.published_at is not None
    assert record.published_at.tzinfo is not None


def test_a_naive_timestamp_is_stored_as_utc(store: EchoStore, entry_factory: EntryFactory) -> None:
    naive = datetime(2026, 11, 13, 12, 0)
    store.record_entries(CHANNEL_ID, [entry_factory("vid000000001")], at=naive)

    record = store.video("vid000000001")
    assert record is not None
    assert record.first_seen_at == NOW


# --- integration with follow/ ----------------------------------------------------------


def test_a_poll_cycle_is_idempotent(
    store: EchoStore, feed_xml: str, settings: ChannelSettings
) -> None:
    """follow/ + the store: polling the same feed twice must do the work once.

    This is the loop the worker will run, assembled here from the real pieces: parse the
    feed, select against the stored seen-set, record what was decided.
    """
    feed = parse_feed(feed_xml)

    def cycle() -> int:
        selection = select_new(
            feed.entries,
            seen=store.seen_video_ids(CHANNEL_ID),
            settings=settings,
            first_sync=store.is_first_sync(CHANNEL_ID),
        )
        inserted = store.record_entries(CHANNEL_ID, selection.fetch)
        inserted += store.record_entries(
            CHANNEL_ID, selection.mark_seen, state=VideoState.FILTERED, reason="not backfilled"
        )
        store.mark_polled(CHANNEL_ID, at=NOW)
        return inserted

    assert cycle() == 3  # the fixture feed has three entries, backfill default is 3
    assert cycle() == 0  # nothing new on a second poll of the same feed
