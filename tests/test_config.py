"""The channel-follow config: inheritance, deep-merge, and refusing bad files.

The file is hand-edited, so these tests care as much about what is *rejected* as what parses:
a config that half-applies is the failure mode worth paying tests for.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from echo.follow import config as cfg
from echo.follow.config import (
    ConfigError,
    Livestreams,
    SponsorBlockMode,
    load_config,
    parse_config,
    parse_duration,
)

MINIMAL = {
    "version": 1,
    "channels": [{"id": "UCXuqSBlHAE6Xw-yeJA0Tunw", "name": "Linus Tech Tips"}],
}


def test_a_minimal_file_inherits_every_documented_default() -> None:
    settings = parse_config(MINIMAL).channels[0].settings

    assert settings.poll_interval == timedelta(minutes=30)
    assert settings.backfill == 3
    assert settings.proton_vpn is True
    assert settings.max_height == 1080
    assert settings.container == "mp4"
    assert settings.shorts is False
    assert settings.livestreams is Livestreams.SKIP
    assert settings.subtitles.langs == ("en",)
    assert settings.sponsorblock.mode is SponsorBlockMode.MARK
    assert settings.filters.min_duration == timedelta(seconds=60)
    assert settings.catalog.pool_per_channel is True
    assert settings.notify.stale_after == timedelta(days=14)


def test_defaults_block_overrides_builtins() -> None:
    parsed = parse_config(
        {**MINIMAL, "defaults": {"proton_vpn": False, "max_height": 2160, "backfill": 0}}
    )
    settings = parsed.channels[0].settings
    assert settings.proton_vpn is False
    assert settings.max_height == 2160
    assert settings.backfill == 0


def test_nested_blocks_deep_merge_per_key() -> None:
    """The schema's promise: `notify: {stale_after: 30d}` keeps the inherited on_available."""
    parsed = parse_config(
        {
            "version": 1,
            "defaults": {"notify": {"on_available": True, "stale_after": "14d"}},
            "channels": [{"id": "UC" + "a" * 22, "notify": {"stale_after": "30d"}}],
        }
    )
    notify = parsed.channels[0].settings.notify
    assert notify.stale_after == timedelta(days=30)
    assert notify.on_available is True


def test_lists_replace_rather_than_append() -> None:
    """extra_tags must be overridable *down*, so a list is a replacement, not an append."""
    parsed = parse_config(
        {
            "version": 1,
            "defaults": {"catalog": {"extra_tags": ["youtube", "inherited"]}},
            "channels": [{"id": "UC" + "b" * 22, "catalog": {"extra_tags": ["cooking"]}}],
        }
    )
    assert parsed.channels[0].settings.catalog.extra_tags == ("cooking",)


def test_channel_overrides_do_not_leak_between_channels() -> None:
    parsed = parse_config(
        {
            "version": 1,
            "channels": [
                {"id": "UC" + "c" * 22, "max_height": 2160, "shorts": True},
                {"id": "UC" + "d" * 22},
            ],
        }
    )
    assert parsed.channels[0].settings.max_height == 2160
    assert parsed.channels[1].settings.max_height == 1080
    assert parsed.channels[1].settings.shorts is False


def test_enabled_false_pauses_without_removing() -> None:
    parsed = parse_config(
        {
            "version": 1,
            "channels": [
                {"id": "UC" + "e" * 22, "name": "live"},
                {"id": "UC" + "f" * 22, "name": "paused", "enabled": False},
            ],
        }
    )
    assert len(parsed.channels) == 2
    assert [c.name for c in parsed.enabled_channels] == ["live"]


def test_handle_and_url_entries_need_resolution() -> None:
    parsed = parse_config(
        {
            "version": 1,
            "channels": [
                {"handle": "@veritasium"},
                {"url": "https://www.youtube.com/@3blue1brown"},
                {"id": "UC" + "g" * 22},
            ],
        }
    )
    assert [c.needs_resolution for c in parsed.channels] == [True, True, False]
    assert parsed.channels[0].label == "@veritasium"


def test_label_prefers_the_human_name() -> None:
    channel = parse_config(MINIMAL).channels[0]
    assert channel.label == "Linus Tech Tips"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("2h", timedelta(hours=2)),
        ("14d", timedelta(days=14)),
        ("1w", timedelta(weeks=1)),
        (" 45m ", timedelta(minutes=45)),
        (120, timedelta(seconds=120)),
    ],
)
def test_parse_duration_accepts_the_documented_forms(raw: object, expected: timedelta) -> None:
    assert parse_duration(raw, where="t") == expected


