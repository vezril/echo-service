"""The filters that need per-video metadata — the ones :mod:`echo.follow` cannot decide.

The feed carries no duration, no Shorts flag and no livestream flag (see
:mod:`echo.follow.feed`), so ``min_duration``, ``max_duration``, ``shorts`` and
``livestreams`` are all decided here, against what yt-dlp extracted. This runs **before the
download**, which is the whole point: the filters exist to avoid spending bandwidth.

Three verdicts rather than two, because "not now" and "not ever" must not be conflated:

* :attr:`Verdict.ACCEPT` — download it.
* :attr:`Verdict.REJECT` — never download it; the caller records it as seen, so it is never
  re-evaluated.
* :attr:`Verdict.DEFER` — not downloadable *yet* (a stream that is still live). The caller
  must **not** record it as seen, or the finished recording would never be collected.

Judging is a pure function of metadata plus settings, so every rule is testable without
touching YouTube.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final

from echo.follow.config import ChannelSettings, Livestreams
from echo.follow.detect import matches_title_filters

__all__ = [
    "SHORTS_MAX_SECONDS",
    "Judgement",
    "VideoMetadata",
    "Verdict",
    "is_short",
    "judge",
    "metadata_from_info",
]

#: A Short is at most 3 minutes since 2024 (it was 60s before). Used only in combination
#: with a portrait aspect ratio — see :func:`is_short`.
SHORTS_MAX_SECONDS: Final = 180


class Verdict(StrEnum):
    """What to do with a video."""

    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class Judgement:
    """A verdict and the reason for it.

    The reason is not decoration: it is what ``echo-ui`` shows for a video that was never
    downloaded, and without it a filtered video is indistinguishable from a lost one.
    """

    verdict: Verdict
    reason: str

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.ACCEPT

    @property
    def retryable(self) -> bool:
        """True when the caller must not record the video as seen."""
        return self.verdict is Verdict.DEFER


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """The subset of yt-dlp's extracted info that Echo actually decides on.

    Narrowing it here keeps yt-dlp's large, untyped and version-dependent dict out of the
    rest of Echo: only :func:`metadata_from_info` knows its key names.
    """

    video_id: str
    title: str
    uploader: str | None = None
    duration: timedelta | None = None
    width: int | None = None
    height: int | None = None
    is_live: bool = False
    was_live: bool = False
    webpage_url: str | None = None

    @property
    def is_portrait(self) -> bool:
        """True when the frame is taller than it is wide — the Shorts aspect ratio."""
        if self.width is None or self.height is None:
            return False
        return self.height > self.width


def metadata_from_info(info: Mapping[str, Any]) -> VideoMetadata:
    """Narrow yt-dlp's info dict to :class:`VideoMetadata`.

    Tolerant by design: yt-dlp's keys move between releases and vary by extractor, so a
    missing optional key yields ``None`` rather than an error. ``id`` is the only key treated
    as required — without it nothing downstream can be keyed.
    """
    video_id = info.get("id")
    if not isinstance(video_id, str) or not video_id:
        raise ValueError("yt-dlp info has no usable 'id'")

    duration_seconds = info.get("duration")
    duration = (
        timedelta(seconds=float(duration_seconds))
        if isinstance(duration_seconds, int | float) and not isinstance(duration_seconds, bool)
        else None
    )

    return VideoMetadata(
        video_id=video_id,
        title=_as_str(info.get("title")) or video_id,
        uploader=_as_str(info.get("uploader")) or _as_str(info.get("channel")),
        duration=duration,
        width=_as_int(info.get("width")),
        height=_as_int(info.get("height")),
        # `is_live` is live *right now*; `was_live` marks the recording of a finished stream.
        is_live=bool(info.get("is_live")),
        was_live=bool(info.get("was_live")),
        webpage_url=_as_str(info.get("webpage_url")),
    )


def is_short(metadata: VideoMetadata) -> bool:
    """Decide whether a video is a Short, honestly: this is a heuristic.

    YouTube exposes no reliable "is a Short" flag in extracted metadata, so two signals are
    used:

    * the canonical URL uses the ``/shorts/`` path — conclusive when present, but the feed
      and most extractions give the ``/watch?v=`` form even for Shorts, so it usually is not;
    * a **portrait** frame *and* a duration at or under :data:`SHORTS_MAX_SECONDS`.

    Both conditions are required for the second signal, so a 45-second landscape clip is not
    mistaken for a Short. The robust lever remains ``filters.min_duration`` (60s by default),
    which is why the design note calls it "kills most Shorts/teasers".
    """
    if metadata.webpage_url and "/shorts/" in metadata.webpage_url:
        return True
    if metadata.duration is None:
        return False
    return metadata.is_portrait and metadata.duration <= timedelta(seconds=SHORTS_MAX_SECONDS)


def judge(metadata: VideoMetadata, settings: ChannelSettings) -> Judgement:
    """Apply every metadata-level filter, in the order that avoids the most work.

    Livestream state comes first because it is the only rule that can produce a
    :attr:`Verdict.DEFER`, and the duration of a stream still in progress is meaningless.
    """
    live = _judge_livestream(metadata, settings)
    if live is not None:
        return live

    if is_short(metadata) and not settings.shorts:
        return Judgement(Verdict.REJECT, "looks like a Short and shorts are disabled")

    if not matches_title_filters(metadata.title, settings):
        # follow/ already applied these to the feed's title. Re-checked because yt-dlp's
        # title can differ from the feed's, and because fetch/ must be safe to call directly.
        return Judgement(Verdict.REJECT, "title matched the channel's title filters")

    return _judge_duration(metadata, settings)


def _judge_livestream(metadata: VideoMetadata, settings: ChannelSettings) -> Judgement | None:
    """Livestream handling. Returns ``None`` when the video is not stream-related.

    ``never`` refuses stream content outright. ``skip`` and ``after`` both defer while the
    stream is live and then treat the finished recording as an ordinary upload — see
    ``docs/DESIGN.md`` on why those two values coincide under the only reading that leaves
    ``never`` meaningful.
    """
    mode = settings.livestreams
    if metadata.is_live:
        if mode is Livestreams.NEVER:
            return Judgement(Verdict.REJECT, "live now and livestreams=never")
        return Judgement(
            Verdict.DEFER,
            f"live now; livestreams={mode.value} waits for the finished recording",
        )
    if metadata.was_live and mode is Livestreams.NEVER:
        return Judgement(Verdict.REJECT, "a past livestream and livestreams=never")
    return None


def _judge_duration(metadata: VideoMetadata, settings: ChannelSettings) -> Judgement:
    minimum = settings.filters.min_duration
    maximum = settings.filters.max_duration

    if metadata.duration is None:
        if minimum is None and maximum is None:
            return Judgement(Verdict.ACCEPT, "accepted")
        # Accepting is the lesser evil: rejecting loses a real upload permanently, and
        # deferring would retry it forever. In practice a missing duration means a live
        # stream, which was already handled above.
        return Judgement(Verdict.ACCEPT, "accepted — duration unknown, bounds not applied")

    if minimum is not None and metadata.duration < minimum:
        return Judgement(
            Verdict.REJECT,
            f"shorter than min_duration ({_seconds(metadata.duration)}s < {_seconds(minimum)}s)",
        )
    if maximum is not None and metadata.duration > maximum:
        return Judgement(
            Verdict.REJECT,
            f"longer than max_duration ({_seconds(metadata.duration)}s > {_seconds(maximum)}s)",
        )
    return Judgement(Verdict.ACCEPT, "accepted")


def _seconds(value: timedelta) -> int:
    return int(value.total_seconds())


def _as_str(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)
