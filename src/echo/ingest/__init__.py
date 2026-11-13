"""Hand-off: store the blob and create the Artemis post.

Echo talks to **Artemis only**. Verified 2026-11-13 against `UploadRoutes.scala` /
`UploadService.scala`: ``POST /uploads`` streams the body to Apollo itself, content-addressed,
then seeds the post and publishes the ``ProcessMediaJob`` Hephaestus consumes. So there is no
Apollo client here and no Hephaestus client — one correction further than ``docs/DESIGN.md``
§5 went.

Metadata mapping, as verified rather than assumed:

* channel → **pool** (`POST /pools` + `POST /pools/{id}/posts`, both idempotent)
* title → **tags**, tokenized in :mod:`~echo.ingest.tags` — Artemis has no free-text field
* uploader → a tag, but a **general** one: the artist *category* is not part of a tag's name
  and no route assigns a category, so "uploader → artist-tag" is not available to Echo today
* ``source`` → **not settable.** ``SetSource`` exists as a domain command but no route exposes
  it, and the fresh-upload path ignores the ``source`` its own service method accepts. The
  post id carries the join to Echo's record instead.

Title tokenization normalizes surface form only — lowercase, whitespace/underscore runs
collapsed to one underscore, edges stripped — matching Artemis's ``Tag.normalize`` and the
Argus precedent. Meaning-level merging is Artemis's alias/implication graph, not Echo's. The
tag vocabulary is shared gallery-wide, so emit too few tags rather than polluting it.
"""

from __future__ import annotations

from echo.ingest.artemis import ArtemisClient, ArtemisError, UploadedPost, content_type_for
from echo.ingest.service import (
    IngestOutcome,
    IngestReport,
    IngestService,
    pool_id_for_channel,
)
from echo.ingest.tags import (
    MAX_TAG_LENGTH,
    MAX_TAGS,
    normalize_surface_form,
    tags_for_video,
    tokenize_title,
)

__all__ = [
    "MAX_TAGS",
    "MAX_TAG_LENGTH",
    "ArtemisClient",
    "ArtemisError",
    "IngestOutcome",
    "IngestReport",
    "IngestService",
    "UploadedPost",
    "content_type_for",
    "normalize_surface_form",
    "pool_id_for_channel",
    "tags_for_video",
    "tokenize_title",
]
