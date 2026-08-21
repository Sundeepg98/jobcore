"""The scoring engine — the one entry point most callers need.

``ScoringEngine`` binds a :class:`~jobcore.skills.SkillTaxonomy` and a Salary
type to the bonus helpers and :class:`~jobcore.fit.FitScore`, so a server gets
job/profile matching without importing anything platform-specific.

Module-level ``normalize_skill`` / ``parse_skills`` / ``compute_fit_score``
delegate to :data:`DEFAULT_ENGINE` for callers that want the flat function API.

Extracted from ``naukri_server/scoring.py`` at commit 0021d82; that module is
now a shim over an engine bound to Naukri's salary configuration.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Optional

from .fit import BonusScore, ExperienceScore, FitScore, SkillMatch
from .policy import (
    DEFAULT_CANDIDATE,
    DEFAULT_SCORING_POLICY,
    CandidatePolicy,
    ScoringPolicy,
)
from .salary import DEFAULT_SALARY_CONFIG, Salary, SalaryConfig
from .skills import DEFAULT_TAXONOMY, SKILL_ALIASES, SkillTaxonomy

__all__ = [
    "ScoringEngine",
    "DEFAULT_ENGINE",
    "normalize_skill",
    "parse_skills",
    "compute_fit_score",
    "score_location",
    "score_work_mode",
    "score_salary",
    "REMOTE_WORDS",
    "WORK_MODE_CATEGORIES",
]

# Words in a job's location field that mean "location is not a constraint".
REMOTE_WORDS = ("remote", "wfh", "work from home", "anywhere")

# The three work-mode categories a job can fall into, and the strings that mean
# each. Mechanism, not preference: which category is worth what is policy.
WORK_MODE_CATEGORIES = {
    "remote": ("wfh", "remote", "work from home"),
    "hybrid": ("hybrid",),
}


class ScoringEngine:
    """Job/profile scoring bound to one taxonomy, salary convention and policy.

    Args:
        taxonomy: Skill normaliser. Defaults to the shared 88-skill taxonomy.
        salary_cls: Salary value type. Pass a :class:`~jobcore.salary.Salary`
            subclass (or use *salary_config*) to bind different units.
        salary_config: Convenience — build the Salary type from a
            :class:`~jobcore.salary.SalaryConfig` instead of subclassing.
            Ignored when *salary_cls* is given.
        policy: the :class:`~jobcore.policy.ScoringPolicy` this engine scores
            under. Defaults to the shipped one, whose values are exactly the
            literals this package used to carry — so an engine constructed the
            way every caller constructs one today behaves identically.
        candidate: supplies ``work_mode_preference``; nothing else on the
            scoring path reads it.

    Raises:
        TypeError: if *salary_cls* is not a Salary subclass, or *policy* is
            not a ScoringPolicy. A silently wrong salary type would score
            every job 0 on the salary bonus and look like "no salary data",
            which is exactly the class of bug that must never be quiet — and
            a silently ignored policy is the same bug one level up.
    """

    def __init__(
        self,
        taxonomy: SkillTaxonomy | None = None,
        salary_cls: type[Salary] | None = None,
        salary_config: SalaryConfig | None = None,
        policy: ScoringPolicy | None = None,
        candidate: CandidatePolicy | None = None,
    ):
        self.taxonomy = taxonomy or DEFAULT_TAXONOMY

        if policy is not None and not isinstance(policy, ScoringPolicy):
            raise TypeError(
                f"policy must be a jobcore.policy.ScoringPolicy, got {policy!r}. "
                f"A policy that is accepted and then ignored is worse than one "
                f"that is refused."
            )
        self.policy = policy or DEFAULT_SCORING_POLICY
        if candidate is not None and not isinstance(candidate, CandidatePolicy):
            raise TypeError(
                f"candidate must be a jobcore.policy.CandidatePolicy, got "
                f"{candidate!r}"
            )
        self.candidate = candidate or DEFAULT_CANDIDATE

        if salary_cls is not None:
            if not (isinstance(salary_cls, type) and issubclass(salary_cls, Salary)):
                raise TypeError(
                    f"salary_cls must be a jobcore.salary.Salary subclass, "
                    f"got {salary_cls!r}"
                )
            self.salary_cls = salary_cls
        elif salary_config is not None:
            self.salary_cls = type(
                "ConfiguredSalary", (Salary,), {"CONFIG": salary_config}
            )
        else:
            self.salary_cls = Salary

    # ── Skills ──────────────────────────────────────────────────────────────

    def normalize_skill(self, skill: str) -> str:
        """Normalize a skill string to its canonical form via alias lookup."""
        return self.taxonomy.normalize(skill)

    def parse_skills(self, raw) -> set:
        """Normalize skills from any format (string, list, set) to a canonical set.

        Handles comma-separated strings, lists, tuples, and sets. All skills go
        through the taxonomy (e.g., "JS" -> "javascript").
        """
        return set(self.taxonomy.parse_set(raw))

    # ── Bonus scoring helpers ───────────────────────────────────────────────

    def score_location(
        self,
        job_location: Optional[str],
        profile_location=None,
        policy: ScoringPolicy | None = None,
    ) -> int:
        """Score location match. Returns 0 or the configured location bonus.

        *profile_location* accepts a single string — preserving today's exact
        substring semantics, which is what keeps the golden cases untouched —
        or a sequence of acceptable locations, so "Bangalore or Hyderabad or
        remote" is expressible. A sequence scores the best of its members.
        """
        pol = policy or self.policy
        if not job_location:
            return 0
        if profile_location is None:
            candidates: Sequence = self.candidate.locations
        elif isinstance(profile_location, str):
            candidates = (profile_location,)
        elif isinstance(profile_location, Sequence):
            candidates = tuple(profile_location)
        else:
            return 0
        if not candidates:
            return 0
        jl = job_location.lower().strip()
        for loc in candidates:
            if not loc or not isinstance(loc, str):
                continue
            pl = loc.lower().strip()
            if not pl:
                continue
            # Exact city match (substring handles "Bangalore/Bengaluru")
            if pl in jl or jl in pl:
                return pol.bonuses.location_match
        # Remote is universally acceptable
        if any(w in jl for w in REMOTE_WORDS):
            return pol.bonuses.location_match
        return 0

    def work_mode_category(self, job_work_mode: Optional[str]) -> Optional[str]:
        """``"remote"`` / ``"hybrid"`` / ``"office"``, or None when unstated."""
        if not job_work_mode:
            return None
        wm = job_work_mode.lower().strip()
        for category, words in WORK_MODE_CATEGORIES.items():
            if wm in words:
                return category
        return "office"

    def score_work_mode(self, job_work_mode: Optional[str],
                        policy: ScoringPolicy | None = None) -> int:
        """Score work mode against his ordered preference.

        The bonus values are the three category bonuses sorted best-first; the
        preference list says which category gets which. With the shipped table
        (remote 5 / hybrid 3 / office 0) and the shipped order
        (remote, hybrid, office) this is byte-for-byte the old
        ``5 / 3 / 0`` ladder. Reordering the preference reassigns the same
        three values, so a hybrid-first candidate scores hybrid 5.
        """
        pol = policy or self.policy
        category = self.work_mode_category(job_work_mode)
        if category is None:
            return 0
        values = pol.bonuses.work_mode_values()
        preference = tuple(self.candidate.work_mode_preference)
        if category not in preference:
            return 0
        rank = preference.index(category)
        if rank >= len(values):
            return 0
        return values[rank]

    def score_salary(self, job_salary: Optional[str], profile_expected_ctc,
                     policy: ScoringPolicy | None = None) -> int:
        """Score salary fit. Returns 0 or one of the configured salary bonuses.

        Only scores when both job salary and profile expected CTC are available.
        Accepts profile_expected_ctc as float or string (e.g., "15.0 Lacs").
        *profile_expected_ctc* must be denominated the way this engine's Salary
        type is — see :meth:`jobcore.salary.Salary.compare_to_ctc`.
        """
        pol = policy or self.policy
        if not job_salary or profile_expected_ctc is None:
            return 0
        # Parse profile CTC to float if string
        if isinstance(profile_expected_ctc, str):
            ctc_nums = re.findall(r'(\d+(?:\.\d+)?)', profile_expected_ctc)
            if not ctc_nums:
                return 0
            profile_expected_ctc = float(ctc_nums[0])
        elif not isinstance(profile_expected_ctc, (int, float)):
            return 0

        salary = self.salary_cls.from_string(job_salary)
        if not salary.is_disclosed:
            return 0
        return salary.compare_to_ctc(profile_expected_ctc, policy=pol)

    # ── Main scoring function ───────────────────────────────────────────────

    def fit_score(
        self,
        job_skills: set,
        profile_skills: set,
        job_exp_str: str,
        profile_exp,
        job_location: Optional[str] = None,
        profile_location: Optional[str] = None,
        job_work_mode: Optional[str] = None,
        job_salary: Optional[str] = None,
        profile_expected_ctc=None,
        experience_min: Optional[int] = None,
        experience_max: Optional[int] = None,
        is_agent_eligible=None,
        policy: ScoringPolicy | None = None,
    ) -> FitScore:
        """Compute the :class:`~jobcore.fit.FitScore` aggregate.

        Use this when you want the typed object; use
        :meth:`compute_fit_score` for the flat dict a tool result wants.

        One policy governs the whole call. The bonus helpers are bound to the
        SAME object the aggregate carries, so there is no way to score the
        base under one policy and the bonuses under another.
        """
        pol = policy or self.policy
        return FitScore.compute(
            job_skills=job_skills,
            profile_skills=profile_skills,
            job_exp_str=job_exp_str,
            profile_exp=profile_exp,
            job_location=job_location,
            profile_location=profile_location,
            job_work_mode=job_work_mode,
            job_salary=job_salary,
            profile_expected_ctc=profile_expected_ctc,
            experience_min=experience_min,
            experience_max=experience_max,
            is_agent_eligible=is_agent_eligible,
            score_location_fn=lambda jl, pl: self.score_location(jl, pl, policy=pol),
            score_work_mode_fn=lambda wm: self.score_work_mode(wm, policy=pol),
            score_salary_fn=lambda js, ctc: self.score_salary(js, ctc, policy=pol),
            policy=pol,
        )

    def compute_fit_score(self, *args, explain: bool = False,
                          stamp: Optional[bool] = None,
                          policy_rev: Optional[int] = None, **kwargs) -> dict:
        """Compute fit score between a job and a candidate profile.

        Base score: 60% skills + 40% experience.
        Additive bonuses: +5 location match, +5 remote/WFH, +5 salary fit,
        +5 agent-eligible. Overall score capped at 100.

        Args:
            job_skills: Set of normalized job skill strings
            profile_skills: Set of normalized profile skill strings
            job_exp_str: Experience string like "3-5 years"
            profile_exp: Profile experience ("5 years 0 months" or numeric)
            job_location: Job city/location string (optional)
            profile_location: User's current location (optional)
            job_work_mode: "WFH", "Hybrid", "Office", etc. (optional)
            job_salary: Salary string like "15-20 LPA" (optional)
            profile_expected_ctc: Expected CTC in LPA (optional)
            experience_min / experience_max: numeric experience bounds, used in
                preference to parsing *job_exp_str* (optional)
            is_agent_eligible: truthy for the +5 agent bonus (optional)
            policy: score under this policy instead of the engine's
            explain: include the arithmetic that produced the number
            stamp / policy_rev: see :meth:`jobcore.fit.FitScore.to_dict`

        Returns:
            {overall_score, skill_match, experience_match, recommendation,
             reasons, and bonuses when any enrichment field was supplied}
        """
        return self.fit_score(*args, **kwargs).to_dict(
            explain=explain, stamp=stamp, policy_rev=policy_rev,
        )


DEFAULT_ENGINE = ScoringEngine()


# ── Flat function API, bound to DEFAULT_ENGINE ──────────────────────────────

def normalize_skill(skill: str) -> str:
    """Normalize a skill string to its canonical form via alias lookup."""
    return DEFAULT_ENGINE.normalize_skill(skill)


def parse_skills(raw) -> set:
    """Normalize skills from any format (string, list, set) to a canonical set."""
    return DEFAULT_ENGINE.parse_skills(raw)


def score_location(job_location: Optional[str], profile_location=None,
                   policy: ScoringPolicy | None = None) -> int:
    """Score location match. Returns 0 or the configured location bonus."""
    return DEFAULT_ENGINE.score_location(job_location, profile_location, policy=policy)


def score_work_mode(job_work_mode: Optional[str],
                    policy: ScoringPolicy | None = None) -> int:
    """Score work mode. Remote/WFH gets a bonus. Returns 0-5 by default."""
    return DEFAULT_ENGINE.score_work_mode(job_work_mode, policy=policy)


def score_salary(job_salary: Optional[str], profile_expected_ctc,
                 policy: ScoringPolicy | None = None) -> int:
    """Score salary fit. Returns 0-5 bonus points by default."""
    return DEFAULT_ENGINE.score_salary(job_salary, profile_expected_ctc, policy=policy)


def compute_fit_score(*args, **kwargs) -> dict:
    """Compute fit score between a job and a candidate profile (flat dict).

    Accepts ``policy=`` so a consumer that never builds an engine — instahyre
    imports this flat function, not :class:`ScoringEngine` — can still score
    under his configured policy. ``DEFAULT_ENGINE`` itself stays exactly what
    it is: a default-everything singleton built at import, which is what
    ``test_independence`` requires.

    See :meth:`ScoringEngine.compute_fit_score`.
    """
    return DEFAULT_ENGINE.compute_fit_score(*args, **kwargs)
