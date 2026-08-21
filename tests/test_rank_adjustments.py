"""``scoring.rank_adjustments`` — the ordering preference, as data.

uplers carried this as ``PREFERENCE_TILT = 4`` plus two hardcoded frozensets,
in a sibling repo, where the operator could not reach it. It is his stated
preference, so it belongs in the file he edits.

Three properties are asserted here and nowhere else:

* **the default IS the deleted constant** — all four of uplers' cases produce
  exactly what the frozensets produced, so nothing moves until he edits;
* **it cannot become a score** — the rule type has no path into any component
  of ``overall_score``; and
* **it cannot outweigh a structural bonus** — the ±4 clamp is applied to the
  SUM, in Python, and is not reachable from the config file.

The third one carries a CONTROL: the same escalation is shown landing when the
clamp is relaxed, so the guarded assertion is known to be able to fail.
"""

import pytest

from jobcore import policy as P


DEFAULT = P.DEFAULT_SCORING_POLICY


class TestTheDefaultIsTheDeletedConstant:
    """uplers/fit.py's four documented cases, verbatim."""

    def test_a_python_leaning_role_is_demoted_by_four(self):
        assert DEFAULT.rank_adjustment({"python", "postgresql", "aws"}) == (
            -4, ("python-leaning stack",))

    def test_a_role_wanting_both_stacks_is_not_demoted(self):
        assert DEFAULT.rank_adjustment({"python", "node.js", "postgresql"}) == (0, ())

    def test_a_role_wanting_neither_stack_is_left_alone(self):
        assert DEFAULT.rank_adjustment({"golang", "postgresql"}) == (0, ())

    def test_a_node_role_is_left_alone(self):
        assert DEFAULT.rank_adjustment({"node.js", "postgresql", "aws"}) == (0, ())

    def test_the_shipped_rule_is_the_one_uplers_hardcoded(self):
        rule, = DEFAULT.rank_adjustments
        assert rule.delta == -4
        assert set(rule.when_skills_include) == {"python", "django", "flask", "fastapi"}
        assert set(rule.and_not) == {
            "javascript", "typescript", "node.js", "express", "nestjs", "next.js"}
        assert rule.label == "python-leaning stack"

    def test_every_python_stack_member_fires_it(self):
        for skill in ("python", "django", "flask", "fastapi"):
            assert DEFAULT.rank_adjustment({skill})[0] == -4, skill

    def test_every_node_stack_member_cancels_it(self):
        for skill in ("javascript", "typescript", "node.js", "express",
                      "nestjs", "next.js"):
            assert DEFAULT.rank_adjustment({"python", skill})[0] == 0, skill

    def test_matching_is_case_insensitive_on_canonical_names(self):
        assert DEFAULT.rank_adjustment({"Python", "AWS"})[0] == -4

    def test_an_empty_skill_set_matches_nothing(self):
        assert DEFAULT.rank_adjustment(set()) == (0, ())


class TestItIsNotAScore:
    """The invariant jobcore exists to hold: a 78 means the same everywhere."""

    def test_the_rule_list_is_not_read_by_any_scoring_component(self):
        from jobcore.fit import FitScore
        tilted = P.ScoringPolicy(rank_adjustments=(
            P.RankRule(when_skills_include=("python",), delta=-4, label="x"),))
        args = dict(
            job_skills={"python", "aws"},
            profile_skills={"python", "aws"},
            job_exp_str="3-5 years",
            profile_exp=5.0,
        )
        plain = FitScore.compute(**args, policy=P.DEFAULT_SCORING_POLICY)
        moved = FitScore.compute(**args, policy=tilted)
        assert plain.overall_score == moved.overall_score

    def test_it_is_absent_from_the_scored_output(self):
        from jobcore.fit import FitScore
        result = FitScore.compute(
            job_skills={"python"}, profile_skills={"python"},
            job_exp_str="3-5 years", profile_exp=5.0,
        ).to_dict()
        assert "rank_adjustment" not in result
        assert "rank_adjustments" not in result


