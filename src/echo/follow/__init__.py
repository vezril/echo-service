"""Discovery: which channels to follow, and what is new on them.

Loads the channel-follow config (schema in ``codex/docs/echo.md``), polls each channel's
public RSS feed at ``https://www.youtube.com/feeds/videos.xml?channel_id=<ID>``, and reports
uploads not yet seen.

The ``UC…`` ``channel_id`` is canonical — it is what the feed keys on. A ``handle`` or
``url`` is a convenience resolved once on first sync and then recorded, so a handle rename
never loses the subscription.

Four pieces, separated so that every decision with a consequence is a pure function:

* :mod:`~echo.follow.config` — parse, deep-merge and validate the config file
* :mod:`~echo.follow.feed` — the Atom feed: a pure parser plus a conditional-GET client
* :mod:`~echo.follow.resolve` — ``handle``/``url`` → ``channel_id``, once per channel
* :mod:`~echo.follow.detect` — which entries are new, and what backfill does on first sync

What the feed cannot tell us is as important as what it can: no duration, no Shorts flag and
no livestream flag, so those filters belong to :mod:`echo.fetch`, which has yt-dlp's
metadata. See :mod:`echo.follow.feed` for the full list and its consequences.
"""

from __future__ import annotations

from echo.follow.config import (
    Catalog,
    Channel,
    ChannelSettings,
    ConfigError,
    EchoConfig,
    Filters,
    Livestreams,
    Notify,
    SponsorBlock,
    SponsorBlockMode,
    Subtitles,
    load_config,
    parse_config,
    parse_duration,
)
from echo.follow.detect import Selection, matches_title_filters, select_new
from echo.follow.feed import (
    Feed,
    FeedClient,
    FeedEntry,
    FeedError,
    FeedParseError,
    FeedResult,
    FeedTooLargeError,
    FeedUnavailableError,
    feed_url,
    parse_feed,
)
from echo.follow.resolve import (
    ChannelResolver,
    ResolveError,
    channel_page_url,
    extract_channel_id,
    normalize_handle,
)

__all__ = [
    "Catalog",
    "Channel",
    "ChannelResolver",
    "ChannelSettings",
    "ConfigError",
    "EchoConfig",
    "Feed",
    "FeedClient",
    "FeedEntry",
    "FeedError",
    "FeedParseError",
    "FeedResult",
    "FeedTooLargeError",
    "FeedUnavailableError",
    "Filters",
    "Livestreams",
    "Notify",
    "ResolveError",
    "Selection",
    "SponsorBlock",
    "SponsorBlockMode",
    "Subtitles",
    "channel_page_url",
    "extract_channel_id",
    "feed_url",
    "load_config",
    "matches_title_filters",
    "normalize_handle",
    "parse_config",
    "parse_duration",
    "parse_feed",
    "select_new",
]
