"""Runtime settings, from the environment.

The split is deliberate and matches the design: the **channel-follow YAML** holds everything
about *what to follow and how*, and is safe to commit and to edit in `echo-ui`. The
**environment** holds everything about *where things are and how to authenticate* — URLs,
paths, tokens, the ProtonVPN egress. Credentials live in a Secret and never in the config file
(``codex/docs/echo.md``), which is only possible if nothing here can be set from the YAML.

:class:`RuntimeSettings` therefore redacts its secrets in ``repr``. A settings object reaches a
log line or a traceback sooner or later, and a proxy URL with embedded credentials is exactly
the sort of thing that ends up in a log aggregator forever.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = ["RuntimeSettings", "SettingsError"]

DEFAULT_CONFIG_PATH: Final = "/etc/echo/channels.yaml"
DEFAULT_STATE_PATH: Final = "/var/lib/echo/echo.db"
DEFAULT_DOWNLOAD_ROOT: Final = "/nas/echo"
DEFAULT_LOG_LEVEL: Final = "INFO"

#: How long the loop waits between cycles. Per-channel `poll_interval` decides what is due;
#: this is only how often the worker looks, so it is deliberately shorter than any sensible
#: poll interval and is not a knob anyone should need.
DEFAULT_CYCLE_SECONDS: Final = 60


class SettingsError(Exception):
    """The environment does not describe a runnable worker."""


@dataclass(frozen=True)
class RuntimeSettings:
    """Where things are, and how to reach them.

    ``artemis_url`` and ``hermes_url`` are optional at this level so ``--check-config`` and a
    download-only run work without them; :meth:`require_for_run` is what refuses to start a
    worker that cannot finish what it begins.
    """

    config_path: Path = Path(DEFAULT_CONFIG_PATH)
    state_path: Path = Path(DEFAULT_STATE_PATH)
    download_root: Path = Path(DEFAULT_DOWNLOAD_ROOT)
    artemis_url: str | None = None
    hermes_url: str | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    cycle_seconds: int = DEFAULT_CYCLE_SECONDS
    allow_unproxied: bool = False
    _artemis_token: str | None = field(default=None, repr=False)
    _hermes_token: str | None = field(default=None, repr=False)
    _proxy_url: str | None = field(default=None, repr=False)

    @property
    def artemis_token(self) -> str | None:
        return self._artemis_token

    @property
    def hermes_token(self) -> str | None:
        return self._hermes_token

    @property
    def proxy_url(self) -> str | None:
        """The ProtonVPN egress, when one is configured.

        A channel with ``proton_vpn: true`` is **skipped** when this is absent — see
        :attr:`allow_unproxied` and ``docs/DESIGN.md``.
        """
        return self._proxy_url

    @property
    def proxy_configured(self) -> bool:
        return bool(self._proxy_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeSettings:
        """Read the settings out of ``env`` (defaults to the process environment)."""
        source = os.environ if env is None else env

        return cls(
            config_path=Path(source.get("ECHO_CONFIG", DEFAULT_CONFIG_PATH)),
            state_path=Path(source.get("ECHO_STATE_DB", DEFAULT_STATE_PATH)),
            download_root=Path(source.get("ECHO_DOWNLOAD_ROOT", DEFAULT_DOWNLOAD_ROOT)),
            artemis_url=_clean(source.get("ECHO_ARTEMIS_URL")),
            hermes_url=_clean(source.get("ECHO_HERMES_URL")),
            log_level=source.get("ECHO_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
            cycle_seconds=_positive_int(
                source.get("ECHO_CYCLE_SECONDS"), DEFAULT_CYCLE_SECONDS, "ECHO_CYCLE_SECONDS"
            ),
            allow_unproxied=_flag(source.get("ECHO_ALLOW_UNPROXIED")),
            _artemis_token=_clean(source.get("ECHO_ARTEMIS_TOKEN")),
            _hermes_token=_clean(source.get("ECHO_HERMES_TOKEN")),
            _proxy_url=_clean(source.get("ECHO_PROXY_URL")),
        )

    def require_for_run(self) -> None:
        """Refuse to start without the services a complete cycle needs.

        Downloading without being able to hand the file to Artemis would fill the NAS with
        videos nothing can show, so a missing URL is a startup failure, not a per-video one.
        """
        missing = [
            name
            for name, value in (
                ("ECHO_ARTEMIS_URL", self.artemis_url),
                ("ECHO_HERMES_URL", self.hermes_url),
            )
            if not value
        ]
        if missing:
            raise SettingsError(
                f"missing required environment variable(s): {', '.join(missing)}. "
                "Echo downloads videos in order to hand them to Artemis and announce them "
                "over Hermes; without both it would only fill the disk."
            )

    def __str__(self) -> str:
        """A log-safe one-liner. Secrets appear only as whether they are set."""
        return (
            f"config={self.config_path} state={self.state_path} downloads={self.download_root} "
            f"artemis={self.artemis_url or '-'} hermes={self.hermes_url or '-'} "
            f"proxy={'set' if self.proxy_configured else 'none'} "
            f"artemis_token={'set' if self._artemis_token else 'none'} "
            f"hermes_token={'set' if self._hermes_token else 'none'} "
            f"allow_unproxied={self.allow_unproxied}"
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str | None, default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise SettingsError(f"{name}: expected a whole number of seconds, got {value!r}") from None
    if parsed <= 0:
        raise SettingsError(f"{name}: must be positive, got {parsed}")
    return parsed
