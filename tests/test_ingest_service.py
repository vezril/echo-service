"""The hand-off, end to end against a faked Artemis.

The property worth protecting is idempotence: every step Echo makes is safe to repeat, so a
failure halfway through needs no compensation — just another cycle.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from echo.follow.config import ChannelSettings
from echo.ingest.artemis import ArtemisClient
from echo.ingest.service import IngestOutcome, IngestService, pool_id_for_channel
from echo.state.schema import VideoState
from echo.state.store import VideoRecord
from tests.conftest import CHANNEL_ID

SettingsFactory = Callable[..., ChannelSettings]


class FakeArtemis:
    """Records calls and answers like the real routes do.

    ``fail`` is keyed by the exact ``"<METHOD> <path>"`` route, so failing the pool *create*
    cannot accidentally also fail the pool *append*.
    """

    def __init__(self, *, fail: dict[str, httpx.Response] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[str] = []
        self._fail = fail or {}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            route = f"{request.method} {request.url.path}"
            self.calls.append((request.method, request.url.path))
            self.bodies.append(request.content.decode(errors="replace"))
            if route in self._fail:
                return self._fail[route]
            if request.url.path == "/uploads":
                return httpx.Response(201, json={"postId": "post-1", "status": "pending"})
            if request.url.path == "/pools" and request.method == "POST":
                return httpx.Response(201, json={"poolId": CHANNEL_ID, "status": "active"})
            return httpx.Response(200)

        return httpx.MockTransport(handler)

    @property
    def paths(self) -> list[str]:
        return [path for _, path in self.calls]


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    path = tmp_path / CHANNEL_ID / "dQw4w9WgXcQ.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"video bytes")
    return path


def record_for(video_file: Path | None, **overrides: object) -> VideoRecord:
    base: dict[str, object] = {
        "video_id": "dQw4w9WgXcQ",
        "channel_id": CHANNEL_ID,
        "title": "We Built a PC Inside a Fish Tank (OFFICIAL VIDEO)",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "state": VideoState.DOWNLOADED,
        "uploader": "Linus Tech Tips",
        "file_path": video_file,
    }
    return VideoRecord(**{**base, **overrides})  # type: ignore[arg-type]


def service_for(fake: FakeArtemis) -> IngestService:
    return IngestService(
        ArtemisClient(
            "http://artemis.svc:8080",
            client=httpx.Client(transport=fake.transport(), base_url="http://artemis.svc:8080"),
        )
    )


# --- the happy path ---------------------------------------------------------------------


def test_a_downloaded_video_is_uploaded_tagged_and_pooled(
    video_file: Path, settings: ChannelSettings
) -> None:
    fake = FakeArtemis()

    report = service_for(fake).ingest(
        record_for(video_file), settings=settings, channel_name="Linus Tech Tips"
    )

    assert report.outcome is IngestOutcome.INGESTED
    assert report.succeeded is True
    assert report.post_id == "post-1"
    assert fake.paths == [
        "/uploads",
        "/posts/post-1/tags",
        "/pools",
        f"/pools/{CHANNEL_ID}/posts",
    ]


def test_the_tag_set_carries_the_uploader_and_the_title(
    video_file: Path, settings: ChannelSettings
) -> None:
    report = service_for(FakeArtemis()).ingest(record_for(video_file), settings=settings)

    assert report.tags[0] == "linus_tech_tips"
    assert set(report.tags) >= {"built", "pc", "inside", "fish", "tank"}
    assert "official" not in report.tags  # boilerplate never reaches the vocabulary


def test_extra_tags_from_the_channel_config_are_stamped(
    video_file: Path, settings_for: SettingsFactory
) -> None:
    settings = settings_for(catalog={"extra_tags": ["hardware", "tech"]})

    report = service_for(FakeArtemis()).ingest(record_for(video_file), settings=settings)

    assert {"hardware", "tech"} <= set(report.tags)


def test_the_pool_id_is_the_channel_id(video_file: Path, settings: ChannelSettings) -> None:
    """Derivable rather than stored: Echo never has to remember the mapping."""
    fake = FakeArtemis()

    service_for(fake).ingest(record_for(video_file), settings=settings)

    assert pool_id_for_channel(CHANNEL_ID) == CHANNEL_ID
    assert f"/pools/{CHANNEL_ID}/posts" in fake.paths


def test_the_pool_is_named_after_the_channel(video_file: Path, settings: ChannelSettings) -> None:
    fake = FakeArtemis()

    service_for(fake).ingest(
        record_for(video_file), settings=settings, channel_name="Linus Tech Tips"
    )

    created = json.loads(fake.bodies[fake.paths.index("/pools")])
    assert created["name"] == "Linus Tech Tips"


def test_an_unnamed_channel_still_gets_a_pool(video_file: Path, settings: ChannelSettings) -> None:
    """A pool must have a name; the channel id is a worse name than none is an option."""
    fake = FakeArtemis()

    service_for(fake).ingest(record_for(video_file), settings=settings, channel_name=None)

    created = json.loads(fake.bodies[fake.paths.index("/pools")])
    assert created["name"] == CHANNEL_ID


# --- the config switches ----------------------------------------------------------------


def test_pool_per_channel_false_skips_both_pool_calls(
    video_file: Path, settings_for: SettingsFactory
) -> None:
    fake = FakeArtemis()
    settings = settings_for(catalog={"pool_per_channel": False})

    service_for(fake).ingest(record_for(video_file), settings=settings)

    assert fake.paths == ["/uploads", "/posts/post-1/tags"]


def test_uploader_as_artist_false_drops_only_the_uploader_tag(
    video_file: Path, settings_for: SettingsFactory
) -> None:
    settings = settings_for(catalog={"uploader_as_artist": False, "extra_tags": ["hardware"]})

    report = service_for(FakeArtemis()).ingest(record_for(video_file), settings=settings)

    assert "linus_tech_tips" not in report.tags
    assert "hardware" in report.tags  # extra_tags is unconditional
    assert "fish" in report.tags


# --- failure handling -------------------------------------------------------------------


def test_a_record_without_a_file_fails_without_calling_artemis(
    settings: ChannelSettings,
) -> None:
    fake = FakeArtemis()

    report = service_for(fake).ingest(record_for(None), settings=settings)

    assert report.outcome is IngestOutcome.FAILED
    assert "no downloaded file" in report.reason
    assert fake.calls == []


def test_a_failed_upload_reports_no_post_id(video_file: Path, settings: ChannelSettings) -> None:
    fake = FakeArtemis(fail={"POST /uploads": httpx.Response(502, json={"error": "apollo down"})})

    report = service_for(fake).ingest(record_for(video_file), settings=settings)

    assert report.outcome is IngestOutcome.FAILED
    assert report.post_id is None
    assert report.retryable is True
    assert "apollo down" in report.reason


def test_a_failure_after_the_upload_keeps_the_post_id(
    video_file: Path, settings: ChannelSettings
) -> None:
    """The post exists; reporting its id makes the failure legible in echo-ui."""
    fake = FakeArtemis(
        fail={"PATCH /posts/post-1/tags": httpx.Response(503, json={"error": "restarting"})}
    )

    report = service_for(fake).ingest(record_for(video_file), settings=settings)

    assert report.outcome is IngestOutcome.FAILED
    assert report.post_id == "post-1"
    assert report.retryable is True
    assert fake.paths == ["/uploads", "/posts/post-1/tags"]  # stopped at the failure


def test_a_rejected_tag_is_reported_as_not_retryable(
    video_file: Path, settings: ChannelSettings
) -> None:
    fake = FakeArtemis(
        fail={"PATCH /posts/post-1/tags": httpx.Response(400, json={"error": "invalid tag"})}
    )

    report = service_for(fake).ingest(record_for(video_file), settings=settings)

    assert report.retryable is False


def test_an_existing_pool_does_not_fail_the_ingest(
    video_file: Path, settings: ChannelSettings
) -> None:
    """409 on pool create is the steady state, not an error."""
    fake = FakeArtemis(
        fail={"POST /pools": httpx.Response(409, json={"error": "pool already exists"})}
    )

    report = service_for(fake).ingest(record_for(video_file), settings=settings)

    assert report.succeeded is True


# --- idempotence ------------------------------------------------------------------------


def test_ingesting_twice_is_safe(video_file: Path, settings: ChannelSettings) -> None:
    """Upload dedups on md5, the tags PATCH is a replace, and both pool calls are no-ops."""
    fake = FakeArtemis()
    service = service_for(fake)

    first = service.ingest(record_for(video_file), settings=settings)
    second = service.ingest(record_for(video_file), settings=settings)

    assert first.post_id == second.post_id
    assert first.tags == second.tags
    assert second.succeeded is True
