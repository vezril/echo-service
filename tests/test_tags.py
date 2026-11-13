"""Title → tags.

The tag vocabulary is shared across the whole gallery, so these tests are mostly about what
the tokenizer *refuses* to emit: a junk tag from Echo degrades search for images Echo never
touched, and retracting one means rewriting every affected post's complete tag set.
"""

from __future__ import annotations

import pytest

from echo.ingest.tags import (
    MAX_TAG_LENGTH,
    MAX_TAGS,
    normalize_surface_form,
    tags_for_video,
    tokenize_title,
)

# --- matching Artemis's Tag.normalize exactly -----------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Linus Tech Tips", "linus_tech_tips"),
        ("  padded  ", "padded"),
        ("UPPER", "upper"),
        ("multiple   spaces", "multiple_spaces"),
        ("already_underscored", "already_underscored"),
        ("runs___of___underscores", "runs_of_underscores"),
        ("mixed _ runs  _ here", "mixed_runs_here"),
        ("_leading", "leading"),
        ("trailing_", "trailing"),
        ("__both__", "both"),
        ("\ttab\tseparated\t", "tab_separated"),
        ("_", ""),
        ("   ", ""),
    ],
)
def test_normalization_matches_the_artemis_rule(raw: str, expected: str) -> None:
    """`input.trim.toLowerCase` → collapse `[\\s_]+` → strip edge underscores (Tag.scala:33).

    Artemis applies this server-side regardless, so a mismatch here means Echo believes it
    wrote a tag that is not the one in the gallery.
    """
    assert normalize_surface_form(raw) == expected


def test_normalization_does_not_merge_meanings() -> None:
    """Surface form only — aliasing is Artemis's graph, following the Argus precedent."""
    assert normalize_surface_form("outdoor") == "outdoor"
    assert normalize_surface_form("outdoors") == "outdoors"
    assert normalize_surface_form("Reviews") == "reviews"


# --- tokenizing a title ----------------------------------------------------------------


def test_a_plain_title_becomes_its_content_words() -> None:
    assert tokenize_title("Fish Tank PC Build") == ("fish", "tank", "pc", "build")


def test_clickbait_boilerplate_is_dropped() -> None:
    tags = tokenize_title("We Built a PC Inside a Fish Tank! (OFFICIAL VIDEO) | Ep. 12")

    assert tags == ("built", "pc", "inside", "fish", "tank", "ep")
    for junk in ("we", "a", "official", "video", "12"):
        assert junk not in tags


def test_emoji_and_punctuation_never_become_tags() -> None:
    tags = tokenize_title("🔥 INSANE Build!!! 🐟 — #shorts")

    assert tags == ("insane", "build")


@pytest.mark.parametrize(
    "title",
    ["2026", "12", "1 2 3", "!!!", "— – —", "🔥🐟", "a I x", "_ __ ___"],
)
def test_a_title_with_no_content_words_yields_nothing(title: str) -> None:
    """Better no tags than tags like `the`, `2` or `_`."""
    assert tokenize_title(title) == ()


def test_pure_numbers_are_dropped_but_alphanumerics_are_kept() -> None:
    tags = tokenize_title("RTX 5090 review in 2026 with 4k footage")

    assert "rtx" in tags
    assert "5090" not in tags  # a pure number tags nothing useful
    assert "4k" in tags  # alphanumeric: a real descriptor
    assert "2026" not in tags


def test_single_characters_are_dropped() -> None:
    assert tokenize_title("A B Cat") == ("cat",)


def test_duplicates_collapse_keeping_first_appearance_order() -> None:
    assert tokenize_title("Cat cat CAT dog") == ("cat", "dog")


def test_the_tag_count_is_capped() -> None:
    """A 180-character clickbait title would otherwise dilute the vocabulary on its own."""
    title = " ".join(f"word{index}" for index in range(40))

    assert len(tokenize_title(title)) == MAX_TAGS
    assert len(tokenize_title(title, limit=3)) == 3


def test_an_over_long_token_is_dropped_not_truncated() -> None:
    """Artemis rejects it (Tag.MaxLength), and a 400 would fail the whole tags PATCH."""
    assert tokenize_title("x" * (MAX_TAG_LENGTH + 1)) == ()
    assert tokenize_title("y" * MAX_TAG_LENGTH) == ("y" * MAX_TAG_LENGTH,)


def test_non_english_words_are_kept() -> None:
    """A non-English channel's words are content; only non-alphanumerics are noise."""
    assert tokenize_title("Café Grünkohl 日本語") == ("café", "grünkohl", "日本語")


def test_hyphenated_and_possessive_words_split_rather_than_mangle() -> None:
    assert tokenize_title("Linus's water-cooled rig") == ("linus", "water", "cooled", "rig")


# --- the complete set for a post --------------------------------------------------------


def test_the_set_leads_with_the_uploader_then_extras_then_the_title() -> None:
    tags = tags_for_video(
        title="Fish Tank PC Build",
        uploader="Linus Tech Tips",
        extra_tags=("hardware", "tech"),
    )

    assert tags[:3] == ("linus_tech_tips", "hardware", "tech")
    assert set(tags[3:]) == {"fish", "tank", "pc", "build"}


def test_the_uploader_is_not_stopword_filtered() -> None:
    """A channel called "The Why Files" must not lose two thirds of its name."""
    tags = tags_for_video(title="Episode 3", uploader="The Why Files")

    assert "the_why_files" in tags


def test_extra_tags_are_not_stopword_filtered_either() -> None:
    """They are human-authored config, not machine-extracted title noise."""
    assert "how_to" in tags_for_video(title="Anything", extra_tags=("how to",))


def test_a_tag_is_never_emitted_twice_across_sources() -> None:
    tags = tags_for_video(title="Veritasium explains gravity", uploader="Veritasium")

    assert tags.count("veritasium") == 1


def test_an_absent_uploader_and_extras_are_fine() -> None:
    assert tags_for_video(title="Fish Tank") == ("fish", "tank")


def test_blank_uploader_and_extras_are_ignored() -> None:
    assert tags_for_video(title="Fish", uploader="   ", extra_tags=("", "_")) == ("fish",)


def test_every_emitted_tag_would_be_accepted_by_artemis() -> None:
    """The three rules Tag.from enforces: non-empty after normalization, and <= MaxLength."""
    tags = tags_for_video(
        title="🔥 INSANE 8K Build!!! (OFFICIAL) | Ep. 12 — " + "z" * 200,
        uploader="  Linus   Tech  Tips  ",
        extra_tags=("Hardware",),
    )

    for tag in tags:
        assert tag
        assert tag == normalize_surface_form(tag)
        assert len(tag) <= MAX_TAG_LENGTH
