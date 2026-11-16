"""Shared fixtures: a realistic channel feed, and the settings an entry is judged against."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from echo.follow.config import ChannelSettings, parse_config
from echo.follow.feed import FeedEntry

CHANNEL_ID = "UCXuqSBlHAE6Xw-yeJA0Tunw"

# Shaped after the real document YouTube serves at
# https://www.youtube.com/feeds/videos.xml?channel_id=<ID> — namespaces, element names and
# nesting match it, because the parser is written against that shape.
FEED_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <link rel="self" href="http://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"/>
  <id>yt:channel:{CHANNEL_ID}</id>
  <yt:channelId>{CHANNEL_ID}</yt:channelId>
  <title>Linus Tech Tips</title>
  <link rel="alternate" href="https://www.youtube.com/channel/{CHANNEL_ID}"/>
  <author>
    <name>Linus Tech Tips</name>
    <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
  </author>
  <published>2008-11-25T19:03:15+00:00</published>
  <entry>
    <id>yt:video:newest00001</id>
    <yt:videoId>newest00001</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>We Built a PC Inside a Fish Tank (OFFICIAL VIDEO)</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=newest00001"/>
    <author>
      <name>Linus Tech Tips</name>
      <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
    </author>
    <published>2026-11-12T16:00:00+00:00</published>
    <updated>2026-11-12T18:30:00+00:00</updated>
    <media:group>
      <media:title>We Built a PC Inside a Fish Tank (OFFICIAL VIDEO)</media:title>
      <media:content url="https://www.youtube.com/v/newest00001?version=3"
                     type="application/x-shockwave-flash" width="640" height="390"/>
      <media:thumbnail url="https://i1.ytimg.com/vi/newest00001/hqdefault.jpg"
                       width="480" height="360"/>
      <media:description>The tank is fine. The PC is not.</media:description>
      <media:community>
        <media:starRating count="12345" average="4.95" min="1" max="5"/>
        <media:statistics views="678901"/>
      </media:community>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:middle00002</id>
    <yt:videoId>middle00002</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>#shorts quick tip</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=middle00002"/>
    <author>
      <name>Linus Tech Tips</name>
      <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
    </author>
    <published>2026-11-11T09:00:00+00:00</published>
    <updated>2026-11-11T09:30:00+00:00</updated>
    <media:group>
      <media:title>#shorts quick tip</media:title>
      <media:thumbnail url="https://i1.ytimg.com/vi/middle00002/hqdefault.jpg"
                       width="480" height="360"/>
      <media:description/>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:oldest00003</id>
    <yt:videoId>oldest00003</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>A Normal Review</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=oldest00003"/>
    <author>
      <name>Linus Tech Tips</name>
      <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
    </author>
    <published>2026-11-10T12:00:00+00:00</published>
    <updated>2026-11-10T12:00:00+00:00</updated>
    <media:group>
      <media:title>A Normal Review</media:title>
      <media:thumbnail url="https://i1.ytimg.com/vi/oldest00003/hqdefault.jpg"
                       width="480" height="360"/>
      <media:description>It is fine.</media:description>
    </media:group>
  </entry>
</feed>
"""


@pytest.fixture
def feed_xml() -> str:
    return FEED_XML


@pytest.fixture
def settings() -> ChannelSettings:
    """Default settings, as a minimal config file would resolve them."""
    return parse_config({"version": 1, "channels": [{"id": CHANNEL_ID}]}).channels[0].settings


@pytest.fixture
def settings_for() -> Callable[..., ChannelSettings]:
    """Build settings with channel-level overrides, e.g. ``settings_for(backfill=1)``."""

    def build(**overrides: Any) -> ChannelSettings:
        channel: dict[str, Any] = {"id": CHANNEL_ID, **overrides}
        return parse_config({"version": 1, "channels": [channel]}).channels[0].settings

    return build


@pytest.fixture
def entry_factory() -> Callable[..., FeedEntry]:
    """Build feed entries with a sane default published ordering (older as index grows)."""
    base = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    def build(
        video_id: str,
        *,
        title: str = "A video",
        age_days: int = 0,
        uploader: str = "Linus Tech Tips",
    ) -> FeedEntry:
        return FeedEntry(
            video_id=video_id,
            title=title,
            url=f"https://www.youtube.com/watch?v={video_id}",
            channel_id=CHANNEL_ID,
            uploader=uploader,
            published=base - timedelta(days=age_days),
        )

    return build
