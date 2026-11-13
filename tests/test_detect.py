"""New-upload detection: the decision that spends bandwidth, so it gets the most tests."""

from __future__ import annotations

from collections.abc import Callable

from echo.follow.config import ChannelSettings
from echo.follow.detect import matches_title_filters, select_new
from echo.follow.feed import FeedEntry, parse_feed

EntryFactory = Callable[..., FeedEntry]
SettingsFactory = Callable[..., ChannelSettings]


def test_an_unseen_upload_is_fetched(
    entry_factory: EntryFactory, settings: ChannelSettings
) -> None:
    entries = [entry_factory("vid000000001")]
    selection = select_new(entries, seen=set(), settings=settings)

    assert [e.video_id for e in selection.fetch] == ["vid000000001"]
    assert selection.mark_seen == ()


def test_already_seen_uploads_are_neither_fetched_nor_re_recorded(
    entry_factory: EntryFactory, settings: ChannelSettings
) -> None:
    entries = [entry_factory("old000000001"), entry_factory("new000000002", age_days=1)]
    selection = select_new(entries, seen={"old000000001", "new000000002"}, settings=settings)

    assert selection.is_empty


def test_fetching_is_ordered_oldest_first(
    entry_factory: EntryFactory, settings: ChannelSettings
) -> None:
    """A channel's uploads should be processed in the order they were published."""
    entries = [
        entry_factory("newest000001", age_days=0),
        entry_factory("middle000002", age_days=1),
        entry_factory("oldest000003", age_days=2),
    ]
    selection = select_new(entries, seen=set(), settings=settings)

    assert [e.video_id for e in selection.fetch] == [
        "oldest000003",
        "middle000002",
        "newest000001",
    ]


def test_ordering_does_not_depend_on_the_documents_order(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    """Backfill means 'most recent N' even if YouTube stops ordering the feed newest-first."""
    entries = [
        entry_factory("oldest000003", age_days=2),
        entry_factory("newest000001", age_days=0),
        entry_factory("middle000002", age_days=1),
    ]
    selection = select_new(entries, seen=set(), settings=settings_for(backfill=1), first_sync=True)

    assert [e.video_id for e in selection.fetch] == ["newest000001"]
    assert {e.video_id for e in selection.mark_seen} == {"middle000002", "oldest000003"}


def test_first_sync_backfills_only_the_n_most_recent(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    entries = [entry_factory(f"video{i:07d}", age_days=i) for i in range(6)]
    selection = select_new(entries, seen=set(), settings=settings_for(backfill=2), first_sync=True)

    assert [e.video_id for e in selection.fetch] == ["video0000001", "video0000000"]
    assert len(selection.mark_seen) == 4
    # Everything in the window is accounted for, so the next poll re-fetches nothing.
    assert len(selection.all_entries) == 6


def test_backfill_zero_is_go_forward_only(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    entries = [entry_factory(f"video{i:07d}", age_days=i) for i in range(3)]
    selection = select_new(entries, seen=set(), settings=settings_for(backfill=0), first_sync=True)

    assert selection.fetch == ()
    assert len(selection.mark_seen) == 3


def test_backfill_larger_than_the_feed_window_takes_what_exists(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    entries = [entry_factory(f"video{i:07d}", age_days=i) for i in range(3)]
    selection = select_new(entries, seen=set(), settings=settings_for(backfill=50), first_sync=True)

    assert len(selection.fetch) == 3
    assert selection.mark_seen == ()


def test_after_the_first_sync_backfill_no_longer_limits_anything(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    entries = [entry_factory(f"video{i:07d}", age_days=i) for i in range(4)]
    selection = select_new(entries, seen=set(), settings=settings_for(backfill=1))

    assert len(selection.fetch) == 4


def test_a_second_poll_of_the_same_feed_is_a_no_op(
    entry_factory: EntryFactory, settings: ChannelSettings
) -> None:
    """Recording what the first poll accounted for must make the second poll idle."""
    entries = [entry_factory(f"video{i:07d}", age_days=i) for i in range(3)]

    first = select_new(entries, seen=set(), settings=settings, first_sync=True)
    seen = {entry.video_id for entry in first.all_entries}

    assert select_new(entries, seen=seen, settings=settings).is_empty


def test_title_excluded_entries_are_recorded_not_dropped(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    """Without recording them, every poll would re-evaluate the same rejects forever."""
    settings = settings_for(filters={"title_exclude": "(?i)#shorts|trailer"})
    entries = [
        entry_factory("keepme000001", title="A Normal Review", age_days=1),
        entry_factory("shorts000002", title="#shorts quick tip", age_days=0),
    ]

    selection = select_new(entries, seen=set(), settings=settings)

    assert [e.video_id for e in selection.fetch] == ["keepme000001"]
    assert [e.video_id for e in selection.mark_seen] == ["shorts000002"]


def test_title_include_narrows_to_matching_titles(
    entry_factory: EntryFactory, settings_for: SettingsFactory
) -> None:
    settings = settings_for(filters={"title_include": "(?i)review"})
    entries = [
        entry_factory("review000001", title="GPU Review", age_days=1),
        entry_factory("vlog00000002", title="Office vlog", age_days=0),
    ]

    selection = select_new(entries, seen=set(), settings=settings)

    assert [e.video_id for e in selection.fetch] == ["review000001"]
    assert [e.video_id for e in selection.mark_seen] == ["vlog00000002"]


def test_exclude_beats_include(settings_for: SettingsFactory) -> None:
    """An explicit 'never this' must not be overridden by a broad include."""
    settings = settings_for(filters={"title_include": "(?i)review", "title_exclude": "(?i)trailer"})
    assert matches_title_filters("GPU Review", settings) is True
    assert matches_title_filters("Review: the trailer", settings) is False


def test_filters_are_unanchored_substring_searches(settings_for: SettingsFactory) -> None:
    settings = settings_for(filters={"title_exclude": "trailer"})
    assert matches_title_filters("Here is the trailer, folks", settings) is False


def test_no_filters_means_everything_matches(settings: ChannelSettings) -> None:
    assert matches_title_filters("literally anything", settings) is True


def test_selection_runs_against_a_real_parsed_feed(
    feed_xml: str, settings_for: SettingsFactory
) -> None:
    """End-to-end over the parser: the shorts entry is filtered, the rest are fetched."""
    feed = parse_feed(feed_xml)
    settings = settings_for(filters={"title_exclude": "(?i)#shorts"})

    selection = select_new(feed.entries, seen={"oldest00003"}, settings=settings)

    assert [e.video_id for e in selection.fetch] == ["newest00001"]
    assert [e.video_id for e in selection.mark_seen] == ["middle00002"]
