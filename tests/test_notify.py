"""Notifications: the payloads, the broker client, and when Echo stays quiet.

The suppression tests carry the most weight. The stale nudge depends on watched-state that
does not exist, and the rule being protected is that Echo would rather say nothing than send a
nudge about videos that may already have been watched.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from echo.follow.config import ChannelSettings
from echo.notify.events import StaleUnwatched, VideoIngested
from echo.notify.hermes import CORRELATION_HEADER, HermesClient, HermesError
from echo.notify.service import (
    NotificationService,
    NotifyOutcome,
    UnknownWatchedState,
    stale_candidates,
)
from echo.state.schema import VideoState
from echo.state.store import VideoRecord
from tests.conftest import CHANNEL_ID

SettingsFactory = Callable[..., ChannelSettings]
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def record_for(**overrides: object) -> VideoRecord:
    base: dict[str, object] = {
        "video_id": "dQw4w9WgXcQ",
        "channel_id": CHANNEL_ID,
        "title": "We Built a PC Inside a Fish Tank 🐟",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "state": VideoState.INGESTED,
        "uploader": "Linus Tech Tips",
        "post_id": "post-1",
        "duration": timedelta(minutes=12),
        "filesize": 1024,
        "file_path": Path("/nas/echo/x.mp4"),
        "updated_at": NOW,
    }
    return VideoRecord(**{**base, **overrides})  # type: ignore[arg-type]


class Watched:
    """A watched-state implementation for the day Artemis has one."""

    def __init__(self, unwatched_ids: set[str] | None) -> None:
        self._unwatched = unwatched_ids

    def unwatched(self, post_ids: Collection[str]) -> set[str] | None:
        if self._unwatched is None:
            return None
        return {post_id for post_id in post_ids if post_id in self._unwatched}


class FakeHermes:
    """Answers like hermesmq's REST routes and records what it was sent."""

    def __init__(self, *, fail: dict[str, httpx.Response] | None = None) -> None:
        self.calls: list[str] = []
        self.published: list[dict[str, object]] = []
        self.headers: list[httpx.Headers] = []
        self._fail = fail or {}
        self._seen_keys: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            route = f"{request.method} {request.url.path}"
            self.calls.append(route)
            self.headers.append(request.headers)
            if route in self._fail:
                return self._fail[route]
            if request.url.path == "/v1/topics":
                return httpx.Response(201, json={"topicId": "t", "labels": {}})
            body = json.loads(request.content)
            self.published.append(body)
            key = body.get("idempotencyKey")
            duplicate = bool(key) and key in self._seen_keys
            if key:
                self._seen_keys.add(key)
            return httpx.Response(202, json={"messageId": "msg-1", "deduplicated": duplicate})

        return httpx.MockTransport(handler)

    def client(self) -> HermesClient:
        return HermesClient(
            "http://hermes.svc:8080",
            client=httpx.Client(transport=self.transport(), base_url="http://hermes.svc:8080"),
        )


def service_for(fake: FakeHermes, **kwargs: object) -> NotificationService:
    return NotificationService(fake.client(), **kwargs)  # type: ignore[arg-type]


# --- payloads --------------------------------------------------------------------------


def test_the_ingested_payload_is_flat_snake_case_json() -> None:
    """Shaped so promoting it into the Lexicon is a rename-free move."""
    event = VideoIngested(
        video_id="dQw4w9WgXcQ",
        post_id="post-1",
        channel_id=CHANNEL_ID,
        channel_name="Linus Tech Tips",
        title="Fish Tank PC",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        duration_seconds=742.0,
        ingested_at=NOW,
    )

    payload = json.loads(event.to_json())

    assert payload["event"] == "video.ingested"
    assert payload["video_id"] == "dQw4w9WgXcQ"
    assert payload["post_id"] == "post-1"
    assert payload["duration_seconds"] == 742.0
    assert payload["ingested_at"] == "2026-11-16T12:00:00+00:00"
    assert all("-" not in key for key in payload)


def test_absent_optional_fields_are_omitted_not_null() -> None:
    """Protobuf canonical JSON omits them, so the shape matches ahead of time."""
    payload = json.loads(
        VideoIngested(
            video_id="v",
            post_id="p",
            channel_id="c",
            title="t",
            url="u",
        ).to_json()
    )

    for absent in ("channel_name", "uploader", "duration_seconds", "file_size_bytes"):
        assert absent not in payload


def test_titles_keep_their_non_ascii_characters() -> None:
    """The verbatim title is the point; \\uXXXX escapes would make it unreadable."""
    event = VideoIngested(
        video_id="v", post_id="p", channel_id="c", title="Café 🐟 日本語", url="u"
    )

    assert "Café 🐟 日本語" in event.to_json()


