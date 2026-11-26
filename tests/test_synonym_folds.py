"""Two spellings of one discipline are one token -- and the token is not a skill.

THE DEFECT (found 2026-11-26, re-scoring 80 live Naukri rows offline)
--------------------------------------------------------------------
A board's skill-tag list carries discipline names, and spells each of them
several ways. "Backend" and "Backend Development" were two different tokens, and
so were "Full Stack" and "Fullstack Development". Fit scoring counts every tag,
so the split cost twice:

* a profile listing "Backend Development" missed every job tagged "Backend";
* a job tagged both "Full Stack" and "Fullstack Development" carried the same
  discipline twice in its denominator, so a profile without it lost it twice.

On those 80 rows "full stack" was tagged on 28 and "fullstack development" on
16, "backend" on 10 and "backend development" on 3. Folding the two families
alone took the jobs a 70 selector would auto-send from 6 to 8, with no profile
change.

WHY THE FIX IS NOT AN ALIAS
---------------------------
They are not skills, and a second consumer depends on that. naukri's skill-gap
report keeps only tokens the skill vocabulary recognises, so that it never
advises learning "full stack" -- and its suite asserts that "full stack" and
"fullstack development" are NOT taxonomy tokens. Adding them to
``SKILL_ALIASES`` makes them tokens: measured against that suite, three of its
tests go red. So they fold in ``SYNONYM_FOLDS``, which never enters the skill
lookup, the vocabulary counts, or the golden floor.

Every spelling below was seen on a live board; none is invented.
"""

import pytest

from jobcore import (
    DEFAULT_TAXONOMY,
    SKILL_ALIASES,
    SkillTaxonomy,
    compute_fit_score,
    parse_skills,
)


def n(s: str) -> str:
    return DEFAULT_TAXONOMY.normalize(s)


# ── The reported defect ──────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("Backend", "Backend Development"),
    ("Full Stack", "Fullstack Development"),
])
def test_the_reported_pairs_are_one_token(a, b):
    assert n(a) == n(b), "%r and %r are still two tokens" % (a, b)


def test_a_profile_spelling_now_matches_the_job_spelling():
    """The user-visible half: the profile says one thing, the job the other."""
    r = compute_fit_score(
        job_skills=parse_skills(["Backend", "Node.js"]),
        profile_skills=parse_skills(["Backend Development", "Node.js"]),
        job_exp_str="", profile_exp=None,
    )
    assert r["skill_match"]["missing"] == []
    assert r["skill_match"]["score"] == 100


def test_a_job_that_tags_one_discipline_twice_counts_it_once():
    """The other half: the denominator must not count one discipline twice."""
    job = parse_skills(["Full Stack", "Fullstack Development", "Node.js", "React"])
    assert len(job) == 3
    r = compute_fit_score(
        job_skills=job, profile_skills=parse_skills(["Node.js", "React"]),
        job_exp_str="", profile_exp=None,
    )
    assert r["skill_match"]["score"] == 67          # 2 of 3, not 2 of 4


# ── Every spelling the corpora carried ───────────────────────────────────────
# Sources: 80 scored Naukri rows, 382 Uplers requisitions, 645 Instahyre jobs.
# Only the semantic variant ("... development") is listed in the table; the
# spacing and hyphen variants are DERIVED, exactly as they are for skills.

@pytest.mark.parametrize("surface,token", [
    ("Backend", "backend"),
    ("Back-End", "backend"),                  # Instahyre
    ("back end", "backend"),
    ("Backend Development", "backend"),       # Naukri, Uplers
    ("Backend developer", "backend"),         # Uplers
    ("Back-End Development", "backend"),
    ("Full Stack", "full stack"),
    ("FullStack", "full stack"),              # Uplers
    ("full-stack", "full stack"),
    ("Fullstack Development", "full stack"),  # Naukri
    ("Full Stack Development", "full stack"), # Uplers
    ("Full-Stack Development", "full stack"),
    ("frontend", "frontend"),                 # Instahyre
    ("Front end", "frontend"),                # Naukri
    ("front-end", "frontend"),                # Instahyre
    ("Frontend Development", "frontend"),     # Naukri
])
def test_every_observed_spelling_reaches_its_token(surface, token):
    assert n(surface) == token


