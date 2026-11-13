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

*Superseded in part — see the dated coordination sections below. Current state as of 2026-09-11:*

- **Artemis must request the transcode** — the live blocker for video playback, and a pre-existing
  Artemis bug rather than an Echo dependency (§5; confirmed by the Artemis session 2026-09-11, which
  is taking it to Calvin as a bug needing a fix authorized, not a feature greenlit).
- ~~**Video player in artemis-ui**~~ — **not needed.** artemis-ui already plays video (§1). The
  original entry pointed at the wrong seam.
- **Artemis watched-state** (viewed flag + last-viewed timestamp) — gates the stale nudge. Surfaced
  to Calvin 2026-09-09; **no direction given, nothing built, no date.** Do not plan around it
  landing — design the notify module against the fallback path (§3).
- **`pool:` metatag** — same status: surfaced, unauthorized, no date (§3).
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

## Resolved — the verbatim title lives in Echo, not Artemis (2026-09-11)

**Verified:** the `posts` table has **no** title/name/description column (`create_tables_postgres.sql`);
`source TEXT` is the only free-form string, and search treats it as exact-match
(`exactString("source", value)`). A `title:`-prefixed *tag* doesn't solve it either — tags go
through `Tag.normalize`, so they're lossy by construction. Artemis genuinely cannot hold a verbatim
title today.

**Decision: Echo stores the verbatim title in its own state. No Artemis change is requested.**

The reasoning is that Echo needs a datastore regardless — it must record which video ids a channel
has already yielded (new-upload detection and re-download avoidance), the `handle → channel_id`
resolution, backfill position, and the per-item queue/retry/error state that `echo-ui` exists to
display. Once that store exists, holding the title costs nothing, and it keeps a cosmetic concern
from turning into an event-sourcing change in another team's repo.

**Join key: `source`.** Echo stamps one canonical YouTube form on the post's `source` (§ titles→tags
— bare video id or full watch URL, chosen once and never varied) and keys its own record by the same
value. `source` is exact-match searchable, which is precisely the lookup this needs, so the mapping
works in both directions: post → Echo record, and Echo record → post.

**The cost, stated honestly:** a video browsed in artemis-ui shows tags and a poster frame, but no
title. Full titles appear in `echo-ui`, not the gallery. That is a real downgrade for video and is
accepted, for two reasons: the gallery is an image-first surface that identifies posts visually by
thumbnail rather than by name, and video posts still carry the poster frame Hephaestus generates
(§5), so they are not anonymous — just untitled.

**If it grates in practice**, the fallback is a small, well-scoped ask: a display-name/title field on
the post — *storage only, not search, and not full-text*, which is far cheaper than the full-text
index rejected in §4. Deferred deliberately: don't spend another repo's budget on a cosmetic
question before Calvin has actually browsed video posts in the gallery and formed an opinion.

`ingest/` is unblocked.

## Implementation — `follow/` landed (2026-11-13)

First behaviour in the repo. `follow/` is four pieces, split so that every decision with a
consequence is a pure function and only two of them touch the network:

| module | owns | network |
|---|---|---|
| `follow/config.py` | parse / deep-merge / validate the channel-follow file | no |
| `follow/feed.py` | the Atom parser (pure) + a conditional-GET `FeedClient` | yes |
| `follow/resolve.py` | `handle`/`url` → `channel_id`, once per channel | yes |
| `follow/detect.py` | which entries are new; what backfill does on first sync | no |

Decisions taken while building, none of which contradict the canonical note:

- **Config rejects unknown keys** rather than ignoring them. A silently-dropped `max_hieght`
  looks like a working config that quietly downloads 4K forever, and this file is hand-edited.
- **`yaml.safe_load` only** — the file is hand-edited, so `yaml.load` would make it a
  code-execution path. Covered by a test.
- **Deep-merge recurses only into the documented nested blocks**; lists replace wholesale, so
  an inherited `extra_tags` can be overridden *down* rather than only appended to.
- **Title-filtered and backfill-skipped entries are recorded as seen, not dropped.** Without
  that, every poll re-evaluates the same rejects forever, and an older entry still inside the
  feed window gets re-fetched.
- **Resolution is restricted to youtube.com hosts** (and re-checked after redirects). The URL
  comes from a config file; a fetcher that follows any URL a file names is a request-forgery
  lever pointed at whatever the worker can reach on the cluster network.
- **Responses are read under a size cap, streamed** — remote input, so an unbounded read is a
  memory lever held by whatever answers the URL.
- **Conditional GET** (`If-None-Match` / `If-Modified-Since`). Polling someone else's free
  endpoint every 30 minutes forever should usually cost them a 304, which also makes a tighter
  `poll_interval` cheap. Callers must persist the returned `etag` / `last_modified`.

