# echo-service — design (service-scoped)

The **canonical** Echo design — constellation rationale, the anonymity model, cross-service
coordination, naming, and all decisions — lives in **`codex/docs/echo.md`**. Read that first. This
doc is the service-scoped shape and must not drift from it.

## Shape — a worker, not a media app

```
   RSS poll (public feed)        yt-dlp download            hand off to the pipeline
   per channel, via optional  →  optional ProtonVPN     →   Apollo (blob) · Hephaestus
   ProtonVPN egress              egress, quality/filters     (poster+transcode) · Artemis
        │                                                     (post) · Hermes (notify)
        └── resolve handle/url → channel_id (recorded once)
```

## Decisions carried from the canonical note (2026-09-08)

- **Name: Echo** (locked).
- **ProtonVPN egress: optional toggle**, default on; **creds live in a Secret, never in config.**
- **Retention: manual** (no auto-delete). Echo emits two notifications only: `available`
  (`video.ingested`) and a **stale/unwatched nudge** after a configurable window (needs Artemis
  watched-state).
- **`channel_id` (UC…) is canonical**; `handle`/`url` resolved once and recorded.

## Modules (proposed)

- `follow/` — load the channel-follow config (schema in `codex/docs/echo.md`), poll each channel's
  RSS, resolve handle/url → `channel_id`, detect new uploads.
- `fetch/` — the yt-dlp wrapper: optional ProtonVPN egress, quality cap, container, SponsorBlock,
  subtitles, and the pre-download filters (duration, title regex, shorts/livestream handling).
- `ingest/` — store the blob in Apollo, request Hephaestus derivatives (poster + web transcode),
  create the Artemis post (channel → pool, uploader → artist-tag, title/description searchable).
- `notify/` — emit `video.ingested`; run the stale/unwatched projection (depends on Artemis
  watched-state) and emit the nudge.

## Dependencies

Apollo (blobs) · Hephaestus (video poster/transcode — **coordinate**) · Artemis (video posts +
gallery player + **watched-state** — coordinate) · Hermes (`video.ingested`) · ProtonVPN (optional
egress). Chart in `codex/charts/echo`.

## Open / to coordinate

- **Artemis watched-state** (viewed flag + last-viewed timestamp) — gates the stale nudge.
- **Video player in artemis-ui** — the "view in Artemis" experience.
- **Stale-window default** — 14d proposed.
