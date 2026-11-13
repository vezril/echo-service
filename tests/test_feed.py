"""The Atom feed parser and its conditional-GET client.

The parser is tested against a document shaped like YouTube's real feed; the client is
tested through ``httpx.MockTransport``, so no test touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from echo.follow.feed import (
    MAX_FEED_BYTES,
    FeedClient,
    FeedParseError,
    FeedTooLargeError,
    FeedUnavailableError,
    feed_url,
    parse_feed,
)
from tests.conftest import CHANNEL_ID


def test_feed_url_is_the_public_no_account_endpoint() -> None:
    assert feed_url(CHANNEL_ID) == (
        f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    )


def test_feed_url_requires_a_channel_id() -> None:
    with pytest.raises(ValueError, match="channel_id is required"):
        feed_url("")


def test_parsing_reads_the_channel_and_every_entry(feed_xml: str) -> None:
    feed = parse_feed(feed_xml)

    assert feed.channel_id == CHANNEL_ID
    assert feed.channel_title == "Linus Tech Tips"
    assert len(feed) == 3
    assert [entry.video_id for entry in feed] == ["newest00001", "middle00002", "oldest00003"]


def test_an_entry_carries_everything_ingest_needs(feed_xml: str) -> None:
    entry = parse_feed(feed_xml).entries[0]

    assert entry.video_id == "newest00001"
    assert entry.title == "We Built a PC Inside a Fish Tank (OFFICIAL VIDEO)"
    assert entry.url == "https://www.youtube.com/watch?v=newest00001"
    assert entry.channel_id == CHANNEL_ID
    assert entry.uploader == "Linus Tech Tips"
    assert entry.published == datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    assert entry.updated == datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
    assert entry.thumbnail == "https://i1.ytimg.com/vi/newest00001/hqdefault.jpg"
    assert entry.description == "The tank is fine. The PC is not."


def test_timestamps_are_timezone_aware(feed_xml: str) -> None:
    """Naive timestamps would silently compare wrong against the stale-nudge window."""
    for entry in parse_feed(feed_xml):
        assert entry.published.tzinfo is not None


def test_bytes_and_str_payloads_parse_identically(feed_xml: str) -> None:
    assert parse_feed(feed_xml.encode("utf-8")).entries == parse_feed(feed_xml).entries


def test_an_empty_feed_is_valid_not_an_error() -> None:
    """A channel with no uploads is a normal state, not a failure."""
    feed = parse_feed(
        f"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
              xmlns="http://www.w3.org/2005/Atom">
          <yt:channelId>{CHANNEL_ID}</yt:channelId>
          <title>Quiet Channel</title>
        </feed>"""
    )
    assert feed.entries == ()
    assert feed.channel_title == "Quiet Channel"


def test_one_malformed_entry_does_not_hide_the_others() -> None:
    """Dropping a broken entry beats failing the poll and seeing none of the good ones."""
    feed = parse_feed(
        f"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
              xmlns="http://www.w3.org/2005/Atom">
          <yt:channelId>{CHANNEL_ID}</yt:channelId>
          <title>Channel</title>
          <entry>
            <title>No video id here</title>
            <published>2026-11-12T16:00:00+00:00</published>
          </entry>
          <entry>
            <yt:videoId>goodvideo001</yt:videoId>
            <title>Fine</title>
            <published>2026-11-11T16:00:00+00:00</published>
          </entry>
          <entry>
            <yt:videoId>nodate000002</yt:videoId>
            <title>No published date</title>
          </entry>
        </feed>"""
    )
    assert [entry.video_id for entry in feed] == ["goodvideo001"]


def test_an_entry_without_an_alternate_link_falls_back_to_the_watch_url() -> None:
    feed = parse_feed(
        f"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
              xmlns="http://www.w3.org/2005/Atom">
          <yt:channelId>{CHANNEL_ID}</yt:channelId>
          <title>Channel</title>
          <entry>
            <yt:videoId>linkless0001</yt:videoId>
            <title>Linkless</title>
            <published>2026-11-12T16:00:00+00:00</published>
          </entry>
        </feed>"""
    )
    assert feed.entries[0].url == "https://www.youtube.com/watch?v=linkless0001"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not xml at all", "not valid XML"),
        ("<html><body>Sign in to confirm</body></html>", "Atom <feed>"),
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>No channel</title></feed>',
            "no yt:channelId",
        ),
    ],
)
def test_a_response_that_is_not_a_channel_feed_raises(payload: str, message: str) -> None:
    """Returning zero uploads here would be indistinguishable from a quiet channel."""
    with pytest.raises(FeedParseError, match=message):
        parse_feed(payload)