def test_a_naive_timestamp_is_rendered_as_utc() -> None:
    event = VideoIngested(
        video_id="v",
        post_id="p",
        channel_id="c",
        title="t",
        url="u",
        ingested_at=datetime(2026, 11, 16, 12, 0),
    )

    assert json.loads(event.to_json())["ingested_at"] == "2026-11-16T12:00:00+00:00"


def test_the_ingested_idempotency_key_is_per_video() -> None:
    event = VideoIngested(video_id="abc", post_id="p", channel_id="c", title="t", url="u")

    assert event.idempotency_key == "video.ingested:abc"


def test_the_nudge_idempotency_key_is_per_channel_per_day() -> None:
    """Stops a 30-minute loop from nudging 48 times about the same backlog."""
    nudge = StaleUnwatched(
        channel_id=CHANNEL_ID, video_count=3, stale_after=timedelta(days=14), as_of=NOW
    )

    assert nudge.idempotency_key == f"video.stale:{CHANNEL_ID}:2026-11-16"

    later_same_day = StaleUnwatched(
        channel_id=CHANNEL_ID,
        video_count=4,
        stale_after=timedelta(days=14),
        as_of=NOW + timedelta(hours=6),
    )
    assert later_same_day.idempotency_key == nudge.idempotency_key


def test_the_nudge_payload_aggregates_the_channel() -> None:
    nudge = StaleUnwatched(
        channel_id=CHANNEL_ID,
        channel_name="Veritasium",
        video_count=4,
        stale_after=timedelta(days=14),
        video_ids=("a", "b", "c", "d"),
        titles=("One", "Two"),
        oldest_ingested_at=NOW - timedelta(days=30),
        as_of=NOW,
    )

    payload = json.loads(nudge.to_json())

    assert payload["video_count"] == 4
    assert payload["stale_after_seconds"] == 14 * 86400
    assert payload["video_ids"] == ["a", "b", "c", "d"]
    assert payload["titles"] == ["One", "Two"]


def test_attributes_let_a_consumer_filter_without_parsing() -> None:
    event = VideoIngested(video_id="v", post_id="p", channel_id=CHANNEL_ID, title="t", url="u")

    assert event.attributes == {
        "event": "video.ingested",
        "channel_id": CHANNEL_ID,
        "video_id": "v",
    }


# --- the broker client -----------------------------------------------------------------


def test_publishing_sends_a_plain_string_payload_and_the_producer_id() -> None:
    """The route does payload.getBytes(UTF_8) — a string, never base64."""
    fake = FakeHermes()

    with fake.client() as client:
        published = client.publish("video.ingested", '{"event":"video.ingested"}')

    assert published.message_id == "msg-1"
    assert fake.calls == ["POST /v1/topics/video.ingested/messages"]
    assert fake.published[0]["payload"] == '{"event":"video.ingested"}'
    assert fake.published[0]["producerId"] == "echo-service"


def test_the_correlation_id_rides_the_header_not_the_body() -> None:
    """REST has no correlationId field; the route adopts X-Correlation-Id and never mints one."""
    fake = FakeHermes()

    with fake.client() as client:
        client.publish("t", "{}", correlation_id="corr-42")

    assert fake.headers[0][CORRELATION_HEADER.lower()] == "corr-42"
    assert "correlationId" not in fake.published[0]


def test_no_correlation_header_is_sent_when_there_is_none() -> None:
    fake = FakeHermes()

    with fake.client() as client:
        client.publish("t", "{}")

    assert CORRELATION_HEADER.lower() not in fake.headers[0]


def test_an_existing_topic_is_success() -> None:
    fake = FakeHermes(fail={"POST /v1/topics": httpx.Response(409, text="exists")})

    with fake.client() as client:
        client.ensure_topic("video.ingested")  # must not raise


def test_a_missing_topic_on_publish_is_retryable() -> None:
    """404 means the topic is gone; the next cycle creates it."""
    fake = FakeHermes(
        fail={"POST /v1/topics/t/messages": httpx.Response(404, text="topic not found")}
    )

    with fake.client() as client, pytest.raises(HermesError) as caught:
        client.publish("t", "{}")

    assert caught.value.retryable is True


def test_a_bad_body_is_not_retryable() -> None:
    fake = FakeHermes(fail={"POST /v1/topics/t/messages": httpx.Response(400, text="bad")})

    with fake.client() as client, pytest.raises(HermesError) as caught:
        client.publish("t", "{}")

    assert caught.value.retryable is False


