"""The yt-dlp wrapper: probe, judge, download, report.

Two passes per video, which is the point of the module:

1. **probe** — extract metadata only (``skip_download``), cheap, no bandwidth spent;
2. **judge** — :mod:`echo.fetch.filters` decides accept / reject / defer;
3. **download** — only for an accepted video.

Every outcome is reported as a :class:`FetchReport` rather than raised, because the caller is
a poll loop processing a batch: one unavailable video must not abort a channel, and each
outcome needs to be *recorded* (``echo-ui`` shows it, and the seen-set depends on whether it
is terminal). The only exception is a programming error, which still raises.

yt-dlp is reached through the :class:`Downloader` protocol so that tests cover every path
without network access. yt-dlp ships no types, and this module is the only place that touches
it — everything else in Echo sees :class:`~echo.fetch.filters.VideoMetadata` instead.

**yt-dlp versus YouTube is cat-and-mouse** (``codex/docs/echo.md``), so failure is a normal
state, not an exception: a download that fails for a transient reason is reported retryable,
and one that fails because the video is gone is reported terminal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from echo.fetch.filters import Judgement, Verdict, VideoMetadata, judge, metadata_from_info
from echo.fetch.options import build_options
from echo.follow.config import ChannelSettings

__all__ = [
    "DownloadedMedia",
    "Downloader",
    "FetchOutcome",
    "FetchReport",
    "VideoFetcher",
    "watch_url",
]

WATCH_URL_TEMPLATE: Final = "https://www.youtube.com/watch?v={video_id}"

# Substrings that mean the video will never become downloadable. Matching on messages is
# unavoidable — yt-dlp signals these through DownloadError text, not distinct types — so the
# list is conservative: anything unrecognised is treated as retryable, because wrongly
# retrying costs a poll cycle while wrongly giving up loses an upload permanently.
_TERMINAL_ERROR_MARKERS: Final = (
    "private video",
    "video unavailable",
    "this video is unavailable",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "members-only",
    "join this channel to get access",
    "video has been removed",
    "who has blocked it on copyright grounds",
)


class FetchOutcome(StrEnum):
    """What happened to one video."""

    DOWNLOADED = "downloaded"
    FILTERED = "filtered"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """A file on the NAS, plus the facts :mod:`echo.ingest` and Echo's state need.

    ``title`` is the **verbatim** title. Artemis has no field that can hold it
    (``docs/DESIGN.md``), so this value is what Echo stores and what ``echo-ui`` displays;
    it must not be normalized here.
    """

    video_id: str
    path: Path
    title: str
    uploader: str | None = None
    duration: timedelta | None = None
    width: int | None = None
    height: int | None = None
    filesize: int | None = None
    subtitle_paths: tuple[Path, ...] = ()

    @property
    def source(self) -> str:
        """The canonical ``source`` value for the Artemis post: the bare video id.

        Locked 2026-11-13 (``docs/DESIGN.md``). ``source`` is exact-match only in Artemis, so
        the form must never vary; the bare id has exactly one spelling, where a watch URL has
        many (``youtu.be``, ``m.``, ``&t=``, ``?si=``).
        """
        return self.video_id


@dataclass(frozen=True, slots=True)
class FetchReport:
    """The outcome of one fetch attempt — always returned, never raised."""

    video_id: str
    outcome: FetchOutcome
    reason: str
    media: DownloadedMedia | None = None
    metadata: VideoMetadata | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is FetchOutcome.DOWNLOADED

    @property
    def retryable(self) -> bool:
        """True when the caller must **not** record the video as seen.

        A deferred stream and a transient failure both need another attempt; a filtered
        video and a permanently-gone one are terminal.
        """
        return self.outcome in {FetchOutcome.DEFERRED, FetchOutcome.FAILED}


def watch_url(video_id: str) -> str:
    """The canonical watch URL for a video id.

    ``source`` stores the bare id (see :attr:`DownloadedMedia.source`); this is how the id is
    turned back into something a human can open, and the only place that conversion lives.
    """
    if not video_id:
        raise ValueError("video_id is required")
    return WATCH_URL_TEMPLATE.format(video_id=video_id)


class Downloader(Protocol):
    """The slice of ``yt_dlp.YoutubeDL`` that Echo uses."""

    def extract_info(self, url: str, *, download: bool = True) -> Mapping[str, Any] | None: ...


#: Builds a downloader from an option dict. Replaced in tests; in production it is yt-dlp.
DownloaderFactory = Callable[[dict[str, Any]], Downloader]


def _default_factory(options: dict[str, Any]) -> Downloader:
    downloader: Downloader = YoutubeDL(options)
    return downloader


class VideoFetcher:
    """Downloads accepted videos into ``root``, one channel directory per channel.

    ``proxy`` is the optional ProtonVPN egress, supplied from a Secret rather than read from
    the config file. Note that nothing in the cluster provides this egress yet — see
    ``docs/DESIGN.md`` — so today it is honoured when given and simply absent when not.
    """

    def __init__(
        self,
        *,
        root: Path,
        proxy: str | None = None,
        downloader_factory: DownloaderFactory = _default_factory,
    ) -> None:
        self._root = Path(root)
        self._proxy = proxy
        self._factory = downloader_factory

    def fetch(
        self,
        video_id: str,
        *,
        channel_id: str,
        settings: ChannelSettings,
    ) -> FetchReport:
        """Probe, judge and (if accepted) download one video."""
        url = watch_url(video_id)

        probed = self._probe(url, video_id=video_id, channel_id=channel_id, settings=settings)
        if isinstance(probed, FetchReport):
            return probed

        verdict = judge(probed, settings)
        if not verdict.accepted:
            return _report_for(video_id, verdict, metadata=probed)

        return self._download(url, metadata=probed, channel_id=channel_id, settings=settings)

    def _probe(
        self,
        url: str,
        *,
        video_id: str,
        channel_id: str,
        settings: ChannelSettings,
    ) -> VideoMetadata | FetchReport:
        """Extract metadata without downloading. Returns a report only on failure."""
        options = build_options(
            settings,
            root=self._root,
            channel_id=channel_id,
            proxy=self._proxy,
            probe_only=True,
        )
        try:
            info = self._factory(options).extract_info(url, download=False)
        except DownloadError as exc:
            return _failure(video_id, exc, stage="probe")

        if info is None:
            return FetchReport(
                video_id=video_id,
                outcome=FetchOutcome.FAILED,
                reason="probe returned no metadata",
            )
        try:
            return metadata_from_info(info)
        except ValueError as exc:
            return FetchReport(
                video_id=video_id, outcome=FetchOutcome.FAILED, reason=f"probe: {exc}"
            )

    def _download(
        self,
        url: str,
        *,
        metadata: VideoMetadata,
        channel_id: str,
        settings: ChannelSettings,
    ) -> FetchReport:
        options = build_options(settings, root=self._root, channel_id=channel_id, proxy=self._proxy)
        try:
            info = self._factory(options).extract_info(url, download=True)
        except DownloadError as exc:
            return _failure(metadata.video_id, exc, stage="download")

        if info is None:
            return FetchReport(
                video_id=metadata.video_id,
                outcome=FetchOutcome.FAILED,
                reason="download returned no result",
                metadata=metadata,
            )

        path = _downloaded_path(info)
        if path is None:
            # yt-dlp reported success but named no file. Reporting success here would make
            # ingest/ fail on a missing path later, far from the cause.
            return FetchReport(
                video_id=metadata.video_id,
                outcome=FetchOutcome.FAILED,
                reason="yt-dlp reported no output file path",
                metadata=metadata,
            )

        return FetchReport(
            video_id=metadata.video_id,
            outcome=FetchOutcome.DOWNLOADED,
            reason="downloaded",
            metadata=metadata,
            media=DownloadedMedia(
                video_id=metadata.video_id,
                path=path,
                title=metadata.title,
                uploader=metadata.uploader,
                duration=metadata.duration,
                width=metadata.width,
                height=metadata.height,
                filesize=_filesize(info),
                subtitle_paths=_subtitle_paths(info),
            ),
        )


def _report_for(video_id: str, verdict: Judgement, *, metadata: VideoMetadata) -> FetchReport:
    outcome = FetchOutcome.DEFERRED if verdict.verdict is Verdict.DEFER else FetchOutcome.FILTERED
    return FetchReport(video_id=video_id, outcome=outcome, reason=verdict.reason, metadata=metadata)


def _failure(video_id: str, exc: BaseException, *, stage: str) -> FetchReport:
    """Classify a yt-dlp failure as permanent or worth retrying."""
    message = str(exc)
    lowered = message.lower()
    permanent = any(marker in lowered for marker in _TERMINAL_ERROR_MARKERS)
    return FetchReport(
        video_id=video_id,
        # A permanently-gone video is recorded as filtered: the outcome is "never download
        # this", which is exactly what FILTERED means to the caller's seen-set.
        outcome=FetchOutcome.FILTERED if permanent else FetchOutcome.FAILED,
        reason=f"{stage} failed: {message}",
    )


def _downloaded_path(info: Mapping[str, Any]) -> Path | None:
    """Find the file yt-dlp wrote.

    ``requested_downloads[0].filepath`` is the post-merge, post-processing path and is what
    yt-dlp fills in for a normal download; ``filepath`` on the info dict is the older shape.
    Both are checked rather than reconstructing the name from the template, which would go
    wrong the moment a postprocessor changes the container.
    """
    requested = info.get("requested_downloads")
    if isinstance(requested, list) and requested:
        first = requested[0]
        if isinstance(first, Mapping):
            candidate = first.get("filepath") or first.get("_filename")
            if isinstance(candidate, str) and candidate:
                return Path(candidate)
    for key in ("filepath", "_filename"):
        candidate = info.get(key)
        if isinstance(candidate, str) and candidate:
            return Path(candidate)
    return None


def _filesize(info: Mapping[str, Any]) -> int | None:
    requested = info.get("requested_downloads")
    if isinstance(requested, list) and requested and isinstance(requested[0], Mapping):
        for key in ("filesize", "filesize_approx"):
            value = requested[0].get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    for key in ("filesize", "filesize_approx"):
        value = info.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _subtitle_paths(info: Mapping[str, Any]) -> tuple[Path, ...]:
    """Collect written subtitle files, if yt-dlp reported any."""
    requested = info.get("requested_subtitles")
    if not isinstance(requested, Mapping):
        return ()
    paths: list[Path] = []
    for entry in requested.values():
        if isinstance(entry, Mapping):
            candidate = entry.get("filepath")
            if isinstance(candidate, str) and candidate:
                paths.append(Path(candidate))
    return tuple(paths)
