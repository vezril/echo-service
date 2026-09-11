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

## Coordination findings — Artemis session, 2026-09-09

Verified against `artemis-service` / `artemis-ui` at the file:line refs the Artemis session gave
(each independently re-checked here). **These supersede the corresponding assumptions in the
canonical note** — `codex/docs/echo.md` has not been updated yet and is the Codex session's tree to
change; route these to it. Nothing below is committed Artemis work: it's a design thread the
Artemis session is surfacing to Calvin, who decides whether it becomes an OpenSpec change.

### 1. Video support already exists — the risk is elsewhere

Video is first-class through Artemis (`Dimensions.duration`, a `duration` read-model column,
`is:video` → `duration IS NOT NULL`, `duration:>=30` and `order:duration`, and a real `<video>` in
`media-view.tsx` with a placeholder when no transcode is ready). **No player work is needed.**

The Artemis session flagged the **derivative naming contract with Hephaestus** as the untested seam
(the UI picks the playable ref by `kind === "transcode"`; `MediaRoutes` serves from the post's
*stored* refs; a key-layout drift is what 404'd every image in v1.2.0, fixed in v1.2.1).

**Checked against `hephaestus-service` on 2026-09-10 — the contract already holds, and the real gap
is in Artemis instead.** See §5.

### 2. Watched-state — shape decided against a read-model-only projection

A read-model-only column is **wrong here**: read-model tables are declared rebuildable (drop and
replay from offset zero, `ddl/create_tables_postgres.sql:71`), and rebuilds are routine. Watched
state is *authored*, not derived — a projection-only column is silent data loss.

Proposed shape (Artemis's, and it follows existing `Favorited`/`Unfavorited` precedent, which is the
same class of high-frequency single-user state):

- additive event `PostViewed(at: Instant)` — satisfies the additive-only journal rule
- projection columns `last_viewed_at TIMESTAMPTZ` (+ optional `view_count`), partial index
- route `POST /posts/{id}/view`, mirroring the existing `/posts/{id}/favorite`
- DSL: an `unviewed:` / `viewed:<date` metatag

The earlier Echo-side worry ("high-frequency state is wrong for an event-sourced aggregate") does
not bite at this scale — views are human-paced, single-user, on a LAN.

### 3. The stale-nudge query has a SECOND dependency

`pool:` is currently **`UnsupportedMetatag`** (`SqlCompiler.scala:160`, stubbed with `ordpool`), so
"unwatched posts in pool X" needs pool reverse-lookup *and* watched-state — two Artemis items, not
one. Pool reverse-lookup was already on Artemis's deferred backlog; Echo is now a downstream
consumer of it.

**Fallback if `pool:` slips:** `GET /pools/{id}/posts` exists and is keyset-paginated on
`(position, post_id)`. With `last_viewed_at` on the post it could take an `unviewed=true` param, or
Echo filters the page client-side. The notify module is not blocked on the metatag.

### 4. Metadata mapping — one of the three does NOT hold

- `channel → pool` — fits. `removePoolPost` renumbers positions densely; keep appends going through
  the API and the invariant holds.
- `uploader → artist-tag` — fits, with a footgun: **tags PATCH is a full-set replace, not a delta.**
  Send the complete tag set on every write or tags are silently dropped.
- `title/description → searchable text` — **does not exist.** Artemis search is tag-based plus
  metatags; there is no free-text field and no full-text index (`source` is exact-match only,
  `exactString("source", value)`; no tsvector/ILIKE anywhere in the compiler). Full-text search is
  not on Artemis's backlog and is meaningfully larger than watched-state.

  **Consequence for Echo:** either Echo tokenizes titles into tags at ingest (Echo's side, no
  Artemis change — the likely answer), or full-text becomes a new Artemis capability. **Do not
  design Echo's or echo-ui's search UX assuming free-text.** This is the correction most likely to
  change Echo's plans, and it contradicts the canonical note in three places.

### 5. The transcode is never requested — verified gap, 2026-09-10

Verified directly in `hephaestus-service` / `artemis-service`. **Hephaestus already produces
everything Echo needs, and needs no change:**

- `DerivativePlan.plan` emits `DerivativeKind.Transcode` for `MediaType.Video` — `720p.mp4`, h264,
  `video/mp4`, `variant = "720p"`; video also gets a poster-frame thumbnail + sample poster
- wire tokens are exactly `thumb` / `sample` / `transcode` (`MediaModel.scala:49`, locked by
  `DerivativeKindSpec`) — and `transcode` is precisely what artemis-ui's `viewVariant` selects on
- keys are content-addressed: `derivatives/<md5[0:2]>/<md5>/<name>`

**But the transcode is gated on the job's `want` set, and Artemis hardcodes it to image kinds:**

- `UploadService.scala:156` → `want = Seq("thumb", "sample")`
- `ReprocessService.scala:57` (the `Derivatives` reprocess lane) → `Seq("thumb", "sample")`

Neither branches on `mediaType`; neither ever requests `"transcode"`. **Consequence:** a video
ingested through Artemis's normal upload path gets no transcode at all — artemis-ui finds no
`kind === "transcode"`, its `.mp4|webm|mkv|mov` extension fallback finds nothing either, and the
post shows the not-ready placeholder forever. The reprocess-derivatives lane would not backfill it.

This is a one-line-ish fix in Artemis (branch `want` on media type), **not** a Hephaestus change.
Open question routed to Hephaestus: its spec calls the video transcode *eager*, but the plan's
`want` gate makes it producer-opt-in — if `MediaType.Video` implied the transcode server-side, this
bug class would vanish for every producer. That's Hephaestus's call; it owns the job contract.

**Spec-vs-code discrepancy to close:** `media-processing/spec.md:125` says derivatives include a
`poster`, but no `poster` kind exists — the poster ships as `thumb`. A consumer building from the
spec prose would look for `kind == "poster"` and render a broken player.

**Topology correction:** the canonical note says Echo hands files to Hephaestus. That looks wrong —
`UploadService` creates the post and publishes `ProcessMediaJob`; `MediaResultHandler` consumes
`MediaProcessed`. The real path is Echo → Apollo + Artemis upload → Artemis publishes → Hephaestus,
with **Echo never talking to Hephaestus directly** (so: no Hephaestus client in Echo). Pending
confirmation from the Hephaestus session.

## Decision — titles become tags at ingest (Calvin, 2026-09-11)

Artemis has no free-text index and full-text search is not on its backlog (§4). **Echo tokenizes
video titles into tags at ingest.** No Artemis change is requested. This supersedes the canonical
note's `title/description → searchable text` mapping in all three places it appears.

### Follow the Argus precedent: surface form only

Argus already solved machine-generated tagging here, and its division of labour is the one to copy
(`argus/normalize.py`): a producer normalizes **surface form only** and never does meaning-level
merging — `outdoor` is not aliased to `outdoors`. Artemis owns canonicalization downstream via its
alias/implication graph (`TagCanonicalization.scala`).

Echo's normalization must match `Tag.normalize` (`Tag.scala:33`) exactly, since Artemis applies it
server-side regardless:

```
input.trim.toLowerCase → replaceAll("[\s_]+", "_") → strip leading/trailing "_"
```

plus Artemis's rejects: empty-after-normalization, and longer than `MaxLength`. Argus's
`normalize_surface_form` is the same function in Python — reuse it rather than re-deriving.

### The real work is filtering, and it carries a blast radius

YouTube titles are clickbait-dense: emoji, `(OFFICIAL VIDEO)`, `| Ep. 12`, ALL-CAPS, `#shorts`,
channel-name suffixes. Naive tokenization yields tags like `the`, `2`, `official`, `_`.

**The tag vocabulary is shared across the whole gallery**, so a sloppy Echo tokenizer degrades
search for images Echo never touched — including Argus's auto-tags. Bias conservative: drop
stopwords, pure numbers, single characters, and punctuation-only tokens; prefer emitting too few
tags over polluting the vocabulary. Junk tags are cheap to create and expensive to retract (tags
PATCH is a full-set replace — §4 — so retraction means rewriting every affected post's complete
tag set).

### Structured facts do NOT go through the tokenizer

- channel → **pool** (§4)
- uploader → **artist-tag** (§4; note `TagCategory.fromPrefix` — the category prefix is the
  mechanism)
- YouTube video id / URL → **`source`**, which is exact-match only (`exactString("source", value)`).
  Pick one canonical form — bare video id or full watch URL — and never vary it, or lookup silently
  returns nothing.

### Open — where does the verbatim title live?

Tokenizing is lossy, and Artemis appears to have no field that stores the original title as-is. If
nothing holds it, the human-readable title is gone after ingest and the gallery shows a post with
tags but no name. Options, none chosen: a `title:`-prefixed metatag, an Artemis field request (small
— storage, not search), or Echo keeping its own channel→video record. **Resolve before building
`ingest/`.** Do not assume the title survives.