@pytest.mark.parametrize("raw", ["", "30", "30 m", "1h30m", "-5m", "soon", True, 1.5, -1])
def test_parse_duration_rejects_everything_else(raw: object) -> None:
    """Compound and unitless forms are rejected: the docs describe neither."""
    with pytest.raises(ConfigError):
        parse_duration(raw, where="t")


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({"version": 2, "channels": [{"id": "UC" + "h" * 22}]}, "version must be 1"),
        ({"channels": [{"id": "UC" + "h" * 22}]}, "version must be 1"),
        ({"version": 1}, "no 'channels'"),
        ({"version": 1, "channels": []}, "empty"),
        ({"version": 1, "channels": [{}]}, "needs one of id, handle or url"),
        (
            {"version": 1, "channels": [{"id": "UC" + "i" * 22, "handle": "@x"}]},
            "exactly one of id/handle/url",
        ),
        ({"version": 1, "channels": [{"id": "@veritasium"}]}, "not a channel_id"),
        ({"version": 1, "channels": [{"id": "UC-too-short"}]}, "not a channel_id"),
        (
            {"version": 1, "channels": [{"id": "UC" + "j" * 22, "max_hieght": 720}]},
            "unknown key",
        ),
        ({"version": 1, "defaults": {"nope": 1}, "channels": [{"id": "UC" + "k" * 22}]}, "unknown"),
        (
            {"version": 1, "channels": [{"id": "UC" + "l" * 22, "notify": {"stale": "1d"}}]},
            "unknown key",
        ),
        (
            {"version": 1, "channels": [{"id": "UC" + "m" * 22, "livestreams": "maybe"}]},
            "expected one of",
        ),
        (
            {"version": 1, "channels": [{"id": "UC" + "n" * 22, "backfill": -1}]},
            "cannot be negative",
        ),
        (
            {"version": 1, "channels": [{"id": "UC" + "o" * 22, "max_height": 0}]},
            "must be positive",
        ),
        (
            {"version": 1, "channels": [{"id": "UC" + "p" * 22, "poll_interval": "0s"}]},
            "must be positive",
        ),
        (
            {
                "version": 1,
                "channels": [
                    {"id": "UC" + "q" * 22, "filters": {"title_exclude": "(unclosed"}},
                ],
            },
            "not a valid regex",
        ),
        (
            {
                "version": 1,
                "channels": [
                    {
                        "id": "UC" + "r" * 22,
                        "filters": {"min_duration": "2h", "max_duration": "60s"},
                    }
                ],
            },
            "exceeds max_duration",
        ),
        (
            {"version": 1, "channels": [{"id": "UC" + "s" * 22, "shorts": "yes"}]},
            "expected true or false",
        ),
        ({"version": 1, "channels": "UC" + "t" * 22}, "expected a list"),
        (
            {
                "version": 1,
                "channels": [{"id": "UC" + "u" * 22}, {"id": "UC" + "u" * 22}],
            },
            "duplicate id",
        ),
        (
            {"version": 1, "channels": [{"handle": "@dup"}, {"handle": "@DUP"}]},
            "duplicate handle",
        ),
    ],
)
def test_bad_configs_are_rejected_with_a_locating_message(doc: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(doc)


def test_an_unknown_key_message_lists_what_is_known() -> None:
    with pytest.raises(ConfigError, match="poll_interval"):
        parse_config({"version": 1, "channels": [{"id": "UC" + "v" * 22, "poll_interva": "5m"}]})


def test_error_messages_point_at_the_offending_channel() -> None:
    with pytest.raises(ConfigError, match=r"channels\[1\]"):
        parse_config(
            {
                "version": 1,
                "channels": [{"id": "UC" + "w" * 22}, {"id": "UC" + "x" * 22, "backfill": -4}],
            }
        )


def test_null_disables_the_optional_knobs() -> None:
    parsed = parse_config(
        {
            "version": 1,
            "channels": [
                {
                    "id": "UC" + "y" * 22,
                    "max_height": None,
                    "notify": {"stale_after": None},
                    "filters": {"min_duration": None},
                }
            ],
        }
    )
    settings = parsed.channels[0].settings
    assert settings.max_height is None
    assert settings.notify.stale_after is None
    assert settings.filters.min_duration is None


def test_the_schema_example_from_the_design_note_parses(tmp_path: Path) -> None:
    """The documented example must stay loadable — docs and parser drifting is the real risk."""
    file = tmp_path / "channels.yaml"
    file.write_text(
        """
version: 1

defaults:
  poll_interval: 30m
  backfill: 3
  proton_vpn: true
  max_height: 1080
  container: mp4
  shorts: false
  livestreams: skip
  subtitles:
    download: true
    langs: [en]
    auto_generated: false
  sponsorblock:
    mode: mark
    categories: [sponsor, selfpromo, interaction]
  filters:
    min_duration: 60s
    max_duration: null
    title_include: null
    title_exclude: null
  catalog:
    pool_per_channel: true
    uploader_as_artist: true
    extra_tags: []
  notify:
    on_available: true
    stale_after: 14d

channels:
  - id: "UCXuqSBlHAE6Xw-yeJA0Tunw"
    name: "Linus Tech Tips"
  - handle: "@veritasium"
  - id: "UCsXVk37bltHxD1rDPwtNM8Q"
    name: "A cooking channel worth 4K"
    max_height: 2160
    shorts: true
    catalog: { extra_tags: [cooking, recipes] }
    notify: { stale_after: 30d }
    enabled: false
""",
        encoding="utf-8",
    )

    parsed = load_config(file)

    assert len(parsed.channels) == 3
    assert len(parsed.enabled_channels) == 2
    cooking = parsed.channels[2]
    assert cooking.settings.max_height == 2160
    assert cooking.settings.catalog.extra_tags == ("cooking", "recipes")
    assert cooking.settings.catalog.pool_per_channel is True  # inherited through the merge
    assert cooking.settings.notify.stale_after == timedelta(days=30)
    assert cooking.enabled is False


def test_builtin_defaults_cover_exactly_the_setting_keys() -> None:
    """A new knob must land in BUILTIN_DEFAULTS, or a minimal config would KeyError."""
    assert set(cfg.BUILTIN_DEFAULTS) == set(cfg._SETTING_KEYS)
    for key in cfg._NESTED_KEYS:
        assert isinstance(cfg.BUILTIN_DEFAULTS[key], dict)


def test_load_config_reports_unreadable_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "absent.yaml")

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config(empty)

    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\nchannels: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(broken)


def test_the_config_loader_does_not_execute_yaml_tags(tmp_path: Path) -> None:
    """safe_load, not load: the file is hand-edited and must never be a code path."""
    file = tmp_path / "evil.yaml"
    file.write_text(
        "version: 1\nchannels: !!python/object/apply:os.system ['true']\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(file)
