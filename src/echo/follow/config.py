"""The channel-follow config: parse, deep-merge, validate.

The schema is specified in ``codex/docs/echo.md`` ("Channel-follow config schema"). This
module is its only reader; every other module receives already-resolved
:class:`ChannelSettings` rather than raw mappings, so the YAML shape is known in exactly one
place.

Three layers resolve per channel, later winning per key:

1. :data:`BUILTIN_DEFAULTS` — the documented defaults, so a minimal file is valid
2. the file's ``defaults:`` block
3. the channel's own overrides

Nested blocks **deep-merge per key**, which is the behaviour the schema promises: a channel
setting ``notify: {stale_after: 30d}`` keeps ``on_available`` from the layer beneath instead
of replacing the whole block.

Unknown keys are rejected rather than ignored. A silently-dropped ``max_hieght`` would look
like a working config that quietly downloads 4K forever, and this file is hand-edited.

Secrets never appear here — the ProtonVPN credentials come from a Secret (see
:mod:`echo.fetch`), and ``proton_vpn`` is only the on/off toggle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "BUILTIN_DEFAULTS",
    "Catalog",
    "Channel",
    "ChannelSettings",
    "ConfigError",
    "EchoConfig",
    "Filters",
    "Livestreams",
    "Notify",
    "SponsorBlock",
    "SponsorBlockMode",
    "Subtitles",
    "load_config",
    "parse_config",
    "parse_duration",
]

SUPPORTED_VERSION: Final = 1

_DURATION_RE: Final = re.compile(r"^(?P<value>\d+)(?P<unit>[smhdw])$")
_UNIT_SECONDS: Final[Mapping[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

# The channel_id the RSS feed keys on: "UC" plus 22 base64url characters.
CHANNEL_ID_RE: Final = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class ConfigError(ValueError):
    """The config file is unusable, with the location of the offending key.

    Raised instead of falling back to a default: a worker that silently ignores half a
    hand-written config is worse than one that refuses to start.
    """


class Livestreams(StrEnum):
    """How to treat a livestream on a followed channel."""

    SKIP = "skip"
    AFTER = "after"
    NEVER = "never"


class SponsorBlockMode(StrEnum):
    """What to do with SponsorBlock segments: nothing, chapter marks, or cut them out."""

    OFF = "off"
    MARK = "mark"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class Subtitles:
    """Subtitle download preferences. ``langs=[]`` means none."""

    download: bool
    langs: tuple[str, ...]
    auto_generated: bool


@dataclass(frozen=True, slots=True)
class SponsorBlock:
    """SponsorBlock handling; ``categories`` is passed through to yt-dlp."""

    mode: SponsorBlockMode
    categories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Filters:
    """Pre-download filters — drop noise before spending bandwidth on it.

    Only ``title_include`` / ``title_exclude`` are decidable from the RSS feed; the duration
    bounds need per-video metadata and are therefore applied by :mod:`echo.fetch` against
    yt-dlp's extracted info (see :func:`echo.follow.feed.parse_feed` on what the feed does
    and does not carry).
    """

    min_duration: timedelta | None
    max_duration: timedelta | None
    title_include: re.Pattern[str] | None
    title_exclude: re.Pattern[str] | None


@dataclass(frozen=True, slots=True)
class Catalog:
    """How a channel's videos land in Artemis.

    ``pool_per_channel`` and ``uploader_as_artist`` are the two mappings verified to work
    against Artemis; the title does *not* map to searchable text (``docs/DESIGN.md`` §4).
    """

    pool_per_channel: bool
    uploader_as_artist: bool
    extra_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Notify:
    """The two notifications. Retention stays manual — neither of these deletes anything.

    ``stale_after = None`` disables the nudge for the channel. The nudge additionally needs
    Artemis watched-state, which does not exist yet (``docs/DESIGN.md`` §2–3), so a channel
    may carry a window that nothing can act on; that is intentional config-ahead-of-feature.
    """

    on_available: bool
    stale_after: timedelta | None


@dataclass(frozen=True, slots=True)
class ChannelSettings:
    """Every knob, fully resolved for one channel — no further merging needed."""

    poll_interval: timedelta
    backfill: int
    proton_vpn: bool
    max_height: int | None
    container: str
    shorts: bool
    livestreams: Livestreams
    subtitles: Subtitles
    sponsorblock: SponsorBlock
    filters: Filters
    catalog: Catalog
    notify: Notify


@dataclass(frozen=True, slots=True)
class Channel:
    """A followed channel: its identity, plus its resolved settings.

    Exactly one identity key is given in the file. ``channel_id`` is canonical and is what
    the feed keys on; ``handle``/``url`` are a convenience that :mod:`echo.follow.resolve`
    turns into a ``channel_id`` once, which Echo then records so a handle rename cannot lose
    the subscription.
    """

    channel_id: str | None
    handle: str | None
    url: str | None
    name: str | None
    enabled: bool
    settings: ChannelSettings

    @property
    def label(self) -> str:
        """A stable human label for logs and notifications."""
        return self.name or self.channel_id or self.handle or self.url or "<unidentified>"

    @property
    def needs_resolution(self) -> bool:
        """True when the ``channel_id`` must be resolved from a handle or URL first."""
        return self.channel_id is None


@dataclass(frozen=True, slots=True)
class EchoConfig:
    """A parsed config file."""

    version: int
    channels: tuple[Channel, ...]

    @property
    def enabled_channels(self) -> tuple[Channel, ...]:
        """The channels to poll. ``enabled: false`` pauses one without losing its config."""
        return tuple(c for c in self.channels if c.enabled)


# The defaults documented in codex/docs/echo.md, so a file may omit anything.
BUILTIN_DEFAULTS: Final[Mapping[str, Any]] = {
    "poll_interval": "30m",
    "backfill": 3,
    "proton_vpn": True,
    "max_height": 1080,
    "container": "mp4",
    "shorts": False,
    "livestreams": "skip",
    "subtitles": {"download": True, "langs": ["en"], "auto_generated": False},
    "sponsorblock": {"mode": "mark", "categories": ["sponsor", "selfpromo", "interaction"]},
    "filters": {
        "min_duration": "60s",
        "max_duration": None,
        "title_include": None,
        "title_exclude": None,
    },
    "catalog": {"pool_per_channel": True, "uploader_as_artist": True, "extra_tags": []},
    "notify": {"on_available": True, "stale_after": "14d"},
}

_IDENTITY_KEYS: Final = ("id", "handle", "url")
_CHANNEL_META_KEYS: Final = (*_IDENTITY_KEYS, "name", "enabled")
_SETTING_KEYS: Final = tuple(BUILTIN_DEFAULTS)
_NESTED_KEYS: Final = ("subtitles", "sponsorblock", "filters", "catalog", "notify")


def parse_duration(raw: object, *, where: str) -> timedelta:
    """Parse ``90s`` / ``30m`` / ``2h`` / ``14d`` / ``1w``, or a bare integer of seconds.

    Compound forms (``1h30m``) are deliberately unsupported: the schema never uses them, and
    accepting a form the docs don't describe invites config that no longer matches them.
    """
    if isinstance(raw, bool):  # bool is an int subclass; "poll_interval: true" is a mistake
        raise ConfigError(f"{where}: expected a duration like '30m', got a boolean")
    if isinstance(raw, int):
        if raw < 0:
            raise ConfigError(f"{where}: duration cannot be negative, got {raw}")
        return timedelta(seconds=raw)
    if not isinstance(raw, str):
        raise ConfigError(f"{where}: expected a duration like '30m', got {type(raw).__name__}")

    match = _DURATION_RE.match(raw.strip())
    if match is None:
        raise ConfigError(
            f"{where}: {raw!r} is not a duration — use <number><s|m|h|d|w>, e.g. '30m' or '14d'"
        )
    return timedelta(seconds=int(match["value"]) * _UNIT_SECONDS[match["unit"]])


def load_config(path: str | Path) -> EchoConfig:
    """Read and parse the config file at ``path``.

    ``yaml.safe_load`` is used deliberately: the file is hand-edited and ``yaml.load`` would
    make it a code-execution surface.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {file}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{file} is empty")
    return parse_config(raw, source=str(file))


