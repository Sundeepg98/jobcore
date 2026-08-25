"""The schema, its defaults, and the tier data.

Two properties matter more than the rest and are asserted first:

* **Every default is exactly today's literal.** That single sentence is the
  whole migration strategy — it is what lets 179 golden parity cases pass
  unchanged and what lets a bare clone of any consumer behave byte-for-byte as
  it does now.
* **The schema and the dataclasses cannot drift.** Two places declare the same
  default; a test compares them so the pair can never disagree silently, which
  is exactly the disease (``min_fit_score`` at six sites, two values) this
  design exists to cure.
"""

import pytest

from jobcore import policy as P


class TestDefaultsAreTodaysLiterals:
    """If any of these change, live scoring changed. That is never incidental."""

    def test_weights_are_sixty_forty(self):
        assert P.DEFAULT_SCORING_POLICY.weights.skills == 0.6
        assert P.DEFAULT_SCORING_POLICY.weights.experience == 0.4

    def test_the_four_plus_five_bonuses(self):
        b = P.DEFAULT_SCORING_POLICY.bonuses
        assert (b.location_match, b.remote, b.salary_meets, b.agent_eligible) == \
            (5, 5, 5, 5)
        assert (b.hybrid, b.office, b.salary_near) == (3, 0, 3)
        assert b.cap == 20

    def test_experience_penalties(self):
        e = P.DEFAULT_SCORING_POLICY.experience
        assert (e.under_penalty_per_year, e.over_coefficient) == (20, 15)
        assert (e.over_floor, e.unknown_default) == (60, 50)

    def test_salary_ratios(self):
        s = P.DEFAULT_SCORING_POLICY.salary
        assert (s.meets_expectation_ratio, s.below_market_ratio,
                s.above_market_ratio) == (0.8, 0.85, 1.15)

    def test_max_package_ceiling_defaults_to_none_not_two_hundred(self):
        """None means 'use this engine's own raw_amount_threshold'.

        A concrete 200 here would silently re-impose naukri's lakhs ceiling on
        uplers, which deliberately binds 10_000_000 for USD/year.
        """
        assert P.DEFAULT_SCORING_POLICY.salary.max_package_ceiling is None
        assert P.DEFAULT_SCORING_POLICY.salary.ceiling_for(200.0) == 200.0
        assert P.DEFAULT_SCORING_POLICY.salary.ceiling_for(10_000_000.0) == 10_000_000.0

    def test_verdict_bands_and_their_exact_wording(self):
        assert [(v.min, v.label) for v in P.DEFAULT_SCORING_POLICY.verdicts] == [
            (80, "Strong match — apply confidently"),
            (60, "Good match — worth applying"),
            (40, "Partial match — review missing skills before applying"),
            (0, "Weak match — consider upskilling first"),
        ]

    def test_reason_thresholds(self):
        r = P.DEFAULT_SCORING_POLICY.reasons
        assert (r.skill_gap_below, r.experience_below, r.missing_skills_shown) == \
            (50, 70, 3)

    def test_skill_weights_start_empty_so_coverage_stays_flat(self):
        assert dict(P.DEFAULT_SCORING_POLICY.skills.weights) == {}
        assert P.DEFAULT_SCORING_POLICY.skills.weight_of("anything") == 1.0

    @pytest.mark.parametrize("path,expected", [
        # The five default FLIPS the design proposed do NOT ride inside the
        # mechanism change. Each of these is today's literal, and three of
        # them are pinned by tests that exist right now in other repos.
        ("servers.naukri.boost_profile.randomize", False),
        ("servers.instahyre.exclude_agencies", False),
        ("servers.instahyre.queue.order", "platform"),
        ("servers.instahyre.unverified_agency", "drop"),
        ("servers.linkedin_own.search.default_sort", "relevance"),
        # H7: two decisions, two values, two keys. Collapsing them to one
        # would silently drop the agent's apply threshold by ten points.
        ("servers.naukri.agent.min_fit_score", 70),
        ("servers.naukri.display_min_score", 60),
    ])
    def test_no_default_flips_ride_inside_the_mechanism(self, path, expected):
        assert P.SCHEMA[path].default == expected


