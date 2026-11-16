"""The poll cycle, wired from the real modules with fakes only at the network edge.

The store, the config parser, `select_new`, the tokenizer and the reports are all real here;
only the four things that would touch YouTube, Artemis and Hermes are faked. So these tests
cover the wiring itself — which is the part that has no other home.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from echo.fetch.download import DownloadedMedia, FetchOutcome, FetchReport
from echo.follow.config import ChannelSettings, EchoConfig, parse_config
from echo.follow.feed import Feed, FeedResult, parse_feed
from echo.follow.resolve import ResolveError
from echo.ingest.service import IngestOutcome, IngestReport
from echo.notify.service import NotifyOutcome, NotifyReport
from echo.state.schema import VideoState
from echo.state.store import EchoStore, VideoRecord
from echo.worker import CycleReport, Worker, WorkerClients
from tests.conftest import CHANNEL_ID

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


# --- fakes, each a stand-in for exactly one network edge -------------------------------


class FakeFeeds:
    def __init__(self, *results: FeedResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str | None]] = []
        self.proxied: list[bool] = []

    def factory(self) -> Callable[[bool], Any]:
        def build(use_proxy: bool) -> FakeFeeds:
            self.proxied.append(use_proxy)
            return self

        return build

    def fetch(
        self, channel_id: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FeedResult:
        self.calls.append((channel_id, etag))
        if not self.results:
            return FeedResult(feed=None, not_modified=True, etag=etag)
        step = self.results.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def __enter__(self) -> FakeFeeds:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeResolver:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls = 0

    def factory(self) -> Callable[[bool], Any]:
        return lambda _use_proxy: self

    def resolve(self, *, handle: str | None = None, url: str | None = None) -> str:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def __enter__(self) -> FakeResolver:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeFetcher:
    def __init__(self, outcome: Callable[[str], FetchReport] | None = None) -> None:
        self.outcome = outcome or downloaded_report
        self.fetched: list[str] = []
        self.proxied: list[bool] = []

    def factory(self) -> Callable[[bool], Any]:
        def build(use_proxy: bool) -> FakeFetcher:
            self.proxied.append(use_proxy)
            return self

        return build

    def fetch(self, video_id: str, *, channel_id: str, settings: ChannelSettings) -> FetchReport:
        self.fetched.append(video_id)
        return self.outcome(video_id)


class FakeIngest:
    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.ingested: list[str] = []

    def ingest(
        self,
        record: VideoRecord,
        *,
        settings: ChannelSettings,
        channel_name: str | None = None,
    ) -> IngestReport:
        self.ingested.append(record.video_id)
        if not self.succeed:
            return IngestReport(
                video_id=record.video_id,
                outcome=IngestOutcome.FAILED,
                reason="artemis down",
                retryable=True,
            )
        return IngestReport(
            video_id=record.video_id,
            outcome=IngestOutcome.INGESTED,
            reason="ingested",
            post_id=f"post-{record.video_id}",
        )


class FakeNotifier:
    def __init__(self, *, outcome: NotifyOutcome = NotifyOutcome.SENT) -> None:
        self.outcome = outcome
        self.announced: list[str] = []

    def video_ingested(
        self,
        record: VideoRecord,
        *,
        settings: ChannelSettings,
        channel_name: str | None = None,
    ) -> NotifyReport:
        self.announced.append(record.video_id)
        return NotifyReport(self.outcome, "test", message_id="msg-1")


def downloaded_report(video_id: str) -> FetchReport:
    return FetchReport(
        video_id=video_id,
        outcome=FetchOutcome.DOWNLOADED,
        reason="downloaded",
        media=DownloadedMedia(
            video_id=video_id,
            path=Path(f"/nas/echo/{CHANNEL_ID}/{video_id}.mp4"),
            title="A Normal Review",
            uploader="Linus Tech Tips",
            duration=timedelta(minutes=12),
            filesize=1024,
        ),
    )


def deferred_report(video_id: str) -> FetchReport:
    return FetchReport(video_id=video_id, outcome=FetchOutcome.DEFERRED, reason="live now")


def failed_report(video_id: str) -> FetchReport:
    return FetchReport(video_id=video_id, outcome=FetchOutcome.FAILED, reason="bot check")


# --- assembly --------------------------------------------------------------------------


@pytest.fixture
def store() -> EchoStore:
    return EchoStore.open(":memory:")


def config_for(**overrides: object) -> EchoConfig:
    channel: dict[str, object] = {"id": CHANNEL_ID, "name": "Linus Tech Tips", **overrides}
    return parse_config({"version": 1, "channels": [channel]})


def worker_for(
    store: EchoStore,
    *,
    config: EchoConfig | None = None,
    feeds: FakeFeeds | None = None,
    resolver: FakeResolver | None = None,
    fetcher: FakeFetcher | None = None,
    ingest: FakeIngest | None = None,
    notifier: FakeNotifier | None = None,
    proxy_configured: bool = True,
    allow_unproxied: bool = False,
    **worker_kwargs: Any,
) -> Worker:
    feeds = feeds or FakeFeeds()
    clients = WorkerClients(
        feed=feeds.factory(),
        resolver=(resolver or FakeResolver("UC" + "a" * 22)).factory(),
        fetcher=(fetcher or FakeFetcher()).factory(),
        ingest=ingest or FakeIngest(),  # type: ignore[arg-type]
        notifier=notifier or FakeNotifier(),  # type: ignore[arg-type]
        proxy_configured=proxy_configured,
        allow_unproxied=allow_unproxied,
    )
    return Worker(store=store, config=config or config_for(), clients=clients, **worker_kwargs)


def feed_result(feed: Feed) -> FeedResult:
    return FeedResult(feed=feed, etag='W/"abc"', last_modified="Sun, 15 Nov 2026 10:00:00 GMT")


# --- a full cycle ----------------------------------------------------------------------


def test_one_cycle_polls_downloads_ingests_and_announces(store: EchoStore, feed_xml: str) -> None:
    """The whole pipeline, with the store and the real selection logic in the middle."""
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))
    fetcher = FakeFetcher()
    ingest = FakeIngest()
    notifier = FakeNotifier()

    report = worker_for(
        store, feeds=feeds, fetcher=fetcher, ingest=ingest, notifier=notifier
    ).run_cycle(now=NOW)

    assert report.polled == 1
    assert report.discovered == 3
    assert report.downloaded == 3
    assert report.ingested == 3
    assert report.notified == 3
    assert report.failures == 0
    assert len(fetcher.fetched) == 3
    assert sorted(ingest.ingested) == sorted(fetcher.fetched)
    assert sorted(notifier.announced) == sorted(ingest.ingested)


def test_the_store_ends_the_cycle_consistent(store: EchoStore, feed_xml: str) -> None:
    worker_for(store, feeds=FakeFeeds(feed_result(parse_feed(feed_xml)))).run_cycle(now=NOW)

    counts = store.counts_by_state(channel_id=CHANNEL_ID)
    assert counts[VideoState.INGESTED] == 3
    assert counts[VideoState.PENDING] == 0
    assert store.due_for_fetch(now=NOW) == ()
    assert store.due_for_ingest() == ()


def test_post_ids_are_recorded_for_the_join(store: EchoStore, feed_xml: str) -> None:
    """post_id is the join key to Artemis, since `source` cannot be stamped."""
    worker_for(store, feeds=FakeFeeds(feed_result(parse_feed(feed_xml)))).run_cycle(now=NOW)

    record = store.video("newest00001")
    assert record is not None
    assert record.post_id == "post-newest00001"
    assert record.title == "A Normal Review"  # the downloaded title replaced the feed's


def test_a_second_cycle_does_nothing_new(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))
    worker = worker_for(store, feeds=feeds)

    worker.run_cycle(now=NOW)
    second = worker.run_cycle(now=NOW + timedelta(hours=1))

    assert second.discovered == 0
    assert second.downloaded == 0
    assert second.ingested == 0


# --- polling cadence -------------------------------------------------------------------


def test_a_channel_is_not_re_polled_before_its_interval(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))
    worker = worker_for(store, feeds=feeds)

    worker.run_cycle(now=NOW)
    worker.run_cycle(now=NOW + timedelta(minutes=5))  # default poll_interval is 30m

    assert len(feeds.calls) == 1


def test_a_channel_is_re_polled_once_the_interval_elapses(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)), FeedResult(feed=None, not_modified=True))
    worker = worker_for(store, feeds=feeds)

    worker.run_cycle(now=NOW)
    report = worker.run_cycle(now=NOW + timedelta(minutes=31))

    assert len(feeds.calls) == 2
    assert report.polled == 1


def test_the_poll_interval_survives_a_restart(store: EchoStore, feed_xml: str) -> None:
    """last_polled_at is stored, so a restarted worker does not re-poll everything."""
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))
    worker_for(store, feeds=feeds).run_cycle(now=NOW)

    fresh_feeds = FakeFeeds(FeedResult(feed=None, not_modified=True))
    worker_for(store, feeds=fresh_feeds).run_cycle(now=NOW + timedelta(minutes=5))

    assert fresh_feeds.calls == []


def test_the_stored_validators_make_the_next_poll_conditional(
    store: EchoStore, feed_xml: str
) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)), FeedResult(feed=None, not_modified=True))
    worker = worker_for(store, feeds=feeds)

    worker.run_cycle(now=NOW)
    worker.run_cycle(now=NOW + timedelta(minutes=31))

    assert feeds.calls[0][1] is None
    assert feeds.calls[1][1] == 'W/"abc"'


def test_an_unchanged_feed_still_counts_as_a_poll(store: EchoStore) -> None:
    feeds = FakeFeeds(FeedResult(feed=None, not_modified=True, etag='W/"abc"'))

    report = worker_for(store, feeds=feeds).run_cycle(now=NOW)

    assert report.polled == 1
    assert report.discovered == 0
    assert store.last_polled_at(CHANNEL_ID) is not None


# --- resolution ------------------------------------------------------------------------


def test_a_handle_is_resolved_once_and_then_remembered(store: EchoStore) -> None:
    resolved = "UC" + "b" * 22
    resolver = FakeResolver(resolved)
    config = parse_config({"version": 1, "channels": [{"handle": "@veritasium"}]})
    worker = worker_for(store, config=config, resolver=resolver)

    worker.run_cycle(now=NOW)
    worker.run_cycle(now=NOW + timedelta(hours=2))

    assert resolver.calls == 1
    assert store.resolved_channel_id(handle="@veritasium") == resolved


def test_a_channel_that_cannot_be_resolved_is_a_failure_not_a_crash(
    store: EchoStore,
) -> None:
    config = parse_config({"version": 1, "channels": [{"handle": "@nosuchchannel"}]})
    worker = worker_for(
        store, config=config, resolver=FakeResolver(ResolveError("no channel id found"))
    )

    report = worker.run_cycle(now=NOW)

    assert report.failures == 1
    assert report.polled == 0


# --- isolation between channels --------------------------------------------------------


def test_one_failing_channel_does_not_stop_the_others(store: EchoStore, feed_xml: str) -> None:
    """A worker that dies because one handle 404s is useless."""
    config = parse_config(
        {
            "version": 1,
            "channels": [
                {"handle": "@broken"},
                {"id": CHANNEL_ID, "name": "Linus Tech Tips"},
            ],
        }
    )
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))

    report = worker_for(
        store,
        config=config,
        resolver=FakeResolver(ResolveError("nope")),
        feeds=feeds,
    ).run_cycle(now=NOW)

    assert report.failures == 1
    assert report.discovered == 3  # the healthy channel still ran


def test_a_feed_failure_is_isolated_to_its_channel(store: EchoStore) -> None:
    from echo.follow.feed import FeedUnavailableError

    feeds = FakeFeeds(FeedUnavailableError("HTTP 500"))

    report = worker_for(store, feeds=feeds).run_cycle(now=NOW)

    assert report.failures == 1
    assert report.polled == 0
    # Not marked polled, so the next cycle retries rather than waiting a full interval.
    assert store.last_polled_at(CHANNEL_ID) is None


# --- retryable outcomes keep their place in the queue ----------------------------------


def test_a_deferred_video_is_retried_after_its_backoff(store: EchoStore, feed_xml: str) -> None:
    fetcher = FakeFetcher(deferred_report)
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)), FeedResult(feed=None, not_modified=True))
    worker = worker_for(store, feeds=feeds, fetcher=fetcher)

    worker.run_cycle(now=NOW)
    assert len(fetcher.fetched) == 3
    assert store.counts_by_state()[VideoState.DEFERRED] == 3

    worker.run_cycle(now=NOW + timedelta(minutes=1))  # inside the backoff
    assert len(fetcher.fetched) == 3

    worker.run_cycle(now=NOW + timedelta(hours=1))  # past it
    assert len(fetcher.fetched) == 6


def test_a_failed_download_is_counted_and_left_owed(store: EchoStore, feed_xml: str) -> None:
    fetcher = FakeFetcher(failed_report)

    report = worker_for(
        store, feeds=FakeFeeds(feed_result(parse_feed(feed_xml))), fetcher=fetcher
    ).run_cycle(now=NOW)

    assert report.downloaded == 0
    assert report.failures == 3
    assert store.counts_by_state()[VideoState.FAILED] == 3
    assert store.seen_video_ids(CHANNEL_ID) == frozenset()


def test_a_failed_ingest_leaves_the_video_downloaded_for_a_retry(
    store: EchoStore, feed_xml: str
) -> None:
    ingest = FakeIngest(succeed=False)
    notifier = FakeNotifier()

    report = worker_for(
        store,
        feeds=FakeFeeds(feed_result(parse_feed(feed_xml))),
        ingest=ingest,
        notifier=notifier,
    ).run_cycle(now=NOW)

    assert report.ingested == 0
    assert report.failures == 3
    assert store.counts_by_state()[VideoState.DOWNLOADED] == 3
    assert notifier.announced == []  # nothing is watchable, so nothing is announced


def test_a_lost_notification_does_not_fail_the_cycle(store: EchoStore, feed_xml: str) -> None:
    """The video is in the gallery, which is the part that matters."""
    notifier = FakeNotifier(outcome=NotifyOutcome.FAILED)

    report = worker_for(
        store, feeds=FakeFeeds(feed_result(parse_feed(feed_xml))), notifier=notifier
    ).run_cycle(now=NOW)

    assert report.ingested == 3
    assert report.notified == 0
    assert report.failures == 0
    assert store.counts_by_state()[VideoState.INGESTED] == 3


# --- batching --------------------------------------------------------------------------


def test_downloads_are_batched_per_cycle(store: EchoStore, feed_xml: str) -> None:
    """One channel's backlog must not starve the others, and a cycle must stay bounded."""
    fetcher = FakeFetcher()
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)), FeedResult(feed=None, not_modified=True))
    worker = worker_for(store, feeds=feeds, fetcher=fetcher, fetch_batch=1)

    first = worker.run_cycle(now=NOW)
    assert first.downloaded == 1

    second = worker.run_cycle(now=NOW + timedelta(minutes=31))
    assert second.downloaded == 1
    assert len(fetcher.fetched) == 2


