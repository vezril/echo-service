"""Title → tags, following the Argus precedent: normalize **surface form only**.

Artemis has no free-text field and no full-text index, so a video's title becomes tags at
ingest (Calvin, 2026-09-11 — ``docs/DESIGN.md``). Two rules govern everything here:

1. **Surface form only.** :func:`normalize_surface_form` matches Artemis's ``Tag.normalize``
   exactly (verified at ``core/.../domain/Tag.scala:33``), because Artemis applies it
   server-side regardless. Meaning-level merging — ``outdoor`` → ``outdoors`` — is Artemis's
   alias/implication graph, never Echo's.
2. **Emit too few rather than too many.** The tag vocabulary is shared across the whole
   gallery, so a sloppy tokenizer degrades search for images Echo never touched, including
   Argus's auto-tags. Junk tags are cheap to create and expensive to retract: the tags PATCH
   is a full-set replace, so retracting one means rewriting every affected post's whole set.

YouTube titles are clickbait-dense — emoji, ``(OFFICIAL VIDEO)``, ``| Ep. 12``, ALL-CAPS,
``#shorts``, channel-name suffixes — so the filtering is where the real work is, and it is
deliberately aggressive about structure words and deliberately conservative about content
words (a word it cannot classify is kept).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

__all__ = [
    "MAX_TAGS",
    "MAX_TAG_LENGTH",
    "STOPWORDS",
    "normalize_surface_form",
    "tags_for_video",
    "tokenize_title",
]

#: Artemis rejects anything longer (``Tag.MaxLength``), so an over-long token is dropped here
#: rather than failing the whole PATCH with a 400.
MAX_TAG_LENGTH: Final = 170

#: A hard cap on title-derived tags. A 180-character clickbait title can yield twenty tokens;
#: past a dozen they stop describing the video and start diluting the vocabulary.
MAX_TAGS: Final = 12

_MIN_TAG_LENGTH: Final = 2

# Structure words, plus the boilerplate that YouTube titles carry for the algorithm rather
# than for the viewer. Everything here would be noise as a gallery tag. Kept short on purpose:
# the goal is to drop words that describe no video, not to curate a vocabulary.
STOPWORDS: Final = frozenset(
    {
        # English function words
        "a",
        "about",
        "after",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "back",
        "be",
        "because",
        "been",
        "before",
        "being",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "dont",
        "down",
        "every",
        "for",
        "from",
        "get",
        "gets",
        "getting",
        "got",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "if",
        "im",
        "in",
        "into",
        "is",
        "it",
        "its",
        "ive",
        "just",
        "like",
        "made",
        "make",
        "makes",
        "me",
        "more",
        "most",
        "much",
        "my",
        "no",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "put",
        "really",
        "same",
        "she",
        "should",
        "so",
        "some",
        "still",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "this",
        "those",
        "through",
        "to",
        "too",
        "try",
        "under",
        "until",
        "up",
        "us",
        "use",
        "using",
        "very",
        "was",
        "way",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "youre",
        # YouTube title boilerplate — present for the algorithm, not the viewer
        "episode",
        "feat",
        "ft",
        "full",
        "official",
        "part",
        "premiere",
        "shorts",
        "sub",
        "subscribe",
        "vid",
        "video",
        "videos",
        "watch",
    }
)

# Anything that is not a letter, digit or underscore separates tokens. Unicode letters are
# kept — a non-English channel's words are content, not noise — while emoji, punctuation and
# symbols fall out here because they match no token character.
_SEPARATORS: Final = re.compile(r"[^\w]+", re.UNICODE)

_NORMALIZE_RUNS: Final = re.compile(r"[\s_]+")
_EDGE_UNDERSCORES: Final = re.compile(r"^_+|_+$")


def normalize_surface_form(value: str) -> str:
    """Normalize exactly as Artemis's ``Tag.normalize`` does.

    ``input.trim.toLowerCase`` → collapse runs of whitespace/underscores to one underscore →
    strip leading and trailing underscores. Matching it here means what Echo sends is what
    Artemis stores, so a tag Echo believes it wrote cannot differ from the one in the gallery.

    Returns ``""`` for input that normalizes away; the caller drops those, as Artemis would
    reject them.
    """
    collapsed = _NORMALIZE_RUNS.sub("_", value.strip().lower())
    return _EDGE_UNDERSCORES.sub("", collapsed)


def tokenize_title(title: str, *, limit: int = MAX_TAGS) -> tuple[str, ...]:
    """Turn a video title into tags, conservatively.

    Dropped: stopwords and title boilerplate, pure numbers (``2``, ``2026`` — a year tags
    nothing useful), single characters, anything with no alphanumeric content (emoji,
    punctuation), and tokens over :data:`MAX_TAG_LENGTH`. Order of first appearance is kept,
    duplicates collapse, and the result is capped at ``limit``.

    A token that survives is emitted as-is in normalized form: no stemming, no synonym
    mapping, no pluralization — that is Artemis's graph to own.
    """
    tags: list[str] = []
    seen: set[str] = set()

    for raw in _SEPARATORS.split(title):
        token = normalize_surface_form(raw)
        if not _is_useful(token) or token in seen:
            continue
        seen.add(token)
        tags.append(token)
        if len(tags) >= limit:
            break

    return tuple(tags)


def tags_for_video(
    *,
    title: str,
    uploader: str | None = None,
    extra_tags: Iterable[str] = (),
    limit: int = MAX_TAGS,
) -> tuple[str, ...]:
    """The complete tag set for one video's post.

    Complete is the operative word: ``PATCH /posts/{id}/tags`` is a **full-set replace**, not
    a delta (verified — ``ChangeTags(tags, …)`` sets the set), so a partial set silently drops
    whatever it omits.

    The uploader and the configured ``extra_tags`` are human-authored names, so they are
    normalized but never stopword-filtered — a channel called "The Why Files" must not lose
    both of its first two words. Only the title goes through :func:`tokenize_title`.

    Note the uploader lands as a **general** tag. Artemis's artist category is not part of a
    tag's name (``TagCategory.prefix`` is a *search* namespace) and no route assigns a
    category, so the "uploader → artist-tag" mapping in the design note is not something Echo
    can do today — see ``docs/DESIGN.md``.
    """
    tags: list[str] = []
    seen: set[str] = set()

    for value in (uploader, *extra_tags):
        if not value:
            continue
        normalized = normalize_surface_form(value)
        if normalized and len(normalized) <= MAX_TAG_LENGTH and normalized not in seen:
            seen.add(normalized)
            tags.append(normalized)

    for token in tokenize_title(title, limit=limit):
        if token not in seen:
            seen.add(token)
            tags.append(token)

    return tuple(tags)


def _is_useful(token: str) -> bool:
    if len(token) < _MIN_TAG_LENGTH or len(token) > MAX_TAG_LENGTH:
        return False
    if token in STOPWORDS:
        return False
    # A pure number describes no video: "2", "12", "2026" are episode numbers and years.
    if token.replace("_", "").isdigit():
        return False
    # Defensive: a token with no alphanumeric content survived separator splitting.
    return any(character.isalnum() for character in token)