class TestSchemaAndDataclassesCannotDrift:
    @pytest.mark.parametrize("path,getter", [
        ("scoring.weights.skills", lambda p: p.scoring.weights.skills),
        ("scoring.weights.experience", lambda p: p.scoring.weights.experience),
        ("scoring.bonuses.location_match", lambda p: p.scoring.bonuses.location_match),
        ("scoring.bonuses.remote", lambda p: p.scoring.bonuses.remote),
        ("scoring.bonuses.hybrid", lambda p: p.scoring.bonuses.hybrid),
        ("scoring.bonuses.office", lambda p: p.scoring.bonuses.office),
        ("scoring.bonuses.salary_meets", lambda p: p.scoring.bonuses.salary_meets),
        ("scoring.bonuses.salary_near", lambda p: p.scoring.bonuses.salary_near),
        ("scoring.bonuses.agent_eligible", lambda p: p.scoring.bonuses.agent_eligible),
        ("scoring.bonuses.cap", lambda p: p.scoring.bonuses.cap),
        ("scoring.experience.under_penalty_per_year",
         lambda p: p.scoring.experience.under_penalty_per_year),
        ("scoring.experience.over_coefficient",
         lambda p: p.scoring.experience.over_coefficient),
        ("scoring.experience.over_floor", lambda p: p.scoring.experience.over_floor),
        ("scoring.experience.unknown_default",
         lambda p: p.scoring.experience.unknown_default),
        ("scoring.skills.unknown_job_skills_default",
         lambda p: p.scoring.skills.unknown_job_skills_default),
        ("scoring.salary.meets_expectation_ratio",
         lambda p: p.scoring.salary.meets_expectation_ratio),
        ("scoring.salary.below_market_ratio",
         lambda p: p.scoring.salary.below_market_ratio),
        ("scoring.salary.above_market_ratio",
         lambda p: p.scoring.salary.above_market_ratio),
        ("scoring.salary.max_package_ceiling",
         lambda p: p.scoring.salary.max_package_ceiling),
        ("scoring.reasons.skill_gap_below", lambda p: p.scoring.reasons.skill_gap_below),
        ("scoring.reasons.experience_below",
         lambda p: p.scoring.reasons.experience_below),
        ("scoring.reasons.missing_skills_shown",
         lambda p: p.scoring.reasons.missing_skills_shown),
        ("candidate.notice_period_days", lambda p: p.candidate.notice_period_days),
    ])
    def test_schema_default_equals_the_dataclass_default(self, path, getter):
        assert P.SCHEMA[path].default == getter(P.DEFAULT_POLICY)

    def test_verdicts_agree(self):
        assert [tuple(v) for v in P.SCHEMA["scoring.verdicts"].default] == \
            [(v.min, v.label) for v in P.DEFAULT_SCORING_POLICY.verdicts]

    def test_work_mode_preference_agrees(self):
        assert tuple(P.SCHEMA["candidate.work_mode_preference"].default) == \
            P.DEFAULT_CANDIDATE.work_mode_preference