# ── False merges: what folding must NOT do ───────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("backend", "frontend"),
    ("backend", "full stack"),
    ("frontend", "full stack"),
    ("full stack", "full stack testing"),   # a suffix that is not development
    ("full stack", "stack"),                # naukri's junk fragment stays junk
    ("backend", "development"),
    ("java full stack", "full stack"),      # Instahyre: a stack AND a language
    ("java", "javascript"),
    ("react", "react native"),
])
def test_distinct_things_stay_distinct(a, b):
    assert n(a) != n(b), "%r and %r were merged" % (a, b)


# ── The contract a consumer depends on: a fold is NOT a skill ────────────────

FOLDED = ["backend", "backend development", "full stack", "fullstack development",
          "frontend", "front end", "full-stack", "back-end"]


@pytest.mark.parametrize("surface", FOLDED)
def test_a_folded_name_never_becomes_a_skill_token(surface):
    """naukri's ``taxonomy_tokens`` reads ``_lookup`` keys and values to decide
    what a skill is. A fold that leaked into it would put "full stack" back in
    his skill-gap report -- the regression naukri fixed on purpose."""
    lookup = DEFAULT_TAXONOMY._lookup
    tokens = set(lookup) | set(lookup.values())
    assert surface not in tokens
    assert n(surface) not in tokens
    assert n(surface) not in SKILL_ALIASES


def test_the_vocabulary_did_not_grow():
    """88 skills and 155 aliases before the folds, the same after."""
    assert DEFAULT_TAXONOMY.canonical_count == 88
    assert DEFAULT_TAXONOMY.alias_count == 155
    assert DEFAULT_TAXONOMY.ambiguous_derived_keys == frozenset()


def test_every_fold_spelling_round_trips_and_none_is_shadowed():
    """A fold applies only to what the skill vocabulary left unresolved, so a
    spelling a skill claims would be dead table. Asserting the whole table
    round-trips is what makes a shadowed entry impossible to ship."""
    from jobcore.skills import SYNONYM_FOLDS

    assert SYNONYM_FOLDS, "the fold table is empty"
    for token, spellings in SYNONYM_FOLDS.items():
        assert n(token) == token
        assert token not in SKILL_ALIASES
        for s in spellings:
            assert n(s) == token, "%r no longer folds onto %r" % (s, token)


# ── Composition: extensions keep the folds, and a declared skill beats one ───

def test_an_extended_taxonomy_keeps_the_folds():
    """uplers and instahyre score through ``DEFAULT_TAXONOMY.extended(...)``
    whenever the config file teaches a skill; losing the folds there would put
    the three servers on two vocabularies."""
    tax = DEFAULT_TAXONOMY.extended({"cobol": {"cobol 85"}})
    assert tax.normalize("Backend Development") == "backend"
    assert tax.normalize("Fullstack Development") == "full stack"
    assert tax.normalize("cobol85") == "cobol"


def test_an_extension_that_declares_a_folded_name_a_skill_wins_for_every_spelling():
    """An operator may decide "backend development" IS a skill. Then every
    spelling of the family must reach that skill, not split between the skill
    and the fold token."""
    tax = DEFAULT_TAXONOMY.extended({"backend development": {"backend"}})
    for s in ("Backend", "Back-End", "Backend Development", "Backend developer"):
        assert tax.normalize(s) == "backend development", s


def test_a_skill_always_beats_a_fold():
    tax = SkillTaxonomy({"widget": {"wdg"}}, folds={"gizmo": {"widget"}})
    assert tax.normalize("widget") == "widget"
    assert tax.normalize("gizmo") == "gizmo"


def test_a_fold_key_two_tokens_would_claim_is_refused_not_guessed():
    tax = SkillTaxonomy({}, folds={"foo-bar": {"foo bar x"}, "foo bar": {"foo bar y"}})
    assert tax.normalize("foo-bar") == "foo-bar"
    assert tax.normalize("foo bar") == "foo bar"
    assert tax.normalize("foobar") == "foobar"        # resolves to neither
    assert "foobar" in tax.ambiguous_derived_keys


def test_a_taxonomy_built_without_folds_behaves_exactly_as_before():
    """``SkillTaxonomy(aliases)`` is a public constructor; callers that pass no
    folds must get no folds."""
    tax = SkillTaxonomy({"cobol": {"cobol85"}})
    assert tax.normalize("Backend Development") == "backend development"
