"""Console entry point (``python -m echo``).

Three modes:

* no arguments — run the worker until it is asked to stop (the container's ``CMD``);
* ``--once`` — run a single cycle and exit, which is what a Chronos-style schedule or a
  human debugging a channel wants;
* ``--check-config`` — parse and resolve the channel-follow file, print what every channel
  would do, and exit without touching the network, the store or the disk.

Exit codes are meant for a supervisor: ``0`` clean, ``1`` the worker failed, ``2`` the
configuration or environment is wrong. The difference matters — a container that exits 2 will
never work until a human changes something, where one that exits 1 may just need restarting.

``SIGTERM`` and ``SIGINT`` request a **graceful** stop: the current cycle finishes, which for
Echo means an in-flight download either completes or is left resumable, and the store is
consistent either way. A second signal is left to the supervisor to escalate.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import FrameType

from echo import __version__
from echo.fetch.download import VideoFetcher
from echo.follow.config import ConfigError, EchoConfig, load_config
from echo.follow.feed import FeedClient
from echo.follow.resolve import ChannelResolver
from echo.ingest.artemis import ArtemisClient
from echo.ingest.service import IngestService
from echo.notify.hermes import HermesClient
from echo.notify.service import NotificationService
from echo.settings import RuntimeSettings, SettingsError
from echo.state.store import EchoStore
from echo.worker import Worker, WorkerClients

logger = logging.getLogger("echo")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run Echo. See the module docstring for the modes and exit codes."""
    args = _parser().parse_args(argv)

    try:
        settings = RuntimeSettings.from_env()
    except SettingsError as exc:
        print(f"echo: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    if args.config:
        settings = replace(settings, config_path=Path(args.config))

    _configure_logging(settings.log_level)

    try:
        config = load_config(settings.config_path)
    except ConfigError as exc:
        print(f"echo: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    if args.check_config:
        _describe(config, settings)
        return EXIT_OK

    try:
        settings.require_for_run()
    except SettingsError as exc:
        print(f"echo: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    logger.info("echo %s starting: %s", __version__, settings)
    if not settings.proxy_configured:
        # Said once at startup rather than per channel: this is the anonymity property Echo
        # exists for, and it is currently unimplemented in the cluster (docs/DESIGN.md).
        logger.warning(
            "no ProtonVPN egress configured (ECHO_PROXY_URL unset): channels with "
            "proton_vpn: true will be %s",
            "run unproxied" if settings.allow_unproxied else "SKIPPED",
        )

    try:
        return _run(config, settings, once=args.once)
    except KeyboardInterrupt:  # pragma: no cover - signal handling covers the graceful path
        logger.info("interrupted")
        return EXIT_OK
    except Exception:
        logger.exception("echo failed")
        return EXIT_FAILED


def _run(config: EchoConfig, settings: RuntimeSettings, *, once: bool) -> int:
    artemis_url = settings.artemis_url
    hermes_url = settings.hermes_url
    assert artemis_url and hermes_url  # require_for_run has already checked both

    stopping = _install_signal_handlers()

    with (
        EchoStore.open(settings.state_path) as store,
        ArtemisClient(artemis_url, token=settings.artemis_token) as artemis,
        HermesClient(hermes_url, token=settings.hermes_token) as hermes,
    ):
        clients = WorkerClients(
            # One factory per collaborator, taking "use the egress proxy?" — the toggle is
            # per channel while the proxy URL is process-wide.
            feed=lambda use_proxy: FeedClient(proxy=settings.proxy_url if use_proxy else None),
            resolver=lambda use_proxy: ChannelResolver(
                proxy=settings.proxy_url if use_proxy else None
            ),
            fetcher=lambda use_proxy: VideoFetcher(
                root=settings.download_root,
                proxy=settings.proxy_url if use_proxy else None,
            ),
            ingest=IngestService(artemis),
            notifier=NotificationService(hermes),
            proxy_configured=settings.proxy_configured,
            allow_unproxied=settings.allow_unproxied,
        )
        worker = Worker(store=store, config=config, clients=clients)

        if once:
            report = worker.run_cycle()
            return EXIT_FAILED if report.failures and not report.did_work else EXIT_OK

        worker.run_forever(
            sleep=time.sleep,
            should_stop=lambda: stopping[0],
            cycle_seconds=settings.cycle_seconds,
        )
    return EXIT_OK


def _install_signal_handlers() -> list[bool]:
    """Flip a flag on SIGTERM/SIGINT so the current cycle can finish first."""
    stopping = [False]

    def handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s; finishing the current cycle", signal.Signals(signum).name)
        stopping[0] = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, handle)
        except ValueError:  # pragma: no cover - not the main thread
            logger.debug("cannot install a handler for %s here", signum)
    return stopping


def _describe(config: EchoConfig, settings: RuntimeSettings) -> None:
    """Print the resolved view of the config — what each channel would actually do."""
    print(f"echo {__version__}")
    print(settings)
    print(f"\n{len(config.channels)} channel(s), {len(config.enabled_channels)} enabled:\n")

    for channel in config.channels:
        knobs = channel.settings
        state = "enabled" if channel.enabled else "PAUSED"
        identity = channel.channel_id or channel.handle or channel.url or "?"
        cap = f"{knobs.max_height}p" if knobs.max_height else "best"
        stale = knobs.notify.stale_after or "off"
        print(f"  {channel.label}  [{state}]")
        print(f"    identity      {identity}")
        print(f"    poll          every {knobs.poll_interval}, backfill {knobs.backfill}")
        print(f"    egress        proton_vpn={knobs.proton_vpn}")
        print(
            f"    download      {cap} {knobs.container}, livestreams={knobs.livestreams}, "
            f"shorts={knobs.shorts}"
        )
        print(
            f"    filters       min={knobs.filters.min_duration} max={knobs.filters.max_duration} "
            f"include={_pattern(knobs.filters.title_include)} "
            f"exclude={_pattern(knobs.filters.title_exclude)}"
        )
        print(
            f"    catalog       pool={knobs.catalog.pool_per_channel} "
            f"artist={knobs.catalog.uploader_as_artist} "
            f"extra_tags={list(knobs.catalog.extra_tags)}"
        )
        print(f"    notify        on_available={knobs.notify.on_available}, stale_after={stale}")
        print()

    if (
        any(c.settings.proton_vpn for c in config.enabled_channels)
        and not settings.proxy_configured
    ):
        print(
            "WARNING: channel(s) ask for ProtonVPN egress but ECHO_PROXY_URL is unset — "
            "they would be skipped (or run unproxied with ECHO_ALLOW_UNPROXIED=true).",
            file=sys.stderr,
        )


def _pattern(pattern: object) -> str:
    return getattr(pattern, "pattern", "none")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        # Logs go to stderr so stdout stays usable for --check-config output.
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="echo",
        description="Follow YouTube channels anonymously and feed them to the constellation.",
    )
    parser.add_argument("--version", action="version", version=f"echo {__version__}")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="channel-follow file to use (overrides ECHO_CONFIG)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="run a single poll cycle and exit instead of looping",
    )
    mode.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config, print what each channel would do, and exit",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