class TestTierIsData:
    def test_every_spec_declares_a_valid_tier(self):
        for spec in P.iter_specs():
            assert spec.tier in (P.TIER_A, P.TIER_B, P.TIER_C), spec.path

    def test_every_tier_b_spec_declares_a_direction_and_a_bound(self):
        for spec in P.iter_specs():
            if spec.tier != P.TIER_B:
                continue
            assert spec.direction is not None, spec.path
            assert (spec.ceiling is not None or spec.floor is not None
                    or spec.max_items is not None
                    or spec.choices is not None
                    or spec.direction in ("grow", "shrink", "up")), (
                f"{spec.path}: a ratchet with no python-side bound is not a "
                f"ratchet — the file could walk it anywhere one step at a time"
            )

    def test_choices_counts_as_a_bound_because_it_is_one(self):
        """`choices` joined the accepted bounds above on 2026-08-25.

        The rule that list is enforcing is WALKABILITY: a ratchet whose value
        can be nudged one step at a time to anywhere is not a ratchet. An
        enumerated domain cannot be walked anywhere -- there is nowhere else to
        go -- and `_check_choices` refuses an off-menu value at every tier, so
        it bounds strictly harder than a ceiling does.

        Both keys that rely on it are booleans-or-enums that arrived when the
        agent block became loadable.
        """
        for path in ("servers.naukri.agent.enabled", "servers.naukri.agent.mode"):
            spec = P.SCHEMA[path]
            assert spec.tier == P.TIER_B, path
            assert spec.choices, path
            assert spec.ceiling is None and spec.floor is None, path

    def test_a_tier_b_spec_with_no_bound_at_all_is_still_caught(self):
        """CONTROL for the rule above: it must be able to fail.

        `direction="down"` is the one direction with no blanket exemption, so a
        spec with that direction and no ceiling/floor/max_items/choices is the
        shape the assertion exists to reject.
        """
        naked = P.KeySpec(path="servers.naukri.agent.made_up", tier=P.TIER_B,
                          doc="x", readers=("naukri",), direction="down")
        assert not (naked.ceiling is not None or naked.floor is not None
                    or naked.max_items is not None or naked.choices is not None
                    or naked.direction in ("grow", "shrink", "up"))

    def test_tier_c_is_never_loadable(self):
        for spec in P.iter_specs():
            if spec.tier == P.TIER_C:
                assert spec.loadable is False, spec.path

    def test_every_non_c_key_declares_a_reader(self):
        """The anti-decoy rule, mechanised. A knob nothing reads cannot ship."""
        for spec in P.iter_specs():
            if spec.tier == P.TIER_C:
                continue
            assert spec.readers, spec.path

    def test_a_spec_with_no_reader_is_rejected_at_construction(self):
        with pytest.raises(P.PolicyError, match="decoy"):
            P.KeySpec(path="scoring.made_up", tier=P.TIER_A, doc="x", readers=())

    def test_a_tier_b_spec_with_no_direction_is_rejected(self):
        with pytest.raises(P.PolicyError, match="ratchet"):
            P.KeySpec(path="scoring.x", tier=P.TIER_B, doc="x", readers=("jobcore",))

    #: The five traced escalation steps plus `per_search_limit`, with the
    #: direction that LOOSENS each one. Tier C until 2026-08-25; tier B since.
    ESCALATION_KEYS = [
        ("servers.naukri.agent.enabled", "down"),
        ("servers.naukri.agent.mode", "down"),
        ("servers.naukri.agent.min_fit_score", "up"),
        ("servers.naukri.agent.searches", "shrink"),
        ("servers.naukri.agent.blocklist.enabled", "up"),
        ("servers.naukri.agent.per_search_limit", "down"),
    ]

    @pytest.mark.parametrize("path,direction", ESCALATION_KEYS,
                             ids=[p.rsplit(".", 1)[-1] for p, _ in ESCALATION_KEYS])
    def test_the_traced_escalation_keys_are_loadable_but_ratcheted(self, path,
                                                                   direction):
        """The 2026-08-25 ruling, as data.

        These six were tier C because a five-write escalation through them
        ended at fifteen unapproved applications a day. The operator overruled
        the CONCLUSION and kept the PROTECTIONS, which never lived in this
        schema -- they are four Python guards in naukri's `agent.py`.

        Tier B, not tier A, is the part this test pins: the file can arm the
        agent, but every one of the six loosens only with `confirm_widen`, and
        each declares which way loosening runs.
        """
        spec = P.SCHEMA[path]
        assert spec.tier == P.TIER_B, path
        assert spec.loadable, path
        assert spec.direction == direction, path
        assert spec.readers == ("naukri",), (
            f"{path}: a loadable key must name a real reader -- the anti-decoy "
            f"rule is what forced `per_search_limit` to acquire one"
        )

    def test_the_daily_quota_is_NOT_one_of_the_six(self):
        """It is the fourth Python guard, so the ruling left it alone.

        Tier B here (it always was, and `_check_ratchet` holds it under the
        Python ceiling), but naukri's agent does not take it from the shared
        file at all -- see `agent.FILE_DECIDABLE_KEYS`. Two different questions;
        this one is only about the schema.
        """
        spec = P.SCHEMA["servers.naukri.agent.max_daily_applications"]
        assert spec.tier == P.TIER_B
        assert spec.direction == "down"
        assert spec.ceiling == float(P.HARD_LIMITS["max_daily_applications_ceiling"])

    def test_the_ruling_did_NOT_touch_the_deny_by_default_subtree(self):
        """The half that must not move. Named keys are loadable; the subtree
        is not, and that is what stops the NEXT key arriving by omission."""
        assert P.tier_for("servers.naukri.agent.newly_invented_switch") == P.TIER_C
        assert P.SCHEMA["servers.*.agent.**"].tier == P.TIER_C

    @pytest.mark.parametrize("path", [
        "servers.naukri.agent.some_future_key",
        "servers.naukri.agent.deeply.nested.thing",
        "servers.uplers.agent.enabled",
        "servers.anything.agent.whatever",
    ])
    def test_an_undeclared_key_under_an_agent_subtree_denies_by_default(self, path):
        """The escalation opened because `enabled` and `mode` had NO tier.

        Deny-by-default under the agent subtree is what stops the next key
        added there from arriving as tier A by omission.
        """
        assert P.tier_for(path) == P.TIER_C

    @pytest.mark.parametrize("path", [
        "servers.uplers.min_fit_score",
        "servers.naukri.min_fit_score",
        "servers.naukri.agent.searches.0.min_fit_score",
        "servers.instahyre.queue.min_fit_score",
    ])
    def test_min_fit_score_is_tier_c_wherever_it_appears(self, path):
        assert P.tier_for(path) == P.TIER_C

    def test_the_display_filter_has_its_own_unambiguous_name_and_is_free(self):
        assert P.tier_for("servers.naukri.display_min_score") == P.TIER_A

    @pytest.mark.parametrize("path,tier", [
        ("candidate.skills", P.TIER_B),
        ("candidate.years_experience", P.TIER_B),
        ("candidate.avoid_companies", P.TIER_B),
        ("scoring.bonuses.cap", P.TIER_B),
        ("servers.naukri.agent.max_daily_applications", P.TIER_B),
        ("servers.naukri.daily_apply_quota", P.TIER_B),
        ("servers.naukri.retention.auto_purge_days", P.TIER_B),
        ("scoring.weights.skills", P.TIER_A),
        ("candidate.locations", P.TIER_A),
    ])
    def test_tier_assignments_that_the_review_forced(self, path, tier):
        assert P.tier_for(path) == tier

    def test_an_explicit_key_beats_the_deny_subtree(self):
        assert P.tier_for("servers.naukri.agent.max_daily_applications") == P.TIER_B

    def test_an_undeclared_key_outside_any_subtree_has_no_tier(self):
        assert P.tier_for("scoring.invented_key") is None


