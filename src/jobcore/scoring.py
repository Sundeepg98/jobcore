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
from typing import Optional

from .fit import BonusScore, ExperienceScore, FitScore, SkillMatch
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
]

# Words in a job's location field that mean "location is not a constraint".
REMOTE_WORDS = ("remote", "wfh", "work from home", "anywhere")


class ScoringEngine:
    """Job/profile scoring bound to one taxonomy and one salary convention.

    Args:
        taxonomy: Skill normaliser. Defaults to the shared 88-skill taxonomy.
        salary_cls: Salary value type. Pass a :class:`~jobcore.salary.Salary`
            subclass (or use *salary_config*) to bind different units.
        salary_config: Convenience — build the Salary type from a
            :class:`~jobcore.salary.SalaryConfig` instead of subclassing.
            Ignored when *salary_cls* is given.

    Raises:
        TypeError: if *salary_cls* is not a Salary subclass. A silently wrong
            salary type would score every job 0 on the salary bonus and look
            like "no salary data", which is exactly the class of bug that must
            never be quiet.
    """

    def __init__(
        self,
        taxonomy: SkillTaxonomy | None = None,
        salary_cls: type[Salary] | None = None,
        salary_config: SalaryConfig | None = None,
    ):
        self.taxonomy = taxonomy or DEFAULT_TAXONOMY

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
        self, job_location: Optional[str], profile_location: Optional[str]
    ) -> int:
        """Score location match. Returns 0 or 5 bonus points."""
        if not job_location or not profile_location:
            return 0
        jl = job_location.lower().strip()
        pl = profile_location.lower().strip()
        # Exact city match (substring to handle "Bangalore/Bengaluru" vs "Bangalore")
        if pl in jl or jl in pl:
            return 5
        # Remote is universally acceptable
        if any(w in jl for w in REMOTE_WORDS):
            return 5
        return 0

    def score_work_mode(self, job_work_mode: Optional[str]) -> int:
        """Score work mode. Remote/WFH gets a bonus. Returns 0-5 bonus points."""
        if not job_work_mode:
            return 0
        wm = job_work_mode.lower().strip()
        if wm in ("wfh", "remote", "work from home"):
            return 5
        if wm == "hybrid":
            return 3
        return 0  # Office — no penalty, just no bonus

    def score_salary(self, job_salary: Optional[str], profile_expected_ctc) -> int:
        """Score salary fit. Returns 0-5 bonus points.

        Only scores when both job salary and profile expected CTC are available.
        Accepts profile_expected_ctc as float or string (e.g., "15.0 Lacs").
        """
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
        return salary.compare_to_ctc(profile_expected_ctc)

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
    ) -> FitScore:
        """Compute the :class:`~jobcore.fit.FitScore` aggregate.

        Use this when you want the typed object; use
        :meth:`compute_fit_score` for the flat dict a tool result wants.
        """
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
            score_location_fn=self.score_location,
            score_work_mode_fn=self.score_work_mode,
            score_salary_fn=self.score_salary,
        )

    def compute_fit_score(self, *args, **kwargs) -> dict:
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

        Returns:
            {overall_score, skill_match, experience_match, recommendation,
             reasons, and bonuses when any enrichment field was supplied}
        """
        return self.fit_score(*args, **kwargs).to_dict()


DEFAULT_ENGINE = ScoringEngine()


# ── Flat function API, bound to DEFAULT_ENGINE ──────────────────────────────

def normalize_skill(skill: str) -> str:
    """Normalize a skill string to its canonical form via alias lookup."""
    return DEFAULT_ENGINE.normalize_skill(skill)


def parse_skills(raw) -> set:
    """Normalize skills from any format (string, list, set) to a canonical set."""
    return DEFAULT_ENGINE.parse_skills(raw)


def score_location(job_location: Optional[str], profile_location: Optional[str]) -> int:
    """Score location match. Returns 0 or 5 bonus points."""
    return DEFAULT_ENGINE.score_location(job_location, profile_location)


def score_work_mode(job_work_mode: Optional[str]) -> int:
    """Score work mode. Remote/WFH gets a bonus. Returns 0-5 bonus points."""
    return DEFAULT_ENGINE.score_work_mode(job_work_mode)


def score_salary(job_salary: Optional[str], profile_expected_ctc) -> int:
    """Score salary fit. Returns 0-5 bonus points."""
    return DEFAULT_ENGINE.score_salary(job_salary, profile_expected_ctc)


def compute_fit_score(*args, **kwargs) -> dict:
    """Compute fit score between a job and a candidate profile (flat dict).

    See :meth:`ScoringEngine.compute_fit_score`.
    """
    return DEFAULT_ENGINE.compute_fit_score(*args, **kwargs)
