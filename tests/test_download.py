"""The fetcher: probe → judge → download → report, with yt-dlp faked out.

Every path is covered without network access, which matters most for the failure paths: those
are the ones that decide whether a video is retried or written off, and they are exactly the
paths a live test could never trigger on demand.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.utils import DownloadError

from echo.fetch.download import FetchOutcome, VideoFetcher, watch_url
from echo.follow.config import ChannelSettings

SettingsFactory = Callable[..., ChannelSettings]

VIDEO_ID = "dQw4w9WgXcQ"
CHANNEL = "UCXuqSBlHAE6Xw-yeJA0Tunw"
ROOT = Path("/nas/echo")

PROBE_INFO: dict[str, Any] = {
    "id": VIDEO_ID,
    "title": "We Built a PC Inside a Fish Tank",
    "uploader": "Linus Tech Tips",
    "duration": 742,
    "width": 1920,
    "height": 1080,
}

DOWNLOAD_INFO: dict[str, Any] = {
    **PROBE_INFO,
    "requested_downloads": [
        {"filepath": f"/nas/echo/{CHANNEL}/{VIDEO_ID}.mp4", "filesize": 524_288_000}
    ],
}


class FakeDownloader:
    """Records the options it was built with and the calls it received."""

    def __init__(self, options: dict[str, Any], script: list[Any]) -> None:
        self.options = options
        self._script = script

    def extract_info(self, url: str, *, download: bool = True) -> Any:
        if not self._script:
            raise AssertionError(f"unexpected extract_info({url!r}, download={download})")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class Spy:
    """A downloader factory that replays a script of results or exceptions."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.builds: list[dict[str, Any]] = []

    def __call__(self, options: dict[str, Any]) -> FakeDownloader:
        self.builds.append(options)
        return FakeDownloader(options, self.script)


def fetcher(spy: Spy, **kwargs: Any) -> VideoFetcher:
    return VideoFetcher(root=ROOT, downloader_factory=spy, **kwargs)


def fetch(spy: Spy, settings: ChannelSettings, **kwargs: Any) -> Any:
    return fetcher(spy, **kwargs).fetch(VIDEO_ID, channel_id=CHANNEL, settings=settings)


# --- the happy path -------------------------------------------------------------------


def test_an_accepted_video_is_downloaded_and_reported(settings: ChannelSettings) -> None:
    spy = Spy(PROBE_INFO, DOWNLOAD_INFO)

    report = fetch(spy, settings)

    assert report.outcome is FetchOutcome.DOWNLOADED
    assert report.succeeded is True
    assert report.retryable is False
    assert report.media is not None
    assert report.media.path == Path(f"/nas/echo/{CHANNEL}/{VIDEO_ID}.mp4")
    assert report.media.filesize == 524_288_000
    assert report.media.duration == timedelta(seconds=742)


def test_the_verbatim_title_survives_into_the_report(settings: ChannelSettings) -> None:
    """Artemis cannot hold the title, so this value is what Echo stores — unnormalized."""
    report = fetch(Spy(PROBE_INFO, DOWNLOAD_INFO), settings)

    assert report.media is not None
    assert report.media.title == "We Built a PC Inside a Fish Tank"


def test_the_canonical_source_is_the_bare_video_id(settings: ChannelSettings) -> None:
    """Locked 2026-11-13: source is exact-match in Artemis, so the form must never vary."""
    report = fetch(Spy(PROBE_INFO, DOWNLOAD_INFO), settings)

    assert report.media is not None
    assert report.media.source == VIDEO_ID
    assert watch_url(report.media.source) == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_the_probe_runs_before_the_download_and_spends_no_bandwidth(
    settings: ChannelSettings,
) -> None:
    spy = Spy(PROBE_INFO, DOWNLOAD_INFO)

    fetch(spy, settings)

    assert len(spy.builds) == 2
    assert spy.builds[0]["skip_download"] is True
    assert "skip_download" not in spy.builds[1]


def test_the_proxy_reaches_both_passes(settings: ChannelSettings) -> None:
    spy = Spy(PROBE_INFO, DOWNLOAD_INFO)

    fetch(spy, settings, proxy="socks5://egress:1080")

    assert [build.get("proxy") for build in spy.builds] == ["socks5://egress:1080"] * 2


def test_watch_url_requires_an_id() -> None:
    with pytest.raises(ValueError, match="video_id is required"):
        watch_url("")


# --- filtered: judged, never downloaded ------------------------------------------------


def test_a_filtered_video_is_never_downloaded(settings_for: SettingsFactory) -> None:
    """The second extract_info would raise from the fake, which is the assertion."""
    spy = Spy({**PROBE_INFO, "duration": 20})

    report = fetch(spy, settings_for(filters={"min_duration": "60s"}))

    assert report.outcome is FetchOutcome.FILTERED
    assert report.retryable is False
    assert "shorter than min_duration" in report.reason
    assert report.media is None
    assert len(spy.builds) == 1


