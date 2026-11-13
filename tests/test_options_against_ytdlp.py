"""Check the options against the real yt-dlp, offline.

``test_options.py`` asserts on the dict Echo builds; it cannot catch the failure that
actually matters — yt-dlp renaming an option or a postprocessor key, which would make every
unit test pass while nothing downloads. These tests hand the options to the real
``YoutubeDL`` and let it validate them. No network: constructing a downloader and compiling a
format selector touch nothing remote.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL

from echo.fetch.options import build_format_selector, build_options
from echo.follow.config import ChannelSettings

SettingsFactory = Callable[..., ChannelSettings]
CHANNEL = "UCXuqSBlHAE6Xw-yeJA0Tunw"


@pytest.mark.parametrize("sponsorblock_mode", ["off", "mark", "remove"])
@pytest.mark.parametrize("probe_only", [True, False])
def test_yt_dlp_accepts_the_options_echo_builds(
    sponsorblock_mode: str, probe_only: bool, settings_for: SettingsFactory, tmp_path: Path
) -> None:
    settings = settings_for(sponsorblock={"mode": sponsorblock_mode})
    options = build_options(settings, root=tmp_path, channel_id=CHANNEL, probe_only=probe_only)

    with YoutubeDL(options):
        pass  # construction validates option names and resolves postprocessor keys


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", []),
        ("mark", ["SponsorBlockPP"]),
        ("remove", ["SponsorBlockPP", "ModifyChaptersPP"]),
    ],
)
def test_the_sponsorblock_keys_resolve_to_real_postprocessors(
    mode: str, expected: list[str], settings_for: SettingsFactory, tmp_path: Path
) -> None:
    """A renamed postprocessor key is the failure a dict assertion cannot see."""
    options = build_options(
        settings_for(sponsorblock={"mode": mode}), root=tmp_path, channel_id=CHANNEL
    )

    with YoutubeDL(options) as ydl:
        loaded = [type(pp).__name__ for queue in ydl._pps.values() for pp in queue]

    assert loaded == expected


@pytest.mark.parametrize("max_height", [None, 720, 1080, 2160])
def test_yt_dlp_can_compile_the_format_selector(
    max_height: int | None, settings_for: SettingsFactory, tmp_path: Path
) -> None:
    """A malformed selector is a silent 'no formats found' at download time."""
    settings = settings_for(max_height=max_height)
    options = build_options(settings, root=tmp_path, channel_id=CHANNEL)

    with YoutubeDL(options) as ydl:
        assert ydl.build_format_selector(build_format_selector(settings)) is not None