### Verified: what the feed does NOT carry (constrains `fetch/`)

The per-channel Atom feed gives video id, title, watch URL, channel id, author, `published`,
`updated`, a thumbnail URL and a description. It does **not** give:

- **duration** — so `filters.min_duration` / `max_duration` *cannot* be applied before the
  download decision at feed level. They belong to `fetch/`, against yt-dlp's extracted
  metadata (`--match-filter`), not to `follow/`.
- **a Shorts flag or a livestream flag** — so `shorts` and `livestreams` are likewise `fetch/`
  decisions. A Short is an ordinary entry; the common proxy is the duration bound, which is
  why `min_duration: 60s` is the default.
- **history** — the feed is a **window of roughly 15 entries, newest first**. Two consequences:
  a channel that uploads faster than Echo polls will drop uploads off the end before they are
  ever seen (that is what `poll_interval` is really tuning), and **`backfill` can never reach
  further back than the window**, whatever number the config names. `backfill: 50` on a
  long-running channel yields ~15, not 50.

Only `title_include` / `title_exclude` are decidable in `follow/`, and that is where they run.

## Decision — `source` is the bare video id (Calvin, 2026-11-13)

The canonical YouTube form Echo stamps on the Artemis post's `source`, and keys its own record
by, is the **bare video id** (`dQw4w9WgXcQ`) — not the watch URL. Locked; `watch_url(video_id)`
in `fetch/download.py` is the single place it is turned back into something openable.

Why the id:

- **`source` is exact-match only** in Artemis (`exactString("source", value)`), so the stored
  form must never vary. A watch URL has many surface forms — `youtu.be/ID`, `m.youtube.com`,
  `&t=`, `&list=`, `?si=` — and each variant is a silent lookup miss in both directions.
- The id is what the feed hands over directly (`yt:videoId`), so there is no construction step
  for a formatting bug to enter.
- In the one place `source` is actually searchable — the Artemis DSL — `source:dQw4w9WgXcQ` is
  typeable where a full URL is not.
- **Verified, so the counter-argument is void:** `source` is *not* rendered as a link in the
  gallery. `artemis-ui/src/components/catalog/post-view.tsx:111-118` puts it in a truncated
  `<span class="truncate text-xs text-muted-foreground">`. A URL would buy no clickability,
  only a longer truncated string.

Cost, stated plainly: the id is opaque to a human reading the Details panel. Since the title
already does not appear in the gallery (§ verbatim title), the `source` row is a video post's
only identifier there — but it is non-link truncated text either way, so this changes nothing
in practice. Full titles live in `echo-ui`.

## Implementation — `fetch/` landed (2026-11-13)

Three pieces, splitting the pure decisions from the one place that touches yt-dlp:

| module | owns | pure |
|---|---|---|
| `fetch/filters.py` | duration bounds, Shorts, livestreams — the feed can't decide these | yes |
| `fetch/options.py` | `ChannelSettings` → yt-dlp options | yes |
| `fetch/download.py` | probe → judge → download → report; the only yt-dlp caller | no |

Decisions taken while building:

- **Two passes per video: probe (`skip_download`) then download.** The filters exist to avoid
  spending bandwidth, so they must run against metadata *before* the download — and both passes
  are built by the same function, so the probe cannot drift from the download it is judging.
- **Three verdicts, not two: accept / reject / defer.** "Not yet" and "not ever" must not be
  conflated. A reject is recorded as seen; a **defer never is**, or a livestream's finished
  recording would be lost permanently.
- **Failure is a reported outcome, not an exception.** `FetchReport.retryable` carries it. A
  permanently-gone video (private, removed, members-only, terminated account) is terminal; an
  unrecognised failure is retryable, because wrongly retrying costs a poll cycle while wrongly
  giving up loses an upload. The classification is substring matching on `DownloadError` text —
  unavoidable, since yt-dlp signals these through message text rather than distinct types — so
  the marker list is deliberately conservative.
- **Filenames are `<root>/<channel_id>/<video_id>.<ext>`, never the title.** Titles carry emoji,
  slashes and 180 characters of clickbait; the verbatim title lives in Echo's state anyway. The
  per-channel tree is also easy to exclude from backup, which `media-backup.md` argues for since
  the content is re-fetchable.
- **`overwrites: False` + `continuedl: True`** — a partial download resumes, a finished file is
  never re-fetched or clobbered.
- **`ignoreerrors: False`, `noplaylist: True`** — Echo decides what to retry, and a video
  carrying a playlist parameter must not pull the playlist.
