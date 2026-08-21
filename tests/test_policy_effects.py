"""Does an injected policy actually change the number, and does it change it
the SAME way at every entry point?

Two failure classes are being guarded here, and both have precedent in this
tree:

* **The decoy class** — a seam that reads a value and ignores it. Eleven were
  counted in one sibling repo. So every knob wired here is asserted to move a
  score by the arithmetically predicted amount, not merely to be readable.
* **The split-brain class** — three of naukri's four scoring call sites build
  ``FitScore`` directly and never touch an engine, so an engine-only injection
  point would have left ``naukri_daily_brief`` honouring his weights while
  ``naukri_auto_hunt``, the agent's own scorer, did not. The golden corpus
  cannot see that, because it runs at default policy where the two paths agree
  by construction. So the agreement is asserted under a NON-default policy, and
  the assertion is shown able to fail.
"""

import pytest

from jobcore import (
    DEFAULT_SCORING_POLICY,
    Bonuses,
    CandidatePay,
    CandidatePolicy,
    ExperiencePolicy,
    FitScore,
    PayBand,
    ReasonsPolicy,
    Salary,
    SalaryConfig,
    SalaryPolicy,
    ScoringEngine,
    ScoringPolicy,
    SkillsPolicy,
    Verdict,
    Weights,
    compute_fit_score,
    score_location,
    score_salary,
    score_work_mode,
)
from jobcore.policy import FrozenMap

# One job, one profile, used everywhere so the arithmetic is checkable by hand.
JOB = dict(
    job_skills={"react", "aws"},
    profile_skills={"react"},
    job_exp_str="3-5 years",
    profile_exp="4 years",
)
# skills 50, experience 100.  default: 50*0.6 + 100*0.4 = 70
DEFAULT_SCORE = 70

EIGHTY_TWENTY = ScoringPolicy(weights=Weights(skills=0.8, experience=0.2))
# 50*0.8 + 100*0.2 = 60
EIGHTY_TWENTY_SCORE = 60