class TestHardLimitsLiveInPython:
    def test_the_ceilings_are_here_and_not_in_the_schema_defaults(self):
        assert P.HARD_LIMITS["max_daily_applications_ceiling"] == 25
        assert P.HARD_LIMITS["daily_apply_quota_ceiling"] == 50
        assert P.HARD_LIMITS["min_agent_fit_floor"] == 60
        assert P.HARD_LIMITS["candidate_skills_max"] == 40

    def test_hard_limits_is_immutable(self):
        with pytest.raises(TypeError):
            P.HARD_LIMITS["min_agent_fit_floor"] = 0

    def test_hard_limits_is_not_a_declared_config_key(self):
        """If it were writable it would not be a limit."""
        for name in P.HARD_LIMITS:
            assert P.spec_for(f"hard_limits.{name}") is None
            assert P.spec_for(name) is None


class TestRoundTrip:
    def test_default_policy_round_trips(self):
        d = P.DEFAULT_POLICY.to_dict()
        assert P.Policy.from_dict(d).to_dict() == d

    def test_a_customised_policy_round_trips(self):
        custom = P.Policy(
            candidate=P.CandidatePolicy(
                name="G. Sundeep",
                skills=("node.js", "typescript"),
                locations=("Bangalore", "Hyderabad"),
                work_mode_preference=("hybrid", "remote", "office"),
                pay=P.CandidatePay(
                    inr_lakhs_per_year=P.PayBand(expected=24.0, floor=20.0),
                    usd_per_year=P.PayBand(expected=30000.0, floor=20959.0),
                ),
                avoid_companies=("SomeCorp",),
            ),
            scoring=P.ScoringPolicy(weights=P.Weights(skills=0.8, experience=0.2)),
            servers=P.FrozenMap(P.schema_defaults("servers")),
        )
        d = custom.to_dict()
        assert P.Policy.from_dict(d).to_dict() == d

    def test_null_reverts_to_the_shipped_default(self):
        """Otherwise the only way back is knowing the value by heart."""
        pol = P.ScoringPolicy.from_dict({"weights": {"skills": None,
                                                     "experience": None}})
        assert (pol.weights.skills, pol.weights.experience) == (0.6, 0.4)

    def test_from_dict_of_nothing_is_the_default(self):
        assert P.Policy.from_dict(None).scoring == P.DEFAULT_SCORING_POLICY
        assert P.Policy.from_dict({}).scoring == P.DEFAULT_SCORING_POLICY


