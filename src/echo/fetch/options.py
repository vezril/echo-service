"""Turn :class:`~echo.follow.config.ChannelSettings` into yt-dlp options.

A pure function, deliberately: the download configuration is where quality caps, SponsorBlock
and subtitles actually take effect, and a wrong format string is a silent 4K download. Keeping
it free of I/O means every knob is asserted in tests rather than discovered on the NAS.

**Filenames use the video id, never the title.** YouTube titles carry emoji, slashes, quotes
and 180 characters of clickbait; the verbatim title lives in Echo's own state (see
``docs/DESIGN.md``), so the file on disk only needs to be stable and collision-free. Files
land under ``<root>/<channel_id>/<video_id>.<ext>``, which also makes the download tree easy
to exclude from backup — it is re-fetchable content, the same argument ``media-backup.md``
makes for derivatives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from echo.follow.config import ChannelSettings, SponsorBlockMode

__all__ = ["OUTPUT_TEMPLATE", "build_format_selector", "build_options", "download_directory"]

#: ``%(ext)s`` is resolved by yt-dlp after the merge, so this names the final container.
OUTPUT_TEMPLATE: Final = "%(id)s.%(ext)s"

_SOCKET_TIMEOUT: Final = 30
_RETRIES: Final = 3


def build_format_selector(settings: ChannelSettings) -> str:
    """Build the format selector for the channel's quality cap.

    ``bv*[height<=N]+ba`` prefers separate best-video + best-audio streams (which is how
    YouTube serves anything above 720p), falling back to a pre-muxed stream under the cap and
    finally to whatever exists. The last fallback is unconstrained on purpose: a channel that
    only publishes above the cap should still be downloadable rather than silently skipped.
    """
    if settings.max_height is None:
        return "bv*+ba/b"
    cap = settings.max_height
    return f"bv*[height<={cap}]+ba/b[height<={cap}]/bv*+ba/b"


def download_directory(root: Path, channel_id: str) -> Path:
    """The directory a channel's downloads land in."""
    if not channel_id:
        raise ValueError("channel_id is required to place a download")
    return Path(root) / channel_id


def build_options(
    settings: ChannelSettings,
    *,
    root: Path,
    channel_id: str,
    proxy: str | None = None,
    probe_only: bool = False,
) -> dict[str, Any]:
    """Build the yt-dlp option dict for one channel.

    ``probe_only`` builds the metadata-extraction pass: no download, no postprocessors, no
    subtitle writes. The two passes share this function so the probe cannot drift from the
    download it is deciding on.

    ``proxy`` is the optional ProtonVPN egress. It is passed in by the caller from a Secret
    and never read from the config file — ``proton_vpn`` in the config is only the toggle.
    """
    options: dict[str, Any] = {
        # Echo is a worker: progress bars and colour go nowhere useful, and yt-dlp's own
        # stdout chatter would drown the service's logs.
        "quiet": True,
        "no_warnings": False,
        "no_color": True,
        "noprogress": True,
        # A feed entry is one video. Without this, a video that happens to carry a playlist
        # parameter would pull the entire playlist.
        "noplaylist": True,
        # Fail loudly per video. Echo decides what to retry; yt-dlp swallowing errors would
        # make a failed download look like a successful one.
        "ignoreerrors": False,
        "socket_timeout": _SOCKET_TIMEOUT,
        "retries": _RETRIES,
        "format": build_format_selector(settings),
    }

    if proxy:
        options["proxy"] = proxy

    if probe_only:
        options["skip_download"] = True
        return options

    options["paths"] = {"home": str(download_directory(root, channel_id))}
    options["outtmpl"] = {"default": OUTPUT_TEMPLATE}
    options["merge_output_format"] = settings.container
    # Only resume/overwrite behaviour that cannot corrupt a finished file: a partial download
    # continues, a complete one is left alone rather than re-fetched.
    options["continuedl"] = True
    options["overwrites"] = False

    if settings.subtitles.download and settings.subtitles.langs:
        options["writesubtitles"] = True
        options["subtitleslangs"] = list(settings.subtitles.langs)
        options["writeautomaticsub"] = settings.subtitles.auto_generated

    postprocessors = _sponsorblock_postprocessors(settings)
    if postprocessors:
        options["postprocessors"] = postprocessors

    return options


def _sponsorblock_postprocessors(settings: ChannelSettings) -> list[dict[str, Any]]:
    """SponsorBlock: nothing, chapter marks, or cut the segments out.

    ``mark`` only queries the SponsorBlock API and writes chapters, so the file keeps every
    frame. ``remove`` additionally runs ``ModifyChapters``, which re-encodes around the cuts —
    irreversible, which is why ``mark`` is the default.
    """
    mode = settings.sponsorblock.mode
    if mode is SponsorBlockMode.OFF or not settings.sponsorblock.categories:
        return []

    categories = list(settings.sponsorblock.categories)
    processors: list[dict[str, Any]] = [
        # `after_filter` is yt-dlp's documented placement: run once the video has survived
        # filtering, before the download work that depends on the chapter data.
        {"key": "SponsorBlock", "categories": categories, "when": "after_filter"}
    ]
    if mode is SponsorBlockMode.REMOVE:
        processors.append({"key": "ModifyChapters", "remove_sponsor_segments": categories})
    return processors