def parse_config(raw: object, *, source: str = "config") -> EchoConfig:
    """Validate an already-loaded config mapping into an :class:`EchoConfig`."""
    doc = _as_mapping(raw, where=source)
    _reject_unknown(doc, allowed=("version", "defaults", "channels"), where=source)

    version = doc.get("version")
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"{source}: version must be {SUPPORTED_VERSION}, got {version!r} — "
            "this build understands no other schema version"
        )

    defaults_raw = doc.get("defaults") or {}
    defaults = _as_mapping(defaults_raw, where=f"{source}.defaults")
    _reject_unknown(defaults, allowed=_SETTING_KEYS, where=f"{source}.defaults")
    base = _deep_merge(BUILTIN_DEFAULTS, defaults, where=f"{source}.defaults")

    channels_raw = doc.get("channels")
    if channels_raw is None:
        raise ConfigError(f"{source}: no 'channels' — nothing to follow")
    if not isinstance(channels_raw, Sequence) or isinstance(channels_raw, str | bytes):
        raise ConfigError(f"{source}.channels: expected a list of channels")
    if not channels_raw:
        raise ConfigError(f"{source}: 'channels' is empty — nothing to follow")

    channels = tuple(
        _parse_channel(entry, base=base, where=f"{source}.channels[{i}]")
        for i, entry in enumerate(channels_raw)
    )
    _reject_duplicate_identities(channels, where=source)
    return EchoConfig(version=SUPPORTED_VERSION, channels=channels)


