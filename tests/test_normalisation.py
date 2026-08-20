"""Mechanical skill-string variants, and the false merges that must not happen.

The taxonomy's job is to make two spellings of ONE skill land on one token. Two
kinds of variant exist and they are handled in two different places:

* **Semantic** aliases -- ``"aws"`` for Amazon Web Services, ``"k8s"`` for
  Kubernetes. Nothing but a lookup table can know these, so they live in
  ``SKILL_ALIASES``.
* **Mechanical** variants -- ``"postgre sql"``, ``"restapi"``, ``"Rest APIs;"``.
  These are the same letters with different spacing, punctuation or number.
  Enumerating them one string at a time is a losing game, so ``normalize()``
  derives them.

Every case below was taken from a live board, not invented. The counts are
requisitions out of the 235 native Uplers reqs captured 2026-08-20.
"""

import pytest

from jobcore import DEFAULT_TAXONOMY, SkillTaxonomy
from jobcore.skills import SKILL_ALIASES


def n(s: str) -> str:
    return DEFAULT_TAXONOMY.normalize(s)


# ── The reported bug ─────────────────────────────────────────────────────────
# Found by ranking real jobs: these deflated the operator's skill coverage on
# 10 of 235 requisitions. His top match's only "missing" must-have was
# "restful apis" -- a false gap; his true coverage was 5/5, not 4/5.

@pytest.mark.parametrize(
    "variant,canonical,reqs",
    [
        ("restapi", "rest api", 7),          # concatenation
        ("restful apis", "rest api", 2),     # plural of a known alias
        ("postgre sql", "postgresql", 1),    # spurious internal space
    ],
)
def test_the_reported_variants_canonicalise(variant, canonical, reqs):
    assert n(variant) == canonical, "%r (%d reqs) still fails to canonicalise" % (variant, reqs)


# ── The same class of miss, mined from the same corpus ───────────────────────

@pytest.mark.parametrize(
    "variant,canonical",
    [
        # internal whitespace that the alias table spells closed
        ("next js", "next.js"),
        ("express js", "express"),
        ("postgre sql", "postgresql"),
        # concatenation of a name the alias table spells open
        ("restapi", "rest api"),
        ("restapis", "rest api"),
        # trailing punctuation, from comma-split job descriptions
        ("aws.", "amazon web services"),
        ("spark.", "apache spark"),
        ("rest apis;", "rest api"),
        ("soapui.", "soapui."),            # unknown stays unknown, not mangled
        # plural / singular of a known form
        ("restful apis", "rest api"),
        ("microservice", "microservices"),
        # separators the table spells with a different one
        ("ci-cd", "ci/cd"),
        ("scikit_learn", "scikit-learn"),
        ("react-js", "react"),
        # casing and stray whitespace, which already worked and must keep working
        ("  ReactJS  ", "react"),
        ("NODE.JS", "node.js"),
    ],
)
def test_mechanical_variants_canonicalise(variant, canonical):
    assert n(variant) == canonical


@pytest.mark.parametrize(
    "typo,canonical",
    [
        ("kubernates", "kubernetes"),
        ("kubernets", "kubernetes"),
        ("typescrpit", "typescript"),
        ("googel cloud", "google cloud platform"),
        ("contenarization", "docker"),
    ],
)
def test_corpus_typos_are_explicit_aliases(typo, canonical):
    """Misspellings cannot be derived, so they are data -- but only ones seen live."""
    assert n(typo) == canonical


# ── False merges: the price of deriving variants ─────────────────────────────
# A normaliser that strips every non-alphanumeric would collapse "c#" and "c++"
# onto "c". These tests are the reason the derivation is narrow.

@pytest.mark.parametrize(
    "a,b",
    [
        ("c#", "c++"),          # the sharpest case: alnum-only squashing merges these
        ("c#", "c"),
        ("c++", "c"),
        ("nest", "next"),
        ("ms sql", "mysql"),    # Microsoft SQL Server is not MySQL
        ("github", "git"),
        ("k8s", "k3s"),
        ("sns", "sqs"),
        ("vue", "vuex"),
        ("next.js", "nestjs"),
    ],
)
def test_distinct_skills_stay_distinct(a, b):
    assert n(a) != n(b), "%r and %r were merged" % (a, b)


@pytest.mark.parametrize(
    "stem,must_not_become",
    [
        ("sas", "css"),                    # SAS is an analytics language, not Sass
        ("cvs", "computer vision"),        # CVS is version control, not CV
        ("tfs", "terraform"),              # TFS is Team Foundation Server
        ("cs", "c#"),
        ("les", "css"),
        ("nodes", "node.js"),
        ("rests", "rest api"),
    ],
)
def test_short_stems_are_never_inflected(stem, must_not_become):
    """Adding or dropping a trailing "s" is only safe on a long enough word."""
    assert n(stem) != must_not_become


def test_every_canonical_normalises_to_itself():
    """No canonical skill may be swallowed by another one's derived form."""
    for canonical in SKILL_ALIASES:
        assert n(canonical) == canonical


def test_every_alias_still_reaches_its_own_canonical():
    """The whole explicit table round-trips. Derivation may not shadow it."""
    for canonical, aliases in SKILL_ALIASES.items():
        for alias in aliases:
            assert n(alias) == canonical, "%r no longer reaches %r" % (alias, canonical)


def test_unknown_skills_pass_through_lowercased():
    """Unchanged contract: an unknown skill is never dropped or invented."""
    assert n("Quantum Annealing") == "quantum annealing"
    assert n("  LLM  ") == "llm"
    assert n("") == ""


# ── The build-time guard that makes the above true ───────────────────────────

def test_ambiguous_derived_keys_are_refused_not_guessed():
    """Two canonicals whose derived forms collide must both keep exact lookup
    and neither may win the derived key."""
    tax = SkillTaxonomy({"foo-bar": {"foobar alias"}, "foo bar": {"other alias"}})
    assert tax.normalize("foo-bar") == "foo-bar"
    assert tax.normalize("foo bar") == "foo bar"
    # "foobar" is the derived form of both, so it resolves to neither.
    assert tax.normalize("foobar") == "foobar"


def test_extended_taxonomy_derives_variants_for_the_new_vocabulary_too():
    tax = DEFAULT_TAXONOMY.extended({"cobol": {"cobol 85"}})
    assert tax.normalize("cobol85") == "cobol"
    assert tax.normalize("restapi") == "rest api"      # shared table still works


def test_parse_set_applies_the_same_normalisation():
    assert DEFAULT_TAXONOMY.parse_set("RestAPI, Postgre SQL, Next JS") == frozenset(
        {"rest api", "postgresql", "next.js"}
    )