# --- the egress rule -------------------------------------------------------------------


def test_a_channel_wanting_the_vpn_is_skipped_when_there_is_no_proxy(
    store: EchoStore, feed_xml: str
) -> None:
    """Downloading anyway would leak the home IP — the one thing Echo exists to avoid."""
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))

    report = worker_for(store, feeds=feeds, proxy_configured=False).run_cycle(now=NOW)

    assert report.skipped_no_egress == 1
    assert report.polled == 0
    assert feeds.calls == []  # the poll is skipped too, not just the download


def test_allow_unproxied_lets_it_run_anyway(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))

    report = worker_for(store, feeds=feeds, proxy_configured=False, allow_unproxied=True).run_cycle(
        now=NOW
    )

    assert report.skipped_no_egress == 0
    assert report.discovered == 3
    assert feeds.proxied == [False]  # ran, but without a proxy


def test_a_channel_with_the_vpn_off_runs_without_a_proxy(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))
    fetcher = FakeFetcher()

    report = worker_for(
        store,
        config=config_for(proton_vpn=False),
        feeds=feeds,
        fetcher=fetcher,
        proxy_configured=False,
    ).run_cycle(now=NOW)

    assert report.skipped_no_egress == 0
    assert feeds.proxied == [False]
    assert fetcher.proxied == [False]