def _parse_channel(raw: object, *, base: Mapping[str, Any], where: str) -> Channel:
    entry = _as_mapping(raw, where=where)
    _reject_unknown(entry, allowed=(*_CHANNEL_META_KEYS, *_SETTING_KEYS), where=where)

    given = [key for key in _IDENTITY_KEYS if entry.get(key) is not None]
    if not given:
        raise ConfigError(f"{where}: needs one of id, handle or url")
    if len(given) > 1:
        raise ConfigError(
            f"{where}: give exactly one of id/handle/url, got {', '.join(given)} — "
            "two identities for one channel cannot be reconciled"
        )

    channel_id = _optional_str(entry.get("id"), where=f"{where}.id")
    if channel_id is not None and not CHANNEL_ID_RE.match(channel_id):
        raise ConfigError(
            f"{where}.id: {channel_id!r} is not a channel_id (UC + 22 chars). "
            "A handle belongs in 'handle:', a URL in 'url:' — Echo resolves either to an id."
        )

    overrides = {key: value for key, value in entry.items() if key in _SETTING_KEYS}
    merged = _deep_merge(base, overrides, where=where)

    return Channel(
        channel_id=channel_id,
        handle=_optional_str(entry.get("handle"), where=f"{where}.handle"),
        url=_optional_str(entry.get("url"), where=f"{where}.url"),
        name=_optional_str(entry.get("name"), where=f"{where}.name"),
        enabled=_as_bool(entry.get("enabled", True), where=f"{where}.enabled"),
        settings=_build_settings(merged, where=where),
    )


def _build_settings(merged: Mapping[str, Any], *, where: str) -> ChannelSettings:
    subtitles = _as_mapping(merged["subtitles"], where=f"{where}.subtitles")
    sponsorblock = _as_mapping(merged["sponsorblock"], where=f"{where}.sponsorblock")
    filters = _as_mapping(merged["filters"], where=f"{where}.filters")
    catalog = _as_mapping(merged["catalog"], where=f"{where}.catalog")
    notify = _as_mapping(merged["notify"], where=f"{where}.notify")

    min_duration = _optional_duration(filters["min_duration"], where=f"{where}.filters")
    max_duration = _optional_duration(filters["max_duration"], where=f"{where}.filters")
    if min_duration is not None and max_duration is not None and min_duration > max_duration:
        raise ConfigError(
            f"{where}.filters: min_duration ({min_duration}) exceeds max_duration "
            f"({max_duration}) — nothing could ever match"
        )

    backfill = _as_int(merged["backfill"], where=f"{where}.backfill")
    if backfill < 0:
        raise ConfigError(f"{where}.backfill: cannot be negative, got {backfill}")

    max_height = merged["max_height"]
    if max_height is not None:
        max_height = _as_int(max_height, where=f"{where}.max_height")
        if max_height <= 0:
            raise ConfigError(
                f"{where}.max_height: must be positive pixels or null (best), got {max_height}"
            )

    poll_interval = parse_duration(merged["poll_interval"], where=f"{where}.poll_interval")
    if poll_interval <= timedelta(0):
        raise ConfigError(f"{where}.poll_interval: must be positive, got {poll_interval}")

    return ChannelSettings(
        poll_interval=poll_interval,
        backfill=backfill,
        proton_vpn=_as_bool(merged["proton_vpn"], where=f"{where}.proton_vpn"),
        max_height=max_height,
        container=_require_str(merged["container"], where=f"{where}.container"),
        shorts=_as_bool(merged["shorts"], where=f"{where}.shorts"),
        livestreams=_as_enum(Livestreams, merged["livestreams"], where=f"{where}.livestreams"),
        subtitles=Subtitles(
            download=_as_bool(subtitles["download"], where=f"{where}.subtitles.download"),
            langs=_as_str_tuple(subtitles["langs"], where=f"{where}.subtitles.langs"),
            auto_generated=_as_bool(
                subtitles["auto_generated"], where=f"{where}.subtitles.auto_generated"
            ),
        ),
        sponsorblock=SponsorBlock(
            mode=_as_enum(
                SponsorBlockMode, sponsorblock["mode"], where=f"{where}.sponsorblock.mode"
            ),
            categories=_as_str_tuple(
                sponsorblock["categories"], where=f"{where}.sponsorblock.categories"
            ),
        ),
        filters=Filters(
            min_duration=min_duration,
            max_duration=max_duration,
            title_include=_optional_regex(
                filters["title_include"], where=f"{where}.filters.title_include"
            ),
            title_exclude=_optional_regex(
                filters["title_exclude"], where=f"{where}.filters.title_exclude"
            ),
        ),
        catalog=Catalog(
            pool_per_channel=_as_bool(
                catalog["pool_per_channel"], where=f"{where}.catalog.pool_per_channel"
            ),
            uploader_as_artist=_as_bool(
                catalog["uploader_as_artist"], where=f"{where}.catalog.uploader_as_artist"
            ),
            extra_tags=_as_str_tuple(catalog["extra_tags"], where=f"{where}.catalog.extra_tags"),
        ),
        notify=Notify(
            on_available=_as_bool(notify["on_available"], where=f"{where}.notify.on_available"),
            stale_after=_optional_duration_value(
                notify["stale_after"], where=f"{where}.notify.stale_after"
            ),
        ),
    )