class TestValidationIsLoud:
    def test_weights_must_sum_to_one(self):
        with pytest.raises(P.PolicyError, match="sum to 1.0"):
            P.ScoringPolicy(weights=P.Weights(skills=0.8, experience=0.4)).validate()

    def test_a_weight_cannot_collapse_to_zero(self):
        """A 0/1 split makes the score equal one component and clear anything."""
        with pytest.raises(P.PolicyError, match="outside"):
            P.ScoringPolicy(weights=P.Weights(skills=0.0, experience=1.0)).validate()

    def test_the_bonus_cap_cannot_exceed_todays_twenty(self):
        with pytest.raises(P.PolicyError, match="cap"):
            P.ScoringPolicy(bonuses=P.Bonuses(cap=50)).validate()

    def test_verdicts_must_descend(self):
        bands = (P.Verdict(40, "low"), P.Verdict(80, "high"), P.Verdict(0, "floor"))
        with pytest.raises(P.PolicyError, match="descending"):
            P.ScoringPolicy(verdicts=bands).validate()

    def test_verdicts_must_reach_zero_or_a_low_score_has_no_verdict(self):
        with pytest.raises(P.PolicyError, match="band starting at 0"):
            P.ScoringPolicy(verdicts=(P.Verdict(40, "ok"),)).validate()

    def test_candidate_skills_over_the_python_maximum_is_refused(self):
        too_many = tuple(f"skill{i}" for i in range(200))
        with pytest.raises(P.PolicyError, match="score-inflation lever"):
            P.CandidatePolicy(skills=too_many).validate()

    def test_work_mode_preference_must_be_a_permutation(self):
        with pytest.raises(P.PolicyError, match="permutation"):
            P.CandidatePolicy(work_mode_preference=("remote",)).validate()

    def test_extra_skills_must_be_canonical_to_aliases_not_the_inverse(self):
        """The design's map was inverted against extended()'s real signature."""
        bad = P.SkillsPolicy(extra_skills=P.FrozenMap({"trpc.io": "trpc"}))
        with pytest.raises(P.PolicyError, match="canonical -> "):
            bad.validate()

    def test_extra_skills_in_the_right_shape_validates(self):
        good = P.SkillsPolicy(extra_skills=P.FrozenMap({"trpc": ("trpc.io", "t-rpc")}))
        good.validate()
        assert good.taxonomy_extension() == {"trpc": {"trpc.io", "t-rpc"}}


class TestPayIsDenominated:
    """C4: one scalar for two unit systems is a silent, permanent wrong answer."""

    def test_a_single_scalar_with_a_unit_tag_is_refused_by_name(self):
        with pytest.raises(P.PolicyError, match="denominated per unit system"):
            P.CandidatePay.from_dict({"unit": "lakhs_per_year", "expected": 24.0,
                                      "floor": 20.0})

    def test_each_denomination_is_reachable_by_name(self):
        pay = P.CandidatePay(
            inr_lakhs_per_year=P.PayBand(expected=24.0, floor=20.0),
            usd_per_year=P.PayBand(expected=30000.0, floor=20959.0),
        )
        assert pay.for_unit("inr_lakhs_per_year").expected == 24.0
        assert pay.for_unit("usd_per_year").floor == 20959.0

    def test_an_unknown_unit_raises_rather_than_guessing(self):
        with pytest.raises(P.PolicyError, match="unknown pay unit"):
            P.CandidatePay().for_unit("eur_per_month")

    def test_expected_and_floor_are_two_decisions_not_one(self):
        band = P.PayBand(expected=24.0, floor=20.0)
        assert band.expected != band.floor

    def test_drifting_denominations_warn_loudly_but_never_convert(self):
        drifted = P.CandidatePay(
            inr_lakhs_per_year=P.PayBand(expected=24.0),
            usd_per_year=P.PayBand(expected=24.0),   # someone copied the number
        )
        warning = drifted.fx_warning()
        assert warning is not None
        assert "wrong unit" in warning
        # ...and nothing was converted: each side still reads its own.
        assert drifted.for_unit("usd_per_year").expected == 24.0

    def test_a_sane_pair_does_not_warn(self):
        sane = P.CandidatePay(
            inr_lakhs_per_year=P.PayBand(expected=24.0),
            usd_per_year=P.PayBand(expected=30000.0),
        )
        assert sane.fx_warning() is None

    def test_no_pay_configured_is_the_default_and_warns_about_nothing(self):
        assert P.DEFAULT_CANDIDATE.pay.for_unit("inr_lakhs_per_year").expected is None
        assert P.DEFAULT_CANDIDATE.pay.fx_warning() is None