def test_the_proxy_is_used_for_every_edge_when_configured(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)))
    fetcher = FakeFetcher()

    worker_for(store, feeds=feeds, fetcher=fetcher, proxy_configured=True).run_cycle(now=NOW)

    assert feeds.proxied == [True]
    assert fetcher.proxied == [True]


# --- paused channels and the loop ------------------------------------------------------


def test_a_paused_channel_is_not_touched(store: EchoStore) -> None:
    feeds = FakeFeeds()
    config = config_for(enabled=False)

    report = worker_for(store, config=config, feeds=feeds).run_cycle(now=NOW)

    assert report == CycleReport()
    assert feeds.calls == []


def test_run_forever_stops_when_asked(store: EchoStore, feed_xml: str) -> None:
    feeds = FakeFeeds(
        feed_result(parse_feed(feed_xml)),
        FeedResult(feed=None, not_modified=True),
        FeedResult(feed=None, not_modified=True),
    )
    worker = worker_for(store, feeds=feeds)
    slept: list[float] = []
    cycles = {"count": 0}

    def should_stop() -> bool:
        # Stop after the second cycle, as a signal handler would.
        return cycles["count"] >= 2

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        cycles["count"] += 1

    totals = worker.run_forever(sleep=sleep, should_stop=should_stop, cycle_seconds=60)

    assert cycles["count"] == 2
    assert slept == [60, 60]
    assert totals.discovered == 3