class TestANonDefaultPolicyMovesTheScore:
    """The single most important test here. A seam that reads and ignores is
    exactly the decoy class this whole design exists to eliminate."""

    def test_the_default_reproduces_todays_number(self):
        assert compute_fit_score(**JOB)["overall_score"] == DEFAULT_SCORE

    def test_changing_the_weights_moves_it_by_the_predicted_amount(self):
        out = compute_fit_score(**JOB, policy=EIGHTY_TWENTY)
        assert out["overall_score"] == EIGHTY_TWENTY_SCORE
        assert out["overall_score"] - DEFAULT_SCORE == -10

    @pytest.mark.parametrize("skills,experience,expected", [
        (0.6, 0.4, 70),     # 50*.60 + 100*.40 = 70
        (0.8, 0.2, 60),     # 50*.80 + 100*.20 = 60
        (0.5, 0.5, 75),     # 50*.50 + 100*.50 = 75
        (0.2, 0.8, 90),     # 50*.20 + 100*.80 = 90
        (0.75, 0.25, 62),   # 62.5, and round() is banker's — 62, not 63
    ])
    def test_a_grid_of_weights(self, skills, experience, expected):
        pol = ScoringPolicy(weights=Weights(skills=skills, experience=experience))
        assert compute_fit_score(**JOB, policy=pol)["overall_score"] == expected

    def test_the_experience_penalty_shape_is_configurable(self):
        gentle = ScoringPolicy(
            experience=ExperiencePolicy(under_penalty_per_year=5))
        harsh = ScoringPolicy(
            experience=ExperiencePolicy(under_penalty_per_year=40))
        job = dict(job_skills={"react"}, profile_skills={"react"},
                   job_exp_str="8-10 years", profile_exp="4 years")
        # under by 4 years: default 100-4*20 = 20; gentle 80; harsh 0 (floored)
        assert compute_fit_score(**job)["experience_match"]["score"] == 20
        assert compute_fit_score(**job, policy=gentle)[
            "experience_match"]["score"] == 80
        assert compute_fit_score(**job, policy=harsh)[
            "experience_match"]["score"] == 0

    def test_the_verdict_bands_are_configurable_and_are_read(self):
        strict = ScoringPolicy(verdicts=(
            Verdict(95, "Exceptional"), Verdict(0, "Not yet"),
        ))
        assert compute_fit_score(**JOB)["recommendation"] == \
            "Good match — worth applying"
        assert compute_fit_score(**JOB, policy=strict)["recommendation"] == "Not yet"

    def test_the_reason_thresholds_are_read(self):
        loud = ScoringPolicy(reasons=ReasonsPolicy(skill_gap_below=99,
                                                   missing_skills_shown=1))
        default_reasons = compute_fit_score(**JOB)["reasons"]
        loud_reasons = compute_fit_score(**JOB, policy=loud)["reasons"]
        assert not any(r.startswith("Skill gap") for r in default_reasons)
        assert any(r.startswith("Skill gap") for r in loud_reasons)

    def test_the_agent_bonus_is_configurable(self):
        quiet = ScoringPolicy(bonuses=Bonuses(agent_eligible=0))
        with_bonus = compute_fit_score(**JOB, is_agent_eligible=True)
        without = compute_fit_score(**JOB, is_agent_eligible=True, policy=quiet)
        assert with_bonus["bonuses"]["agent_eligible"] == 5
        assert without["bonuses"]["agent_eligible"] == 0
        assert with_bonus["overall_score"] - without["overall_score"] == 5

    def test_the_location_bonus_is_configurable(self):
        assert score_location("Bangalore", "Bangalore") == 5
        pol = ScoringPolicy(bonuses=Bonuses(location_match=2))
        assert score_location("Bangalore", "Bangalore", policy=pol) == 2

    def test_the_bonus_cap_bounds_an_inflated_bonus_table(self):
        inflated = ScoringPolicy(bonuses=Bonuses(
            location_match=10, remote=10, salary_meets=10, agent_eligible=10,
            hybrid=10, office=0, salary_near=10, cap=20))
        fit = ScoringEngine(policy=inflated).fit_score(
            **JOB, job_location="Bangalore", profile_location="Bangalore",
            job_work_mode="remote", job_salary="20-25 Lacs",
            profile_expected_ctc=20, is_agent_eligible=True)
        assert fit.bonuses.total == 40
        assert fit.bonus_total == 20, "the cap is what stops a bonus blow-up"
        assert fit.explain()["bonus_cap_applied"] is True


class TestEveryEntryPointAgrees:
    """C2/C3: the injection point must reach the direct call sites too."""

    @staticmethod
    def _engine_path(policy):
        return ScoringEngine(policy=policy).compute_fit_score(**JOB)

    @staticmethod
    def _flat_api_path(policy):
        # instahyre imports this flat function and never builds an engine.
        return compute_fit_score(**JOB, policy=policy)

    @staticmethod
    def _direct_fitscore_path(policy):
        # auto_hunt / compare / smart_apply build FitScore themselves.
        engine = ScoringEngine(policy=policy)
        return FitScore.compute(
            **JOB,
            score_location_fn=lambda jl, pl: engine.score_location(jl, pl,
                                                                   policy=policy),
            score_work_mode_fn=lambda wm: engine.score_work_mode(wm, policy=policy),
            score_salary_fn=lambda js, ctc: engine.score_salary(js, ctc,
                                                                policy=policy),
            policy=policy,
        ).to_dict()

    @pytest.mark.parametrize("policy,expected", [
        (None, DEFAULT_SCORE),
        (DEFAULT_SCORING_POLICY, DEFAULT_SCORE),
        (EIGHTY_TWENTY, EIGHTY_TWENTY_SCORE),
        (ScoringPolicy(weights=Weights(skills=0.2, experience=0.8)), 90),
    ])
    def test_all_three_entry_points_return_the_same_number(self, policy, expected):
        scores = {
            "engine": self._engine_path(policy)["overall_score"],
            "flat": self._flat_api_path(policy)["overall_score"],
            "direct": self._direct_fitscore_path(policy)["overall_score"],
        }
        assert len(set(scores.values())) == 1, scores
        assert scores["engine"] == expected

    def test_the_agreement_check_CAN_fail(self):
        """The control. Without it, the test above proves nothing.

        This is the exact shape of the bug: a call site that passes the policy
        to the aggregate but keeps the module-level bonus helpers, which are
        bound to the default engine. The base score moves, the bonuses do not,
        and nothing says so.
        """
        no_location_bonus = ScoringPolicy(bonuses=Bonuses(location_match=0))
        enriched = dict(JOB, job_location="Bangalore",
                        profile_location="Bangalore")

        correct = ScoringEngine(policy=no_location_bonus).compute_fit_score(
            **enriched)
        mismatched = FitScore.compute(
            **enriched,
            score_location_fn=score_location,        # bound to the DEFAULT policy
            score_work_mode_fn=score_work_mode,
            score_salary_fn=score_salary,
            policy=no_location_bonus,
        ).to_dict()

        assert correct["bonuses"]["location"] == 0
        assert mismatched["bonuses"]["location"] == 5
        assert correct["overall_score"] != mismatched["overall_score"], (
            "if these ever match, the agreement test above is vacuous"
        )

    def test_the_default_singleton_is_untouched_by_a_per_call_policy(self):
        """DEFAULT_ENGINE must stay a default-everything import-time object."""
        from jobcore import DEFAULT_ENGINE
        compute_fit_score(**JOB, policy=EIGHTY_TWENTY)
        assert DEFAULT_ENGINE.policy == DEFAULT_SCORING_POLICY
        assert compute_fit_score(**JOB)["overall_score"] == DEFAULT_SCORE


