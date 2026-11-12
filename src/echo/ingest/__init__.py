"""Hand-off: store the blob and create the Artemis post.

Echo does **not** talk to Hephaestus directly. The path is Echo → Apollo (blob) + the Artemis
upload endpoint → Artemis publishes a ``ProcessMediaJob`` → Hephaestus → ``MediaProcessed``
back to Artemis (``docs/DESIGN.md`` §5).

Metadata mapping, as verified against Artemis rather than assumed:

* channel → **pool**
* uploader → **artist-tag** (tags PATCH is a full-set replace — always send the complete set)
* title → **tags**, tokenized here. Artemis has no free-text field and no full-text index, so
  the verbatim title lives in Echo's own state, joined to the post on ``source``.

Title tokenization must normalize surface form only — lowercase, whitespace/underscore runs
collapsed to one underscore, edges stripped — matching Artemis's ``Tag.normalize`` and the
Argus precedent. Meaning-level merging is Artemis's alias/implication graph, not Echo's. The
tag vocabulary is shared gallery-wide, so emit too few tags rather than polluting it.
"""
