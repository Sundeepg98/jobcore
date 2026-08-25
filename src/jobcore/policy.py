"""Scoring policy — the schema, its defaults, and its trust tiers.

This module holds the *values that encode a judgement*: how much a skill match
is worth against experience, what a location bonus pays, where the verdict
bands sit. Until now every one of them was a literal welded into
:mod:`jobcore.fit`, :mod:`jobcore.salary` and :mod:`jobcore.scoring`.

Three things make this module safe to put on the scoring path:

1. **It does no I/O.** Not at import, not ever. Reading a file is
   :mod:`jobcore.config`'s job, and that module is never imported here.
   ``tests/test_independence.py`` runs a clean interpreter with cwd elsewhere
   and asserts a specific score of exactly ``100``; a policy that read a file
   would make that score machine-dependent and jobcore would stop being a
   library.
2. **Every default is exactly today's literal.** ``DEFAULT_POLICY`` reproduces
   the pre-policy arithmetic byte-for-byte, which is what lets all 179 golden
   parity cases pass unchanged and what lets a bare clone of any consumer
   behave exactly as it does today with no config file anywhere.
3. **Every key carries its trust tier as DATA** (:data:`SCHEMA`), not as a
   convention a future reader has to infer.

The tiers
=========

The rule is *an agent may change what it LOOKS AT; it may not change what
CONSTRAINS it* — and the tier is derived from what a key **gates in the call
graph**, never from what it is called.

``A``  free to write.  The worst case is that he sees bad jobs or misses good
       ones, and notices within a day. Reversible, visible, no outward effect.
``B``  one-way ratchet. Tightening is free; loosening needs an explicit
       ``confirm_widen`` **and** must land under a ceiling that lives HERE, in
       Python, not in the file. The file can never raise the ceiling.
``C``  **not loadable from the file at all.** The file may DISPLAY the value
       for transparency; a differing value is refused loudly and the Python
       value is used. A write is refused by name.

Tier C once held one invariant, and that invariant is now RETIRED -- read this
before quoting the old sentence back at the code:

    Until 2026-08-25: *no sequence of config writes, from any server, may grant
    autonomous apply authority.* **This is no longer true, deliberately.**

The escalation the tier table was built to stop is traced in
``tests/test_safety_invariant.py``: ``agent.enabled`` -> ``agent.mode:"auto"``
-> ``min_fit_score:0`` -> ``blocklist.enabled:false`` -> arbitrary searches
= real applications, per day, on a live account, with no human in the loop.
The operator overruled the CONCLUSION and kept the PROTECTIONS. Those five
keys plus ``per_search_limit`` are now Tier B: loadable, ratcheted, and
bounded -- see the ``servers.naukri`` block for the four Python protections
that actually hold the line, and for the one honest caveat about which of
them the agent block does and does not trip.

What did NOT change: anything under an ``agent`` subtree that this schema does
not explicitly name is still Tier C. Omission is how the escalation opened, so
the subtree still denies by default.

Two levers reach the same selector without touching the agent block at all —
inflating ``candidate.skills`` until every job scores 100, and collapsing
``scoring.weights`` onto whichever component a job maxes out. Neither can be
put in Tier C (they are the operator's headline feature). They are bounded
instead, by :data:`HARD_LIMITS` and by the fingerprint rule: a cycle that
observes a POLICY fingerprint it has not seen before must run in approval mode
regardless of configured mode. See :func:`requires_approval_cycle`.

The fingerprint that rule reads is :attr:`Policy.policy_hash`, which covers
scoring AND candidate. It is NOT :attr:`Policy.scoring_hash`, which covers the
arithmetic alone and is what a scored RESULT is stamped with. The two are
different questions and were briefly the same field name; the difference is
spelled out at :meth:`Policy.fingerprint` and guarded by
``tests/test_stamp_identity.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Optional

__all__ = [
    # tiers + schema
    "TIER_A",
    "TIER_B",
    "TIER_C",
    "KeySpec",
    "SCHEMA",
    "HARD_LIMITS",
    "spec_for",
    "tier_for",
    "iter_specs",
    "schema_defaults",
    # value objects
    "FrozenMap",
    "Weights",
    "Bonuses",
    "ExperiencePolicy",
    "SkillsPolicy",
    "SalaryPolicy",
    "Verdict",
    "ReasonsPolicy",
    "ScoringPolicy",
    "PayBand",
    "CandidatePay",
    "CandidatePolicy",
    "Policy",
    # defaults
    "DEFAULT_SCORING_POLICY",
    "DEFAULT_CANDIDATE",
    "DEFAULT_POLICY",
    # helpers
    "PolicyError",
    "canonical_json",
    "fingerprint_hash",
    "requires_approval_cycle",
]


class PolicyError(ValueError):
    """A policy value is out of range, malformed, or refused by its tier."""


# ── An immutable, hashable mapping ─────────────────────────────────────────
#
# The policy objects are frozen dataclasses and therefore hashable; a bare
# ``dict`` field would silently make them unhashable (FitScore is frozen and
# hashable today) and would also let a caller mutate a "frozen" policy in
# place. Neither is acceptable for an object that gets stamped onto results as
# proof of what produced a number.

class FrozenMap(Mapping):
    """A hashable, immutable mapping with deterministic ordering."""

    __slots__ = ("_d", "_hash")

    def __init__(self, source: Mapping | Sequence | None = None):
        if source is None:
            items: dict = {}
        elif isinstance(source, FrozenMap):
            items = dict(source._d)
        elif isinstance(source, Mapping):
            items = dict(source)
        else:
            items = dict(source)
        self._d = {k: items[k] for k in sorted(items, key=str)}
        self._hash: Optional[int] = None

    def __getitem__(self, key):
        return self._d[key]

    def __iter__(self) -> Iterator:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __repr__(self) -> str:
        return f"FrozenMap({self._d!r})"

    def __eq__(self, other) -> bool:
        if isinstance(other, FrozenMap):
            return self._d == other._d
        if isinstance(other, Mapping):
            return self._d == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(tuple(sorted(self._d.items(), key=lambda kv: str(kv[0]))))
        return self._hash

    def as_dict(self) -> dict:
        return dict(self._d)


# ── Hard limits: the ceilings the config file cannot reach ─────────────────
#
# These are the "in Python, not in the file" half of the Tier-B ratchet, plus
# the independent floors that bound the two Tier-A levers that reach the apply
# selector. ``apply_patch`` has no code path that writes them, and a config
# file containing them is refused as an unknown key.

HARD_LIMITS = FrozenMap({
    # The autonomous-apply selector floor. naukri's agent must refuse to
    # enqueue a candidate scoring below this whatever any file says, so a bad
    # threshold costs display noise rather than applications.
    "min_agent_fit_floor": 60,
    # Tier-B ceilings (design §4c).
    "max_daily_applications_ceiling": 25,
    "daily_apply_quota_ceiling": 50,
    "min_cycle_interval_hours": 1,
    "min_auto_purge_days": 30,
    # A candidate claiming more canonical skills than this is not a candidate
    # description, it is a score-inflation lever: SkillMatch.score is
    # |matched| / |job_skills|, so a profile holding every canonical skill in
    # the taxonomy scores 100 on every job that exists.
    "candidate_skills_max": 40,
    "candidate_locations_max": 25,
    # Neither scoring weight may collapse to zero: a 0.0/1.0 split makes the
    # overall score equal to whichever single component a job happens to max,
    # which is the same "everything scores 100" lever by another route.
    "weight_min": 0.1,
    "weight_max": 0.9,
    # fit.py's min(100, ...) collapse. The bonus block may shrink, never grow.
    "bonus_cap_ceiling": 20,
    "bonus_max": 10,
    # A rank adjustment reorders a result list; it never moves overall_score.
    # This bound is the CALIBRATION uplers' deleted PREFERENCE_TILT carried in
    # a comment: strictly under the smallest structural bonus (+5 for location
    # match / remote / salary fit / agent-eligible), so a stack preference can
    # decide a near-tie but can never outrank "this role is actually remote".
    # The clamp is applied to the SUM as well as to each rule, so N rules
    # cannot stack past it either.
    "rank_adjustment_max": 4,
    # A rule list long enough to be unauditable is not a preference.
    "rank_adjustment_rules_max": 20,
    # The agent block became loadable on 2026-08-25 (see the servers.naukri
    # section below). These two bound the shapes that arrive with it. Neither
    # is the safety story -- the four Python protections in naukri's agent.py
    # are -- but a ratchet with no python-side bound is not a ratchet, so the
    # two list-ish keys carry one.
    #
    # A search list long enough to be unauditable is not a policy: he cannot
    # read twenty-one queries and say what the agent will do.
    "agent_searches_max": 20,
    # How many results per agent search enter the apply queue. Above the
    # daily-application ceiling it buys nothing at all -- `_decide` caps
    # candidates at `daily_remaining` long before `_act` sees them -- so this
    # is a bound on wasted scoring, not on applications.
    "agent_per_search_limit_ceiling": 100,
})

#: Frozen at import, like the KeySpec bounds, so rewriting ``HARD_LIMITS`` in
#: a live process cannot widen the ordering clamp by assignment. The config
#: file cannot reach either name.
_RANK_ADJUSTMENT_MAX: float = float(HARD_LIMITS["rank_adjustment_max"])
_RANK_ADJUSTMENT_RULES_MAX: int = int(HARD_LIMITS["rank_adjustment_rules_max"])


# ── Trust tiers + the schema that carries them ─────────────────────────────

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"

_DIRECTIONS = frozenset({"down", "up", "grow", "shrink", None})


@dataclass(frozen=True)
class KeySpec:
    """One schema key: its tier, its default, and what bounds it.

    Args:
        path: dotted key path. ``*`` matches exactly one segment; a trailing
            ``**`` matches one or more segments (a deny-by-default subtree).
        tier: ``"A"``, ``"B"`` or ``"C"``.
        doc: why this key exists and what it gates. Not decoration — the tier
            argument has to be readable by the next person.
        readers: which servers actually read it (M3). A key with no reader is
            a decoy and must not enter the schema.
        default: the value shipped in Python. For concrete (wildcard-free)
            paths this is also what :func:`schema_defaults` builds the default
            ``servers`` block from.
        direction: for tier B — which way is free. ``"down"``/``"up"`` for
            numbers, ``"grow"``/``"shrink"`` for lists.
        ceiling / floor: python-side hard bounds. A loosening write must land
            inside them even with ``confirm_widen``.
        max_items: list-length bound.
        choices: allowed values for an enum-ish key.
    """

    path: str
    tier: str
    doc: str
    readers: tuple[str, ...] = ()
    default: Any = None
    has_default: bool = True
    direction: Optional[str] = None
    ceiling: Optional[float] = None
    floor: Optional[float] = None
    max_items: Optional[int] = None
    choices: Optional[tuple] = None

    def __post_init__(self):
        if self.tier not in (TIER_A, TIER_B, TIER_C):
            raise PolicyError(f"{self.path}: unknown tier {self.tier!r}")
        if self.direction not in _DIRECTIONS:
            raise PolicyError(f"{self.path}: unknown direction {self.direction!r}")
        if self.tier == TIER_B and self.direction is None:
            raise PolicyError(
                f"{self.path}: tier B is a ratchet and must declare a direction"
            )
        if self.tier != TIER_C and not self.readers:
            raise PolicyError(
                f"{self.path}: a key with no declared reader is a decoy "
                f"(see the anti-decoy rule); declare readers or drop the key"
            )

    @property
    def loadable(self) -> bool:
        """Tier C is never loadable from the file. Not 'ignored' — refused."""
        return self.tier != TIER_C

    @property
    def is_pattern(self) -> bool:
        return "*" in self.path

    @property
    def specificity(self) -> tuple[int, int]:
        """Sort key: more literal segments wins, then fewer wildcards."""
        parts = self.path.split(".")
        literals = sum(1 for p in parts if p not in ("*", "**"))
        wilds = len(parts) - literals
        return (literals, -wilds)


# A leaf name that is tier C wherever it appears EXCEPT where a spec names it
# explicitly -- `spec_for` resolves exact declarations first, so this is the
# rule for undeclared occurrences only. As of 2026-08-25 there is exactly one
# declared exception, `servers.naukri.agent.min_fit_score` (tier B), and this
# rule still covers every other spelling: another server's
# `servers.<x>.min_fit_score`, a nested `queue.min_fit_score`, and any future
# copy of the name.
#
# `min_fit_score` is naukri's autonomous-apply SELECTOR (`_decide` enqueues
# every job at or above it with apply_status "pending"), not a display filter.
# The display-side filter lives under the deliberately unambiguous name
# ``servers.naukri.display_min_score`` so the two decisions cannot be confused
# again (they are 60 and 70 today, and collapsing them to one value would
# silently drop the agent's threshold by ten points).
#
# Per-search overrides never reach this rule at all: `flatten` treats a list as
# a leaf, so `searches[0].min_fit_score` is not a path the schema ever sees.
# The Python floor, re-applied per search in `_decide`, is what bounds those.
TIER_C_LEAF_NAMES = frozenset({"min_fit_score"})


def _spec(path, tier, doc, readers=(), **kw) -> KeySpec:
    return KeySpec(path=path, tier=tier, doc=doc, readers=tuple(readers), **kw)


_SCHEMA_LIST: tuple[KeySpec, ...] = (
    # ── candidate ──────────────────────────────────────────────────────────
    _spec("candidate.name", TIER_A, "Display name.", ("uplers", "instahyre")),
    _spec("candidate.headline", TIER_A, "One-line self description.",
          ("uplers", "instahyre")),
    _spec(
        "candidate.years_experience", TIER_B,
        "Feeds ExperienceScore, which is 40% of every score. Shrinking is "
        "free; growing is a score-raising write and needs confirmation.",
        ("uplers", "instahyre"), default=None, direction="down",
        floor=0.0, ceiling=50.0,
    ),
    _spec(
        "candidate.skills", TIER_B,
        "SkillMatch.score is |matched| / |job_skills|, so every skill added "
        "raises every score. Removing is free; adding needs confirmation and "
        "is bounded by HARD_LIMITS['candidate_skills_max'].",
        ("uplers", "instahyre"), default=(), direction="shrink",
        max_items=int(HARD_LIMITS["candidate_skills_max"]),
    ),
    _spec("candidate.titles", TIER_A, "Titles he is looking for.",
          ("uplers", "instahyre"), default=()),
    _spec(
        "candidate.locations", TIER_A,
        "Acceptable locations. Worth at most bonuses.location_match (+5) "
        "however long the list is, so it is bounded by arithmetic.",
        ("uplers", "instahyre", "jobcore"), default=(),
        max_items=int(HARD_LIMITS["candidate_locations_max"]),
    ),
    _spec(
        "candidate.work_mode_preference", TIER_A,
        "Ordered preference over remote/hybrid/office. The bonus values come "
        "from scoring.bonuses; this list assigns them by rank.",
        ("jobcore", "uplers"), default=("remote", "hybrid", "office"),
    ),
    # C4: pay is denominated PER UNIT SYSTEM. One scalar shared between a
    # lakhs board and a USD board silently pins the salary bonus at 5 on one
    # and 0 on the other, and both look exactly like "no salary data" -- the
    # bug salary.py's is_disclosed contract exists to prevent.
    _spec("candidate.pay.inr_lakhs_per_year.expected", TIER_A,
          "Bonus target in lakhs/year (naukri). None = not configured.",
          ("naukri",), default=None, floor=0.0),
    _spec("candidate.pay.inr_lakhs_per_year.floor", TIER_A,
          "Walk-away floor in lakhs/year. A separate decision from expected.",
          ("naukri",), default=None, floor=0.0),
    _spec("candidate.pay.usd_per_year.expected", TIER_A,
          "Bonus target in USD/year (uplers). None = not configured.",
          ("uplers",), default=None, floor=0.0),
    _spec("candidate.pay.usd_per_year.floor", TIER_A,
          "Walk-away floor in USD/year.", ("uplers",), default=None, floor=0.0),
    _spec("candidate.notice_period_days", TIER_A, "Notice he must serve.",
          ("uplers",), default=0, floor=0.0, ceiling=365.0),
    _spec(
        "candidate.avoid_companies", TIER_B,
        "Companies no server may surface or apply to. Adding is free; "
        "REMOVING one he blocked is the loosening direction.",
        ("naukri", "uplers", "instahyre"), default=(), direction="grow",
    ),

    # ── scoring ────────────────────────────────────────────────────────────
    _spec(
        "scoring.weights.skills", TIER_A,
        "fit.py's 0.6. The value he named. Bounded to [weight_min, "
        "weight_max] in Python so it cannot collapse to 0 or 1 -- a 0/1 split "
        "makes the overall score equal one component and clears any threshold.",
        ("jobcore",), default=0.6,
        floor=float(HARD_LIMITS["weight_min"]), ceiling=float(HARD_LIMITS["weight_max"]),
    ),
    _spec(
        "scoring.weights.experience", TIER_A,
        "fit.py's 0.4. Must sum with skills to 1.0.",
        ("jobcore",), default=0.4,
        floor=float(HARD_LIMITS["weight_min"]), ceiling=float(HARD_LIMITS["weight_max"]),
    ),
    _spec("scoring.bonuses.location_match", TIER_A, "scoring.py's +5.",
          ("jobcore",), default=5, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec("scoring.bonuses.remote", TIER_A, "scoring.py's remote +5.",
          ("jobcore",), default=5, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec("scoring.bonuses.hybrid", TIER_A, "scoring.py's hybrid +3.",
          ("jobcore",), default=3, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec("scoring.bonuses.office", TIER_A, "scoring.py's office +0.",
          ("jobcore",), default=0, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec("scoring.bonuses.salary_meets", TIER_A, "salary.py's +5.",
          ("jobcore",), default=5, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec("scoring.bonuses.salary_near", TIER_A, "salary.py's +3.",
          ("jobcore",), default=3, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec("scoring.bonuses.agent_eligible", TIER_A,
          "fit.py's +5 -- the only bonus scoring a property of the TOOLING.",
          ("jobcore",), default=5, floor=0.0, ceiling=float(HARD_LIMITS["bonus_max"])),
    _spec(
        "scoring.bonuses.cap", TIER_B,
        "fit.py's min(100, ...) collapse. Raising it raises every score, so "
        "it ratchets DOWN only and can never exceed today's 20.",
        ("jobcore",), default=20, direction="down",
        floor=0.0, ceiling=float(HARD_LIMITS["bonus_cap_ceiling"]),
    ),
    _spec("scoring.experience.under_penalty_per_year", TIER_A,
          "fit.py's 20 points per missing year.",
          ("jobcore",), default=20, floor=0.0, ceiling=100.0),
    _spec("scoring.experience.over_coefficient", TIER_A,
          "fit.py's sqrt coefficient 15.",
          ("jobcore",), default=15, floor=0.0, ceiling=100.0),
    _spec("scoring.experience.over_floor", TIER_A,
          "fit.py's over-qualification floor 60.",
          ("jobcore",), default=60, floor=0.0, ceiling=100.0),
    _spec("scoring.experience.unknown_default", TIER_A,
          "fit.py's 50 when experience cannot be determined.",
          ("jobcore",), default=50, floor=0.0, ceiling=100.0),
    _spec("scoring.skills.unknown_job_skills_default", TIER_A,
          "fit.py's 50 when a job lists no skills.",
          ("jobcore",), default=50, floor=0.0, ceiling=100.0),
    _spec(
        "scoring.skills.weights", TIER_A,
        "Per-skill weight in coverage. Default {} = today's flat unweighted "
        "coverage, exactly. NOTE the direction of the arithmetic: weighted "
        "coverage is sum(w[matched]) / sum(w[job]), so down-weighting a skill "
        "he LACKS raises the score of jobs asking for it. This is not a "
        "substitute for a rank tilt; see test_policy_effects.",
        ("jobcore",), default=FrozenMap(),
    ),
    _spec("scoring.skills.must_have", TIER_A,
          "Skills whose absence blocks, reported alongside the score.",
          ("uplers",), default=()),
    _spec(
        "scoring.skills.extra_skills", TIER_A,
        "Vocabulary additions, canonical -> [aliases], matching "
        "SkillTaxonomy.extended()'s real signature. A write that would make "
        "ambiguous_derived_keys non-empty is refused: an ambiguous derived "
        "form silently stops resolving an EXISTING skill.",
        ("jobcore", "uplers"), default=FrozenMap(),
    ),
    _spec("scoring.salary.meets_expectation_ratio", TIER_A,
          "salary.py's 0.8.", ("jobcore",), default=0.8, floor=0.0, ceiling=2.0),
    _spec("scoring.salary.below_market_ratio", TIER_A,
          "salary.py's 0.85.", ("jobcore",), default=0.85, floor=0.0, ceiling=2.0),
    _spec("scoring.salary.above_market_ratio", TIER_A,
          "salary.py's 1.15.", ("jobcore",), default=1.15, floor=0.0, ceiling=5.0),
    _spec(
        "scoring.salary.max_package_ceiling", TIER_A,
        "The sanity ceiling in compare_to_ctc. Default None means 'use this "
        "engine's SalaryConfig.raw_amount_threshold', which is what the code "
        "does today -- uplers deliberately sets that to 10_000_000 for USD, "
        "and a concrete default here would silently re-impose 200 on it.",
        ("jobcore",), default=None, floor=0.0,
    ),
    _spec("scoring.verdicts", TIER_A,
          "fit.py's four bands, [{min,label}, ...] descending by min.",
          ("jobcore", "uplers"),
          default=(
              (80, "Strong match — apply confidently"),
              (60, "Good match — worth applying"),
              (40, "Partial match — review missing skills before applying"),
              (0, "Weak match — consider upskilling first"),
          )),
    _spec(
        "scoring.rank_adjustments", TIER_A,
        "ORDERING preferences: [{when_skills_include, and_not, delta, "
        "label}]. NEVER moves overall_score -- that stays jobcore's, so a 78 "
        "means the same thing on every board; the adjustment is reported "
        "separately. Default is the single rule uplers used to carry as the "
        "hardcoded PREFERENCE_TILT = 4 (demote a python-leaning stack that "
        "does not also ask for the node one), so nothing moves until he "
        "edits it. Each delta AND their sum are clamped to "
        "HARD_LIMITS['rank_adjustment_max'] = 4 in Python, which is strictly "
        "under the smallest structural bonus (+5). An explicit [] turns the "
        "preference off; omitting the key keeps the shipped rule.",
        ("uplers",),
        default=(
            {"when_skills_include": ["python", "django", "flask", "fastapi"],
             "and_not": ["javascript", "typescript", "node.js", "express",
                         "nestjs", "next.js"],
             "delta": -4,
             "label": "python-leaning stack"},
        ),
        max_items=int(HARD_LIMITS["rank_adjustment_rules_max"]),
    ),
    _spec("scoring.reasons.skill_gap_below", TIER_A,
          "fit.py's 50 -- below this a skill-gap reason is emitted.",
          ("jobcore",), default=50, floor=0.0, ceiling=100.0),
    _spec("scoring.reasons.experience_below", TIER_A,
          "fit.py's 70 -- below this an experience reason is emitted.",
          ("jobcore",), default=70, floor=0.0, ceiling=100.0),
    _spec("scoring.reasons.missing_skills_shown", TIER_A,
          "fit.py's 3 -- how many missing skills the reason names.",
          ("jobcore",), default=3, floor=0.0, ceiling=50.0),

    # ── servers.naukri ─────────────────────────────────────────────────────
    #
    # DENY BY DEFAULT under any agent subtree. This one line is what stops the
    # next key added under `agent` from arriving as Tier A by omission --
    # `enabled` and `mode` had no tier at all in the design, which is how the
    # escalation path opened. It is UNCHANGED by the 2026-08-25 ruling below:
    # the six keys named under it are loadable BY NAME, and a seventh invented
    # tomorrow is still refused.
    _spec("servers.*.agent.**", TIER_C,
          "Anything under an agent block that this schema does not explicitly "
          "name is refused. Autonomous-apply machinery is opt-in by an "
          "explicitly named key, never by omission."),

    # THE SIX, AND WHAT ACTUALLY BOUNDS THEM (ruling 2026-08-25).
    #
    # These were tier C. The reasoning that put them there traced a five-write
    # escalation -- enabled -> mode:auto -> min_fit_score:0 -> blocklist off ->
    # arbitrary searches -- ending at fifteen unapproved applications a day.
    # That reasoning was SOUND and the conclusion has been overruled: the file
    # may now ARM the agent. The protections were NOT overruled, and none of
    # them ever lived in this schema. They live in Python, in naukri's
    # `agent.py`, where no config write of any kind can reach them:
    #
    #   1. MIN_AGENT_FIT_FLOOR (=60) -- `_decide` computes
    #      `min_fit = max(configured, floor)` and re-applies the same `max()`
    #      to every per-search override. `min_fit_score: 0` from ANY source,
    #      this file included, selects nothing under 60.
    #   2. The forced approval cycle -- `_effective_mode` downgrades "auto" to
    #      "approval" for one cycle whenever the POLICY fingerprint moves, and
    #      on the first-ever cycle (`requires_approval_cycle(h, None)` is
    #      True). Keyed on {scoring, candidate}; see the caveat below.
    #   3. The kill switch -- checked inside the auto-apply loop itself.
    #   4. The daily quota -- `_decide` caps candidates at `daily_remaining`
    #      BEFORE `_act` runs, and `validate_agent_config` bounds
    #      `max_daily_applications` to 1-100 whatever the source.
    #
    # THE HONEST CAVEAT, so nobody re-derives it as a discovery: protection 2
    # is keyed on `policy_hash`, which covers {scoring, candidate} and NOT
    # `servers.*`. Writing `mode: "auto"` here therefore does NOT itself force
    # an approval cycle -- that guard was built for the two levers that CANNOT
    # be tier C (candidate.skills, scoring), and it still catches those. What
    # bounds a file-armed agent is 1, 3 and 4, plus tier B's own ratchet: every
    # one of the six loosens only with confirm_widen on the write path, and
    # naukri re-validates all six through `validate_agent_config` on load.
    #
    # Tier B, not tier A, for all six. Tightening stays free; loosening costs
    # an explicit flag.
    _spec("servers.naukri.agent.enabled", TIER_B,
          "Arms the autonomous apply loop. Disarming is free; arming is the "
          "loosening direction and needs confirm_widen. What the armed loop "
          "may then do is bounded in Python, not here.",
          ("naukri",), default=False, direction="down",
          choices=(False, True)),
    _spec("servers.naukri.agent.mode", TIER_B,
          "dry_run | approval | auto. 'auto' takes the branch that applies "
          "with no human approval. Any change needs confirm_widen: the values "
          "are not ordered, so `_is_loosening` treats every move as loosening "
          "-- more friction than a ratchet, never less.",
          ("naukri",), default="dry_run", direction="down",
          choices=("dry_run", "approval", "auto")),
    _spec("servers.naukri.agent.min_fit_score", TIER_B,
          "The apply SELECTOR -- a different decision from the display "
          "filter's 60, not a drifted copy of it. Raising is free; lowering "
          "needs confirm_widen and CANNOT take the agent below "
          "MIN_AGENT_FIT_FLOOR, which is enforced in Python on every cycle.",
          ("naukri",), default=70, direction="up", floor=0.0, ceiling=100.0),
    _spec("servers.naukri.agent.searches", TIER_B,
          "What the agent looks for. Each entry may carry its own "
          "min_fit_score override; those overrides are LIST ELEMENTS, so they "
          "never reach this schema at all -- the Python floor is what bounds "
          "them, applied per search. An EMPTY list means 'not specified' to "
          "naukri's loader, never 'search for nothing'.",
          ("naukri",), default=(), direction="shrink",
          max_items=int(HARD_LIMITS["agent_searches_max"])),
    _spec("servers.naukri.agent.per_search_limit", TIER_B,
          "How many results per agent search enter the apply queue. Bounded "
          "downstream by the daily cap regardless, so this bounds wasted "
          "scoring rather than applications.",
          ("naukri",), default=20, direction="down", floor=1.0,
          ceiling=float(HARD_LIMITS["agent_per_search_limit_ceiling"])),
    _spec("servers.naukri.agent.blocklist.enabled", TIER_B,
          "Turning the blocklist off makes companies he blocked eligible for "
          "an irreversible application, so True is the safe state and "
          "switching it off is the loosening direction.",
          ("naukri",), default=True, direction="up"),
    _spec("servers.naukri.agent.blocklist.companies", TIER_B,
          "Adding is free; removing is the loosening direction.",
          ("naukri",), default=(), direction="grow"),
    _spec("servers.naukri.agent.blocklist.title_keywords", TIER_B,
          "Adding is free; removing is the loosening direction.",
          ("naukri",), default=(), direction="grow"),
    _spec("servers.naukri.agent.max_daily_applications", TIER_B,
          "The count bound on autonomous applying. Ratchets down freely; "
          "raising needs confirm_widen and cannot pass the Python ceiling.",
          ("naukri",), default=15, direction="down", floor=0.0,
          ceiling=float(HARD_LIMITS["max_daily_applications_ceiling"])),
    _spec("servers.naukri.agent.cycle_interval_hours", TIER_B,
          "How often the agent wakes. Slower is safer, so 'up' is free.",
          ("naukri",), default=2, direction="up",
          floor=float(HARD_LIMITS["min_cycle_interval_hours"]), ceiling=168.0),
    _spec("servers.naukri.agent.quiet_hours.enabled", TIER_B,
          "Quiet hours may be switched on freely; switching them off is "
          "loosening.", ("naukri",), default=True, direction="up"),
    _spec("servers.naukri.agent.quiet_hours.start_hour", TIER_B,
          "Window may only widen without confirmation.",
          ("naukri",), default=20, direction="down", floor=0.0, ceiling=23.0),
    _spec("servers.naukri.agent.quiet_hours.end_hour", TIER_B,
          "Window may only widen without confirmation.",
          ("naukri",), default=8, direction="up", floor=0.0, ceiling=23.0),
    _spec("servers.naukri.agent.quiet_hours.tz", TIER_B,
          "IANA zone for the quiet window.",
          ("naukri",), default="Asia/Kolkata", direction="up"),
    _spec("servers.naukri.display_min_score", TIER_A,
          "The DISPLAY filter -- naukri's 60. Deliberately not named "
          "min_fit_score: that name is the agent's apply selector and is "
          "tier C wherever it appears.", ("naukri",), default=60,
          floor=0.0, ceiling=100.0),
    _spec("servers.naukri.daily_apply_quota", TIER_B,
          "The second, harder daily cap (config.py:202).",
          ("naukri",), default=50, direction="down", floor=0.0,
          ceiling=float(HARD_LIMITS["daily_apply_quota_ceiling"])),
    _spec("servers.naukri.retention.auto_purge_days", TIER_B,
          "Deletes his application history on every sync. LOWERING it "
          "destroys more history and is irreversible, so 'up' is the free "
          "direction.", ("naukri",), default=180, direction="up",
          floor=float(HARD_LIMITS["min_auto_purge_days"]), ceiling=3650.0),
    _spec("servers.naukri.staleness.days", TIER_A,
          "One home for the 14 retyped in seven files.",
          ("naukri",), default=14, floor=1.0, ceiling=365.0),
    _spec("servers.naukri.staleness.min_stale_score", TIER_A,
          "40 in scheduler_tasks, 50 in daily_brief -- one value now.",
          ("naukri",), default=40, floor=0.0, ceiling=100.0),
    _spec("servers.naukri.daily_brief.hour", TIER_A, "Brief delivery hour.",
          ("naukri",), default=8, floor=0.0, ceiling=23.0),
    _spec("servers.naukri.daily_brief.sections.inbox", TIER_A, "Rows shown.",
          ("naukri",), default=5, floor=0.0, ceiling=50.0),
    _spec("servers.naukri.daily_brief.sections.notifications", TIER_A, "Rows shown.",
          ("naukri",), default=5, floor=0.0, ceiling=50.0),
    _spec("servers.naukri.daily_brief.sections.recommendations", TIER_A, "Rows shown.",
          ("naukri",), default=5, floor=0.0, ceiling=50.0),
    _spec("servers.naukri.daily_brief.sections.recruiter", TIER_A, "Rows shown.",
          ("naukri",), default=5, floor=0.0, ceiling=50.0),
    _spec("servers.naukri.daily_brief.sections.early_access", TIER_A, "Rows shown.",
          ("naukri",), default=3, floor=0.0, ceiling=50.0),
    _spec("servers.naukri.daily_brief.sections.saved", TIER_A, "Rows shown.",
          ("naukri",), default=1, floor=0.0, ceiling=50.0),
    _spec("servers.naukri.follow_up.auto_draft_at", TIER_A,
          "Score at or above which a follow-up is drafted (never sent).",
          ("naukri",), default=70, floor=0.0, ceiling=100.0),
    _spec("servers.naukri.follow_up.notify_at", TIER_A, "Score to notify at.",
          ("naukri",), default=60, floor=0.0, ceiling=100.0),
    _spec("servers.naukri.follow_up.template_id", TIER_A, "Which template.",
          ("naukri",), default="default"),
    _spec("servers.naukri.boost_profile.enabled", TIER_A, "Daily profile boost.",
          ("naukri",), default=True),
    _spec("servers.naukri.boost_profile.hour", TIER_A, "Hour to boost at.",
          ("naukri",), default=9, floor=0.0, ceiling=23.0),
    _spec("servers.naukri.boost_profile.randomize", TIER_A,
          "TODAY'S LITERAL IS False (scheduler_tasks.py, pinned by "
          "test_scheduler.py and test_profile_deep.py). The design proposed "
          "flipping it; a default flip is a behaviour change and ships in its "
          "own commit, never inside the mechanism.",
          ("naukri",), default=False),
    _spec("servers.naukri.saved_jobs.expiry_days", TIER_A, "Saved-job expiry.",
          ("naukri",), default=30, floor=1.0, ceiling=365.0),
    _spec("servers.naukri.saved_jobs.warn_days", TIER_A, "Warn before expiry.",
          ("naukri",), default=27, floor=0.0, ceiling=365.0),

    # ── servers.uplers ─────────────────────────────────────────────────────
    _spec("servers.uplers.must_have.zero_coverage_blocks", TIER_A,
          "Zero must-have coverage blocks the opportunity.",
          ("uplers",), default=True),
    _spec("servers.uplers.must_have.warn_ratio", TIER_A,
          "Below this coverage ratio, warn.",
          ("uplers",), default=0.5, floor=0.0, ceiling=1.0),
    _spec("servers.uplers.notice.shortfall_blocks", TIER_A,
          "A notice-period shortfall is a hard block today.",
          ("uplers",), default=True),
    _spec("servers.uplers.notice.tolerance_days", TIER_A, "Slack in days.",
          ("uplers",), default=0, floor=0.0, ceiling=180.0),
    _spec("servers.uplers.experience_slack_years", TIER_A,
          "fit.py's bare 1 -- years of slack against a job's minimum.",
          ("uplers",), default=1, floor=0.0, ceiling=10.0),
    _spec("servers.uplers.exclude_blocked.rank", TIER_A,
          "The 3-way divergence, in one place: rank defaults True today.",
          ("uplers",), default=True),
    _spec("servers.uplers.exclude_blocked.brief", TIER_A,
          "brief.py hardcodes True today.", ("uplers",), default=True),
    _spec("servers.uplers.exclude_blocked.alerts", TIER_A,
          "alerts.py defaults False today.", ("uplers",), default=False),
    _spec("servers.uplers.include_aggregated", TIER_A,
          "Aggregated (non-native) requisitions.", ("uplers",), default=False),
    _spec("servers.uplers.follow_up_stale_days", TIER_A, "Follow-up staleness.",
          ("uplers",), default=7, floor=1.0, ceiling=365.0),
    _spec("servers.uplers.index_stale_hours", TIER_A, "Index staleness.",
          ("uplers",), default=36, floor=1.0, ceiling=720.0),
    _spec("servers.uplers.auto_sync.enabled", TIER_A, "Background index sync.",
          ("uplers",), default=True),
    _spec("servers.uplers.auto_sync.interval_hours", TIER_A, "Sync cadence.",
          ("uplers",), default=6, floor=1.0, ceiling=168.0),
    _spec("servers.uplers.auto_sync.budget", TIER_A, "Requests per sync.",
          ("uplers",), default=120, floor=1.0, ceiling=2000.0),

    # ── servers.instahyre ──────────────────────────────────────────────────
    _spec("servers.instahyre.exclude_agencies", TIER_A,
          "TODAY'S LITERAL IS False (server.py, client.py). The server's own "
          "docs argue for True; that flip is a product decision and ships "
          "separately.", ("instahyre",), default=False),
    _spec("servers.instahyre.show_agency_flag", TIER_A, "Show the agency flag.",
          ("instahyre",), default=True),
    _spec("servers.instahyre.unverified_agency", TIER_A,
          "What to do with a job whose agency status cannot be verified. "
          "TODAY'S LITERAL IS 'drop' (client.py silently drops them).",
          ("instahyre",), default="drop", choices=("drop", "keep", "flag")),
    _spec("servers.instahyre.queue.order", TIER_A,
          "TODAY'S LITERAL IS 'platform' (inbound.py computes a jobcore score "
          "and then orders by the platform's own).",
          ("instahyre",), default="platform", choices=("platform", "fit", "date")),
    _spec("servers.instahyre.queue.full_queue", TIER_A, "Whole queue or a page.",
          ("instahyre",), default=True),
    _spec("servers.instahyre.queue.digest_top_n", TIER_A, "Digest size.",
          ("instahyre",), default=8, floor=1.0, ceiling=100.0),
    _spec("servers.instahyre.queue.new_window_hours", TIER_A, "What counts as new.",
          ("instahyre",), default=24, floor=1.0, ceiling=720.0),
    _spec("servers.instahyre.skill_cap_priority", TIER_A,
          "Order to keep when the platform's skill cap truncates additions.",
          ("instahyre",), default=()),

    # ── servers.linkedin_own ───────────────────────────────────────────────
    _spec("servers.linkedin_own.job_card_signals.promoted", TIER_A,
          "keep | drop. shape.py discards all four with no trace today.",
          ("linkedin_own",), default="keep", choices=("keep", "drop")),
    _spec("servers.linkedin_own.job_card_signals.easy_apply", TIER_A,
          "keep | drop.", ("linkedin_own",), default="keep", choices=("keep", "drop")),
    _spec("servers.linkedin_own.job_card_signals.actively_recruiting", TIER_A,
          "keep | drop.", ("linkedin_own",), default="keep", choices=("keep", "drop")),
    _spec("servers.linkedin_own.job_card_signals.early_applicant", TIER_A,
          "keep | drop.", ("linkedin_own",), default="keep", choices=("keep", "drop")),
    _spec("servers.linkedin_own.search.default_sort", TIER_A,
          "TODAY'S LITERAL IS 'relevance' (pinned by test_tools.py). The "
          "design proposed 'date'; that flip ships separately.",
          ("linkedin_own",), default="relevance", choices=("relevance", "date")),
    _spec("servers.linkedin_own.caps.default_limit", TIER_A, "Default page size.",
          ("linkedin_own",), default=25, floor=1.0, ceiling=200.0),
    _spec("servers.linkedin_own.caps.search_default_limit", TIER_A,
          "Default search page size.",
          ("linkedin_own",), default=25, floor=1.0, ceiling=200.0),
)

SCHEMA: dict[str, KeySpec] = {s.path: s for s in _SCHEMA_LIST}
assert len(SCHEMA) == len(_SCHEMA_LIST), "duplicate path in SCHEMA"

_PATTERNS: tuple[KeySpec, ...] = tuple(
    sorted((s for s in _SCHEMA_LIST if s.is_pattern),
           key=lambda s: s.specificity, reverse=True)
)


def iter_specs() -> tuple[KeySpec, ...]:
    """Every declared key, in declaration order."""
    return _SCHEMA_LIST


def _pattern_matches(pattern: str, path: str) -> bool:
    pat = pattern.split(".")
    seg = path.split(".")
    for i, p in enumerate(pat):
        if p == "**":
            return len(seg) > i
        if i >= len(seg):
            return False
        if p != "*" and p != seg[i]:
            return False
    return len(seg) == len(pat)


def spec_for(path: str) -> Optional[KeySpec]:
    """The most specific :class:`KeySpec` governing *path*, or None.

    Exact declarations win. Then the always-tier-C leaf rule. Then patterns,
    most-literal-segments first, so an explicitly named key under a
    deny-by-default subtree keeps its own tier.
    """
    exact = SCHEMA.get(path)
    if exact is not None:
        return exact
    leaf = path.rsplit(".", 1)[-1]
    if leaf in TIER_C_LEAF_NAMES:
        return KeySpec(
            path=path, tier=TIER_C,
            doc=(f"{leaf!r} is the autonomous-apply selector wherever it "
                 f"appears; the display filter is display_min_score."),
        )
    for pattern in _PATTERNS:
        if _pattern_matches(pattern.path, path):
            return pattern
    return None


def tier_for(path: str) -> Optional[str]:
    """Tier of *path*, or None when the key is not declared at all."""
    spec = spec_for(path)
    return spec.tier if spec else None


def schema_defaults(prefix: str) -> dict:
    """Build the default nested dict for a schema *prefix* (e.g. ``servers``).

    Only wildcard-free specs contribute, so the deny-by-default subtree adds
    nothing. Tier C keys DO contribute — the file may display them.
    """
    out: dict = {}
    plen = len(prefix.split("."))
    for spec in _SCHEMA_LIST:
        if spec.is_pattern or not spec.path.startswith(prefix + "."):
            continue
        parts = spec.path.split(".")[plen:]
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = _plain(spec.default)
    return out


def _plain(value):
    """Schema defaults as plain JSON-able values (tuples -> lists)."""
    if isinstance(value, FrozenMap):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# ── Scoring policy value objects ───────────────────────────────────────────

@dataclass(frozen=True)
class Weights:
    """Base-score split. ``skills * s + experience * e``, ``s + e == 1``."""

    skills: float = 0.6
    experience: float = 0.4

    def validate(self) -> None:
        lo = float(HARD_LIMITS["weight_min"])
        hi = float(HARD_LIMITS["weight_max"])
        for name in ("skills", "experience"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise PolicyError(f"scoring.weights.{name} must be a number, got {v!r}")
            if not (lo <= v <= hi):
                raise PolicyError(
                    f"scoring.weights.{name}={v} is outside [{lo}, {hi}]. A "
                    f"weight at 0 or 1 makes the score equal one component, "
                    f"which clears any threshold; that bound lives in Python "
                    f"and the config file cannot move it."
                )
        total = self.skills + self.experience
        if abs(total - 1.0) > 1e-9:
            raise PolicyError(
                f"scoring.weights must sum to 1.0, got {total} "
                f"(skills={self.skills}, experience={self.experience})"
            )


@dataclass(frozen=True)
class Bonuses:
    """The additive bonus table and its cap."""

    location_match: int = 5
    remote: int = 5
    hybrid: int = 3
    office: int = 0
    salary_meets: int = 5
    salary_near: int = 3
    agent_eligible: int = 5
    cap: int = 20

    def validate(self) -> None:
        hi = float(HARD_LIMITS["bonus_max"])
        for name in ("location_match", "remote", "hybrid", "office",
                     "salary_meets", "salary_near", "agent_eligible"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise PolicyError(f"scoring.bonuses.{name} must be a number, got {v!r}")
            if not (0 <= v <= hi):
                raise PolicyError(f"scoring.bonuses.{name}={v} is outside [0, {hi}]")
        ceiling = float(HARD_LIMITS["bonus_cap_ceiling"])
        if not (0 <= self.cap <= ceiling):
            raise PolicyError(
                f"scoring.bonuses.cap={self.cap} is outside [0, {ceiling}]"
            )

    def work_mode_values(self) -> tuple[int, ...]:
        """Bonus values assigned by preference RANK, best first.

        The three category values sorted descending. With the shipped table
        that is ``(5, 3, 0)``; with the shipped preference order
        ``("remote", "hybrid", "office")`` this reproduces scoring.py exactly.
        Reordering the preference reassigns the same values.
        """
        return tuple(sorted((self.remote, self.hybrid, self.office), reverse=True))


@dataclass(frozen=True)
class ExperiencePolicy:
    under_penalty_per_year: float = 20
    over_coefficient: float = 15
    over_floor: float = 60
    unknown_default: float = 50

    def validate(self) -> None:
        for name in ("under_penalty_per_year", "over_coefficient",
                     "over_floor", "unknown_default"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise PolicyError(f"scoring.experience.{name} must be a number")
            if not (0 <= v <= 100):
                raise PolicyError(f"scoring.experience.{name}={v} is outside [0, 100]")


@dataclass(frozen=True)
class SkillsPolicy:
    unknown_job_skills_default: float = 50
    weights: FrozenMap = field(default_factory=FrozenMap)
    must_have: tuple[str, ...] = ()
    extra_skills: FrozenMap = field(default_factory=FrozenMap)

    def validate(self) -> None:
        if not (0 <= self.unknown_job_skills_default <= 100):
            raise PolicyError(
                f"scoring.skills.unknown_job_skills_default="
                f"{self.unknown_job_skills_default} is outside [0, 100]"
            )
        for skill, w in self.weights.items():
            if not isinstance(w, (int, float)) or isinstance(w, bool):
                raise PolicyError(
                    f"scoring.skills.weights[{skill!r}] must be a number, got {w!r}"
                )
            if not (0.0 <= w <= 5.0):
                raise PolicyError(
                    f"scoring.skills.weights[{skill!r}]={w} is outside [0.0, 5.0]"
                )
        for canonical, aliases in self.extra_skills.items():
            if not isinstance(canonical, str) or not canonical.strip():
                raise PolicyError(
                    f"scoring.skills.extra_skills key {canonical!r} must be a "
                    f"non-empty canonical skill name"
                )
            if isinstance(aliases, str) or not isinstance(aliases, (list, tuple, set,
                                                                    frozenset)):
                raise PolicyError(
                    f"scoring.skills.extra_skills[{canonical!r}] must be a list "
                    f"of aliases (canonical -> [aliases]), got {aliases!r}. This "
                    f"is SkillTaxonomy.extended()'s real signature; the inverse "
                    f"map would silently mint canonicals nobody declared."
                )

    def weight_of(self, skill: str) -> float:
        """Weight for *skill*; 1.0 when unweighted, which is today's behaviour."""
        try:
            return float(self.weights[skill])
        except (KeyError, TypeError, ValueError):
            return 1.0

    def taxonomy_extension(self) -> dict[str, set[str]]:
        """``extra_skills`` in the shape ``SkillTaxonomy.extended()`` wants."""
        return {k: set(v) for k, v in self.extra_skills.items()}


