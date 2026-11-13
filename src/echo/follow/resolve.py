"""``handle``/``url`` → ``channel_id``, resolved once and then recorded.

The RSS feed keys on the ``UC…`` ``channel_id``, so that is the canonical identity. A handle
(``@veritasium``) or a channel URL is a convenience for the human editing the config; this
module turns one into an id by reading the channel page, and the caller records the result so
a later handle rename cannot lose the subscription.

Two deliberate constraints:

* **youtube.com hosts only.** The URL comes from a config file, and a fetcher that follows
  any URL a file names is a request-forgery lever aimed at whatever the worker can reach on
  the cluster network. The allowlist is also simply correct: no other host can answer this.
* **No login, no API key** — this is a plain page read, consistent with the anonymity model.

The page scrape is the fragile part of Echo's discovery path (YouTube changes its HTML
freely). It runs **once per channel**, not per poll, and several independent markers are
tried before giving up, so a layout change degrades to "resolve this one handle by hand"
rather than breaking polling for channels already carrying an id.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlparse

import httpx

from echo.follow.config import CHANNEL_ID_RE

__all__ = [
    "ALLOWED_HOSTS",
    "ChannelResolver",
    "ResolveError",
    "channel_page_url",
    "extract_channel_id",
    "normalize_handle",
]

#: Hosts that can legitimately answer a channel-page request.
ALLOWED_HOSTS: Final = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)

MAX_PAGE_BYTES: Final = 8 * 1024 * 1024
_DEFAULT_TIMEOUT: Final = 15.0
_USER_AGENT: Final = "echo-service (+https://github.com/vezril/echo-service)"

_HANDLE_RE: Final = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")

# Markers that carry the id, most reliable first. Each is independently sufficient; they are
# tried in order so that a single HTML redesign is unlikely to remove all of them at once.
_ID_PATTERNS: Final = (
    re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'rel="canonical"\s+href="[^"]*?/channel/(UC[A-Za-z0-9_-]{22})'),
    re.compile(r'itemprop="(?:identifier|channelId)"\s+content="(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r"/feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})"),
    re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})"),
)


class ResolveError(Exception):
    """A handle or URL could not be turned into a ``channel_id``."""


def normalize_handle(handle: str) -> str:
    """Normalize a handle to its ``@name`` form.

    Accepts ``veritasium`` and ``@veritasium`` alike, because both are what people type.
    """
    candidate = handle.strip()
    if not candidate:
        raise ResolveError("handle is empty")
    if not candidate.startswith("@"):
        candidate = f"@{candidate}"
    if not _HANDLE_RE.match(candidate):
        raise ResolveError(
            f"{handle!r} is not a YouTube handle — expected something like '@veritasium'"
        )
    return candidate


def channel_page_url(*, handle: str | None = None, url: str | None = None) -> str:
    """The page URL to read for a channel identified by handle or URL.

    Raises :class:`ResolveError` for any host outside :data:`ALLOWED_HOSTS`, or for a
    non-HTTPS URL.
    """
    if (handle is None) == (url is None):
        raise ResolveError("give exactly one of handle or url")

    if handle is not None:
        return f"https://www.youtube.com/{normalize_handle(handle)}"

    assert url is not None
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ResolveError(f"{url!r} must be an http(s) URL")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ResolveError(
            f"{url!r} is not a youtube.com URL (host {host!r}). Echo only reads channel "
            "pages from YouTube; point 'url' at the channel, or use 'handle'."
        )
    # Normalize to https even if the file said http — this request is for a public page, but
    # there is no reason to make it in clear text.
    return parsed._replace(scheme="https").geturl()


def extract_channel_id(page: str) -> str:
    """Pull the ``UC…`` channel id out of a channel page.

    Raises :class:`ResolveError` when no marker is present, which usually means the handle
    does not exist, the page was a consent/bot-check interstitial, or YouTube's markup moved.
    """
    for pattern in _ID_PATTERNS:
        match = pattern.search(page)
        if match is not None:
            channel_id = match.group(1)
            if CHANNEL_ID_RE.match(channel_id):
                return channel_id
    raise ResolveError(
        "no channel id found on the page — the handle may not exist, or YouTube served an "
        "interstitial instead of the channel page"
    )


class ChannelResolver:
    """Reads a channel page over HTTPS to resolve its id.

    ``proxy`` carries the optional ProtonVPN egress, supplied by the caller from a Secret;
    resolution is part of discovery, so it goes through the same egress as the feed poll.
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
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
        )

    def resolve(self, *, handle: str | None = None, url: str | None = None) -> str:
        """Resolve a handle or channel URL to its ``channel_id``."""
        page_url = channel_page_url(handle=handle, url=url)
        try:
            with self._client.stream("GET", page_url) as response:
                if response.status_code != httpx.codes.OK:
                    raise ResolveError(
                        f"{page_url} returned HTTP {response.status_code} — "
                        "the handle or channel URL is probably wrong"
                    )
                # Redirects are followed, so re-check the host actually served: a redirect
                # off YouTube must not turn into a request Echo never intended to make.
                final_host = (response.url.host or "").lower()
                if final_host not in ALLOWED_HOSTS:
                    raise ResolveError(f"{page_url} redirected off YouTube to {final_host!r}")
                page = self._read_capped(response, page_url)
        except httpx.HTTPError as exc:
            raise ResolveError(f"cannot reach {page_url}: {exc}") from exc

        return extract_channel_id(page)

    @staticmethod
    def _read_capped(response: httpx.Response, page_url: str) -> str:
        """Read at most :data:`MAX_PAGE_BYTES`; a channel page is ~1 MiB of HTML."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                raise ResolveError(f"{page_url} exceeds the {MAX_PAGE_BYTES}-byte cap")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self) -> None:
        """Close the underlying connection pool, if this resolver owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ChannelResolver:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