class TestPayIsScoredInTheServersOwnUnit:
    """C4, as an executable invariant rather than a comment."""

    class NaukriSalary(Salary):
        CONFIG = SalaryConfig(lakhs_multiplier=100_000.0, raw_amount_threshold=200.0)

    class UplersSalary(Salary):
        CONFIG = SalaryConfig(lakhs_multiplier=1.0,
                              raw_amount_threshold=10_000_000.0)

    PAY = CandidatePay(
        inr_lakhs_per_year=PayBand(expected=24.0, floor=20.0),
        usd_per_year=PayBand(expected=30000.0, floor=20959.0),
    )

    def test_each_server_reads_its_own_denomination_and_scores_correctly(self):
        naukri = ScoringEngine(salary_cls=self.NaukriSalary)
        uplers = ScoringEngine(salary_cls=self.UplersSalary,
                               policy=ScoringPolicy(salary=SalaryPolicy(
                                   max_package_ceiling=None)))
        assert naukri.score_salary(
            "20-25 Lacs", self.PAY.for_unit("inr_lakhs_per_year").expected) == 5
        assert uplers.score_salary(
            "150000", self.PAY.for_unit("usd_per_year").expected) == 5

    def test_the_wrong_denomination_fails_SILENTLY_which_is_the_whole_point(self):
        """Control. Both wrong answers look exactly like 'no salary data'."""
        naukri = ScoringEngine(salary_cls=self.NaukriSalary)
        uplers = ScoringEngine(salary_cls=self.UplersSalary)
        usd_number = self.PAY.for_unit("usd_per_year").expected
        inr_number = self.PAY.for_unit("inr_lakhs_per_year").expected

        # A 25-lakh job against a dollar figure: zero, forever, on every job.
        assert naukri.score_salary("20-25 Lacs", usd_number) == 0
        # A $20,000 job against a lakhs figure: full marks, on every job.
        assert uplers.score_salary("20000", inr_number) == 5
        # ...where the correctly-denominated answer is 0.
        assert uplers.score_salary("20000", usd_number) == 0

    def test_the_ceiling_stays_bound_to_the_engines_own_threshold(self):
        """A concrete default would re-impose 200 lakhs on a USD board."""
        uplers = ScoringEngine(salary_cls=self.UplersSalary)
        assert uplers.score_salary("150000", 30000) == 5
        capped = ScoringEngine(salary_cls=self.UplersSalary,
                               policy=ScoringPolicy(salary=SalaryPolicy(
                                   max_package_ceiling=200.0)))
        assert capped.score_salary("150000", 30000) == 0

    def test_the_salary_bonus_values_are_configurable(self):
        naukri = ScoringEngine(salary_cls=self.NaukriSalary,
                               policy=ScoringPolicy(bonuses=Bonuses(
                                   salary_meets=4, salary_near=1)))
        assert naukri.score_salary("20-25 Lacs", 24) == 4
        assert naukri.score_salary("20-25 Lacs", 30) == 1   # 25 >= 30*0.8