- **Options are validated against the real yt-dlp** in `tests/test_options_against_ytdlp.py`
  (offline). A renamed option or postprocessor key is the one failure a dict assertion cannot
  see: every unit test would pass while nothing downloads.

### Resolved ambiguity — `livestreams: skip` vs `after`

The schema offers `skip | after | never`, but only two behaviours are distinguishable. Under the
reading where each name means what it says in plain English — `skip` = pass on it for now,
`after` = take it once it ends, `never` = never touch stream content — `skip` and `after` reach
the same long-run outcome: defer while live, then treat the finished recording as an ordinary
upload. Implemented that way, with `never` terminal for both the live event and the VOD.

**For Calvin:** the schema could drop to two values (`never`, `after`) with `skip` kept as the
default alias, or `skip` could be given a distinct meaning. Nothing is blocked either way.

### Shorts detection is a heuristic — stated honestly

YouTube exposes no reliable "is a Short" flag in extracted metadata. `is_short` uses the
`/shorts/` URL path (conclusive, but rarely present — most extractions give `/watch?v=`) and,
failing that, requires **both** a portrait frame and a duration ≤ 180s, so a 45-second landscape
teaser is not mislabelled. The robust lever remains `filters.min_duration` (60s default), which
is what the canonical note means by "kills most Shorts/teasers".

### Blocker surfaced — no in-cluster ProtonVPN egress exists

