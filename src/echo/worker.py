"""The poll cycle — the piece that turns four modules into a worker.

One cycle, per enabled channel:

1. **resolve** a handle or URL to a ``channel_id``, once ever, then record it;
2. **poll** the feed if ``poll_interval`` has elapsed since the last poll (conditionally, so
   an unchanged feed costs a 304), and record what :mod:`echo.follow.detect` decided;
3. **fetch** whatever is due — probe, judge, download — and record the report;
4. **ingest** whatever is downloaded, then **notify**.

Three properties the loop is built around:

* **One bad channel must not stop the others.** Every step is caught per channel; the cycle
  reports failures and moves on. A worker that dies because one handle 404s is useless.
* **Work is driven by the store, not by the cycle's own discoveries.** Step 3 asks the store
  what is due, so a video found two cycles ago and deferred is picked up without the feed
  having to mention it again — and a crash mid-cycle loses nothing.
* **Nothing is marked seen until it reaches a terminal state.** A deferred livestream or a
  failed download stays owed, with the backoff the store holds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from echo.fetch.download import VideoFetcher
from echo.follow.config import Channel, EchoConfig
from echo.follow.detect import select_new
from echo.follow.feed import FeedClient, FeedError
from echo.follow.resolve import ChannelResolver, ResolveError
from echo.ingest.service import IngestService
from echo.notify.service import NotificationService, NotifyOutcome
from echo.state.schema import VideoState
from echo.state.store import EchoStore, FeedValidators

__all__ = ["CycleReport", "Worker", "WorkerClients"]

logger = logging.getLogger("echo.worker")

#: Per cycle, per channel. Keeps one channel's backlog from starving the others, and bounds
#: how long a cycle can run (a download can take minutes).
DEFAULT_FETCH_BATCH = 3
DEFAULT_INGEST_BATCH = 5


@dataclass(frozen=True, slots=True)
class CycleReport:
    """What one cycle did — the log line, and what the tests assert on."""

    polled: int = 0
    discovered: int = 0
    downloaded: int = 0
    ingested: int = 0
    notified: int = 0
    skipped_no_egress: int = 0
    failures: int = 0

    def __str__(self) -> str:
        return (
            f"polled={self.polled} discovered={self.discovered} downloaded={self.downloaded} "
            f"ingested={self.ingested} notified={self.notified} "
            f"skipped_no_egress={self.skipped_no_egress} failures={self.failures}"
        )

    @property
    def did_work(self) -> bool:
        return bool(self.discovered or self.downloaded or self.ingested)


@dataclass(frozen=True, slots=True)
class WorkerClients:
    """The collaborators, injected so a cycle is testable without a network.

    ``feed``, ``resolver`` and ``fetcher`` are factories taking "should this use the egress
    proxy?", because that is a **per-channel** decision (``proton_vpn``) while the proxy URL
    itself is process-wide.
    """

    feed: Callable[[bool], FeedClient]
    resolver: Callable[[bool], ChannelResolver]
    fetcher: Callable[[bool], VideoFetcher]
    ingest: IngestService
    notifier: NotificationService
    proxy_configured: bool = False
    allow_unproxied: bool = False


class Worker:
    """Runs poll cycles against a config and a store."""

    def __init__(
        self,
        *,
        store: EchoStore,
        config: EchoConfig,
        clients: WorkerClients,
        fetch_batch: int = DEFAULT_FETCH_BATCH,
        ingest_batch: int = DEFAULT_INGEST_BATCH,
    ) -> None:
        self._store = store
        self._config = config
        self._clients = clients
        self._fetch_batch = fetch_batch
        self._ingest_batch = ingest_batch

    def run_cycle(self, *, now: datetime | None = None) -> CycleReport:
        """One pass over every enabled channel."""
        moment = now or datetime.now(UTC)
        report = CycleReport()

        for channel in self._config.enabled_channels:
            try:
                report = self._run_channel(channel, report, now=moment)
            except Exception:
                # Deliberately broad: an unexpected failure in one channel's handling must not
                # take down a worker that still has other channels to serve.
                logger.exception("channel %s failed this cycle", channel.label)
                report = replace(report, failures=report.failures + 1)

        logger.info("cycle complete: %s", report)
        return report

    def run_forever(
        self,
        *,
        sleep: Callable[[float], None],
        should_stop: Callable[[], bool],
        cycle_seconds: float,
    ) -> CycleReport:
        """Run cycles until ``should_stop`` says otherwise, returning the running totals.

        ``sleep`` and ``should_stop`` are injected so the loop is driven in tests rather than
        waited on, and so signal handling stays in the entry point where it belongs.
        """
        totals = CycleReport()
        while not should_stop():
            report = self.run_cycle()
            totals = _add(totals, report)
            if should_stop():
                break
            sleep(cycle_seconds)
        logger.info("worker stopping: totals %s", totals)
        return totals

    # --- one channel ------------------------------------------------------------------

    def _run_channel(self, channel: Channel, report: CycleReport, *, now: datetime) -> CycleReport:
        use_proxy = channel.settings.proton_vpn

        if use_proxy and not self._clients.proxy_configured:
            if not self._clients.allow_unproxied:
                # The config asked for anonymous egress and there is none. Downloading anyway
                # would leak the home IP to YouTube — the one thing Echo exists to avoid — so
                # the channel is skipped entirely, poll included, until an operator either
                # configures ECHO_PROXY_URL or sets proton_vpn: false.
                logger.error(
                    "channel %s wants ProtonVPN egress but no proxy is configured; skipping. "
                    "Set ECHO_PROXY_URL, or proton_vpn: false for this channel, or "
                    "ECHO_ALLOW_UNPROXIED=true to accept the IP leak.",
                    channel.label,
                )
                return replace(report, skipped_no_egress=report.skipped_no_egress + 1)
            logger.warning(
                "channel %s wants ProtonVPN egress but none is configured; proceeding "
                "unproxied because ECHO_ALLOW_UNPROXIED is set — YouTube sees this host's IP",
                channel.label,
            )
            use_proxy = False

        channel_id = self._resolve(channel, use_proxy=use_proxy)
        if channel_id is None:
            return replace(report, failures=report.failures + 1)

        if self._is_due(channel_id, channel, now=now):
            report = self._poll(channel, channel_id, report, now=now, use_proxy=use_proxy)

        report = self._fetch_due(channel, channel_id, report, now=now, use_proxy=use_proxy)
        return self._ingest_due(channel, channel_id, report, now=now)

    def _resolve(self, channel: Channel, *, use_proxy: bool) -> str | None:
        """Return the channel's id, resolving and recording it the first time only."""
        if channel.channel_id:
            return self._store.upsert_channel(channel)

        known = self._store.resolved_channel_id(handle=channel.handle, url=channel.url)
        if known:
            return self._store.upsert_channel(channel, channel_id=known)

        try:
            with self._clients.resolver(use_proxy) as resolver:
                resolved = resolver.resolve(handle=channel.handle, url=channel.url)
        except ResolveError as exc:
            logger.error("cannot resolve %s: %s", channel.label, exc)
            return None

        logger.info("resolved %s to %s", channel.label, resolved)
        return self._store.upsert_channel(channel, channel_id=resolved)

    def _is_due(self, channel_id: str, channel: Channel, *, now: datetime) -> bool:
        last = self._store.last_polled_at(channel_id)
        if last is None:
            return True
        return now - last >= channel.settings.poll_interval

    def _poll(
        self,
        channel: Channel,
        channel_id: str,
        report: CycleReport,
        *,
        now: datetime,
        use_proxy: bool,
    ) -> CycleReport:
        validators = self._store.feed_validators(channel_id)
        try:
            with self._clients.feed(use_proxy) as feeds:
                result = feeds.fetch(
                    channel_id, etag=validators.etag, last_modified=validators.last_modified
                )
        except FeedError as exc:
            logger.warning("feed poll failed for %s: %s", channel.label, exc)
            return replace(report, failures=report.failures + 1)

        if result.not_modified or result.feed is None:
            # Still a poll: the interval has been served, and the validators are refreshed.
            self._store.mark_polled(
                channel_id,
                validators=FeedValidators(result.etag, result.last_modified),
                at=now,
            )
            logger.debug("feed unchanged for %s", channel.label)
            return replace(report, polled=report.polled + 1)

        selection = select_new(
            result.feed.entries,
            seen=self._store.seen_video_ids(channel_id),
            settings=channel.settings,
            first_sync=self._store.is_first_sync(channel_id),
        )
        discovered = self._store.record_entries(channel_id, selection.fetch, at=now)
        self._store.record_entries(
            channel_id,
            selection.mark_seen,
            state=VideoState.FILTERED,
            reason="not selected: outside the backfill window or title-filtered",
            at=now,
        )
        # Marked polled only after recording, so a crash in between re-polls rather than
        # silently skipping a window of uploads.
        self._store.mark_polled(
            channel_id,
            validators=FeedValidators(result.etag, result.last_modified),
            at=now,
        )

        if discovered:
            logger.info("%s: %d new upload(s)", channel.label, discovered)
        return replace(report, polled=report.polled + 1, discovered=report.discovered + discovered)

    def _fetch_due(
        self,
        channel: Channel,
        channel_id: str,
        report: CycleReport,
        *,
        now: datetime,
        use_proxy: bool,
    ) -> CycleReport:
        due = self._store.due_for_fetch(channel_id=channel_id, now=now, limit=self._fetch_batch)
        if not due:
            return report

        fetcher = self._clients.fetcher(use_proxy)
        downloaded = 0
        failures = 0

        for record in due:
            result = fetcher.fetch(
                record.video_id, channel_id=channel_id, settings=channel.settings
            )
            state = self._store.record_report(result, at=now)
            if result.succeeded:
                downloaded += 1
                logger.info("downloaded %s (%s)", record.video_id, record.title)
            elif state is VideoState.FAILED:
                failures += 1
                logger.warning("fetch failed for %s: %s", record.video_id, result.reason)
            else:
                logger.info("%s: %s", record.video_id, result.reason)

        return replace(
            report,
            downloaded=report.downloaded + downloaded,
            failures=report.failures + failures,
        )

    def _ingest_due(
        self, channel: Channel, channel_id: str, report: CycleReport, *, now: datetime
    ) -> CycleReport:
        due = self._store.due_for_ingest(channel_id=channel_id, limit=self._ingest_batch)
        if not due:
            return report

        ingested = 0
        notified = 0
        failures = 0

        for record in due:
            result = self._clients.ingest.ingest(
                record, settings=channel.settings, channel_name=channel.name
            )
            if not result.succeeded or result.post_id is None:
                failures += 1
                logger.warning("ingest failed for %s: %s", record.video_id, result.reason)
                continue

            self._store.mark_ingested(record.video_id, post_id=result.post_id, at=now)
            ingested += 1
            logger.info("ingested %s as post %s", record.video_id, result.post_id)

            # Re-read so the notification carries the stored post id and ingest timestamp
            # rather than this function's idea of them.
            stored = self._store.video(record.video_id)
            if stored is None:
                continue
            notification = self._clients.notifier.video_ingested(
                stored, settings=channel.settings, channel_name=channel.name
            )
            if notification.delivered:
                notified += 1
            elif notification.outcome is NotifyOutcome.FAILED:
                # A lost notification is not worth failing the cycle over: the video is in
                # the gallery, which is the part that matters.
                logger.warning("could not announce %s: %s", record.video_id, notification.reason)

        return replace(
            report,
            ingested=report.ingested + ingested,
            notified=report.notified + notified,
            failures=report.failures + failures,
        )


def _add(left: CycleReport, right: CycleReport) -> CycleReport:
    return CycleReport(
        polled=left.polled + right.polled,
        discovered=left.discovered + right.discovered,
        downloaded=left.downloaded + right.downloaded,
        ingested=left.ingested + right.ingested,
        notified=left.notified + right.notified,
        skipped_no_egress=left.skipped_no_egress + right.skipped_no_egress,
        failures=left.failures + right.failures,
    )