class TestSkillWeightsBehaveAsDocumented:
    """H6: the arithmetic runs in a direction people do not expect."""

    def test_no_weights_is_byte_identical_to_flat_coverage(self):
        from jobcore import SkillMatch
        flat = SkillMatch.compute({"a", "b"}, {"a"})
        assert flat.score == 50.0

    def test_down_weighting_a_skill_he_LACKS_RAISES_the_score(self):
        """Not a corner case — it is the modal python-stack requisition."""
        from jobcore import SkillMatch
        pol = SkillsPolicy(weights=FrozenMap({"django": 0.7}))
        weighted = SkillMatch.compute({"node.js", "django"}, {"node.js"},
                                      policy=pol)
        assert round(weighted.score, 1) == 58.8
        assert weighted.score > 50.0, (
            "down-weighting shrinks the denominator only; a Django role scores "
            "HIGHER. Weights are not a rank tilt and must not be sold as one."
        )

    def test_weights_cancel_when_the_matched_set_equals_the_job_set(self):
        from jobcore import SkillMatch
        pol = SkillsPolicy(weights=FrozenMap({"python": 0.7}))
        assert SkillMatch.compute({"python"}, {"python"}, policy=pol).score == 100.0

    def test_down_weighting_a_skill_he_HAS_demotes_the_job_that_needs_it(self):
        from jobcore import SkillMatch
        pol = SkillsPolicy(weights=FrozenMap({"python": 0.5}))
        # job wants python + kafka, he has python only
        flat = SkillMatch.compute({"python", "kafka"}, {"python"})
        weighted = SkillMatch.compute({"python", "kafka"}, {"python"}, policy=pol)
        assert flat.score == 50.0
        assert round(weighted.score, 1) == 33.3

    def test_a_job_with_no_skills_uses_the_configured_default(self):
        from jobcore import SkillMatch
        assert SkillMatch.compute(set(), {"react"}).score == 50.0
        pol = SkillsPolicy(unknown_job_skills_default=0)
        assert SkillMatch.compute(set(), {"react"}, policy=pol).score == 0.0


class TestWorkModePreference:
    def test_the_shipped_order_reproduces_the_old_ladder_exactly(self):
        assert (score_work_mode("remote"), score_work_mode("hybrid"),
                score_work_mode("office"), score_work_mode(None)) == (5, 3, 0, 0)
        assert score_work_mode("wfh") == 5
        assert score_work_mode("Work From Home") == 5

    def test_reordering_the_preference_reassigns_the_same_three_values(self):
        engine = ScoringEngine(candidate=CandidatePolicy(
            work_mode_preference=("hybrid", "remote", "office")))
        assert engine.score_work_mode("hybrid") == 5
        assert engine.score_work_mode("remote") == 3
        assert engine.score_work_mode("office") == 0

    def test_the_bonus_values_themselves_are_still_configurable(self):
        engine = ScoringEngine(policy=ScoringPolicy(
            bonuses=Bonuses(remote=8, hybrid=4, office=1)))
        assert engine.score_work_mode("remote") == 8
        assert engine.score_work_mode("hybrid") == 4
        assert engine.score_work_mode("office") == 1


class TestLocationAcceptsMoreThanOneCity:
    def test_a_single_string_keeps_todays_exact_substring_semantics(self):
        assert score_location("Bangalore/Bengaluru", "Bangalore") == 5
        assert score_location("Pune", "Bangalore") == 0
        assert score_location("Remote - India", "Bangalore") == 5
        assert score_location(None, "Bangalore") == 0
        assert score_location("Pune", None) == 0

    def test_a_list_scores_the_best_of_its_members(self):
        assert score_location("Hyderabad", ["Bangalore", "Hyderabad"]) == 5
        assert score_location("Chennai", ["Bangalore", "Hyderabad"]) == 0

    def test_the_engines_candidate_supplies_the_list_when_none_is_passed(self):
        engine = ScoringEngine(candidate=CandidatePolicy(
            locations=("Bangalore", "Hyderabad")))
        assert engine.score_location("Hyderabad") == 5
        assert engine.score_location("Chennai") == 0

    def test_an_empty_list_scores_nothing_rather_than_everything(self):
        assert score_location("Bangalore", []) == 0


