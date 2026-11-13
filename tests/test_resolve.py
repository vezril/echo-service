"""``handle``/``url`` → ``channel_id``, and the host allowlist that keeps it safe.

The scrape is the fragile part of discovery, so these tests cover several independent page
shapes: a YouTube redesign that removes one marker should not break resolution outright.
"""

from __future__ import annotations

import httpx
import pytest

from echo.follow.resolve import (
    ChannelResolver,
    ResolveError,
    channel_page_url,
    extract_channel_id,
    normalize_handle,
)
from tests.conftest import CHANNEL_ID


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@veritasium", "@veritasium"),
        ("veritasium", "@veritasium"),  # people type it both ways
        ("  @3blue1brown  ", "@3blue1brown"),
        ("@some.channel_name-2", "@some.channel_name-2"),
    ],
)
def test_normalize_handle_accepts_both_forms(raw: str, expected: str) -> None:
    assert normalize_handle(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "@ab", "@has spaces", "@has/slash", "@" + "x" * 31])
def test_normalize_handle_rejects_non_handles(raw: str) -> None:
    with pytest.raises(ResolveError):
        normalize_handle(raw)


def test_channel_page_url_from_a_handle() -> None:
    assert channel_page_url(handle="@veritasium") == "https://www.youtube.com/@veritasium"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@veritasium",
        "https://youtube.com/c/veritasium",
        f"https://m.youtube.com/channel/{CHANNEL_ID}",
    ],
)
def test_channel_page_url_accepts_youtube_hosts(url: str) -> None:
    assert channel_page_url(url=url).startswith("https://")


def test_channel_page_url_upgrades_http_to_https() -> None:
    assert channel_page_url(url="http://www.youtube.com/@v3ritasium") == (
        "https://www.youtube.com/@v3ritasium"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/@veritasium",
        "https://youtube.com.evil.example/@veritasium",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8080/admin",
        "file:///etc/passwd",
        "https://notyoutube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_channel_page_url_refuses_non_youtube_targets(url: str) -> None:
    """The URL comes from a config file; fetching anything it names is request forgery."""
    with pytest.raises(ResolveError):
        channel_page_url(url=url)


def test_channel_page_url_needs_exactly_one_identity() -> None:
    with pytest.raises(ResolveError, match="exactly one"):
        channel_page_url()
    with pytest.raises(ResolveError, match="exactly one"):
        channel_page_url(handle="@v", url="https://www.youtube.com/@v")


@pytest.mark.parametrize(
    "page",
    [
        f'<script>{{"externalId":"{CHANNEL_ID}","title":"x"}}</script>',
        f'<script>{{"channelId": "{CHANNEL_ID}"}}</script>',
        f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">',
        f'<meta itemprop="identifier" content="{CHANNEL_ID}">',
        f'<link rel="alternate" href="/feeds/videos.xml?channel_id={CHANNEL_ID}">',
        f'<a href="/channel/{CHANNEL_ID}">Channel</a>',
    ],
)
def test_extract_channel_id_reads_any_of_the_known_markers(page: str) -> None:
    assert extract_channel_id(page) == CHANNEL_ID


def test_extract_channel_id_prefers_the_most_reliable_marker() -> None:
    """A page carries many ids (related channels); externalId is the page's own."""
    other = "UC" + "z" * 22
    page = f'<a href="/channel/{other}">related</a><script>{{"externalId":"{CHANNEL_ID}"}}</script>'
    assert extract_channel_id(page) == CHANNEL_ID


@pytest.mark.parametrize(
    "page",
    [
        "",
        "<html><body>This channel does not exist.</body></html>",
        "<html><body>Before you continue to YouTube</body></html>",
        '<script>{"externalId":"UCtooshort"}</script>',
    ],
)
def test_extract_channel_id_fails_loudly_when_nothing_matches(page: str) -> None:
    with pytest.raises(ResolveError, match="no channel id found"):
        extract_channel_id(page)


def _resolver(handler: httpx.MockTransport) -> ChannelResolver:
    return ChannelResolver(client=httpx.Client(transport=handler, follow_redirects=True))


def test_resolve_reads_the_channel_page_once_and_returns_the_id() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=f'<script>{{"externalId":"{CHANNEL_ID}"}}</script>')

    with _resolver(httpx.MockTransport(handler)) as resolver:
        assert resolver.resolve(handle="@veritasium") == CHANNEL_ID

    assert requests == ["https://www.youtube.com/@veritasium"]


def test_resolve_reports_a_missing_handle_as_a_resolve_error() -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(404))
    with _resolver(handler) as resolver, pytest.raises(ResolveError, match="HTTP 404"):
        resolver.resolve(handle="@nosuchchannel")


def test_resolve_maps_transport_failures_to_a_resolve_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with (
        _resolver(httpx.MockTransport(handler)) as resolver,
        pytest.raises(ResolveError, match="cannot reach"),
    ):
        resolver.resolve(handle="@veritasium")


def test_resolve_refuses_a_redirect_that_leaves_youtube() -> None:
    """Redirects are followed, so the host that actually answered is re-checked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            return httpx.Response(302, headers={"Location": "https://evil.example.com/page"})
        return httpx.Response(200, text=f'<script>{{"externalId":"{CHANNEL_ID}"}}</script>')

    with (
        _resolver(httpx.MockTransport(handler)) as resolver,
        pytest.raises(ResolveError, match="redirected off YouTube"),
    ):
        resolver.resolve(handle="@veritasium")


def test_resolve_abandons_an_oversized_page() -> None:
    from echo.follow.resolve import MAX_PAGE_BYTES

    oversized = b"x" * (MAX_PAGE_BYTES + 1)
    handler = httpx.MockTransport(lambda _: httpx.Response(200, content=oversized))
    with _resolver(handler) as resolver, pytest.raises(ResolveError, match="cap"):
        resolver.resolve(handle="@veritasium")
