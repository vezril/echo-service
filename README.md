# echo-service — anonymous YouTube follower (worker)

Echo follows YouTube channels **without a Google account**, downloads new uploads to the NAS, and
feeds them into the constellation's media pipeline for viewing in Artemis + notifications. The last
piece of the de-google project.

**Status:** SEED (2026-09-08). Design captured, not built. Canonical design + rationale:
`codex/docs/echo.md`; service-scoped shape in [docs/DESIGN.md](docs/DESIGN.md).

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

## Build (when it's a go)

Not built. When it is: Python 3.12 + uv (the `new-python-project` pattern), `yt-dlp`, a Hermes
client. The channel-follow config schema is in `codex/docs/echo.md`.

## License

MIT — see [LICENSE.md](LICENSE.md).