def test_a_filtered_report_still_carries_the_metadata(settings_for: SettingsFactory) -> None:
    """echo-ui needs the title to explain *which* video was skipped and why."""
    report = fetch(
        Spy({**PROBE_INFO, "duration": 20}), settings_for(filters={"min_duration": "60s"})
    )

    assert report.metadata is not None
    assert report.metadata.title == "We Built a PC Inside a Fish Tank"


def test_a_live_stream_is_deferred_and_stays_retryable(settings_for: SettingsFactory) -> None:
    """Deferred, not filtered: recording it as seen would lose the finished recording."""
    spy = Spy({**PROBE_INFO, "is_live": True, "duration": None})

    report = fetch(spy, settings_for(livestreams="skip"))

    assert report.outcome is FetchOutcome.DEFERRED
    assert report.retryable is True
    assert len(spy.builds) == 1


# --- failure classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable",
        "ERROR: [youtube] dQw4w9WgXcQ: This video has been removed by the uploader",
        "ERROR: [youtube] dQw4w9WgXcQ: Join this channel to get access to members-only content",
    ],
)
def test_a_permanently_gone_video_is_terminal(message: str, settings: ChannelSettings) -> None:
    """Retrying a deleted video every 30 minutes forever is noise, not resilience."""
    report = fetch(Spy(DownloadError(message)), settings)

    assert report.outcome is FetchOutcome.FILTERED
    assert report.retryable is False
    assert "probe failed" in report.reason


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Sign in to confirm you're not a bot",
        "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
        "ERROR: [youtube] Unable to extract player response",
        "ERROR: something nobody has seen before",
    ],
)
def test_an_unrecognised_failure_is_retried(message: str, settings: ChannelSettings) -> None:
    """Bot-checks and extractor breakage are the normal weather here, not the end state."""
    report = fetch(Spy(DownloadError(message)), settings)

    assert report.outcome is FetchOutcome.FAILED
    assert report.retryable is True


def test_a_failure_during_the_download_is_reported_with_its_stage(
    settings: ChannelSettings,
) -> None:
    report = fetch(Spy(PROBE_INFO, DownloadError("ERROR: HTTP Error 403: Forbidden")), settings)

    assert report.outcome is FetchOutcome.FAILED
    assert report.reason.startswith("download failed:")


def test_a_probe_that_returns_nothing_is_a_failure(settings: ChannelSettings) -> None:
    report = fetch(Spy(None), settings)

    assert report.outcome is FetchOutcome.FAILED
    assert "no metadata" in report.reason


def test_a_probe_without_an_id_is_a_failure(settings: ChannelSettings) -> None:
    report = fetch(Spy({"title": "no id here"}), settings)

    assert report.outcome is FetchOutcome.FAILED
    assert "no usable 'id'" in report.reason


def test_a_download_that_names_no_file_is_a_failure_not_a_success(
    settings: ChannelSettings,
) -> None:
    """Reporting success here would strand ingest/ on a missing path, far from the cause."""
    report = fetch(Spy(PROBE_INFO, {**PROBE_INFO, "requested_downloads": []}), settings)

    assert report.outcome is FetchOutcome.FAILED
    assert "no output file path" in report.reason
    assert report.media is None


# --- reading yt-dlp's result shapes -----------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"requested_downloads": [{"filepath": "/nas/echo/c/v.mp4"}]},
        {"requested_downloads": [{"_filename": "/nas/echo/c/v.mp4"}]},
        {"filepath": "/nas/echo/c/v.mp4"},
        {"_filename": "/nas/echo/c/v.mp4"},
    ],
)
def test_the_output_path_is_read_from_any_known_shape(
    result: dict[str, Any], settings: ChannelSettings
) -> None:
    """yt-dlp's keys differ by version and postprocessor; the path is never reconstructed."""
    report = fetch(Spy(PROBE_INFO, {**PROBE_INFO, **result}), settings)

    assert report.media is not None
    assert report.media.path == Path("/nas/echo/c/v.mp4")


def test_written_subtitles_are_collected(settings: ChannelSettings) -> None:
    report = fetch(
        Spy(
            PROBE_INFO,
            {
                **DOWNLOAD_INFO,
                "requested_subtitles": {
                    "en": {"filepath": f"/nas/echo/{CHANNEL}/{VIDEO_ID}.en.vtt"},
                    "fr": {"url": "https://example.invalid/none"},
                },
            },
        ),
        settings,
    )

    assert report.media is not None
    assert report.media.subtitle_paths == (Path(f"/nas/echo/{CHANNEL}/{VIDEO_ID}.en.vtt"),)


def test_an_approximate_filesize_is_accepted(settings: ChannelSettings) -> None:
    report = fetch(
        Spy(
            PROBE_INFO,
            {**PROBE_INFO, "requested_downloads": [{"filepath": "/x.mp4", "filesize_approx": 99}]},
        ),
        settings,
    )

    assert report.media is not None
    assert report.media.filesize == 99


def test_a_missing_filesize_is_not_invented(settings: ChannelSettings) -> None:
    report = fetch(Spy(PROBE_INFO, {**PROBE_INFO, "filepath": "/x.mp4"}), settings)

    assert report.media is not None
    assert report.media.filesize is None
