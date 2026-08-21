"""FitScore aggregate — job/profile match scoring.

Four frozen dataclasses, no platform coupling:
  SkillMatch      — skill overlap calculation
  ExperienceScore — experience fit with sqrt over-qualification penalty
  BonusScore      — additive bonuses (location, work_mode, salary, agent)
  FitScore        — aggregate combining all three into overall score + recommendation

Extracted verbatim from ``naukri_server/domain/fit_score.py`` at commit 0021d82;
that module is now a re-export shim over this one. The arithmetic is unchanged
on purpose — a scoring change and an extraction must never ride together, or
neither can be verified.

2026-08-21: the numbers became POLICY rather than literals. ``policy=`` is a
parameter of every ``compute`` here and a field on the frozen aggregate — not
only a constructor argument on :class:`~jobcore.scoring.ScoringEngine`. That
matters: three of naukri's four scoring call sites build ``FitScore`` directly
and never touch an engine, so an engine-only seam would have produced a
split-brain in which ``naukri_daily_brief`` honoured his weights and
``naukri_auto_hunt`` — the agent's own scorer — did not. Every parameter
defaults to :data:`~jobcore.policy.DEFAULT_SCORING_POLICY`, whose values are
exactly the literals this file used to carry, so every existing call site
keeps compiling and keeps its numbers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional

from .policy import (
    DEFAULT_SCORING_POLICY,
    ExperiencePolicy,
    ScoringPolicy,
    SkillsPolicy,
)


# ── Skill Match ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SkillMatch:
    """Immutable value object for skill overlap between job and profile.

    score property: percentage of job skills matched (50 default when no job skills).
    """
    matched: frozenset
    missing: frozenset
    job_skills: frozenset
    policy: SkillsPolicy = field(default=DEFAULT_SCORING_POLICY.skills)

    @classmethod
    def compute(cls, job_skills: set, profile_skills: set,
                policy: Optional[SkillsPolicy] = None) -> "SkillMatch":
        matched = frozenset(job_skills & profile_skills)
        missing = frozenset(job_skills - profile_skills)
        return cls(matched=matched, missing=missing, job_skills=frozenset(job_skills),
                   policy=policy or DEFAULT_SCORING_POLICY.skills)

    @property
    def score(self) -> float:
        """Coverage of the job's skills, weighted when weights are configured.

        With no configured weights every skill weighs 1.0 and this reduces to
        ``len(matched) / len(job_skills) * 100`` — today's arithmetic exactly.

        Read the weighted form before using it: it is
        ``sum(w[matched]) / sum(w[job])``, so down-weighting a skill he does
        NOT have shrinks the denominator only and RAISES the score of jobs
        asking for it. Weights demote a stack by lowering the weight of the
        skills he HAS in it; they are not a rank tilt and do not behave like
        one. ``test_policy_effects.py`` pins both directions.
        """
        if not self.job_skills:
            return float(self.policy.unknown_job_skills_default)
        if not self.policy.weights:
            return len(self.matched) / len(self.job_skills) * 100
        denominator = sum(self.policy.weight_of(s) for s in self.job_skills)
        if denominator <= 0:
            return float(self.policy.unknown_job_skills_default)
        numerator = sum(self.policy.weight_of(s) for s in self.matched)
        return numerator / denominator * 100


# ── Experience Score ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExperienceScore:
    """Immutable value object for experience fit.

    Uses sqrt penalty for over-qualification (floor 60, not linear cliff).
    Under-qualification: linear 20-point penalty per missing year.
    """
    score: float
    profile_years: float
    min_required: Optional[float]
    max_required: Optional[float]

    @classmethod
    def compute(
        cls,
        job_exp_str: str,
        profile_exp,
        experience_min: Optional[int] = None,
        experience_max: Optional[int] = None,
        policy: Optional[ExperiencePolicy] = None,
    ) -> "ExperienceScore":
        """Compute experience score from job requirements and profile experience.

        Args:
            job_exp_str: Experience string like "3-5 years"
            profile_exp: Profile experience (string like "5 years 0 months" or numeric)
            experience_min: Numeric min experience (avoids regex round-trip)
            experience_max: Numeric max experience (avoids regex round-trip)
            policy: penalty shape. Defaults reproduce the shipped 20/15/60/50.
        """
        pol = policy or DEFAULT_SCORING_POLICY.experience
        exp_score = float(pol.unknown_default)  # Default if can't determine
        p_exp = 0.0
        min_exp = max_exp = None

        if profile_exp is not None and (job_exp_str or experience_min is not None):
            p_exp_match = re.findall(r'(\d+)', str(profile_exp))
            if len(p_exp_match) >= 2:
                p_exp = float(p_exp_match[0]) + float(p_exp_match[1]) / 12.0
            elif p_exp_match:
                p_exp = float(p_exp_match[0])
            else:
                p_exp = 0.0

            # Prefer numeric fields, fall back to regex
            if experience_min is not None and experience_max is not None:
                min_exp, max_exp = float(experience_min), float(experience_max)
            elif job_exp_str:
                exp_nums = re.findall(r'(\d+)', str(job_exp_str))
                if len(exp_nums) >= 2:
                    min_exp, max_exp = float(exp_nums[0]), float(exp_nums[1])
                else:
                    min_exp = max_exp = None
            else:
                min_exp = max_exp = None

            if min_exp is not None and max_exp is not None:
                if min_exp <= p_exp <= max_exp:
                    exp_score = 100
                elif p_exp < min_exp:
                    exp_score = max(
                        0,
                        100 - (min_exp - p_exp) * pol.under_penalty_per_year,
                    )
                else:
                    exp_score = max(
                        pol.over_floor,
                        100 - math.sqrt(max(0, p_exp - max_exp)) * pol.over_coefficient,
                    )

        return cls(
            score=exp_score,
            profile_years=p_exp,
            min_required=min_exp,
            max_required=max_exp,
        )


# ── Bonus Score ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BonusScore:
    """Immutable value object for additive bonus points.

    Up to +20 total: location (+5), work_mode (+5), salary (+5), agent (+5).
    """
    location: int
    work_mode: int
    salary: int
    agent_eligible: int

    @property
    def total(self) -> int:
        return self.location + self.work_mode + self.salary + self.agent_eligible

    @classmethod
    def compute(
        cls,
        job_location: Optional[str],
        profile_location: Optional[str],
        job_work_mode: Optional[str],
        job_salary: Optional[str],
        profile_expected_ctc,
        is_agent_eligible,
        score_location_fn,
        score_work_mode_fn,
        score_salary_fn,
        policy: Optional[ScoringPolicy] = None,
    ) -> "BonusScore":
        """Compute all bonus scores using provided scoring functions.

        Scoring functions are injected so the aggregate stays free of any
        opinion about salary units or geography.
        """
        pol = policy or DEFAULT_SCORING_POLICY
        return cls(
            location=score_location_fn(job_location, profile_location),
            work_mode=score_work_mode_fn(job_work_mode),
            salary=score_salary_fn(job_salary, profile_expected_ctc),
            agent_eligible=pol.bonuses.agent_eligible if is_agent_eligible else 0,
        )


# ── FitScore Aggregate ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class FitScore:
    """Aggregate root — combines skill, experience, and bonus scores.

    Produces overall_score (capped at 100), recommendation string,
    and explanatory reasons list.
    """
    skill_match: SkillMatch
    experience: ExperienceScore
    bonuses: BonusScore
    # Raw inputs preserved for to_dict() output fidelity
    _profile_exp: object  # raw profile_exp value
    _job_exp_str: str  # raw job_exp_str value
    _has_enrichment: bool  # whether bonus breakdown should appear
    #: The numbers that produced this result. Carried on the aggregate so a
    #: score can be explained after the fact and so two results can be told
    #: apart when they are not comparable.
    policy: ScoringPolicy = field(default=DEFAULT_SCORING_POLICY)

    @property
    def bonus_total(self) -> int:
        """Bonus points actually added, after the configured bonus cap."""
        return min(self.policy.bonuses.cap, self.bonuses.total)

    @property
    def overall_score(self) -> int:
        w = self.policy.weights
        base_score = (
            self.skill_match.score * w.skills + self.experience.score * w.experience
        )
        return min(100, round(base_score + self.bonus_total))

    @property
    def recommendation(self) -> str:
        return self.policy.verdict_for(self.overall_score).label

    @property
    def reasons(self) -> list:
        reasons = []
        r = self.policy.reasons
        if self.skill_match.score < r.skill_gap_below:
            missing_sample = sorted(self.skill_match.missing)[:r.missing_skills_shown]
            reasons.append(f"Skill gap: missing {', '.join(missing_sample)}")
        if (
            self.experience.score < r.experience_below
            and self.experience.min_required is not None
            and self.experience.max_required is not None
        ):
            p = self.experience.profile_years
            mn = self.experience.min_required
            mx = self.experience.max_required
            if p < mn:
                reasons.append(
                    f"Under-experienced: {p:.0f}yr vs {mn:.0f}-{mx:.0f}yr required"
                )
            elif p > mx:
                reasons.append(
                    f"Over-experienced: {p:.0f}yr vs {mn:.0f}-{mx:.0f}yr range"
                )
        if self.bonuses.total == 0:
            reasons.append("No location/salary/work-mode bonuses")
        return reasons

    @classmethod
    def compute(
        cls,
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
        score_location_fn=None,
        score_work_mode_fn=None,
        score_salary_fn=None,
        policy: Optional[ScoringPolicy] = None,
    ) -> "FitScore":
        """Factory method mirroring compute_fit_score() signature.

        score_location_fn, score_work_mode_fn, score_salary_fn are injected by
        the caller (see :mod:`jobcore.scoring`); omitting one scores it 0.

        *policy* defaults to the shipped one, so a call site that has not been
        migrated keeps today's numbers exactly. Pass the same policy you pass
        to the bonus functions — a mismatch is the split-brain this parameter
        exists to prevent, and ``ScoringEngine.fit_score`` binds both from one
        object so a caller cannot get it wrong.
        """
        pol = policy or DEFAULT_SCORING_POLICY
        skill = SkillMatch.compute(job_skills, profile_skills, policy=pol.skills)
        exp = ExperienceScore.compute(
            job_exp_str, profile_exp, experience_min, experience_max,
            policy=pol.experience,
        )

        # Default no-op scoring functions when none provided
        _loc_fn = score_location_fn or (lambda jl, pl: 0)
        _wm_fn = score_work_mode_fn or (lambda wm: 0)
        _sal_fn = score_salary_fn or (lambda js, ctc: 0)

        bonus = BonusScore.compute(
            job_location, profile_location,
            job_work_mode,
            job_salary, profile_expected_ctc,
            is_agent_eligible,
            _loc_fn, _wm_fn, _sal_fn,
            policy=pol,
        )

        has_enrichment = (
            job_location is not None
            or job_work_mode is not None
            or job_salary is not None
            or is_agent_eligible is not None
        )

        return cls(
            skill_match=skill,
            experience=exp,
            bonuses=bonus,
            _profile_exp=profile_exp,
            _job_exp_str=job_exp_str,
            _has_enrichment=has_enrichment,
            policy=pol,
        )

    # ── Explainability ──────────────────────────────────────────────────────

    @property
    def scoring_hash(self) -> str:
        """Short hash of the ARITHMETIC that produced this number.

        Two results with the same hash were scored by the same weights,
        bonuses, caps and verdict bands, and are directly comparable. Two with
        different hashes are not — and now you can tell, which is strictly
        more than scores carry today.

        NOT ``policy_hash``, and the distinction is not cosmetic. This hash
        covers ``scoring`` only; :attr:`jobcore.policy.Policy.policy_hash`
        covers ``scoring`` AND ``candidate``, and the two produce different
        values for the same file. Both were called ``policy_hash`` until
        2026-08-21, so comparing a stored score's stamp against a config
        readout's reported a difference that did not exist. The bridge is now
        explicit: a config readout prints ``scoring_hash`` beside its
        ``policy_hash``, and THAT is the field this one compares against.

        Delegates to the one implementation on ``ScoringPolicy`` — a second
        hand-written copy of the payload is how the ambiguity got in.
        """
        return self.policy.scoring_hash

    def explain(self) -> dict:
        """The arithmetic actually used — not the score, the working."""
        w = self.policy.weights
        skills_component = self.skill_match.score
        exp_component = self.experience.score
        combined = skills_component * w.skills + exp_component * w.experience
        band = self.policy.verdict_for(self.overall_score)
        return {
            "weights": {"skills": w.skills, "experience": w.experience},
            "base": {
                "skills": round(skills_component, 1),
                "experience": round(exp_component, 1),
                "combined": round(combined, 1),
            },
            "bonuses": {
                "location": self.bonuses.location,
                "work_mode": self.bonuses.work_mode,
                "salary": self.bonuses.salary,
                "agent_eligible": self.bonuses.agent_eligible,
                "raw_total": self.bonuses.total,
                "cap": self.policy.bonuses.cap,
                "total": self.bonus_total,
            },
            "bonus_cap_applied": self.bonuses.total > self.policy.bonuses.cap,
            "score_ceiling_applied": round(combined + self.bonus_total) > 100,
            "overall_score": self.overall_score,
            "verdict_band": {"min": band.min, "label": band.label},
            "skill_weighting": (
                "flat" if not self.policy.skills.weights else "weighted"
            ),
            "scoring_hash": self.scoring_hash,
        }

    def to_dict(self, *, explain: bool = False,
                stamp: Optional[bool] = None,
                policy_rev: Optional[int] = None) -> dict:
        """Produce the flat dict shape the MCP tools return.

        Keys: overall_score, skill_match, experience_match, recommendation,
              reasons, and conditionally bonuses.

        Args:
            explain: add the ``explain`` block — the arithmetic, not the score.
            stamp: add ``scoring_hash`` (and ``policy_rev`` when supplied).
                ``None`` means *auto*: stamp exactly when the policy is not the
                shipped default, so a default-policy result stays byte-for-byte
                what it is today and the 179 golden parity cases still pass.
            policy_rev: the loader's content-derived revision, when the caller
                has one. The file's own ``revision`` integer is a
                compare-and-swap token and is deliberately not this number.

        The stamp key is ``scoring_hash``, NOT ``policy_hash``. A result can
        only vouch for the arithmetic; the candidate half of the policy is a
        call argument here and on two of the three servers it comes from the
        live platform profile, not from the config file. See
        :attr:`scoring_hash` and :meth:`jobcore.policy.Policy.fingerprint`.
        """
        result = {
            "overall_score": self.overall_score,
            "skill_match": {
                "score": round(self.skill_match.score),
                "matched": sorted(self.skill_match.matched),
                "missing": sorted(self.skill_match.missing),
            },
            "experience_match": {
                "score": round(self.experience.score),
                "your_experience": self._profile_exp,
                "required": self._job_exp_str,
            },
            "recommendation": self.recommendation,
            "reasons": self.reasons,
        }

        if self._has_enrichment:
            result["bonuses"] = {
                "location": self.bonuses.location,
                "work_mode": self.bonuses.work_mode,
                "salary": self.bonuses.salary,
                "agent_eligible": self.bonuses.agent_eligible,
                "total": self.bonuses.total,
            }

        if stamp is None:
            stamp = self.policy != DEFAULT_SCORING_POLICY
        if stamp:
            result["scoring_hash"] = self.scoring_hash
            if policy_rev is not None:
                result["policy_rev"] = policy_rev
        if explain:
            result["explain"] = self.explain()

        return result
