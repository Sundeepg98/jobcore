"""jobcore — the platform-agnostic job/profile scoring engine.

Everything here is true on every job board that exists: what "react" is a
name for, how a 3-5 year requirement scores against a 7-year candidate, and
what a salary string means once you know how many rupees make a lakh.

Nothing here knows about a specific board. There is no HTTP, no browser, no
database, no config module import — a consumer supplies its own units and gets
typed values back.

Quick start::

    from jobcore import compute_fit_score, parse_skills

    result = compute_fit_score(
        job_skills=parse_skills("React, Node.js, AWS"),
        profile_skills=parse_skills(["reactjs", "nodejs", "typescript"]),
        job_exp_str="3-5 years",
        profile_exp="4 years 0 months",
    )
    result["overall_score"]   # -> int, 0..100

Different salary units::

    from jobcore import ScoringEngine, SalaryConfig

    engine = ScoringEngine(salary_config=SalaryConfig(lakhs_multiplier=100_000))
    engine.compute_fit_score(...)
"""

from .fit import BonusScore, ExperienceScore, FitScore, SkillMatch
from .salary import DEFAULT_SALARY_CONFIG, Salary, SalaryConfig
from .scoring import (
    DEFAULT_ENGINE,
    ScoringEngine,
    compute_fit_score,
    normalize_skill,
    parse_skills,
    score_location,
    score_salary,
    score_work_mode,
)
from .skills import DEFAULT_TAXONOMY, SKILL_ALIASES, SkillTaxonomy

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # skills
    "SKILL_ALIASES",
    "SkillTaxonomy",
    "DEFAULT_TAXONOMY",
    # salary
    "Salary",
    "SalaryConfig",
    "DEFAULT_SALARY_CONFIG",
    # fit
    "SkillMatch",
    "ExperienceScore",
    "BonusScore",
    "FitScore",
    # scoring
    "ScoringEngine",
    "DEFAULT_ENGINE",
    "normalize_skill",
    "parse_skills",
    "score_location",
    "score_work_mode",
    "score_salary",
    "compute_fit_score",
]