class TestTheClampIsInPython:
    def test_one_rule_may_not_exceed_the_bound(self):
        with pytest.raises(P.PolicyError, match=r"exceeds"):
            P.ScoringPolicy(rank_adjustments=(
                P.RankRule(when_skills_include=("python",), delta=-40,
                           label="hide python"),)).validate()

    def test_stacked_rules_cannot_get_past_it_either(self):
        """Ten legal -4 rules sum to -40; the clamp holds them at -4."""
        stacked = P.ScoringPolicy(rank_adjustments=tuple(
            P.RankRule(when_skills_include=("python",), delta=-4,
                       label=f"rule {i}")
            for i in range(10)))
        stacked.validate()
        delta, labels = stacked.rank_adjustment({"python"})
        assert delta == -4
        assert len(labels) == 10

    def test_rewriting_HARD_LIMITS_at_runtime_does_not_widen_it(self, monkeypatch):
        """The bound is frozen at import, like every KeySpec bound."""
        monkeypatch.setattr(P, "HARD_LIMITS", P.FrozenMap(
            {**P.HARD_LIMITS.as_dict(), "rank_adjustment_max": 1000}))
        with pytest.raises(P.PolicyError, match=r"exceeds"):
            P.RankRule(when_skills_include=("python",), delta=-40,
                       label="hide python").validate()
        stacked = P.ScoringPolicy(rank_adjustments=tuple(
            P.RankRule(when_skills_include=("python",), delta=-4,
                       label=f"rule {i}")
            for i in range(10)))
        assert stacked.rank_adjustment({"python"})[0] == -4

    def test_the_stacking_control_lands_when_the_clamp_is_relaxed(self, monkeypatch):
        """CONTROL. Without the clamp the same ten rules reach -40.

        A guard nobody has watched fail is not a guard. This is the same
        policy object, the same call, and the only difference is the bound.
        """
        monkeypatch.setattr(P, "_RANK_ADJUSTMENT_MAX", 1000.0)
        stacked = P.ScoringPolicy(rank_adjustments=tuple(
            P.RankRule(when_skills_include=("python",), delta=-4,
                       label=f"rule {i}")
            for i in range(10)))
        assert stacked.rank_adjustment({"python"})[0] == -40

    def test_the_single_rule_control_lands_too(self, monkeypatch):
        """CONTROL. -40 in one rule is refused only because of the bound."""
        monkeypatch.setattr(P, "_RANK_ADJUSTMENT_MAX", 1000.0)
        huge = P.ScoringPolicy(rank_adjustments=(
            P.RankRule(when_skills_include=("python",), delta=-40,
                       label="hide python"),))
        huge.validate()
        assert huge.rank_adjustment({"python"})[0] == -40

    def test_the_bound_is_under_the_smallest_structural_bonus(self):
        """The calibration, stated as an assertion rather than a comment."""
        b = P.DEFAULT_SCORING_POLICY.bonuses
        smallest = min(b.location_match, b.remote, b.salary_meets, b.agent_eligible)
        assert float(P.HARD_LIMITS["rank_adjustment_max"]) < smallest

    def test_too_many_rules_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"cannot audit|maximum"):
            P.ScoringPolicy(rank_adjustments=tuple(
                P.RankRule(when_skills_include=("python",), delta=-1,
                           label=f"r{i}")
                for i in range(50))).validate()


