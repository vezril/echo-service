# echo-service — anonymous YouTube follower (worker)

Echo follows YouTube channels **without a Google account**, downloads new uploads to the NAS, and
feeds them into the constellation's media pipeline for viewing in Artemis + notifications. The last
piece of the de-google project.

**Status:** SCAFFOLDED (2026-11-12). Build, CI and the module skeleton are in place; **no behaviour
is implemented yet** — `python -m echo` exits non-zero by design. Canonical design + rationale:
`codex/docs/echo.md`; service-scoped shape and the verified cross-service findings in
[docs/DESIGN.md](docs/DESIGN.md).

## What it does

Public per-channel RSS (no login / no API key) → filter → **yt-dlp** download (optional ProtonVPN
egress) → hand off to **Apollo** (blob) and **Hephaestus** (poster + web transcode) → post to
**Artemis** (channel → pool, uploader → artist-tag) → **Hermes** `video.ingested`. Retention is
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
pieces the design names); pytest suite in `tests/`. The channel-follow config schema is in
`codex/docs/echo.md`.

```bash
uv sync                     # environment + lockfile
uv run pytest               # tests
uv run python -m echo       # entry point (exits 1 until implemented)
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy src             # types (strict)
```

Docker: `docker build -t calvinference/echoservice .` then `docker run calvinference/echoservice`.
The Dockerfile ships regardless of Docker Hub wiring — publishing is CI's job and is gated on
secrets.

**CI/CD:** `ci.yml` runs format, lint, types, tests and a secret scan on PRs; `dev.yml` publishes
`:dev` images from `development`; `release.yml` publishes `:X.Y.Z` + `:latest` from a `vX.Y.Z` tag
on `main`. Image publishing skips gracefully while the `DOCKERHUB_*` secrets are absent.

## License

MIT — see [LICENSE.md](LICENSE.md).