@dataclass(frozen=True)
class SalaryPolicy:
    meets_expectation_ratio: float = 0.8
    below_market_ratio: float = 0.85
    above_market_ratio: float = 1.15
    #: ``None`` means "use the engine's SalaryConfig.raw_amount_threshold",
    #: which is exactly what ``Salary.compare_to_ctc`` does today. uplers binds
    #: that to 10_000_000 for USD/year; a concrete default here would silently
    #: re-impose naukri's 200-lakh ceiling on a USD board.
    max_package_ceiling: Optional[float] = None

    def validate(self) -> None:
        for name in ("meets_expectation_ratio", "below_market_ratio",
                     "above_market_ratio"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise PolicyError(f"scoring.salary.{name} must be a number")
            if v < 0:
                raise PolicyError(f"scoring.salary.{name}={v} must be >= 0")
        if self.max_package_ceiling is not None and self.max_package_ceiling < 0:
            raise PolicyError("scoring.salary.max_package_ceiling must be >= 0")

    def ceiling_for(self, salary_config_threshold: float) -> float:
        """The sanity ceiling to apply, honouring the engine's own unit."""
        if self.max_package_ceiling is None:
            return salary_config_threshold
        return float(self.max_package_ceiling)


@dataclass(frozen=True)
class Verdict:
    min: float
    label: str


@dataclass(frozen=True)
class ReasonsPolicy:
    skill_gap_below: float = 50
    experience_below: float = 70
    missing_skills_shown: int = 3

    def validate(self) -> None:
        if not (0 <= self.skill_gap_below <= 100):
            raise PolicyError("scoring.reasons.skill_gap_below outside [0, 100]")
        if not (0 <= self.experience_below <= 100):
            raise PolicyError("scoring.reasons.experience_below outside [0, 100]")
        if not (0 <= self.missing_skills_shown <= 50):
            raise PolicyError("scoring.reasons.missing_skills_shown outside [0, 50]")


_DEFAULT_VERDICTS: tuple[Verdict, ...] = (
    Verdict(80, "Strong match — apply confidently"),
    Verdict(60, "Good match — worth applying"),
    Verdict(40, "Partial match — review missing skills before applying"),
    Verdict(0, "Weak match — consider upskilling first"),
)


@dataclass(frozen=True)
class RankRule:
    """One ORDERING preference, expressed over a job's skill set.

    This is deliberately NOT a score change. ``overall_score`` stays exactly
    jobcore's, so a 78 means the same thing on every board that uses this
    package — which is the invariant jobcore exists to hold. A rule moves the
    ORDER and is reported separately, with its label, so the reader can see
    why one row is above another.

    It is also deliberately not ``scoring.skills.weights``. Weighted coverage
    is ``sum(w[matched]) / sum(w[job])``, which CANCELS whenever the matched
    set equals the job set (a pure-Python role against a profile holding
    Python is untouched) and RAISES the score of a job asking for a
    down-weighted skill the candidate lacks. Measured: `{node.js, django}`
    scores 50 flat and 58.8 with ``django`` at 0.7. A demotion expressed that
    way runs backwards on the modal case.

    Args:
        when_skills_include: the rule fires when the job asks for ANY of
            these. Empty means the rule can never fire, and is refused.
        and_not: ...unless the job also asks for any of THESE. This is the
            "a role wanting both stacks is already the direction he is moving
            in" clause, and it is why one signed number per skill is not
            enough to express the preference.
        delta: points added to the ordering key. Negative demotes. Bounded by
            ``HARD_LIMITS['rank_adjustment_max']``.
        label: why. Required — a row that prints an adjustment it cannot
            explain is worse than no adjustment.
    """

    when_skills_include: tuple[str, ...] = ()
    and_not: tuple[str, ...] = ()
    delta: float = 0
    label: str = ""

    def validate(self, index: int = 0) -> None:
        where = f"scoring.rank_adjustments[{index}]"
        if not self.when_skills_include:
            raise PolicyError(
                f"{where}.when_skills_include is empty, so the rule can never "
                f"fire. A rule that matches nothing is a decoy."
            )
        for name, seq in (("when_skills_include", self.when_skills_include),
                          ("and_not", self.and_not)):
            for entry in seq:
                if not isinstance(entry, str) or not entry.strip():
                    raise PolicyError(
                        f"{where}.{name} must hold non-empty skill names, "
                        f"got {entry!r}"
                    )
        overlap = self._lower(self.when_skills_include) & self._lower(self.and_not)
        if overlap:
            raise PolicyError(
                f"{where}: {sorted(overlap)} appears in both "
                f"when_skills_include and and_not, so the rule can never "
                f"fire — the and_not clause always cancels the match."
            )
        if isinstance(self.delta, bool) or not isinstance(self.delta, (int, float)):
            raise PolicyError(f"{where}.delta must be a number, got {self.delta!r}")
        cap = _RANK_ADJUSTMENT_MAX
        if abs(self.delta) > cap:
            raise PolicyError(
                f"{where}.delta={self.delta} exceeds ±{cap:g}. That bound is "
                f"the calibration: strictly under the smallest structural "
                f"bonus (+5), so a stack preference can break a near-tie but "
                f"never outrank a real signal like 'this role is remote'. It "
                f"is not in the config file and cannot be raised from it."
            )
        if not isinstance(self.label, str) or not self.label.strip():
            raise PolicyError(
                f"{where}.label is required: a row may not print an "
                f"adjustment it cannot explain."
            )

    @staticmethod
    def _lower(seq) -> set:
        return {str(s).strip().lower() for s in seq if str(s).strip()}

    def matches(self, skills) -> bool:
        """True when *skills* (canonical names) trigger this rule."""
        have = self._lower(skills)
        if not have:
            return False
        if not (have & self._lower(self.when_skills_include)):
            return False
        if self.and_not and (have & self._lower(self.and_not)):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "when_skills_include": list(self.when_skills_include),
            "and_not": list(self.and_not),
            "delta": self.delta,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "RankRule":
        if not isinstance(data, Mapping):
            raise PolicyError(
                f"scoring.rank_adjustments entries must be objects with "
                f"when_skills_include / and_not / delta / label, got {data!r}"
            )
        d = dict(data)
        unknown = set(d) - {"when_skills_include", "and_not", "delta", "label"}
        if unknown:
            raise PolicyError(
                f"scoring.rank_adjustments entry has unknown field(s) "
                f"{sorted(unknown)}; nothing reads them."
            )
        return cls(
            when_skills_include=tuple(d.get("when_skills_include") or ()),
            and_not=tuple(d.get("and_not") or ()),
            delta=_num(d.get("delta"), 0),
            label=str(d.get("label") or ""),
        )


#: uplers carried this as ``PREFERENCE_TILT = 4`` plus two hardcoded
#: frozensets, in a sibling repo, where the operator could not reach it. It is
#: his stated preference — "keep Python but rank it lower" — so it belongs in
#: the file he edits. The DEFAULT is exactly what the constant did, so nothing
#: moves until he changes it.
_DEFAULT_RANK_ADJUSTMENTS: tuple[RankRule, ...] = (
    RankRule(
        when_skills_include=("python", "django", "flask", "fastapi"),
        and_not=("javascript", "typescript", "node.js", "express",
                 "nestjs", "next.js"),
        delta=-4,
        label="python-leaning stack",
    ),
)


@dataclass(frozen=True)
class ScoringPolicy:
    """Everything that can move a number: the ARITHMETIC, and nothing else.

    This half fingerprints as :attr:`scoring_hash`. The distinction from
    :attr:`Policy.policy_hash` is load-bearing and is documented at
    :meth:`Policy.fingerprint`; the short version is that this one answers
    "was the same arithmetic applied?" and the other answers "was the same
    policy in effect?", which are different questions about the same file.
    """

    weights: Weights = field(default_factory=Weights)
    bonuses: Bonuses = field(default_factory=Bonuses)
    experience: ExperiencePolicy = field(default_factory=ExperiencePolicy)
    skills: SkillsPolicy = field(default_factory=SkillsPolicy)
    salary: SalaryPolicy = field(default_factory=SalaryPolicy)
    verdicts: tuple[Verdict, ...] = _DEFAULT_VERDICTS
    reasons: ReasonsPolicy = field(default_factory=ReasonsPolicy)
    rank_adjustments: tuple[RankRule, ...] = _DEFAULT_RANK_ADJUSTMENTS

    def validate(self) -> None:
        self.weights.validate()
        self.bonuses.validate()
        self.experience.validate()
        self.skills.validate()
        self.salary.validate()
        self.reasons.validate()
        rules_cap = _RANK_ADJUSTMENT_RULES_MAX
        if len(self.rank_adjustments) > rules_cap:
            raise PolicyError(
                f"scoring.rank_adjustments has {len(self.rank_adjustments)} "
                f"rules, over the maximum of {rules_cap}. A rule list nobody "
                f"can audit is not a preference."
            )
        for index, rule in enumerate(self.rank_adjustments):
            rule.validate(index)
        if not self.verdicts:
            raise PolicyError("scoring.verdicts must not be empty")
        mins = [v.min for v in self.verdicts]
        if mins != sorted(mins, reverse=True):
            raise PolicyError(
                f"scoring.verdicts must be ordered descending by min, got {mins}"
            )
        if mins[-1] > 0:
            raise PolicyError(
                f"scoring.verdicts must have a band starting at 0 or below, "
                f"or a low score falls through to no verdict at all "
                f"(lowest band starts at {mins[-1]})"
            )

    def verdict_for(self, score: float) -> Verdict:
        for band in self.verdicts:
            if score >= band.min:
                return band
        return self.verdicts[-1]

    def rank_adjustment(self, skills) -> tuple:
        """``(delta, labels)`` for a job asking for *skills*.

        Deltas from every matching rule are summed and then clamped to
        ``±HARD_LIMITS['rank_adjustment_max']``, so the ordering preference
        stays under the smallest structural bonus no matter how many rules
        are written. Returns ``(0, ())`` when nothing matches, which is what
        every job on the board gets today except a python-leaning one.
        """
        total: float = 0.0
        labels: list[str] = []
        for rule in self.rank_adjustments:
            if rule.matches(skills):
                total += rule.delta
                labels.append(rule.label)
        cap = _RANK_ADJUSTMENT_MAX
        total = max(-cap, min(cap, total))
        if float(total).is_integer():
            total = int(total)
        return (total, tuple(labels))

    def to_dict(self) -> dict:
        return {
            "weights": {"skills": self.weights.skills,
                        "experience": self.weights.experience},
            "bonuses": {
                "location_match": self.bonuses.location_match,
                "remote": self.bonuses.remote,
                "hybrid": self.bonuses.hybrid,
                "office": self.bonuses.office,
                "salary_meets": self.bonuses.salary_meets,
                "salary_near": self.bonuses.salary_near,
                "agent_eligible": self.bonuses.agent_eligible,
                "cap": self.bonuses.cap,
            },
            "experience": {
                "under_penalty_per_year": self.experience.under_penalty_per_year,
                "over_coefficient": self.experience.over_coefficient,
                "over_floor": self.experience.over_floor,
                "unknown_default": self.experience.unknown_default,
            },
            "skills": {
                "unknown_job_skills_default":
                    self.skills.unknown_job_skills_default,
                "weights": self.skills.weights.as_dict(),
                "must_have": list(self.skills.must_have),
                "extra_skills": {k: sorted(v)
                                 for k, v in self.skills.extra_skills.items()},
            },
            "salary": {
                "meets_expectation_ratio": self.salary.meets_expectation_ratio,
                "below_market_ratio": self.salary.below_market_ratio,
                "above_market_ratio": self.salary.above_market_ratio,
                "max_package_ceiling": self.salary.max_package_ceiling,
            },
            "verdicts": [{"min": v.min, "label": v.label} for v in self.verdicts],
            "reasons": {
                "skill_gap_below": self.reasons.skill_gap_below,
                "experience_below": self.reasons.experience_below,
                "missing_skills_shown": self.reasons.missing_skills_shown,
            },
            "rank_adjustments": [r.to_dict() for r in self.rank_adjustments],
        }

    # ── stamping ───────────────────────────────────────────────────────────

    def fingerprint(self) -> dict:
        """The arithmetic, and only the arithmetic.

        Wrapped in a ``{"scoring": ...}`` envelope rather than hashed bare, so
        that the two fingerprints in this module are the same SHAPE and one
        can never accidentally collide with the other's payload.
        """
        return {"scoring": self.to_dict()}

    @property
    def scoring_hash(self) -> str:
        """Short hash of the arithmetic. See :meth:`Policy.fingerprint`.

        This is the single implementation. :attr:`Policy.scoring_hash` and
        :attr:`jobcore.fit.FitScore.scoring_hash` both delegate here rather
        than rebuilding the payload, because two hand-written copies of a
        fingerprint is precisely how the last ambiguity got in.
        """
        return fingerprint_hash(self.fingerprint())

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "ScoringPolicy":
        if not data:
            return cls()
        d = dict(data)
        w = dict(d.get("weights") or {})
        b = dict(d.get("bonuses") or {})
        e = dict(d.get("experience") or {})
        s = dict(d.get("skills") or {})
        sal = dict(d.get("salary") or {})
        r = dict(d.get("reasons") or {})
        verdicts = d.get("verdicts")
        if verdicts:
            bands = tuple(
                Verdict(min=v["min"], label=v["label"])
                if isinstance(v, Mapping) else Verdict(min=v[0], label=v[1])
                for v in verdicts
            )
        else:
            bands = _DEFAULT_VERDICTS
        # ``None`` (absent) reverts to the shipped rule, per the None-means-
        # default convention. An explicit ``[]`` is NOT the same thing: it is
        # "no ordering preference at all", which has to be expressible or
        # turning the tilt off would be impossible from the file.
        raw_rules = d.get("rank_adjustments")
        if raw_rules is None:
            rules = _DEFAULT_RANK_ADJUSTMENTS
        elif isinstance(raw_rules, (list, tuple)):
            rules = tuple(RankRule.from_dict(r) for r in raw_rules)
        else:
            raise PolicyError(
                f"scoring.rank_adjustments must be a list of rule objects, "
                f"got {raw_rules!r}"
            )
        return cls(
            weights=Weights(
                skills=_num(w.get("skills"), 0.6),
                experience=_num(w.get("experience"), 0.4),
            ),
            bonuses=Bonuses(
                location_match=_num(b.get("location_match"), 5),
                remote=_num(b.get("remote"), 5),
                hybrid=_num(b.get("hybrid"), 3),
                office=_num(b.get("office"), 0),
                salary_meets=_num(b.get("salary_meets"), 5),
                salary_near=_num(b.get("salary_near"), 3),
                agent_eligible=_num(b.get("agent_eligible"), 5),
                cap=_num(b.get("cap"), 20),
            ),
            experience=ExperiencePolicy(
                under_penalty_per_year=_num(e.get("under_penalty_per_year"), 20),
                over_coefficient=_num(e.get("over_coefficient"), 15),
                over_floor=_num(e.get("over_floor"), 60),
                unknown_default=_num(e.get("unknown_default"), 50),
            ),
            skills=SkillsPolicy(
                unknown_job_skills_default=_num(
                    s.get("unknown_job_skills_default"), 50),
                weights=FrozenMap(s.get("weights") or {}),
                must_have=tuple(s.get("must_have") or ()),
                # A bare string value is NOT coerced into a one-element list:
                # {"trpc.io": "trpc"} is the INVERTED map, and quietly
                # accepting it would mint a canonical nobody declared. It is
                # left as-is so ``validate`` can refuse it by name.
                extra_skills=FrozenMap(
                    {k: (tuple(v)
                         if isinstance(v, (list, tuple, set, frozenset)) else v)
                     for k, v in (s.get("extra_skills") or {}).items()}
                ),
            ),
            salary=SalaryPolicy(
                meets_expectation_ratio=_num(sal.get("meets_expectation_ratio"), 0.8),
                below_market_ratio=_num(sal.get("below_market_ratio"), 0.85),
                above_market_ratio=_num(sal.get("above_market_ratio"), 1.15),
                max_package_ceiling=(
                    None if sal.get("max_package_ceiling") is None
                    else float(sal["max_package_ceiling"])
                ),
            ),
            verdicts=bands,
            reasons=ReasonsPolicy(
                skill_gap_below=_num(r.get("skill_gap_below"), 50),
                experience_below=_num(r.get("experience_below"), 70),
                missing_skills_shown=_num(r.get("missing_skills_shown"), 3),
            ),
            rank_adjustments=rules,
        )


def _num(value, default):
    """``None`` means 'revert to the shipped default' — see design §2.5."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise PolicyError(f"expected a number, got the boolean {value!r}")
    if not isinstance(value, (int, float)):
        raise PolicyError(f"expected a number, got {value!r}")
    return value


# ── Candidate ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PayBand:
    """One denomination's pair of decisions: the bonus target and the floor."""

    expected: Optional[float] = None
    floor: Optional[float] = None

    def to_dict(self) -> dict:
        return {"expected": self.expected, "floor": self.floor}


#: The denominations a server may bind to. A server MUST name the one it
#: speaks; there is no default and no conversion. C4: a single shared scalar
#: silently scored every uplers job +5 (a $150k figure clears a 24-lakh
#: expectation) and every naukri job 0 (a 25-lakh figure never clears a
#: 20,959-dollar one), and both look exactly like "no salary data".
PAY_UNITS: tuple[str, ...] = ("inr_lakhs_per_year", "usd_per_year")

#: Rough INR-per-USD band used only to WARN when the two denominations drift
#: apart. Never used to convert — a score must not depend on a network call
#: or on the day.
FX_WARN_BAND = (60.0, 120.0)


@dataclass(frozen=True)
class CandidatePay:
    inr_lakhs_per_year: PayBand = field(default_factory=PayBand)
    usd_per_year: PayBand = field(default_factory=PayBand)

    def for_unit(self, unit: str) -> PayBand:
        """The band denominated in *unit*. Unknown units raise, never guess."""
        if unit not in PAY_UNITS:
            raise PolicyError(
                f"unknown pay unit {unit!r}; declare one of {PAY_UNITS}. "
                f"Guessing a denomination is the bug this split exists to stop."
            )
        return getattr(self, unit)

    def fx_warning(self) -> Optional[str]:
        """A human-readable warning when the two denominations disagree."""
        a = self.inr_lakhs_per_year.expected
        b = self.usd_per_year.expected
        if a is None or b is None or not a or not b:
            return None
        implied = (float(a) * 100_000.0) / float(b)
        lo, hi = FX_WARN_BAND
        if lo <= implied <= hi:
            return None
        return (
            f"candidate.pay denominations imply {implied:.1f} INR/USD, outside "
            f"the sanity band {lo}-{hi}: inr_lakhs_per_year.expected={a}, "
            f"usd_per_year.expected={b}. One of them is probably in the wrong "
            f"unit. Nothing was converted; each server still uses its own."
        )

    def to_dict(self) -> dict:
        return {
            "inr_lakhs_per_year": self.inr_lakhs_per_year.to_dict(),
            "usd_per_year": self.usd_per_year.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "CandidatePay":
        d = dict(data or {})
        if "expected" in d or "floor" in d or "unit" in d:
            raise PolicyError(
                "candidate.pay is denominated per unit system: use "
                "{'inr_lakhs_per_year': {...}, 'usd_per_year': {...}}. A "
                "single 'expected'/'floor' pair with a 'unit' tag is the "
                "shape that scores every job on one board +5 and every job "
                "on the other 0, silently."
            )
        def band(key):
            raw = dict(d.get(key) or {})
            return PayBand(expected=raw.get("expected"), floor=raw.get("floor"))
        return cls(inr_lakhs_per_year=band("inr_lakhs_per_year"),
                   usd_per_year=band("usd_per_year"))


@dataclass(frozen=True)
class CandidatePolicy:
    """His canonical self-description — NOT his platform profile.

    The real Uplers / Instahyre / Naukri / LinkedIn profiles live on those
    platforms. Servers read them and, where a write tool exists, write them.
    This object never mirrors, syncs or overwrites them; the name is
    ``candidate`` rather than ``profile`` so no future reader has to work out
    which one is meant.
    """

    name: str = ""
    headline: str = ""
    years_experience: Optional[float] = None
    skills: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    work_mode_preference: tuple[str, ...] = ("remote", "hybrid", "office")
    pay: CandidatePay = field(default_factory=CandidatePay)
    notice_period_days: int = 0
    avoid_companies: tuple[str, ...] = ()

    def validate(self) -> None:
        cap = int(HARD_LIMITS["candidate_skills_max"])
        if len(self.skills) > cap:
            raise PolicyError(
                f"candidate.skills has {len(self.skills)} entries, over the "
                f"Python-side maximum of {cap}. SkillMatch.score is "
                f"|matched| / |job_skills|, so a candidate holding every "
                f"canonical skill scores 100 on every job that exists — that "
                f"is a score-inflation lever, not a self-description. This "
                f"bound is not in the config file and cannot be raised from it."
            )
        loc_cap = int(HARD_LIMITS["candidate_locations_max"])
        if len(self.locations) > loc_cap:
            raise PolicyError(
                f"candidate.locations has {len(self.locations)} entries, over "
                f"the maximum of {loc_cap}"
            )
        if self.years_experience is not None:
            if not isinstance(self.years_experience, (int, float)) or \
                    isinstance(self.years_experience, bool):
                raise PolicyError("candidate.years_experience must be a number")
            if not (0 <= self.years_experience <= 50):
                raise PolicyError(
                    f"candidate.years_experience={self.years_experience} "
                    f"outside [0, 50]"
                )
        if sorted(self.work_mode_preference) != ["hybrid", "office", "remote"]:
            raise PolicyError(
                f"candidate.work_mode_preference must be a permutation of "
                f"['remote', 'hybrid', 'office'], got "
                f"{list(self.work_mode_preference)}"
            )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "headline": self.headline,
            "years_experience": self.years_experience,
            "skills": list(self.skills),
            "titles": list(self.titles),
            "locations": list(self.locations),
            "work_mode_preference": list(self.work_mode_preference),
            "pay": self.pay.to_dict(),
            "notice_period_days": self.notice_period_days,
            "avoid_companies": list(self.avoid_companies),
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "CandidatePolicy":
        d = dict(data or {})
        wmp = d.get("work_mode_preference")
        return cls(
            name=d.get("name") or "",
            headline=d.get("headline") or "",
            years_experience=d.get("years_experience"),
            skills=tuple(d.get("skills") or ()),
            titles=tuple(d.get("titles") or ()),
            locations=tuple(d.get("locations") or ()),
            work_mode_preference=tuple(wmp) if wmp else ("remote", "hybrid", "office"),
            pay=CandidatePay.from_dict(d.get("pay")),
            notice_period_days=(
                0 if d.get("notice_period_days") is None
                else d["notice_period_days"]
            ),
            avoid_companies=tuple(d.get("avoid_companies") or ()),
        )


# ── The whole policy ───────────────────────────────────────────────────────

CONFIG_VERSION = 1


@dataclass(frozen=True)
class Policy:
    """``candidate`` + ``scoring`` + ``servers``, plus the CAS revision.

    ``servers`` is a plain nested mapping: jobcore has no reader for any of it
    and inventing typed objects for another repo's settings would be surface
    area with no payoff. It is still fully schema-governed — every key in it
    is declared in :data:`SCHEMA` with a tier and a reader.
    """

    candidate: CandidatePolicy = field(default_factory=CandidatePolicy)
    scoring: ScoringPolicy = field(default_factory=ScoringPolicy)
    servers: FrozenMap = field(default_factory=FrozenMap)
    revision: int = 0
    config_version: int = CONFIG_VERSION

    def validate(self) -> None:
        self.candidate.validate()
        self.scoring.validate()

    def server(self, name: str) -> dict:
        """This server's section, defaults filled in."""
        return dict(self.servers.get(name) or {})

    def to_dict(self) -> dict:
        return {
            "config_version": self.config_version,
            "revision": self.revision,
            "candidate": self.candidate.to_dict(),
            "scoring": self.scoring.to_dict(),
            "servers": _plain(self.servers),
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "Policy":
        d = dict(data or {})
        servers = _merge_defaults(schema_defaults("servers"), d.get("servers") or {})
        return cls(
            candidate=CandidatePolicy.from_dict(d.get("candidate")),
            scoring=ScoringPolicy.from_dict(d.get("scoring")),
            servers=FrozenMap(servers),
            revision=int(d.get("revision") or 0),
            config_version=int(d.get("config_version") or CONFIG_VERSION),
        )

    def with_revision(self, revision: int) -> "Policy":
        return replace(self, revision=revision)

    # ── stamping ───────────────────────────────────────────────────────────

    def fingerprint(self) -> dict:
        """Exactly the inputs that can move a number: arithmetic AND candidate.

        Name, headline, titles, notice period and every display cap are
        excluded, so the hash does not churn on a change that cannot affect a
        score.

        TWO HASHES, TWO QUESTIONS -- read this before comparing any stamp.
        The system prints two 12-hex fingerprints and they are NOT
        interchangeable. They were briefly both called ``policy_hash``, which
        made a stamp comparison silently wrong in both directions; the names
        below are now the contract, and ``tests/test_stamp_identity.py`` is
        the guard.

          :attr:`scoring_hash` -- covers ``scoring`` only. "Was the same
              ARITHMETIC applied?"  This is what goes on a RESULT. A result
              already carries its own inputs; what it cannot otherwise say is
              which weights, bonuses, caps and verdict bands turned those
              inputs into that number. It is deliberately blind to the
              candidate, because ``FitScore`` receives ``profile_skills`` as a
              call argument and on uplers and instahyre those come from the
              LIVE platform profile rather than from this file -- a
              candidate-covering stamp on such a result would be asserting
              something the result cannot vouch for.

          :attr:`policy_hash` -- covers ``scoring`` AND ``candidate``, i.e.
              this method. "Was the same POLICY in effect?"  This is what goes
              on a CONFIG readout, into the ledger, and into
              :func:`requires_approval_cycle`, whose whole purpose is to catch
              an inflated ``candidate.skills`` -- a change that moves no
              arithmetic at all and that a scoring-only hash is right to
              ignore and would therefore miss.

        A config readout prints BOTH. That is what lets a stored score be
        matched back to the configuration that produced it: the result's
        ``scoring_hash`` against the readout's ``scoring_hash``. Before the
        split there was no such bridge, and the shared name was standing in
        for one.
        """
        return {
            "scoring": self.scoring.to_dict(),
            "candidate": {
                "skills": sorted(self.candidate.skills),
                "years_experience": self.candidate.years_experience,
                "locations": sorted(self.candidate.locations),
                "work_mode_preference": list(self.candidate.work_mode_preference),
                "pay": self.candidate.pay.to_dict(),
            },
        }

    @property
    def policy_hash(self) -> str:
        """Short hash of scoring AND candidate. See :meth:`fingerprint`."""
        return fingerprint_hash(self.fingerprint())

    @property
    def scoring_hash(self) -> str:
        """Short hash of the arithmetic alone. See :meth:`fingerprint`.

        Equal to the ``scoring_hash`` stamped on every result scored under
        this policy -- that equality is the bridge, and it is pinned by
        ``tests/test_stamp_identity.py``.
        """
        return self.scoring.scoring_hash


def _merge_defaults(base: dict, override: Mapping) -> dict:
    """Deep merge *override* onto *base*. Dicts merge, everything else wins."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _merge_defaults(dict(out[k]), v)
        else:
            out[k] = _plain(v)
    return out


def canonical_json(obj) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=_plain)


def fingerprint_hash(fingerprint: Mapping) -> str:
    """First 12 hex of sha256 over the canonical JSON of *fingerprint*."""
    payload = canonical_json(fingerprint).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def requires_approval_cycle(current_hash: str, last_seen_hash: Optional[str]) -> bool:
    """Must the next agent cycle run in approval mode regardless of its mode?

    Yes whenever the POLICY fingerprint changed since the last cycle. This is
    the guard against the two Tier-A levers that reach the apply selector
    without touching the agent block — inflating ``candidate.skills`` and
    reshaping ``scoring`` — neither of which can be Tier C because they are
    the operator's headline feature. One condition, and "policy was quietly
    widened" becomes "he sees the list".

    FEED THIS :attr:`Policy.policy_hash`, NEVER :attr:`Policy.scoring_hash`.
    Both are 12 hex characters and both compare cleanly, so passing the wrong
    one fails silently and open: ``candidate.skills`` moves no arithmetic, so
    a scoring-only hash does not change, so the widened cycle runs in auto and
    he never sees the list. That is the exact hole this function exists to
    close. Until 2026-08-21 both hashes were called ``policy_hash`` and this
    docstring called the full one "the scoring fingerprint" — the naming was
    inverted at both ends. See :meth:`Policy.fingerprint`.
    """
    if last_seen_hash is None:
        return True
    return current_hash != last_seen_hash


DEFAULT_SCORING_POLICY = ScoringPolicy()
DEFAULT_CANDIDATE = CandidatePolicy()
DEFAULT_POLICY = Policy(
    candidate=DEFAULT_CANDIDATE,
    scoring=DEFAULT_SCORING_POLICY,
    servers=FrozenMap(schema_defaults("servers")),
)
