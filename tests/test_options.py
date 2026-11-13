"""The yt-dlp option builder — where a wrong string is a silent 4K download."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from echo.fetch.options import (
    OUTPUT_TEMPLATE,
    build_format_selector,
    build_options,
    download_directory,
)
from echo.follow.config import ChannelSettings

SettingsFactory = Callable[..., ChannelSettings]
ROOT = Path("/nas/echo")
CHANNEL = "UCXuqSBlHAE6Xw-yeJA0Tunw"


def options(settings: ChannelSettings, **kwargs: Any) -> dict[str, Any]:
    return build_options(settings, root=ROOT, channel_id=CHANNEL, **kwargs)


# --- quality cap ----------------------------------------------------------------------


def test_the_quality_cap_lands_in_the_format_selector(settings: ChannelSettings) -> None:
    assert build_format_selector(settings) == "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"


def test_a_raised_cap_is_honoured(settings_for: SettingsFactory) -> None:
    assert "height<=2160" in build_format_selector(settings_for(max_height=2160))


def test_a_null_cap_means_best_available(settings_for: SettingsFactory) -> None:
    assert build_format_selector(settings_for(max_height=None)) == "bv*+ba/b"


def test_the_last_fallback_is_unconstrained(settings: ChannelSettings) -> None:
    """A channel that only publishes above the cap should still be downloadable."""
    assert build_format_selector(settings).endswith("/bv*+ba/b")


# --- paths and naming -----------------------------------------------------------------


def test_downloads_are_grouped_per_channel() -> None:
    assert download_directory(ROOT, CHANNEL) == ROOT / CHANNEL


def test_a_download_needs_a_channel_to_live_under() -> None:
    with pytest.raises(ValueError, match="channel_id is required"):
        download_directory(ROOT, "")


def test_filenames_use_the_video_id_not_the_title(settings: ChannelSettings) -> None:
    """Titles carry emoji, slashes and 180 characters of clickbait; the id does not."""
    built = options(settings)

    assert built["outtmpl"] == {"default": OUTPUT_TEMPLATE}
    assert "%(title)s" not in OUTPUT_TEMPLATE
    assert built["paths"] == {"home": str(ROOT / CHANNEL)}


def test_the_container_preference_is_the_merge_target(settings_for: SettingsFactory) -> None:
    assert options(settings_for(container="mkv"))["merge_output_format"] == "mkv"


# --- the knobs that must not drift ----------------------------------------------------


def test_a_single_video_never_pulls_a_playlist(settings: ChannelSettings) -> None:
    assert options(settings)["noplaylist"] is True


def test_errors_are_not_swallowed(settings: ChannelSettings) -> None:
    """Echo decides what to retry; ignoreerrors would make a failure look like a success."""
    assert options(settings)["ignoreerrors"] is False


def test_a_finished_file_is_never_overwritten(settings: ChannelSettings) -> None:
    built = options(settings)
    assert built["overwrites"] is False
    assert built["continuedl"] is True


def test_the_worker_does_not_write_progress_bars(settings: ChannelSettings) -> None:
    built = options(settings)
    assert built["quiet"] is True
    assert built["noprogress"] is True
    assert built["no_color"] is True


# --- subtitles ------------------------------------------------------------------------


def test_subtitles_are_requested_for_the_configured_languages(settings: ChannelSettings) -> None:
    built = options(settings)
    assert built["writesubtitles"] is True
    assert built["subtitleslangs"] == ["en"]
    assert built["writeautomaticsub"] is False


def test_auto_captions_are_opt_in(settings_for: SettingsFactory) -> None:
    built = options(settings_for(subtitles={"auto_generated": True}))
    assert built["writeautomaticsub"] is True


@pytest.mark.parametrize(
    "overrides",
    [{"subtitles": {"download": False}}, {"subtitles": {"langs": []}}],
)
def test_subtitles_can_be_switched_off_two_ways(
    overrides: dict[str, object], settings_for: SettingsFactory
) -> None:
    built = options(settings_for(**overrides))
    assert "writesubtitles" not in built
    assert "subtitleslangs" not in built


# --- SponsorBlock ---------------------------------------------------------------------


def test_sponsorblock_mark_only_writes_chapters(settings: ChannelSettings) -> None:
    """mark keeps every frame — nothing is cut, so it is safe as the default."""
    processors = options(settings)["postprocessors"]
    assert processors == [
        {
            "key": "SponsorBlock",
            "categories": ["sponsor", "selfpromo", "interaction"],
            "when": "after_filter",
        }
    ]


def test_sponsorblock_remove_adds_the_chapter_cut(settings_for: SettingsFactory) -> None:
    processors = options(settings_for(sponsorblock={"mode": "remove"}))["postprocessors"]
    assert isinstance(processors, list)
    assert [step["key"] for step in processors] == ["SponsorBlock", "ModifyChapters"]
    assert processors[1]["remove_sponsor_segments"] == ["sponsor", "selfpromo", "interaction"]


@pytest.mark.parametrize(
    "overrides",
    [{"sponsorblock": {"mode": "off"}}, {"sponsorblock": {"categories": []}}],
)
def test_sponsorblock_off_adds_no_postprocessors(
    overrides: dict[str, object], settings_for: SettingsFactory
) -> None:
    assert "postprocessors" not in options(settings_for(**overrides))


# --- egress ---------------------------------------------------------------------------


def test_the_proxy_is_only_set_when_given(settings: ChannelSettings) -> None:
    """proton_vpn in the config is the toggle; the credential-bearing URL comes from a Secret."""
    assert "proxy" not in options(settings)
    assert options(settings, proxy="socks5://egress:1080")["proxy"] == "socks5://egress:1080"


def test_the_config_file_cannot_smuggle_in_a_proxy(settings_for: SettingsFactory) -> None:
    """proton_vpn: true must not, by itself, produce a proxy — there is no URL in the file."""
    assert "proxy" not in options(settings_for(proton_vpn=True))


# --- the probe pass -------------------------------------------------------------------


def test_the_probe_pass_downloads_nothing(settings: ChannelSettings) -> None:
    built = options(settings, probe_only=True)

    assert built["skip_download"] is True
    for key in ("postprocessors", "writesubtitles", "paths", "outtmpl"):
        assert key not in built


def test_the_probe_shares_the_format_and_proxy_with_the_download(
    settings: ChannelSettings,
) -> None:
    """The probe must judge the same thing the download would fetch."""
    probe = options(settings, probe_only=True, proxy="http://egress:8080")
    full = options(settings, proxy="http://egress:8080")

    assert probe["format"] == full["format"]
    assert probe["proxy"] == full["proxy"]