`proton_vpn: true` is the **default**, and nothing in the constellation implements it. Checked
`codex/docs`, `codex/charts` and `codex/apps`: ProtonVPN appears only as a **device-level
client** (`access-model.md:113`, "ProtonVPN coexistence note" — the operator's phone/laptop), and
there is no egress gateway, sidecar or proxy anywhere in the charts.

`fetch/` is not blocked: it takes a proxy URL from the caller (Secret-sourced, never from the
config file) and simply runs without one when absent. But **the documented default is
unimplementable at deploy time**, which means the anonymity property Echo is named for is not
actually in place yet. This is a chart/infra item for the **Codex session**, not an Echo one —
and it should be settled before Echo first runs against YouTube, not after.

## Implementation — `state/` landed (2026-11-13)

Echo's own store: **SQLite**, single writer, file on the NAS volume beside the downloads
(`state/schema.py`, `state/store.py`). A fifth module beyond the four the canonical note names —
recorded here as an addition, not a drift. A server-class database would add a dependency, a
Secret and a backup story for a few thousand rows that only Echo writes.

It holds exactly what the earlier decisions require: seen video ids (the feed is a ~15-entry
window), the `handle → channel_id` resolution, the feed's conditional-GET validators, the
**verbatim title** Artemis has no column for, and per-video queue/retry/error state for
`echo-ui`.

Decisions taken while building:

- **"Seen" and "done" are separate.** `SEEN_STATES` = `downloaded | ingested | filtered` is what
  `follow/` must not re-select; `deferred` and `failed` are deliberately **not** seen, because
  they are owed another attempt. The two work queues (`due_for_fetch`, `due_for_ingest`) are
  separate from both.
- **Retry backoff is stored, not computed at read time** — 5m → 15m → 1h → 6h → 24h, capped, so
  a broken extractor backs off instead of hammering an endpoint that is already refusing, and a
  restart does not reset the schedule.
- **A report for a video nobody recorded raises** rather than updating zero rows. Silently
  no-oping would lose the outcome of a download that really happened.
- **Re-recording a feed entry cannot walk a video backwards** (`ON CONFLICT` updates only title
  and URL). The feed keeps showing a video Echo already downloaded; that must be inert.
- **`first_synced_at` gates backfill**, so it applies to the first poll only and does not
  re-trigger on restart.
- **Timestamps are ISO-8601 UTC text.** SQLite has no date type; text keeps the file legible
  from the `sqlite3` CLI when something needs explaining at 2am, and everything is read back
  timezone-aware — a naive timestamp would compare wrong against the stale-nudge window.

## Implementation — `ingest/` landed (2026-11-13)

Three pieces: `ingest/tags.py` (the tokenizer, pure), `ingest/artemis.py` (the HTTP client),
`ingest/service.py` (the four-step hand-off). Verified against `artemis-service` at the
file:line level before writing anything, and three of the design note's assumptions moved.

### Topology correction — Echo talks to **Artemis only**

`POST /uploads` streams the request body **to Apollo itself**, content-addressed, then seeds the
post and publishes the `ProcessMediaJob` Hephaestus consumes (`http/UploadRoutes.scala:17-25`,
`ingest/UploadService.scala:43-80`). So:

- **No Apollo client in Echo.** This supersedes both the canonical note ("hand off to Apollo
  (blob)") and §5 above, which still had Echo calling Apollo directly. Echo makes four HTTP
  calls, all to Artemis.
- No Hephaestus client either, as §5 already concluded.

### Blocker — `source` cannot be set, so the join key is the **post id**

The decision locked earlier today (join on `source`) **cannot be implemented against Artemis's
API as it stands.** Verified:

- `SetSource(source, at)` exists as a domain command (`domain/PostCommand.scala:72`) and
  `UploadService.upload` takes a `source` parameter —
- but **no HTTP route exposes it.** The post routes are `PATCH …/tags`, `POST/DELETE
  …/favorite`, `POST …/score`, `PATCH …/rating`, `GET …` (`http/CatalogRoutes.scala:54-105`).
  There is no `source` setter and `CreatePostRequest` is `{id, md5, filetype}`.
- `UploadRoutes` calls `upload(dataBytes, contentType, mediaType)` — three arguments, so `tags`
  and `source` take their empty defaults.
- Worse, even passing them would not help a new video: `mergeMetadata` runs only on the
  **dedup/restore** branches; `createFresh` applies neither. Artemis's own comment says so —
  "the metadata-merge activates once the endpoint (or Muses) supplies tags/source."

**Decision: Echo joins on the `post_id` returned by the upload**, which `state/` already stores.
This needs no change in another repo and is a complete two-way join (Echo record → post, and
post → Echo record by looking up `post_id`).

What is lost: `source:<video_id>` lookups in the Artemis DSL. **Putting the video id in a tag is
not a substitute and must not be done** — `Tag.normalize` lowercases, and YouTube ids are
case-sensitive (`dQw4w9WgXcQ` → `dqw4w9wgxcq`), so it would be lossy and collision-prone.

The `source` decision is not wasted: the form (bare video id) is still right, and sending it
becomes one line once a seam exists. **Ask for the Artemis session**, small and well-scoped:
pass `tags`/`source` through `POST /uploads` *and* apply them in `createFresh`. Not blocking.

### Correction — "uploader → artist-tag" is not available to Echo

`TagCategory.prefix` is a **search** namespace (`character:cat`), not part of a tag's name, and
no route assigns a tag's category — `ChangeTags` carries names only. So the uploader lands as a
**general** tag (`linus_tech_tips`). §4's note that "the category prefix is the mechanism" is
wrong. Categorizing it later is an Artemis-side concern.

### The tokenizer

Matches `Tag.normalize` exactly (`domain/Tag.scala:33`), `MaxLength` 170. Drops stopwords and
YouTube title boilerplate, pure numbers (an episode number or year tags nothing), single
characters, and anything with no alphanumeric content (emoji, punctuation). Caps at 12 tags.
Unicode letters are kept — a non-English channel's words are content. The uploader and
`extra_tags` are normalized but **not** stopword-filtered: a channel called "The Why Files" must
not lose two thirds of its name.

### Every step is idempotent, so a partial failure needs no compensation

Upload dedups on md5 (same bytes → same post), the tags PATCH is a full-set replace, pool create
answers `409` (treated as success), and `AddPost` on a member is an explicit no-op in the pool
aggregate (`PoolDomain.scala:34`). So `ingest/` reports failure and the next cycle retries from
the top; the store marks a video ingested only on complete success.

### Coordination finding — Artemis buffers the whole upload in memory

`UploadService` does `bytes.runFold(ByteString.empty)(_ ++ _)` to compute the md5 before storing,
commented "fine at personal scale". That was written for images. **Echo's uploads are 1080p
videos — hundreds of MiB to several GiB each**, and that buffer is JVM heap. Echo streams from
disk on its side, but the receiving end will hold the entire file in memory. This is likely to
OOM Artemis on the first large video, and it is an Artemis-side change (stream to Apollo while
hashing incrementally). **Route to the Artemis session — this one should land before Echo first
ingests.**

### Still open, in the order they now block work

1. **`notify/`** — the last module. `video.ingested` over Hermes is unblocked; the stale nudge
   is not (below).
2. **The worker loop + entry point** — `python -m echo` still exits 1. Both halves of the cycle
   are now assembled in tests (`test_store.py::test_a_poll_cycle_is_idempotent`,
   `test_ingest_service.py`).
3. **Artemis: the md5 buffering** (above) — the one item that can break Echo's first real run.
4. **ProtonVPN egress** — Codex session; the anonymity property is not in place without it.
5. **Artemis: `tags`/`source` on upload** (above) — would let Echo stamp `source` as decided.
6. Artemis **watched-state** and the **`pool:`** metatag — unchanged: surfaced, unauthorized, no
   date. `notify/` designs against the `GET /pools/{id}/posts` fallback.
