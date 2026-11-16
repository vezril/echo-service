"""The entry point and the runtime settings.

Two things carry real weight here: the exit codes a supervisor reads, and that **no secret
ever reaches a log line** — a settings object ends up in a traceback eventually, and a proxy
URL with embedded credentials would live in a log aggregator forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from echo.__main__ import EXIT_MISCONFIGURED, EXIT_OK, main
from echo.settings import RuntimeSettings, SettingsError

CONFIG = """
version: 1
channels:
  - id: "UCXuqSBlHAE6Xw-yeJA0Tunw"
    name: "Linus Tech Tips"
  - handle: "@veritasium"
    proton_vpn: false
    max_height: 2160
    notify: { stale_after: 30d }
  - id: "UCsXVk37bltHxD1rDPwtNM8Q"
    name: "Paused"
    enabled: false
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "channels.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file should depend on the developer's own environment."""
    for name in (
        "ECHO_CONFIG",
        "ECHO_STATE_DB",
        "ECHO_DOWNLOAD_ROOT",
        "ECHO_ARTEMIS_URL",
        "ECHO_ARTEMIS_TOKEN",
        "ECHO_HERMES_URL",
        "ECHO_HERMES_TOKEN",
        "ECHO_PROXY_URL",
        "ECHO_LOG_LEVEL",
        "ECHO_CYCLE_SECONDS",
        "ECHO_ALLOW_UNPROXIED",
    ):
        monkeypatch.delenv(name, raising=False)


# --- settings from the environment ------------------------------------------------------


def test_settings_have_documented_defaults() -> None:
    settings = RuntimeSettings.from_env({})

    assert settings.config_path == Path("/etc/echo/channels.yaml")
    assert settings.state_path == Path("/var/lib/echo/echo.db")
    assert settings.download_root == Path("/nas/echo")
    assert settings.proxy_configured is False
    assert settings.allow_unproxied is False


def test_settings_read_the_environment() -> None:
    settings = RuntimeSettings.from_env(
        {
            "ECHO_CONFIG": "/etc/echo/mine.yaml",
            "ECHO_STATE_DB": "/data/echo.db",
            "ECHO_DOWNLOAD_ROOT": "/nas/youtube",
            "ECHO_ARTEMIS_URL": "http://artemis:8080",
            "ECHO_HERMES_URL": "http://hermes:8080",
            "ECHO_PROXY_URL": "socks5://user:pass@egress:1080",
            "ECHO_CYCLE_SECONDS": "120",
            "ECHO_ALLOW_UNPROXIED": "true",
            "ECHO_LOG_LEVEL": "debug",
        }
    )

    assert settings.config_path == Path("/etc/echo/mine.yaml")
    assert settings.artemis_url == "http://artemis:8080"
    assert settings.cycle_seconds == 120
    assert settings.allow_unproxied is True
    assert settings.log_level == "DEBUG"
    assert settings.proxy_configured is True


def test_blank_values_are_treated_as_absent() -> None:
    """An empty env var in a Helm values file is a common accident."""
    settings = RuntimeSettings.from_env({"ECHO_ARTEMIS_URL": "   ", "ECHO_PROXY_URL": ""})

    assert settings.artemis_url is None
    assert settings.proxy_configured is False


@pytest.mark.parametrize("raw", ["notanumber", "0", "-5"])
def test_a_bad_cycle_interval_is_a_settings_error(raw: str) -> None:
    with pytest.raises(SettingsError):
        RuntimeSettings.from_env({"ECHO_CYCLE_SECONDS": raw})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False)],
)
def test_the_unproxied_flag_accepts_the_usual_spellings(raw: str, expected: bool) -> None:
    assert RuntimeSettings.from_env({"ECHO_ALLOW_UNPROXIED": raw}).allow_unproxied is expected


# --- secrets stay out of logs -----------------------------------------------------------


SECRETS = {
    "ECHO_ARTEMIS_TOKEN": "artemis-s3cret",
    "ECHO_HERMES_TOKEN": "hermes-s3cret",
    "ECHO_PROXY_URL": "socks5://user:hunter2@egress:1080",
}


def test_no_secret_appears_in_repr_or_str() -> None:
    settings = RuntimeSettings.from_env(SECRETS)

    rendered = f"{settings!r} {settings}"

    for secret in ("artemis-s3cret", "hermes-s3cret", "hunter2"):
        assert secret not in rendered


def test_the_log_line_still_says_whether_a_secret_is_set() -> None:
    """Redaction must not cost the operator the ability to see what is configured."""
    described = str(RuntimeSettings.from_env(SECRETS))

    assert "proxy=set" in described
    assert "artemis_token=set" in described
    assert "hermes_token=set" in described
    assert "proxy=none" in str(RuntimeSettings.from_env({}))


def test_the_secrets_are_still_reachable_for_use() -> None:
    settings = RuntimeSettings.from_env(SECRETS)

    assert settings.artemis_token == "artemis-s3cret"
    assert settings.hermes_token == "hermes-s3cret"
    assert settings.proxy_url == "socks5://user:hunter2@egress:1080"