def test_a_broker_503_is_retryable() -> None:
    fake = FakeHermes(fail={"POST /v1/topics/t/messages": httpx.Response(503)})

    with fake.client() as client, pytest.raises(HermesError) as caught:
        client.publish("t", "{}")

    assert caught.value.retryable is True


def test_a_transport_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("broker down", request=request)

    client = HermesClient(
        "http://hermes.svc:8080",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hermes.svc:8080"
        ),
    )
    with client, pytest.raises(HermesError, match="did not reach Hermes") as caught:
        client.publish("t", "{}")

    assert caught.value.retryable is True


# --- the available notification ---------------------------------------------------------


def test_an_ingested_video_is_announced(settings: ChannelSettings) -> None:
    fake = FakeHermes()

    report = service_for(fake).video_ingested(
        record_for(), settings=settings, channel_name="Linus Tech Tips"
    )

    assert report.outcome is NotifyOutcome.SENT
    assert report.delivered is True
    assert report.message_id == "msg-1"
    payload = json.loads(str(fake.published[0]["payload"]))
    assert payload["event"] == "video.ingested"
    assert payload["post_id"] == "post-1"
    assert payload["channel_name"] == "Linus Tech Tips"


def test_the_topic_is_created_once_per_process(settings: ChannelSettings) -> None:
    fake = FakeHermes()
    service = service_for(fake)

    service.video_ingested(record_for(), settings=settings)
    service.video_ingested(record_for(video_id="second00001"), settings=settings)

    assert fake.calls.count("POST /v1/topics") == 1


def test_a_repeated_publish_is_reported_as_deduplicated(settings: ChannelSettings) -> None:
    """The idempotency key makes a retried notification safe."""
    fake = FakeHermes()
    service = service_for(fake)

    first = service.video_ingested(record_for(), settings=settings)
    second = service.video_ingested(record_for(), settings=settings)

    assert first.outcome is NotifyOutcome.SENT
    assert second.outcome is NotifyOutcome.DEDUPLICATED
    assert second.delivered is True


def test_on_available_false_suppresses_the_announcement(
    settings_for: SettingsFactory,
) -> None:
    fake = FakeHermes()

    report = service_for(fake).video_ingested(
        record_for(), settings=settings_for(notify={"on_available": False})
    )

    assert report.outcome is NotifyOutcome.SUPPRESSED
    assert fake.calls == []


def test_a_video_without_a_post_is_not_announced(settings: ChannelSettings) -> None:
    """Nothing is watchable yet, so there is nothing to announce."""
    report = service_for(FakeHermes()).video_ingested(record_for(post_id=None), settings=settings)

    assert report.outcome is NotifyOutcome.SUPPRESSED
    assert "no Artemis post" in report.reason


def test_a_publish_failure_is_reported_as_retryable(settings: ChannelSettings) -> None:
    fake = FakeHermes(fail={"POST /v1/topics/video.ingested/messages": httpx.Response(503)})

    report = service_for(fake).video_ingested(record_for(), settings=settings)

    assert report.outcome is NotifyOutcome.FAILED
    assert report.retryable is True
    assert report.delivered is False


# --- the stale window -------------------------------------------------------------------


def test_stale_candidates_are_those_past_the_window(settings: ChannelSettings) -> None:
    records = [
        record_for(video_id="old000000001", updated_at=NOW - timedelta(days=30)),
        record_for(video_id="edge00000002", updated_at=NOW - timedelta(days=14)),
        record_for(video_id="fresh0000003", updated_at=NOW - timedelta(days=1)),
    ]

    stale = stale_candidates(records, settings=settings, now=NOW)

    assert [record.video_id for record in stale] == ["old000000001", "edge00000002"]


def test_stale_candidates_are_oldest_first(settings: ChannelSettings) -> None:
    records = [
        record_for(video_id="newer0000001", updated_at=NOW - timedelta(days=20)),
        record_for(video_id="older0000002", updated_at=NOW - timedelta(days=40)),
    ]

    stale = stale_candidates(records, settings=settings, now=NOW)

    assert [record.video_id for record in stale] == ["older0000002", "newer0000001"]


def test_a_null_stale_after_disables_the_window(settings_for: SettingsFactory) -> None:
    records = [record_for(updated_at=NOW - timedelta(days=365))]

    assert (
        stale_candidates(records, settings=settings_for(notify={"stale_after": None}), now=NOW)
        == ()
    )


def test_a_per_channel_window_is_honoured(settings_for: SettingsFactory) -> None:
    records = [record_for(updated_at=NOW - timedelta(days=20))]

    assert (
        stale_candidates(records, settings=settings_for(notify={"stale_after": "30d"}), now=NOW)
        == ()
    )
    assert (
        stale_candidates(records, settings=settings_for(notify={"stale_after": "7d"}), now=NOW)
        != ()
    )