class TestFingerprint:
    def test_it_covers_the_inputs_that_can_move_a_number(self):
        fp = P.DEFAULT_POLICY.fingerprint()
        assert set(fp) == {"scoring", "candidate"}
        assert set(fp["candidate"]) == {
            "skills", "years_experience", "locations",
            "work_mode_preference", "pay",
        }

    def test_it_excludes_what_cannot_move_a_number(self):
        noisy = P.Policy(
            candidate=P.CandidatePolicy(name="Someone Else",
                                        headline="Different headline",
                                        titles=("SDE-3",),
                                        notice_period_days=90),
        )
        assert noisy.policy_hash == P.Policy().policy_hash

    def test_a_weight_change_moves_the_hash(self):
        moved = P.Policy(scoring=P.ScoringPolicy(
            weights=P.Weights(skills=0.8, experience=0.2)))
        assert moved.policy_hash != P.Policy().policy_hash

    def test_a_skill_added_to_the_candidate_moves_the_hash(self):
        moved = P.Policy(candidate=P.CandidatePolicy(skills=("rust",)))
        assert moved.policy_hash != P.Policy().policy_hash

    def test_the_hash_is_stable_across_key_order(self):
        a = P.fingerprint_hash({"x": 1, "y": 2})
        b = P.fingerprint_hash({"y": 2, "x": 1})
        assert a == b

    def test_it_is_twelve_hex_characters(self):
        h = P.DEFAULT_POLICY.policy_hash
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


class TestApprovalCycleRule:
    """The guard against the two levers that cannot be tier C."""

    def test_a_first_ever_cycle_needs_approval(self):
        assert P.requires_approval_cycle("abc123abc123", None) is True

    def test_an_unchanged_fingerprint_does_not(self):
        assert P.requires_approval_cycle("abc123abc123", "abc123abc123") is False

    def test_any_fingerprint_change_forces_approval(self):
        assert P.requires_approval_cycle("abc123abc123", "def456def456") is True

    def test_inflating_candidate_skills_trips_it(self):
        before = P.Policy()
        after = P.Policy(candidate=P.CandidatePolicy(
            skills=tuple(f"s{i}" for i in range(30))))
        assert P.requires_approval_cycle(after.policy_hash, before.policy_hash)

    def test_collapsing_the_weights_trips_it(self):
        before = P.Policy()
        after = P.Policy(scoring=P.ScoringPolicy(
            weights=P.Weights(skills=0.1, experience=0.9)))
        assert P.requires_approval_cycle(after.policy_hash, before.policy_hash)


class TestFrozenMap:
    def test_it_is_hashable_and_immutable(self):
        m = P.FrozenMap({"b": 2, "a": 1})
        assert hash(m) == hash(P.FrozenMap({"a": 1, "b": 2}))
        with pytest.raises(TypeError):
            m["c"] = 3

    def test_policies_stay_hashable_so_a_frozen_aggregate_stays_frozen(self):
        assert hash(P.DEFAULT_SCORING_POLICY) is not None
        assert hash(P.ScoringPolicy(skills=P.SkillsPolicy(
            weights=P.FrozenMap({"python": 0.7})))) is not None

    def test_ordering_is_deterministic(self):
        assert list(P.FrozenMap({"z": 1, "a": 2})) == ["a", "z"]
