# echo-service — anonymous YouTube follower (worker)

Echo follows YouTube channels **without a Google account**, downloads new uploads to the NAS, and
feeds them into the constellation's media pipeline for viewing in Artemis + notifications. The last
piece of the de-google project.

**Status:** FEATURE-COMPLETE, NEVER RUN FOR REAL (2026-11-16). All four modules, the store and
the worker loop are implemented and tested — `follow/` (config, feed polling, handle resolution,
new-upload detection), `fetch/` (metadata filters, yt-dlp options, probe-judge-download),
`ingest/` (title→tags, the Artemis client, the hand-off), `notify/` (Hermes `video.ingested`;
the stale nudge is built but stays silent until Artemis has watched-state), `state/` (SQLite)
and `worker.py` + the CLI. **No part of it has ever talked to YouTube, Artemis or Hermes** — every
test fakes the network edge, and two cross-service blockers are open
([docs/DESIGN.md](docs/DESIGN.md)): Artemis buffers a whole upload in memory, and the cluster has
no ProtonVPN egress. Canonical design + rationale: `codex/docs/echo.md`; service-scoped shape,
the verified cross-service findings and the per-module decisions in
[docs/DESIGN.md](docs/DESIGN.md).

## What it does

Public per-channel RSS (no login / no API key) → filter → **yt-dlp** download (optional ProtonVPN
egress) → **Artemis** `POST /uploads`, which stores the blob in **Apollo** and has **Hephaestus**
build the poster + web transcode (channel → pool, title → tags) → **Hermes** `video.ingested`.
Echo talks to Artemis only — verified, see [docs/DESIGN.md](docs/DESIGN.md). Retention is
**manual**; Echo assists with two notifications (available + stale/unwatched nudge).

A thin **worker** — the Argus pattern: Python (yt-dlp is Python), chart lives in `codex`, rides
Hermes. It *orchestrates* existing services; it does not reimplement storage / derivatives / gallery.

## Constellation conventions

Greek name = software. Deploys are the **Codex** session's (pin-first, mirrored-values helm; chart in
`codex/charts/echo` per the worker pattern). Owned/coordinated per
`codex/docs/session-coordination.md`. **Coordinate the video pieces** — gallery player + watched-state
in Artemis, poster/transcode in Hephaestus — with those sessions (a relay is queued in
session-coordination.md). Mark/accent: **none yet** (generate via the constellation-logo pipeline).

## Build

Python 3.12 + uv. Sources in `src/echo/` (`follow/`, `fetch/`, `ingest/`, `notify/` — the four
pieces the design names — plus `state/`, Echo's own SQLite store); pytest suite in `tests/`. The
channel-follow config schema is in `codex/docs/echo.md`, and `follow/config.py` is its only reader — every other module gets
already-resolved settings, so the YAML shape is known in exactly one place.

```bash
uv sync                     # environment + lockfile
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy src             # types (strict)
```

## Running it

```bash
uv run python -m echo --check-config    # parse the config, print what each channel would do
uv run python -m echo --once            # one poll cycle, then exit
uv run python -m echo                   # loop until SIGTERM/SIGINT (the container's CMD)
```

Everything about *what to follow* is in the channel-follow YAML; everything about *where things
are and how to authenticate* is in the environment, so no credential can be committed:

| Variable | Default | Purpose |
|---|---|---|
| `ECHO_CONFIG` | `/etc/echo/channels.yaml` | the channel-follow file |
| `ECHO_STATE_DB` | `/var/lib/echo/echo.db` | Echo's SQLite store |
| `ECHO_DOWNLOAD_ROOT` | `/nas/echo` | where downloads land (`<root>/<channel_id>/<video_id>.<ext>`) |
| `ECHO_ARTEMIS_URL` | — | **required to run**; the gallery Echo uploads to |
| `ECHO_HERMES_URL` | — | **required to run**; the broker it announces on |
| `ECHO_ARTEMIS_TOKEN`, `ECHO_HERMES_TOKEN` | — | bearer tokens, when those services enable auth |
| `ECHO_PROXY_URL` | — | the ProtonVPN egress. **Without it, channels with `proton_vpn: true` are skipped** |
| `ECHO_ALLOW_UNPROXIED` | `false` | run those channels anyway, accepting that YouTube sees this host's IP |
| `ECHO_CYCLE_SECONDS` | `60` | how often the loop looks for due work (per-channel `poll_interval` decides what *is* due) |
| `ECHO_LOG_LEVEL` | `INFO` | |

Exit codes are for a supervisor: `0` clean, `1` the worker failed, `2` the config or environment
is wrong — and no amount of restarting will fix a `2`.

Docker: `docker build -t calvinference/echo .` then `docker run calvinference/echo`.
The Dockerfile ships regardless of Docker Hub wiring — publishing is CI's job and is gated on
secrets.

**CI/CD:** `ci.yml` runs format, lint, types, tests and a secret scan on PRs; `dev.yml` publishes
`:dev` images from `development`; `release.yml` publishes `:X.Y.Z` + `:latest` from a `vX.Y.Z` tag
on `main`. Image publishing skips gracefully while the `DOCKERHUB_*` secrets are absent.

## License

MIT — see [LICENSE.md](LICENSE.md).
