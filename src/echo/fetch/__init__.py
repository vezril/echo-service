"""Download: the yt-dlp wrapper and the pre-download filters.

Owns the optional ProtonVPN egress (default on; credentials come from a Secret and never
from config), the quality cap, container choice, SponsorBlock, subtitles, and the filters
that drop noise *before* spending bandwidth on it — duration bounds, title include/exclude
regexes, and Shorts/livestream handling.

yt-dlp versus YouTube is cat-and-mouse; plan for mostly-unattended, occasionally-maintained.
Failure is therefore a reported outcome, not an exception: :class:`~echo.fetch.FetchReport`
says whether an attempt is worth retrying, so a bot-check costs a poll cycle while a deleted
video is recorded and never retried.

Three pieces:

* :mod:`~echo.fetch.filters` — metadata-level filters (duration, Shorts, livestreams), the
  ones the RSS feed cannot decide. Pure.
* :mod:`~echo.fetch.options` — :class:`~echo.follow.config.ChannelSettings` → yt-dlp options.
  Pure, because a wrong format string is a silent 4K download.
* :mod:`~echo.fetch.download` — probe, judge, download, report. The only module in Echo that
  touches yt-dlp.
"""

from __future__ import annotations

from echo.fetch.download import (
    DownloadedMedia,
    Downloader,
    DownloaderFactory,
    FetchOutcome,
    FetchReport,
    VideoFetcher,
    watch_url,
)
from echo.fetch.filters import (
    Judgement,
    Verdict,
    VideoMetadata,
    is_short,
    judge,
    metadata_from_info,
)
from echo.fetch.options import build_format_selector, build_options, download_directory

__all__ = [
    "DownloadedMedia",
    "Downloader",
    "DownloaderFactory",
    "FetchOutcome",
    "FetchReport",
    "Judgement",
    "Verdict",
    "VideoFetcher",
    "VideoMetadata",
    "build_format_selector",
    "build_options",
    "download_directory",
    "is_short",
    "judge",
    "metadata_from_info",
    "watch_url",
]