class TestStampingAndExplain:
    def test_a_default_policy_result_is_byte_for_byte_unstamped(self):
        """179 golden parity cases depend on this exact shape."""
        out = compute_fit_score(**JOB)
        assert "policy_hash" not in out
        assert "explain" not in out

    def test_a_non_default_policy_stamps_itself_automatically(self):
        out = compute_fit_score(**JOB, policy=EIGHTY_TWENTY)
        assert len(out["policy_hash"]) == 12

    def test_the_stamp_can_be_forced_or_suppressed(self):
        assert "policy_hash" in compute_fit_score(**JOB, stamp=True)
        assert "policy_hash" not in compute_fit_score(**JOB, policy=EIGHTY_TWENTY,
                                                      stamp=False)

    def test_two_results_under_the_same_policy_carry_the_same_stamp(self):
        a = compute_fit_score(**JOB, policy=EIGHTY_TWENTY, stamp=True)
        b = compute_fit_score(job_skills={"go"}, profile_skills={"go"},
                              job_exp_str="1-3 years", profile_exp="2 years",
                              policy=EIGHTY_TWENTY, stamp=True)
        assert a["policy_hash"] == b["policy_hash"], "these ARE comparable"

    def test_two_results_under_different_policies_do_not(self):
        a = compute_fit_score(**JOB, policy=EIGHTY_TWENTY, stamp=True)
        b = compute_fit_score(**JOB, stamp=True)
        assert a["policy_hash"] != b["policy_hash"], "these are NOT comparable"

    def test_policy_rev_rides_alongside_when_the_caller_has_one(self):
        out = compute_fit_score(**JOB, stamp=True, policy_rev=7)
        assert out["policy_rev"] == 7

    def test_explain_returns_the_arithmetic_not_the_score(self):
        out = compute_fit_score(**JOB, policy=EIGHTY_TWENTY, explain=True)
        ex = out["explain"]
        assert ex["weights"] == {"skills": 0.8, "experience": 0.2}
        assert ex["base"] == {"skills": 50.0, "experience": 100.0,
                              "combined": 60.0}
        assert ex["bonuses"]["total"] == 0
        assert ex["verdict_band"] == {"min": 60, "label": "Good match — worth applying"}
        assert ex["overall_score"] == out["overall_score"]

    def test_the_explained_arithmetic_reproduces_the_score(self):
        out = compute_fit_score(
            **JOB, job_location="Bangalore", profile_location="Bangalore",
            job_work_mode="remote", explain=True, policy=EIGHTY_TWENTY)
        ex = out["explain"]
        recomputed = min(100, round(ex["base"]["combined"] + ex["bonuses"]["total"]))
        assert recomputed == out["overall_score"]

    def test_explain_names_whether_skill_weighting_was_in_play(self):
        flat = compute_fit_score(**JOB, explain=True)["explain"]
        weighted = compute_fit_score(
            **JOB, explain=True,
            policy=ScoringPolicy(skills=SkillsPolicy(
                weights=FrozenMap({"aws": 0.5}))))["explain"]
        assert flat["skill_weighting"] == "flat"
        assert weighted["skill_weighting"] == "weighted"


class TestTheEngineRefusesAWrongPolicyRatherThanIgnoringIt:
    def test_a_non_policy_raises(self):
        with pytest.raises(TypeError, match="ScoringPolicy"):
            ScoringEngine(policy={"weights": {"skills": 0.8}})

    def test_a_non_candidate_raises(self):
        with pytest.raises(TypeError, match="CandidatePolicy"):
            ScoringEngine(candidate={"locations": ["Bangalore"]})

    def test_the_existing_salary_guard_still_raises(self):
        with pytest.raises(TypeError, match="Salary subclass"):
            ScoringEngine(salary_cls=str)
