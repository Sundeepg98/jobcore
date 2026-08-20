"""Dirty surface text: quotes, dashes and exotic whitespace from real boards.

Every case here came out of one live capture -- 704 distinct recruiter search
keywords pulled from Naukri's search-impressions endpoint on 2026-08-20. Of
those, 20 carried a character no ASCII-only cleaner would touch:

* 17 wrapped in typographic double quotes: ``"aws"``, ``"java"``, ``"sre"`` ...
* 2 whose spaces were U+00A0 NO-BREAK SPACE, which ``str.strip()`` leaves alone
* 1 containing an en dash: ``Software Engineer - Backend``

Before this module's fix each of those was its OWN taxonomy entry, so the same
skill was counted twice and the quoted copy never matched anything. Recruiters
paste search strings out of word processors; this is not an exotic case, it is
the normal case for any free-text field a human touched.

The safety property is at the bottom and matters more than any single case:
cleaning must be a NO-OP for every string already in the table. An exact hit has
to keep winning, or a release of this library would silently re-resolve skills
that resolved fine before.
"""

import pytest

from jobcore import DEFAULT_TAXONOMY
from jobcore.skills import SKILL_ALIASES


def n(s: str) -> str:
    return DEFAULT_TAXONOMY.normalize(s)


# -- Typographic quotes -------------------------------------------------------
# Curly quotes arrive whenever a recruiter drafts in Word or Google Docs and
# pastes into a search box. The quotes are not part of the skill name.

@pytest.mark.parametrize("dirty,expected", [
    ("“aws”", "amazon web services"),
    ("“java”", "java"),
    ("“python”", "python"),
    ("“microservices”", "microservices"),
    ("‘react’", "react"),
    ('"typescript"', "typescript"),
    ("“node.js”", "node.js"),
])
def test_typographic_quotes_are_not_part_of_the_skill(dirty, expected):
    assert n(dirty) == expected


def test_a_quoted_unknown_skill_still_loses_its_quotes():
    """Unknown input is returned, not dropped -- but returned CLEAN."""
    assert n("“widget wrangling”") == "widget wrangling"


# -- Exotic whitespace --------------------------------------------------------
# U+00A0 survives str.strip(). It arrived on 2 of the 704 live keywords, each
# with EVERY space replaced, so the string never matched anything.

def test_no_break_space_is_ordinary_whitespace():
    assert n(" next.js") == "next.js"
    assert n(" azure container apps") == "azure container apps"


def test_runs_of_whitespace_collapse():
    assert n("node   js") == "node.js"
    assert n("rest\t\tapi") == "rest api"


# -- Dashes -------------------------------------------------------------------
# An en dash is a hyphen someone's editor prettified. The hyphen is already a
# separator for derived lookup, so mapping the pretty forms onto it makes
# "type-script" and "type–script" the same string.

def test_en_and_em_dashes_behave_like_hyphens():
    assert n("type–script") == n("type-script") == "typescript"
    assert n("node—js") == "node.js"


# -- The safety property ------------------------------------------------------

def test_cleaning_is_a_no_op_for_every_string_already_in_the_table():
    """No canonical or alias may change meaning because of this fix.

    This is the regression guard for the whole module. Cleaning runs BEFORE
    lookup, so if it altered a string the table already knew, that entry would
    stop resolving -- silently, and only for some skills. Asserting over the
    real table (not a sample) is what makes that impossible.
    """
    for canonical, aliases in SKILL_ALIASES.items():
        assert n(canonical) == canonical, (
            "canonical %r no longer resolves to itself" % canonical
        )
        for alias in aliases:
            assert n(alias) == canonical, (
                "alias %r stopped resolving to %r" % (alias, canonical)
            )


def test_plain_ascii_input_is_untouched():
    """The common path must be byte-identical to lower().strip()."""
    for s in ["Python", "  AWS  ", "Node.js", "C++", "C#", "ci/cd", "REST API"]:
        assert n(s) == n(s.lower().strip())


def test_separator_only_input_does_not_crash():
    assert n("“”") == ""
    assert n("   ") == ""


# -- Split families the same live capture exposed, NOT fixed here --------------
# "Dynamo Db" (11) / "Dynamodb" (9) and "Click House" (1) / "Clickhouse" (9) are
# each two entries for one database, because neither name is in the table at
# all. Adding them is a VOCABULARY change, and test_parity_golden.py gates those
# on purpose: "a new canonical SKILL is a vocabulary decision, not a spelling
# fix, so it does not slip in under the additive rule." Surface hygiene above is
# a spelling fix and passes that guard untouched; these two need the golden
# updated deliberately by whoever owns the taxonomy, so they are recorded here
# rather than smuggled in behind a weakened guard.
#
# This test documents the CURRENT behaviour. It is not an endorsement of it --
# flip the expectations when the canonicals land.

@pytest.mark.parametrize("surface,still_split_as", [
    ("Dynamo Db", "dynamo db"),
    ("DynamoDB", "dynamodb"),
    ("Click House", "click house"),
    ("ClickHouse", "clickhouse"),
])
def test_split_families_are_still_split(surface, still_split_as):
    assert n(surface) == still_split_as
