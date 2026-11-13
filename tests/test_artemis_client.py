"""The Artemis client, against the contracts verified in artemis-service.

Each test encodes a real route's behaviour: the 201 upload body, the full-set tags PATCH, the
409 that means "pool already exists", and the error shape ``{"error": ...}``. If Artemis
changes one of these, the matching test is where it should bite.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from echo.ingest.artemis import ArtemisClient, ArtemisError, content_type_for


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "dQw4w9WgXcQ.mp4"
    path.write_bytes(b"not really an mp4, but bytes are bytes")
    return path


def client_for(handler: httpx.MockTransport, **kwargs: object) -> ArtemisClient:
    return ArtemisClient(
        "http://artemis.svc:8080",
        client=httpx.Client(transport=handler, base_url="http://artemis.svc:8080"),
        **kwargs,  # type: ignore[arg-type]
    )


# --- content types ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("v.mp4", "video/mp4"),
        ("v.mkv", "video/x-matroska"),
        ("v.webm", "video/webm"),
        ("v.mov", "video/quicktime"),
        ("V.MP4", "video/mp4"),
    ],
)
def test_the_containers_yt_dlp_writes_map_to_video_types(name: str, expected: str) -> None:
    """The media class Artemis derives from this decides whether a transcode is produced."""
    assert content_type_for(Path(name)) == expected


def test_an_unknown_extension_is_not_guessed_as_video() -> None:
    assert content_type_for(Path("mystery.zzz")) == "application/octet-stream"


# --- upload ----------------------------------------------------------------------------


def test_upload_posts_the_file_and_returns_the_post_id(video: Path) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers["content-type"]
        seen["media_type"] = request.url.params["mediaType"]
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"postId": "post-1", "status": "pending"})

    with client_for(httpx.MockTransport(handler)) as client:
        uploaded = client.upload(video)

    assert uploaded.post_id == "post-1"
    assert uploaded.status == "pending"
    assert uploaded.deduplicated is False
    assert seen["path"] == "/uploads"
    assert seen["content_type"] == "video/mp4"
    assert seen["media_type"] == "video"
    assert seen["body"] == video.read_text()


def test_the_media_type_is_sent_explicitly(video: Path) -> None:
    """So a mistyped Content-Type cannot make Artemis classify a video as `application`."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(201, json={"postId": "post-1", "status": "pending"})

    with client_for(httpx.MockTransport(handler)) as client:
        client.upload(video, content_type="application/octet-stream")

    assert captured["mediaType"] == "application"


def test_an_active_status_means_artemis_deduplicated(video: Path) -> None:
    """Re-uploading the same bytes returns the existing post — which makes retry safe."""
    handler = httpx.MockTransport(
        lambda _: httpx.Response(201, json={"postId": "post-1", "status": "active"})
    )
    with client_for(handler) as client:
        assert client.upload(video).deduplicated is True


def test_uploading_a_missing_file_fails_before_any_request(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have been called")

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(ArtemisError, match="not a file"),
    ):
        client.upload(tmp_path / "absent.mp4")


def test_an_upload_response_without_a_post_id_is_an_error(video: Path) -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(201, json={"status": "pending"}))
    with client_for(handler) as client, pytest.raises(ArtemisError, match="no postId"):
        client.upload(video)


def test_a_502_from_the_upload_route_is_retryable(video: Path) -> None:
    """Artemis answers 502 when Apollo or Hermes failed underneath — a transient dependency."""
    handler = httpx.MockTransport(
        lambda _: httpx.Response(502, json={"error": "apollo put failed"})
    )
    with (
        client_for(handler) as client,
        pytest.raises(ArtemisError, match="apollo put failed") as caught,
    ):
        client.upload(video)

    assert caught.value.status == 502
    assert caught.value.retryable is True


# --- tags ------------------------------------------------------------------------------


def test_tags_are_sent_as_a_complete_set() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        return httpx.Response(200)

    with client_for(httpx.MockTransport(handler)) as client:
        client.replace_tags("post-1", ["linus_tech_tips", "fish", "tank"])

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/posts/post-1/tags"
    assert json.loads(str(captured["body"])) == {"tags": ["linus_tech_tips", "fish", "tank"]}


def test_a_rejected_tag_is_not_retryable() -> None:
    """A 400 means Echo sent something Artemis refuses; retrying sends it again."""
    handler = httpx.MockTransport(
        lambda _: httpx.Response(400, json={"error": "must not be empty after normalization"})
    )
    with client_for(handler) as client, pytest.raises(ArtemisError) as caught:
        client.replace_tags("post-1", [""])

    assert caught.value.retryable is False


# --- pools -----------------------------------------------------------------------------


def test_creating_a_pool_sends_the_id_and_name() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"poolId": "UC123", "status": "active"})

    with client_for(httpx.MockTransport(handler)) as client:
        client.ensure_pool("UC123", "Linus Tech Tips")

    assert captured["path"] == "/pools"
    assert json.loads(captured["body"]) == {"id": "UC123", "name": "Linus Tech Tips"}


def test_an_existing_pool_is_success_not_an_error() -> None:
    """409 is the steady-state answer: Echo re-creates the pool rather than tracking it."""
    handler = httpx.MockTransport(
        lambda _: httpx.Response(409, json={"error": "pool already exists"})
    )
    with client_for(handler) as client:
        client.ensure_pool("UC123", "Linus Tech Tips")  # must not raise


def test_another_error_while_creating_a_pool_still_raises() -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "bad pool name"}))
    with client_for(handler) as client, pytest.raises(ArtemisError, match="bad pool name"):
        client.ensure_pool("UC123", "")


def test_adding_a_post_to_a_pool_sends_the_post_id() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        return httpx.Response(200)

    with client_for(httpx.MockTransport(handler)) as client:
        client.add_to_pool("UC123", "post-1")

    assert captured["path"] == "/pools/UC123/posts"
    assert json.loads(captured["body"]) == {"postId": "post-1"}


# --- transport and auth ----------------------------------------------------------------


def test_a_transport_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(ArtemisError, match="did not reach Artemis") as caught,
    ):
        client.add_to_pool("UC123", "post-1")

    assert caught.value.status is None
    assert caught.value.retryable is True


def test_a_429_is_retryable_despite_being_a_4xx() -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(429))
    with client_for(handler) as client, pytest.raises(ArtemisError) as caught:
        client.add_to_pool("UC123", "post-1")

    assert caught.value.retryable is True


def test_a_token_rides_every_request_when_configured(video: Path) -> None:
    """Artemis has no auth on these routes today; this keeps enabling it a config change."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(201, json={"postId": "post-1", "status": "pending"})

    client = ArtemisClient(
        "http://artemis.svc:8080",
        token="s3cret",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://artemis.svc:8080",
            headers={"Authorization": "Bearer s3cret"},
        ),
    )
    with client:
        client.upload(video)

    assert captured["authorization"] == "Bearer s3cret"


def test_an_empty_response_body_is_not_a_parse_error() -> None:
    """The tags and pool routes answer 200 with no body."""
    handler = httpx.MockTransport(lambda _: httpx.Response(200, content=b""))
    with client_for(handler) as client:
        client.replace_tags("post-1", ["fish"])  # must not raise