# --- require_for_run --------------------------------------------------------------------


def test_a_run_needs_both_service_urls() -> None:
    """Downloading without being able to hand files on would just fill the NAS."""
    with pytest.raises(SettingsError, match="ECHO_ARTEMIS_URL, ECHO_HERMES_URL"):
        RuntimeSettings.from_env({}).require_for_run()

    with pytest.raises(SettingsError, match="ECHO_HERMES_URL"):
        RuntimeSettings.from_env({"ECHO_ARTEMIS_URL": "http://artemis:8080"}).require_for_run()

    RuntimeSettings.from_env(
        {"ECHO_ARTEMIS_URL": "http://a:8080", "ECHO_HERMES_URL": "http://h:8080"}
    ).require_for_run()


# --- --check-config ---------------------------------------------------------------------


def test_check_config_describes_every_channel_and_exits_clean(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--check-config", "--config", str(config_file)])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "3 channel(s), 2 enabled" in out
    assert "Linus Tech Tips" in out
    assert "@veritasium" in out
    assert "[PAUSED]" in out


def test_check_config_shows_the_resolved_settings_not_the_raw_file(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inherited defaults are the thing a human cannot see by reading the YAML."""
    main(["--check-config", "--config", str(config_file)])

    out = capsys.readouterr().out
    assert "every 0:30:00, backfill 3" in out  # inherited
    assert "2160p mp4" in out  # overridden cap, inherited container
    assert "stale_after=30 days, 0:00:00" in out


def test_check_config_needs_no_services(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It must work on a laptop with nothing running — that is the point of it."""
    assert main(["--check-config", "--config", str(config_file)]) == EXIT_OK
    assert "echo:" not in capsys.readouterr().err


def test_check_config_warns_when_the_vpn_is_unavailable(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--check-config", "--config", str(config_file)])

    assert code == EXIT_OK
    assert "WARNING" in capsys.readouterr().err


def test_check_config_is_quiet_about_egress_when_a_proxy_is_set(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECHO_PROXY_URL", "socks5://egress:1080")

    main(["--check-config", "--config", str(config_file)])

    assert "WARNING" not in capsys.readouterr().err


# --- exit codes -------------------------------------------------------------------------


def test_a_missing_config_file_is_a_configuration_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--check-config", "--config", str(tmp_path / "absent.yaml")])

    assert code == EXIT_MISCONFIGURED
    assert "cannot read config" in capsys.readouterr().err


def test_an_invalid_config_file_is_a_configuration_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\nchannels:\n  - max_hieght: 720\n", encoding="utf-8")

    code = main(["--check-config", "--config", str(broken)])

    assert code == EXIT_MISCONFIGURED
    assert "unknown key" in capsys.readouterr().err


def test_running_without_the_service_urls_is_a_configuration_failure(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2, not 1: no amount of restarting fixes this, so a supervisor should give up."""
    code = main(["--once", "--config", str(config_file)])

    assert code == EXIT_MISCONFIGURED
    assert "ECHO_ARTEMIS_URL" in capsys.readouterr().err


def test_a_bad_environment_is_reported_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECHO_CYCLE_SECONDS", "soon")

    code = main(["--check-config"])

    assert code == EXIT_MISCONFIGURED
    assert "ECHO_CYCLE_SECONDS" in capsys.readouterr().err


# --- a real --once run, with the network edges stubbed ----------------------------------


def test_once_runs_a_single_cycle_against_a_real_store(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through main(): settings → config → store → one cycle → exit 0.

    The four network edges are replaced at their construction points, so everything between
    the CLI and the store is the real code path.
    """
    from echo import __main__ as entry

    monkeypatch.setenv("ECHO_ARTEMIS_URL", "http://artemis:8080")
    monkeypatch.setenv("ECHO_HERMES_URL", "http://hermes:8080")
    monkeypatch.setenv("ECHO_STATE_DB", str(tmp_path / "echo.db"))
    monkeypatch.setenv("ECHO_DOWNLOAD_ROOT", str(tmp_path / "downloads"))
    monkeypatch.setenv("ECHO_PROXY_URL", "socks5://egress:1080")

    cycles: list[str] = []

    class StubWorker:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def run_cycle(self) -> object:
            cycles.append("ran")
            from echo.worker import CycleReport

            return CycleReport(polled=1)

    monkeypatch.setattr(entry, "ArtemisClient", _stub_client)
    monkeypatch.setattr(entry, "HermesClient", _stub_client)
    monkeypatch.setattr(entry, "Worker", StubWorker)

    code = main(["--once", "--config", str(config_file)])

    assert code == EXIT_OK
    assert cycles == ["ran"]
    assert (tmp_path / "echo.db").exists()  # the store was really created and migrated


class _StubClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _stub_client(*args: object, **kwargs: object) -> _StubClient:
    return _StubClient(*args, **kwargs)