def test_run_forever_does_not_sleep_after_a_stop_request(store: EchoStore) -> None:
    """A SIGTERM arriving during a cycle should not cost a full sleep before exiting."""
    worker = worker_for(store, feeds=FakeFeeds(FeedResult(feed=None, not_modified=True)))
    slept: list[float] = []
    state = {"stop": False}

    def should_stop() -> bool:
        if state["stop"]:
            return True
        state["stop"] = True  # the next check (after the cycle) stops it
        return False

    worker.run_forever(sleep=slept.append, should_stop=should_stop, cycle_seconds=60)

    assert slept == []


def test_cycle_reports_add_up(store: EchoStore) -> None:
    report = CycleReport(polled=1, discovered=2)

    assert "polled=1" in str(report)
    assert report.did_work is True
    assert CycleReport().did_work is False


def test_multiple_channels_are_each_handled(store: EchoStore, feed_xml: str) -> None:
    other = "UC" + "c" * 22
    config = parse_config(
        {
            "version": 1,
            "channels": [{"id": CHANNEL_ID, "name": "One"}, {"id": other, "name": "Two"}],
        }
    )
    feeds = FakeFeeds(feed_result(parse_feed(feed_xml)), FeedResult(feed=None, not_modified=True))

    report = worker_for(store, config=config, feeds=feeds).run_cycle(now=NOW)

    assert report.polled == 2
    assert [call[0] for call in feeds.calls] == [CHANNEL_ID, other]


def test_entries_outside_the_backfill_window_are_recorded_not_fetched(
    store: EchoStore, feed_xml: str
) -> None:
    """The real select_new is in the loop, so backfill applies on the first poll only."""
    fetcher = FakeFetcher()
    config = config_for(backfill=1)

    report = worker_for(
        store,
        config=config,
        feeds=FakeFeeds(feed_result(parse_feed(feed_xml))),
        fetcher=fetcher,
    ).run_cycle(now=NOW)

    assert report.discovered == 1
    assert fetcher.fetched == ["newest00001"]
    assert store.counts_by_state()[VideoState.FILTERED] == 2


def _unused(records: Sequence[VideoRecord]) -> None:  # pragma: no cover - typing aid
    return None