def _deep_merge(
    base: Mapping[str, Any], override: Mapping[str, Any], *, where: str
) -> dict[str, Any]:
    """Merge ``override`` onto ``base``, recursing into the schema's nested blocks.

    Only the documented nested blocks recurse; everything else replaces wholesale, so a list
    such as ``extra_tags`` is an override and not an append (appending would make it
    impossible to *remove* an inherited tag).
    """
    merged = dict(base)
    for key, value in override.items():
        if key in _NESTED_KEYS and value is not None:
            nested = _as_mapping(value, where=f"{where}.{key}")
            inherited = _as_mapping(base[key], where=f"{where}.{key}")
            _reject_unknown(nested, allowed=tuple(inherited), where=f"{where}.{key}")
            merged[key] = {**inherited, **nested}
        else:
            merged[key] = value
    return merged


def _reject_duplicate_identities(channels: Sequence[Channel], *, where: str) -> None:
    """Two entries for one channel would double-download it; catch it at parse time."""
    for key, values in (
        ("id", [c.channel_id for c in channels]),
        ("handle", [c.handle.lower() for c in channels if c.handle]),
        ("url", [c.url.rstrip("/").lower() for c in channels if c.url]),
    ):
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            if value in seen:
                raise ConfigError(f"{where}.channels: duplicate {key} {value!r}")
            seen.add(value)


def _as_mapping(raw: object, *, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where}: expected a block of keys, got {type(raw).__name__}")
    for key in raw:
        if not isinstance(key, str):
            raise ConfigError(f"{where}: keys must be strings, got {key!r}")
    return raw


def _reject_unknown(mapping: Mapping[str, Any], *, allowed: Sequence[str], where: str) -> None:
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(sorted(allowed))}"
        )


def _as_bool(raw: object, *, where: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"{where}: expected true or false, got {raw!r}")
    return raw


def _as_int(raw: object, *, where: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{where}: expected a whole number, got {raw!r}")
    return raw


def _require_str(raw: object, *, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}: expected a non-empty string, got {raw!r}")
    return raw.strip()


def _optional_str(raw: object, *, where: str) -> str | None:
    if raw is None:
        return None
    return _require_str(raw, where=where)


def _as_str_tuple(raw: object, *, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ConfigError(f"{where}: expected a list of strings, got {raw!r}")
    return tuple(_require_str(item, where=where) for item in raw)


def _as_enum[E: StrEnum](enum: type[E], raw: object, *, where: str) -> E:
    allowed = ", ".join(member.value for member in enum)
    if isinstance(raw, str):
        try:
            return enum(raw)
        except ValueError:
            pass
    raise ConfigError(f"{where}: expected one of {allowed}, got {raw!r}")


def _optional_duration(raw: object, *, where: str) -> timedelta | None:
    return _optional_duration_value(raw, where=where)


def _optional_duration_value(raw: object, *, where: str) -> timedelta | None:
    if raw is None:
        return None
    return parse_duration(raw, where=where)


def _optional_regex(raw: object, *, where: str) -> re.Pattern[str] | None:
    """Compile a title filter at parse time — a bad regex must not surface mid-poll."""
    if raw is None:
        return None
    pattern = _require_str(raw, where=where)
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{where}: {pattern!r} is not a valid regex: {exc}") from exc
