"""YouTube's public per-channel Atom feed: the whole anonymity mechanism.

``https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>`` needs no login, no API key and
no Google account — it is the reason Echo can follow channels without a profile being built.

**What the feed actually carries** (verified against the published Atom shape, which is what
the parser below is written against): video id, title, watch URL, channel id, channel title,
author name, ``published`` and ``updated`` timestamps, a thumbnail URL and a description.

**What it does not carry, and the consequences:**

* **no duration** — so ``filters.min_duration`` / ``max_duration`` cannot be decided here.
  They belong to :mod:`echo.fetch`, which has yt-dlp's extracted metadata.
* **no Shorts flag and no livestream flag** — ``shorts`` and ``livestreams`` are likewise
  not decidable from the feed. A Short appears as an ordinary entry.
* **about 15 entries, newest first** — the feed is a window, not a history. A channel that
  uploads faster than Echo polls will drop uploads off the end of the feed before they are
  ever seen, which is what ``poll_interval`` is really tuning. Backfill beyond that window
  is not possible from the feed at all.

Only ``title_include`` / ``title_exclude`` can be applied at this level.

The parser is pure: it takes bytes and returns a :class:`Feed`. Fetching lives in
:class:`FeedClient` so that every parsing edge case is testable without a network.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode

import httpx

__all__ = [
    "FEED_ENTRY_WINDOW",
    "MAX_FEED_BYTES",
    "Feed",
    "FeedClient",
    "FeedEntry",
    "FeedError",
    "FeedParseError",
    "FeedResult",
    "FeedTooLargeError",
    "FeedUnavailableError",
    "feed_url",
    "parse_feed",
]

FEED_BASE_URL: Final = "https://www.youtube.com/feeds/videos.xml"

#: The feed returns roughly this many entries. Treated as a documented observation, not a
#: guarantee — nothing here depends on the exact number.
FEED_ENTRY_WINDOW: Final = 15

#: A per-channel feed is a few tens of KiB. The cap exists because the response is remote
#: input: an unbounded read is a memory-exhaustion lever held by whatever answers the URL.
MAX_FEED_BYTES: Final = 2 * 1024 * 1024

_DEFAULT_TIMEOUT: Final = 15.0

# Echo identifies itself honestly rather than impersonating a browser. The point is to avoid
# a Google *account*, not to disguise the client.
_USER_AGENT: Final = "echo-service (+https://github.com/vezril/echo-service)"

_NS: Final[dict[str, str]] = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


class FeedError(Exception):
    """Base class for feed failures — one channel failing must not stop a poll cycle."""


class FeedUnavailableError(FeedError):
    """The feed could not be retrieved (transport error, or a non-200 response)."""


class FeedParseError(FeedError):
    """The response was retrieved but is not a usable channel feed."""


class FeedTooLargeError(FeedError):
    """The response exceeded :data:`MAX_FEED_BYTES` and was abandoned."""


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One upload as the feed describes it.

    ``video_id`` is the join key everything downstream keys on, and the basis of the
    canonical ``source`` value Echo stamps on the Artemis post (``docs/DESIGN.md``).
    """

    video_id: str
    title: str
    url: str
    channel_id: str
    uploader: str
    published: datetime
    updated: datetime | None = None
    thumbnail: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Feed:
    """A parsed channel feed, entries newest-first as YouTube orders them."""

    channel_id: str
    channel_title: str
    entries: tuple[FeedEntry, ...]

    def __iter__(self) -> Iterator[FeedEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class FeedResult:
    """The outcome of one conditional fetch.

    ``feed is None`` with ``not_modified`` set means YouTube answered 304: nothing has
    changed since ``etag`` / ``last_modified`` were issued, and the caller should do nothing.
    """

    feed: Feed | None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


def feed_url(channel_id: str) -> str:
    """The public feed URL for a channel id."""
    if not channel_id:
        raise ValueError("channel_id is required")
    return f"{FEED_BASE_URL}?{urlencode({'channel_id': channel_id})}"


def parse_feed(payload: str | bytes) -> Feed:
    """Parse a channel feed document.

    Entries missing a video id, title or published timestamp are dropped rather than
    failing the whole feed: one malformed entry should not hide the other fourteen uploads.
    A document that is not a channel feed at all *does* raise — that is a real failure
    (YouTube serving an error page, or a wrong URL), and silently returning zero uploads
    would look exactly like a quiet channel.
    """
    try:
        root = ET.fromstring(payload)  # noqa: S314 - HTTPS-only input, size-capped, no DTDs
    except ET.ParseError as exc:
        raise FeedParseError(f"feed is not valid XML: {exc}") from exc

    if root.tag != f"{{{_NS['atom']}}}feed":
        raise FeedParseError(f"expected an Atom <feed> document, got <{root.tag}>")

    channel_id = _text(root, "yt:channelId")
    if channel_id is None:
        raise FeedParseError("feed has no yt:channelId — not a channel feed")
    channel_title = _text(root, "atom:title") or channel_id

    entries = tuple(
        entry
        for element in root.findall("atom:entry", _NS)
        if (entry := _parse_entry(element, channel_id, channel_title)) is not None
    )
    return Feed(channel_id=channel_id, channel_title=channel_title, entries=entries)


def _parse_entry(element: ET.Element, channel_id: str, channel_title: str) -> FeedEntry | None:
    video_id = _text(element, "yt:videoId")
    title = _text(element, "atom:title") or _text(element, "media:group/media:title")
    published = _timestamp(_text(element, "atom:published"))
    if video_id is None or title is None or published is None:
        return None

    link = element.find("atom:link[@rel='alternate']", _NS)
    url = link.get("href") if link is not None else None

    thumbnail = element.find("media:group/media:thumbnail", _NS)

    return FeedEntry(
        video_id=video_id,
        title=title,
        url=url or f"https://www.youtube.com/watch?v={video_id}",
        channel_id=_text(element, "yt:channelId") or channel_id,
        uploader=_text(element, "atom:author/atom:name") or channel_title,
        published=published,
        updated=_timestamp(_text(element, "atom:updated")),
        thumbnail=thumbnail.get("url") if thumbnail is not None else None,
        description=_text(element, "media:group/media:description"),
    )


def _text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, _NS)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _timestamp(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class FeedClient:
    """Fetches channel feeds over HTTPS, politely and conditionally.

    Conditional requests (``If-None-Match`` / ``If-Modified-Since``) matter here: polling
    every 30 minutes forever against someone else's free endpoint should cost them a 304,
    not a full document, and it makes a tighter ``poll_interval`` cheap.

    ``proxy`` carries the optional ProtonVPN egress. It is passed in by the caller from a
    Secret and is never read from the config file (``codex/docs/echo.md``: credentials live
    in a Secret, never in config).
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            proxy=proxy,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/atom+xml, text/xml"},
        )

    def fetch(
        self,
        channel_id: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FeedResult:
        """Fetch one channel's feed, returning early if it has not changed."""
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        url = feed_url(channel_id)
        # Streamed, so the size cap is enforced while reading rather than after the whole
        # body is already in memory — the cap is only protection if it can stop the read.
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                response_etag = response.headers.get("ETag")
                response_modified = response.headers.get("Last-Modified")

                if response.status_code == httpx.codes.NOT_MODIFIED:
                    return FeedResult(
                        feed=None,
                        not_modified=True,
                        etag=response_etag or etag,
                        last_modified=response_modified or last_modified,
                    )
                if response.status_code != httpx.codes.OK:
                    raise FeedUnavailableError(
                        f"feed for {channel_id} returned HTTP {response.status_code}"
                        + (
                            " — the channel id may be wrong or the channel removed"
                            if response.status_code == httpx.codes.NOT_FOUND
                            else ""
                        )
                    )
                payload = self._read_capped(response, channel_id)
        except httpx.HTTPError as exc:
            raise FeedUnavailableError(f"cannot reach the feed for {channel_id}: {exc}") from exc

        return FeedResult(
            feed=parse_feed(payload),
            etag=response_etag,
            last_modified=response_modified,
        )

    @staticmethod
    def _read_capped(response: httpx.Response, channel_id: str) -> bytes:
        """Read the body, abandoning it as soon as it passes :data:`MAX_FEED_BYTES`."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise FeedTooLargeError(
                    f"feed for {channel_id} exceeds the {MAX_FEED_BYTES}-byte cap"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        """Close the underlying connection pool, if this client owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FeedClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