class TestRuleValidation:
    def test_a_rule_that_can_never_fire_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"decoy"):
            P.RankRule(delta=-4, label="nothing").validate()

    def test_a_rule_cancelled_by_its_own_and_not_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"never fire"):
            P.RankRule(when_skills_include=("python",), and_not=("Python",),
                       delta=-4, label="self-cancelling").validate()

    def test_an_unlabelled_rule_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"label is required"):
            P.RankRule(when_skills_include=("python",), delta=-4).validate()

    def test_a_non_numeric_delta_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"delta must be a number"):
            P.RankRule(when_skills_include=("python",), delta="lots",
                       label="x").validate()

    def test_a_blank_skill_name_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"non-empty skill names"):
            P.RankRule(when_skills_include=("python", "  "), delta=-4,
                       label="x").validate()

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        with pytest.raises(P.PolicyError, match=r"unknown field"):
            P.RankRule.from_dict({"when_skills_include": ["python"],
                                  "delta": -4, "label": "x",
                                  "weight": 0.7})

    def test_a_bare_list_instead_of_objects_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"must be objects"):
            P.RankRule.from_dict(["python"])

    def test_a_non_list_value_is_refused(self):
        with pytest.raises(P.PolicyError, match=r"must be a list of rule"):
            P.ScoringPolicy.from_dict({"rank_adjustments": {"python": -4}})


class TestRoundTrip:
    def test_the_default_survives_to_dict_and_back(self):
        d = DEFAULT.to_dict()
        assert P.ScoringPolicy.from_dict(d).rank_adjustments == \
            DEFAULT.rank_adjustments

    def test_an_absent_key_keeps_the_shipped_rule(self):
        assert P.ScoringPolicy.from_dict({}).rank_adjustments == \
            DEFAULT.rank_adjustments

    def test_an_explicit_empty_list_turns_the_preference_off(self):
        """He must be able to say 'no tilt', and [] is how."""
        off = P.ScoringPolicy.from_dict({"rank_adjustments": []})
        off.validate()
        assert off.rank_adjustments == ()
        assert off.rank_adjustment({"python", "django"}) == (0, ())

    def test_null_reverts_to_the_shipped_rule_not_to_empty(self):
        assert P.ScoringPolicy.from_dict(
            {"rank_adjustments": None}).rank_adjustments == DEFAULT.rank_adjustments

    def test_a_retuned_rule_round_trips(self):
        retuned = P.ScoringPolicy.from_dict({"rank_adjustments": [
            {"when_skills_include": ["php", "laravel"], "and_not": [],
             "delta": -2, "label": "php shop"},
        ]})
        retuned.validate()
        assert retuned.rank_adjustment({"php"}) == (-2, ("php shop",))
        assert retuned.rank_adjustment({"python"}) == (0, ())

    def test_the_policy_stays_hashable(self):
        assert hash(P.ScoringPolicy(rank_adjustments=(
            P.RankRule(when_skills_include=("php",), delta=-1, label="x"),
        ))) is not None


class TestSchemaAndDataclassCannotDrift:
    """Two places declare this default; they may never disagree silently."""

    def test_the_schema_default_equals_the_dataclass_default(self):
        assert [dict(r) for r in P.SCHEMA["scoring.rank_adjustments"].default] == \
            [r.to_dict() for r in P.DEFAULT_SCORING_POLICY.rank_adjustments]

    def test_the_schema_default_survives_a_load(self):
        loaded = P.ScoringPolicy.from_dict(
            {"rank_adjustments": [dict(r) for r in
                                  P.SCHEMA["scoring.rank_adjustments"].default]})
        assert loaded.rank_adjustments == P.DEFAULT_SCORING_POLICY.rank_adjustments

    def test_it_declares_a_reader(self):
        spec = P.SCHEMA["scoring.rank_adjustments"]
        assert spec.readers == ("uplers",)
        assert spec.tier == P.TIER_A


class TestFingerprint:
    def test_retuning_the_tilt_moves_the_hash(self):
        """It changes which rows surface, so a cycle must re-approve."""
        before = P.Policy()
        after = P.Policy(scoring=P.ScoringPolicy(rank_adjustments=()))
        assert after.policy_hash != before.policy_hash
        assert P.requires_approval_cycle(after.policy_hash, before.policy_hash)