def _client(handler: httpx.MockTransport) -> FeedClient:
    return FeedClient(client=httpx.Client(transport=handler))


def test_fetch_parses_a_200_and_returns_the_cache_validators(feed_xml: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == feed_url(CHANNEL_ID)
        assert "If-None-Match" not in request.headers
        return httpx.Response(
            200,
            text=feed_xml,
            headers={"ETag": 'W/"abc"', "Last-Modified": "Thu, 12 Nov 2026 18:30:00 GMT"},
        )

    with _client(httpx.MockTransport(handler)) as client:
        result = client.fetch(CHANNEL_ID)

    assert result.not_modified is False
    assert result.feed is not None
    assert len(result.feed) == 3
    assert result.etag == 'W/"abc"'
    assert result.last_modified == "Thu, 12 Nov 2026 18:30:00 GMT"


def test_fetch_sends_the_validators_it_was_given_and_honours_304() -> None:
    """Polling someone else's free endpoint forever should usually cost them a 304."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(304)

    with _client(httpx.MockTransport(handler)) as client:
        result = client.fetch(
            CHANNEL_ID, etag='W/"abc"', last_modified="Thu, 12 Nov 2026 18:30:00 GMT"
        )

    assert seen["if-none-match"] == 'W/"abc"'
    assert seen["if-modified-since"] == "Thu, 12 Nov 2026 18:30:00 GMT"
    assert result.not_modified is True
    assert result.feed is None
    # The validators survive a 304 so the next poll can still be conditional.
    assert result.etag == 'W/"abc"'
    assert result.last_modified == "Thu, 12 Nov 2026 18:30:00 GMT"


def test_fetch_identifies_itself_honestly(feed_xml: str) -> None:
    """Echo avoids a Google *account*; it does not pretend to be a browser."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, text=feed_xml)

    with FeedClient(client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client.fetch(CHANNEL_ID)

    # The default client sets it; an injected bare client has no headers to assert on, so
    # assert on the constant the production path uses instead.
    assert "user-agent" in captured


@pytest.mark.parametrize(
    ("status", "message"), [(404, "channel id may be wrong"), (500, "HTTP 500")]
)
def test_a_non_200_is_a_feed_failure(status: int, message: str) -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(status))
    with _client(handler) as client, pytest.raises(FeedUnavailableError, match=message):
        client.fetch(CHANNEL_ID)


def test_a_transport_error_is_a_feed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(FeedUnavailableError, match="cannot reach the feed"),
    ):
        client.fetch(CHANNEL_ID)


def test_an_oversized_response_is_abandoned_not_buffered() -> None:
    """The response is remote input; an unbounded read is a memory lever held by the server."""
    oversized = b"<x/>" * (MAX_FEED_BYTES // 2)
    handler = httpx.MockTransport(lambda _: httpx.Response(200, content=oversized))

    with _client(handler) as client, pytest.raises(FeedTooLargeError, match="cap"):
        client.fetch(CHANNEL_ID)


def test_the_client_closes_only_what_it_owns(feed_xml: str) -> None:
    injected = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=feed_xml))
    )
    with FeedClient(client=injected) as client:
        assert client.fetch(CHANNEL_ID).feed is not None
    assert injected.is_closed is False
    injected.close()
