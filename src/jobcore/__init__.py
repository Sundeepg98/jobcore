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

Configurable policy — the weights, bonuses and bands are values, not literals::

    from jobcore import ScoringEngine, ScoringPolicy, Weights
    from jobcore import config                      # the loader; NOT imported here

    loaded = config.current(start=__file__)         # reads the file, or defaults
    engine = ScoringEngine(policy=loaded.scoring, candidate=loaded.candidate)

``jobcore.config`` is deliberately NOT imported by this package: the scoring
path must never read a file, or the same job scores differently on two
machines and this stops being a library. Import it yourself, from the server.

Three submodules follow that same rule and are imported the same way, from the
server rather than from here, each for its own reason:

* :mod:`jobcore.config` — reads a file (above).
* :mod:`jobcore.buildinfo` — shells out to ``git`` to answer "what code is this
  process actually running", which is a question about a deployment, not about
  scoring. ``from jobcore import buildinfo``.
* :mod:`jobcore.paths` — renders a filesystem path into a form that carries no
  machine layout. Pure, but it is presentation, and the scoring path has no
  presentation. ``from jobcore import paths``.
"""

from .fit import BonusScore, ExperienceScore, FitScore, SkillMatch
from .policy import (
    DEFAULT_CANDIDATE,
    DEFAULT_POLICY,
    DEFAULT_SCORING_POLICY,
    HARD_LIMITS,
    SCHEMA,
    Bonuses,
    CandidatePay,
    CandidatePolicy,
    ExperiencePolicy,
    KeySpec,
    PayBand,
    Policy,
    PolicyError,
    ReasonsPolicy,
    SalaryPolicy,
    ScoringPolicy,
    SkillsPolicy,
    Verdict,
    Weights,
    requires_approval_cycle,
    spec_for,
    tier_for,
)
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

__version__ = "0.2.0"

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
    # policy
    "Policy",
    "ScoringPolicy",
    "CandidatePolicy",
    "CandidatePay",
    "PayBand",
    "Weights",
    "Bonuses",
    "ExperiencePolicy",
    "SkillsPolicy",
    "SalaryPolicy",
    "ReasonsPolicy",
    "Verdict",
    "KeySpec",
    "SCHEMA",
    "HARD_LIMITS",
    "PolicyError",
    "DEFAULT_POLICY",
    "DEFAULT_SCORING_POLICY",
    "DEFAULT_CANDIDATE",
    "spec_for",
    "tier_for",
    "requires_approval_cycle",
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