def test_videos_without_a_post_are_not_stale_candidates(settings: ChannelSettings) -> None:
    """Never ingested means never watchable — it is a download problem, not a backlog one."""
    records = [record_for(post_id=None, updated_at=NOW - timedelta(days=30))]

    assert stale_candidates(records, settings=settings, now=NOW) == ()


# --- the nudge, and refusing to guess ---------------------------------------------------


def test_the_nudge_is_suppressed_without_watched_state(settings: ChannelSettings) -> None:
    """Today's reality: a nudge about maybe-watched videos is worse than none."""
    fake = FakeHermes()
    records = [record_for(updated_at=NOW - timedelta(days=30))]

    report = service_for(fake).stale_unwatched(
        records, settings=settings, channel_id=CHANNEL_ID, now=NOW
    )

    assert report.outcome is NotifyOutcome.SUPPRESSED
    assert "no watched-state source" in report.reason
    assert fake.calls == []


def test_unknown_watched_state_never_claims_to_know() -> None:
    assert UnknownWatchedState().unwatched(["post-1"]) is None


def test_the_nudge_fires_once_watched_state_exists(settings: ChannelSettings) -> None:
    """The day Artemis exposes a viewed flag, this lights up by injection alone."""
    fake = FakeHermes()
    records = [
        record_for(video_id="unwatched001", post_id="post-1", updated_at=NOW - timedelta(days=30)),
        record_for(video_id="watched00002", post_id="post-2", updated_at=NOW - timedelta(days=20)),
    ]
    service = service_for(fake, watched_state=Watched({"post-1"}))

    report = service.stale_unwatched(
        records, settings=settings, channel_id=CHANNEL_ID, channel_name="LTT", now=NOW
    )

    assert report.outcome is NotifyOutcome.SENT
    payload = json.loads(str(fake.published[0]["payload"]))
    assert payload["event"] == "video.stale"
    assert payload["video_count"] == 1
    assert payload["video_ids"] == ["unwatched001"]
    assert payload["channel_name"] == "LTT"


def test_the_nudge_is_suppressed_when_everything_is_watched(
    settings: ChannelSettings,
) -> None:
    fake = FakeHermes()
    records = [record_for(updated_at=NOW - timedelta(days=30))]

    report = service_for(fake, watched_state=Watched(set())).stale_unwatched(
        records, settings=settings, channel_id=CHANNEL_ID, now=NOW
    )

    assert report.outcome is NotifyOutcome.SUPPRESSED
    assert "watched" in report.reason
    assert fake.calls == []


def test_the_nudge_is_suppressed_when_nothing_is_stale_yet(
    settings: ChannelSettings,
) -> None:
    """Checked before watched-state, so a quiet channel costs no Artemis call either."""
    fake = FakeHermes()
    records = [record_for(updated_at=NOW - timedelta(days=1))]

    report = service_for(fake, watched_state=Watched({"post-1"})).stale_unwatched(
        records, settings=settings, channel_id=CHANNEL_ID, now=NOW
    )

    assert report.outcome is NotifyOutcome.SUPPRESSED
    assert "nothing past the stale window" in report.reason


def test_a_null_window_suppresses_the_nudge_for_that_channel(
    settings_for: SettingsFactory,
) -> None:
    report = service_for(FakeHermes(), watched_state=Watched({"post-1"})).stale_unwatched(
        [record_for(updated_at=NOW - timedelta(days=365))],
        settings=settings_for(notify={"stale_after": None}),
        channel_id=CHANNEL_ID,
        now=NOW,
    )

    assert report.outcome is NotifyOutcome.SUPPRESSED
    assert "stale_after is null" in report.reason


def test_the_nudge_names_only_a_handful_of_titles(settings: ChannelSettings) -> None:
    """A nudge listing twenty titles is a wall of text, not a prompt."""
    fake = FakeHermes()
    records = [
        record_for(
            video_id=f"video{i:07d}", post_id=f"post-{i}", updated_at=NOW - timedelta(days=30)
        )
        for i in range(20)
    ]
    service = service_for(fake, watched_state=Watched({f"post-{i}" for i in range(20)}))

    service.stale_unwatched(records, settings=settings, channel_id=CHANNEL_ID, now=NOW)

    payload = json.loads(str(fake.published[0]["payload"]))
    assert payload["video_count"] == 20
    assert len(payload["titles"]) == 5
    assert len(payload["video_ids"]) == 20  # the ids are complete; the titles are a sample
