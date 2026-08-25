"""THE BOUNDARY, and where each half of it is proved.

    RETIRED 2026-08-25: *no sequence of config writes, from any server, may
    grant autonomous apply authority.* The file CAN now arm naukri's agent.

The five-write escalation this file was built to refuse is REAL and is still
run here -- what changed is the expected outcome. It now LANDS, deliberately,
and is neutralised one layer down in Python. The operator overruled the
conclusion; he did not overrule the protections, and the protections were
never in this package.

**WHY THIS FILE CANNOT PROVE THE OTHER HALF.** The four guards that bound a
file-armed agent live in ``naukri_server/agent.py``: the MIN_AGENT_FIT_FLOOR
clamp in ``_decide``, the kill switch inside the auto-apply loop, the daily cap
on candidates, and ``validate_agent_config`` on the merged config. jobcore
ships with ZERO runtime dependencies and ``test_independence.py`` runs it in a
clean interpreter, so this suite cannot import naukri to exercise them --
verified, not assumed: ``import naukri_server`` is a ModuleNotFoundError here.

The proof is therefore split, each half asserted where its code lives:

  * HERE: the escalation lands, with tier-B friction (every loosening step
    needs ``confirm_widen``), under Python ceilings the file cannot raise, and
    with the deny-by-default subtree intact. Plus the one guard that IS a
    jobcore constant, :data:`MIN_AGENT_FIT_FLOOR`, proved unwritable.
  * ``naukri/tests/test_safety_invariant.py``: the same escalation planted in
    a file, loaded through ``load_agent_config``, and then measured through
    ``_decide`` and ``_effective_mode`` -- the floor clamping the selector, the
    quota capping candidates, the overlay validated or dropped whole.

**Path 2 -- one write, through a sibling -- is UNCHANGED and still refused.**
``candidate`` is writable from any server, so ``uplers_set_config({"candidate":
{"skills": [<the whole taxonomy>]}})`` drives ``SkillMatch.score`` to 100 for
every job in existence. That lever cannot be tier C (it is the feature he asked
for by name), so it is bounded by HARD_LIMITS and by the forced approval cycle,
and every assertion about it below is untouched by the ruling.

**Every guarded assertion below has a CONTROL that shows it CAN fail** --
against a permissive build, or as a refuse/accept pair for the ratcheted keys.
Without that, a refusal could be coming from a typo in a key name rather than
from a guard. Six bugs in this codebase in one week were checks that could not
fail.
"""

import json

import pytest

from jobcore import config as C
from jobcore import policy as P
from jobcore import SkillMatch


# ── the attack, as data ────────────────────────────────────────────────────

ESCALATION = [
    ("arm the agent",
     {"servers": {"naukri": {"agent": {"enabled": True}}}}),
    ("take the no-approval branch",
     {"servers": {"naukri": {"agent": {"mode": "auto"}}}}),
    ("drop the apply selector to zero",
     {"servers": {"naukri": {"agent": {"min_fit_score": 0}}}}),
    ("free every company he blocked",
     {"servers": {"naukri": {"agent": {"blocklist": {"enabled": False}}}}}),
    ("point it at arbitrary searches",
     {"servers": {"naukri": {"agent": {"searches": [
         {"name": "anything", "query": "*", "min_fit_score": 0}]}}}}),
]

EVERY_SKILL_IN_THE_TAXONOMY = sorted(P.FrozenMap(
    {k: 1 for k in __import__("jobcore").SKILL_ALIASES}
))
SIBLING_SKILLS_PATCH = {"candidate": {"skills": EVERY_SKILL_IN_THE_TAXONOMY}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config" / "jobhunt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"config_version": 1, "revision": 1}, indent=2),
                    encoding="utf-8")
    monkeypatch.setenv(C.ENV_CONFIG, str(path))
    C.invalidate_cache()
    return path


@pytest.fixture
def permissive(monkeypatch):
    """The build the review found: every key Tier A, no python-side bounds.

    This is not a strawman — it is the tier table exactly as the design
    document specified it, where ``enabled`` and ``mode`` had no tier at all
    and ``min_fit_score`` was explicitly listed as free.
    """
    def permissive_spec_for(path):
        real = P.spec_for(path)
        doc = real.doc if real else "synthesised"
        default = real.default if real else None
        return P.KeySpec(path=path, tier=P.TIER_A, doc=doc,
                         readers=("permissive",), default=default)

    monkeypatch.setattr(C, "spec_for", permissive_spec_for)
    monkeypatch.setattr(
        P, "HARD_LIMITS",
        P.FrozenMap({**P.HARD_LIMITS.as_dict(),
                     "candidate_skills_max": 100_000,
                     "candidate_locations_max": 100_000}),
    )
    return permissive_spec_for


# ── CONTROLS: prove the attack is real and the harness can land a write ────

class TestTheAttackSucceedsAgainstAPermissiveBuild:
    """If these ever fail, every refusal below is meaningless.

    NOTE after the 2026-08-25 ruling: the FIRST test here no longer
    distinguishes the permissive build from the real one -- the escalation
    lands against both, which is the ruling. It is kept because it still
    proves the harness can land a write at all, which is the precondition for
    reading anything into the refuse/accept pairs further down. The other
    controls in this class are untouched by the ruling and still do the job
    they were written for: they cover `candidate.skills` and `scoring`, which
    were never tier C and are still bounded by HARD_LIMITS.
    """

    def test_the_five_step_escalation_lands_completely(self, cfg, permissive):
        for label, patch in ESCALATION:
            out = C.apply_patch(patch, path=cfg, actor="attacker",
                                allowed_sections=("candidate", "scoring",
                                                  "servers.naukri"))
            assert out["status"] == "ok", (label, out)

        armed = json.loads(cfg.read_text(encoding="utf-8"))["servers"]["naukri"]["agent"]
        assert armed["enabled"] is True
        assert armed["mode"] == "auto"
        assert armed["min_fit_score"] == 0
        assert armed["blocklist"]["enabled"] is False
        assert armed["searches"], "and pointed at whatever it likes"

    def test_the_sibling_skills_write_lands_and_scores_every_job_100(self,
                                                                     cfg,
                                                                     permissive):
        out = C.apply_patch(SIBLING_SKILLS_PATCH, path=cfg, actor="uplers",
                            allowed_sections=("candidate", "scoring",
                                              "servers.uplers"),
                            confirm_widen=True)
        assert out["status"] == "ok", out
        skills = set(json.loads(cfg.read_text(encoding="utf-8"))["candidate"]["skills"])
        assert len(skills) > 80
        for job in ({"react"}, {"aws", "kubernetes"}, {"python", "django"}):
            assert SkillMatch.compute(job & skills or job, skills).score == 100.0

    def test_collapsing_the_weights_would_also_reach_the_selector(self, cfg,
                                                                  permissive):
        """Documented so the bound in HARD_LIMITS is not mistaken for fussiness."""
        degenerate = P.ScoringPolicy(weights=P.Weights(skills=0.0, experience=1.0))
        # experience maxes at 100 for any in-range job -> every such job is 100
        from jobcore import compute_fit_score
        out = compute_fit_score(job_skills={"cobol"}, profile_skills=set(),
                                job_exp_str="3-5 years", profile_exp="4 years",
                                policy=degenerate)
        assert out["overall_score"] == 100, (
            "a zero weight makes the score equal one component; that is why "
            "weight_min/weight_max live in Python"
        )

    def test_the_weight_and_cap_bounds_are_the_only_thing_stopping_those_two(
            self, cfg, permissive, monkeypatch):
        """Control for the SECOND layer.

        The tier table alone does not stop these — the bounds do, in two
        independent places (the frozen KeySpec and the dataclass validator).
        Relax both and the writes land, which is what makes them load-bearing
        rather than decorative.
        """
        relaxed = {**P.HARD_LIMITS.as_dict(), "weight_min": 0.0,
                   "weight_max": 1.0, "bonus_cap_ceiling": 1000, "bonus_max": 1000}
        monkeypatch.setattr(P, "HARD_LIMITS", P.FrozenMap(relaxed))
        collapse = C.apply_patch(
            {"scoring": {"weights": {"skills": 0.0, "experience": 1.0}}},
            path=cfg, actor="attacker", confirm_widen=True)
        raise_cap = C.apply_patch({"scoring": {"bonuses": {"cap": 60}}},
                                  path=cfg, actor="attacker", confirm_widen=True)
        assert collapse["status"] == "ok", collapse
        assert raise_cap["status"] == "ok", raise_cap

    def test_the_shipped_bounds_survive_runtime_mutation_of_HARD_LIMITS(self,
                                                                        cfg,
                                                                        monkeypatch):
        """Belt and braces: the KeySpec bounds are frozen at import.

        Rewriting ``HARD_LIMITS`` in a live process does not move them, so a
        compromised or confused caller cannot widen the ceiling by assignment.
        """
        relaxed = {**P.HARD_LIMITS.as_dict(), "bonus_cap_ceiling": 1000}
        monkeypatch.setattr(P, "HARD_LIMITS", P.FrozenMap(relaxed))
        out = C.apply_patch({"scoring": {"bonuses": {"cap": 60}}}, path=cfg,
                            actor="attacker", confirm_widen=True)
        assert out["status"] == "refused"
        assert "ceiling 20" in " ".join(out["refusals"])


# ── THE GUARD: the same attack against the real build ──────────────────────

class TestTheEscalationLandsHereAndIsStoppedInPython:
    """The four tests the ruling inverted, CONVERTED rather than deleted.

    Each one still runs the same attack. Each one now asserts the boundary
    that actually exists: the config layer lets the six through as a ratchet,
    and naukri's Python guards are what bound the result. A suite that simply
    dropped these would be strictly worse than one that states the new line.
    """

    @pytest.mark.parametrize("label,patch", ESCALATION,
                             ids=[label for label, _ in ESCALATION])
    def test_each_step_is_ACCEPTED_by_name_with_confirmation(self, cfg, label,
                                                             patch):
        """Was ``test_each_step_is_refused_by_name``.

        Every step is a LOOSENING of a tier-B key, so it costs an explicit
        ``confirm_widen`` and then it lands. That flag is the whole remaining
        friction at this layer, and it is friction, not a gate.
        """
        out = C.apply_patch(patch, path=cfg, actor="attacker",
                            allowed_sections=("candidate", "scoring",
                                              "servers.naukri"),
                            confirm_widen=True)
        assert out["status"] == "ok", (label, out)

    @pytest.mark.parametrize("label,patch", ESCALATION,
                             ids=[label for label, _ in ESCALATION])
    def test_every_step_is_STILL_refused_without_confirm_widen(self, cfg, label,
                                                               patch):
        """CONTROL for the pair above, and the ratchet in its own right.

        Refuse-then-accept is what makes both halves meaningful: if the write
        landed either way the confirmation would be decoration, and if it
        refused either way the acceptance test above would be passing on a
        typo.
        """
        out = C.apply_patch(patch, path=cfg, actor="attacker",
                            allowed_sections=("candidate", "scoring",
                                              "servers.naukri"))
        assert out["status"] == "refused", (label, out)
        assert "confirm_widen" in " ".join(out["refusals"]), out["refusals"]

    def test_the_whole_sequence_ARMS_the_agent(self, cfg):
        """Was ``test_the_whole_sequence_changes_nothing``.

        The inversion, stated at full strength: all five writes land, the
        agent block in the loaded policy is armed, aimed and unfiltered, and
        the file on disk says so. What this does NOT show is an application
        being submitted -- see the class below, and naukri's suite.
        """
        for label, patch in ESCALATION:
            out = C.apply_patch(patch, path=cfg, actor="attacker",
                                allowed_sections=("candidate", "scoring",
                                                  "servers.naukri"),
                                confirm_widen=True)
            assert out["status"] == "ok", (label, out)

        agent = C.current().policy.server("naukri")["agent"]
        assert agent["enabled"] is True
        assert agent["mode"] == "auto"
        assert agent["min_fit_score"] == 0
        assert agent["blocklist"]["enabled"] is False
        assert len(agent["searches"]) == 1

        on_disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert on_disk["servers"]["naukri"]["agent"]["mode"] == "auto", (
            "every byte of the escalation reached the file"
        )

    def test_a_HAND_EDITED_file_CAN_arm_it_now(self, cfg):
        """Was ``test_a_HAND_EDITED_file_cannot_arm_it_either``.

        The file is the surface a text editor reaches, so it is THE surface --
        that reasoning is unchanged and is exactly why the ruling had to be
        made at the tier, not at the write path. Notepad now arms the agent,
        with no ceremony at all, and everything that stops it from applying to
        the wrong thing lives in naukri.
        """
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"agent": {
                "enabled": True, "mode": "auto", "min_fit_score": 0,
                "per_search_limit": 50,
                "blocklist": {"enabled": False},
                "searches": [{"name": "anything", "keywords": "*"}],
            }}},
        }, indent=2), encoding="utf-8")
        C.invalidate_cache()

        loaded = C.current()
        agent = loaded.policy.server("naukri")["agent"]
        assert agent["enabled"] is True
        assert agent["mode"] == "auto"
        assert agent["min_fit_score"] == 0
        assert agent["per_search_limit"] == 50
        assert agent["blocklist"]["enabled"] is False
        assert len(agent["searches"]) == 1
        assert loaded.tier_c_refusals == (), (
            "nothing in the escalation is tier C any more"
        )

    def test_a_HAND_EDIT_still_cannot_pass_the_python_ceilings(self, cfg):
        """CONTROL, and the honest limit of the sentence above.

        The load path does not clamp -- jobcore enforces floor/ceiling on the
        WRITE path only -- so a hand edit reaches naukri with any integer at
        all. That is a statement about where the clamp lives, not an absence
        of one: `_decide` clamps `min_fit_score` to the floor and
        `per_search_limit` to its ceiling on every cycle. Asserted here so the
        division of labour is written down rather than assumed.
        """
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"agent": {"per_search_limit": 100_000}}},
        }, indent=2), encoding="utf-8")
        C.invalidate_cache()
        assert C.current().policy.server("naukri")["agent"][
            "per_search_limit"] == 100_000

        # ...but the WRITE path refuses the same value, confirmed or not.
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"per_search_limit": 100_000}}}},
            path=cfg, actor="attacker", allowed_sections=("servers.naukri",),
            confirm_widen=True)
        assert out["status"] == "refused", out
        assert "ceiling" in " ".join(out["refusals"])

    def test_the_daily_quota_did_NOT_join_the_six(self, cfg):
        """It is one of the four Python guards, so it kept its ceiling.

        A guard whose value the same file can raise is worth less than one it
        cannot: `max_daily_applications` ratchets DOWN freely and cannot pass
        25 even with confirmation, and naukri does not read it from this file
        at all.
        """
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 99}}}},
            path=cfg, actor="attacker", allowed_sections=("servers.naukri",),
            confirm_widen=True)
        assert out["status"] == "refused", out
        assert "ceiling 25" in " ".join(out["refusals"])

        tighten = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 3}}}},
            path=cfg, actor="attacker", allowed_sections=("servers.naukri",))
        assert tighten["status"] == "ok", tighten

    def test_a_future_key_under_the_agent_subtree_is_denied_by_default(self, cfg):
        """UNCHANGED by the ruling, and the half that most needed to survive.

        The escalation opened because two keys had NO tier. Six keys are named
        and loadable now; the seventh, invented tomorrow, is still refused --
        omission is the bug, deny-by-default is the fix, and naming six
        exceptions is not the same as removing the rule.
        """
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"newly_invented_switch": True}}}},
            path=cfg, actor="attacker",
            allowed_sections=("candidate", "scoring", "servers.naukri"),
            confirm_widen=True)
        assert out["status"] == "refused"
        assert "tier C" in " ".join(out["refusals"])

    def test_a_bare_min_fit_score_leaf_ELSEWHERE_is_still_tier_c(self, cfg):
        """Was ``test_a_per_search_min_fit_score_override_is_refused_too``.

        `TIER_C_LEAF_NAMES` did not go away; it acquired exactly one declared
        exception. `servers.naukri.agent.min_fit_score` is tier B by name, and
        every other spelling of that leaf -- another server's, a nested one, a
        per-search override -- is still refused. That is what stops the ruling
        from generalising itself to keys nobody ruled on.
        """
        out = C.apply_patch(
            {"servers": {"uplers": {"min_fit_score": 0}}},
            path=cfg, actor="uplers",
            allowed_sections=("candidate", "scoring", "servers.uplers"),
            confirm_widen=True)
        assert out["status"] == "refused"
        assert "tier C" in " ".join(out["refusals"])

        assert P.tier_for("servers.naukri.agent.min_fit_score") == P.TIER_B
        for elsewhere in ("servers.uplers.min_fit_score",
                          "servers.naukri.min_fit_score",
                          "servers.naukri.agent.searches.0.min_fit_score",
                          "servers.instahyre.queue.min_fit_score"):
            assert P.tier_for(elsewhere) == P.TIER_C, elsewhere


class TestNoSiblingServerCanDriveAnothersSelector:

    def test_writing_the_whole_taxonomy_into_candidate_skills_is_refused(self, cfg):
        out = C.apply_patch(SIBLING_SKILLS_PATCH, path=cfg, actor="uplers",
                            allowed_sections=("candidate", "scoring",
                                              "servers.uplers"),
                            confirm_widen=True)
        assert out["status"] == "refused"
        joined = " ".join(out["refusals"])
        assert "maximum" in joined and "raise it" in joined

    def test_it_is_refused_on_LOAD_as_well_as_on_WRITE(self, cfg):
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "candidate": {"skills": EVERY_SKILL_IN_THE_TAXONOMY},
        }, indent=2), encoding="utf-8")
        C.invalidate_cache()
        loaded = C.current()
        assert loaded.config_error is not None
        assert "score-inflation lever" in loaded.config_error
        assert loaded.policy.candidate.skills == (), (
            "the whole policy falls back; never half-applied"
        )

    def test_a_plausible_human_skill_list_still_works_with_confirmation(self, cfg):
        real = ["javascript", "typescript", "node.js", "express.js", "react",
                "postgresql", "mongodb", "redis", "docker", "kubernetes", "aws"]
        out = C.apply_patch({"candidate": {"skills": real}}, path=cfg,
                            actor="claude-desktop", confirm_widen=True)
        assert out["status"] == "ok", out
        assert list(C.current().policy.candidate.skills) == real

    def test_adding_skills_without_confirmation_is_refused(self, cfg):
        out = C.apply_patch({"candidate": {"skills": ["rust"]}}, path=cfg,
                            actor="claude-desktop")
        assert out["status"] == "refused"
        assert "confirm_widen" in " ".join(out["refusals"])

    def test_removing_skills_is_always_free(self, cfg):
        C.apply_patch({"candidate": {"skills": ["rust", "go"]}}, path=cfg,
                      actor="test", confirm_widen=True)
        out = C.apply_patch({"candidate": {"skills": ["rust"]}}, path=cfg,
                            actor="test")
        assert out["status"] == "ok", out

    def test_collapsing_the_weights_is_refused_by_a_python_side_bound(self, cfg):
        out = C.apply_patch(
            {"scoring": {"weights": {"skills": 0.0, "experience": 1.0}}},
            path=cfg, actor="uplers", confirm_widen=True)
        assert out["status"] == "refused"

    def test_raising_the_bonus_cap_past_todays_twenty_is_refused(self, cfg):
        out = C.apply_patch({"scoring": {"bonuses": {"cap": 60}}}, path=cfg,
                            actor="uplers", confirm_widen=True)
        assert out["status"] == "refused"

    def test_inflating_a_single_bonus_past_the_python_maximum_is_refused(self, cfg):
        out = C.apply_patch({"scoring": {"bonuses": {"agent_eligible": 90}}},
                            path=cfg, actor="uplers", confirm_widen=True)
        assert out["status"] == "refused"

    def test_a_sibling_cannot_write_naukris_section_at_all(self, cfg):
        out = C.apply_patch({"servers": {"naukri": {"display_min_score": 0}}},
                            path=cfg, actor="uplers",
                            allowed_sections=("candidate", "scoring",
                                              "servers.uplers"))
        assert out["status"] == "refused"


class TestTheSecondFloorConfigCannotReach:
    """C1 fix 3: a bad threshold must cost display noise, not applications.

    AFTER 2026-08-25 THIS IS THE LOAD-BEARING GUARD, not a second layer. The
    file may now set `agent.min_fit_score: 0`; this floor is what makes that
    cost display noise rather than applications, and it is a jobcore constant
    with no config key anywhere, which is why the assertions live here.

    What this class CANNOT show is the clamp firing, because the clamp is
    `max(configured, floor)` in `naukri_server.agent._decide` and jobcore
    cannot import naukri (zero runtime deps; `test_independence.py` runs a
    clean interpreter). That half is proved in
    `naukri/tests/test_safety_invariant.py::TestTheFloorTheFileCannotReach`,
    which plants `min_fit_score: 0` in a real file and asserts the search was
    asked for 60. Do not read the absence of that assertion here as its
    absence from the system.
    """

    def test_the_floor_exists_in_python_and_is_not_a_config_key(self):
        assert C.MIN_AGENT_FIT_FLOOR == 60
        assert P.spec_for("min_agent_fit_floor") is None
        assert P.spec_for("servers.naukri.agent.min_agent_fit_floor") == \
            P.spec_for("servers.naukri.agent.anything_else")   # the deny subtree

    def test_no_write_can_move_it(self, cfg):
        for patch in (
            {"servers": {"naukri": {"agent": {"min_agent_fit_floor": 0}}}},
            {"scoring": {"min_agent_fit_floor": 0}},
            {"min_agent_fit_floor": 0},
        ):
            out = C.apply_patch(patch, path=cfg, actor="attacker",
                                allowed_sections=("candidate", "scoring",
                                                  "servers.naukri",
                                                  "min_agent_fit_floor"),
                                confirm_widen=True)
            assert out["status"] == "refused", patch
        assert C.MIN_AGENT_FIT_FLOOR == 60


class TestAPolicyChangeForcesAnApprovalCycle:
    """C1 fix 4: the guard against the levers that cannot be tier C.

    Neither ``candidate.skills`` nor ``scoring`` can be locked away — they are
    the operator's headline feature. So a cycle that observes a fingerprint it
    has not seen runs in approval mode whatever the configured mode says. One
    condition, and "policy was quietly widened" becomes "he sees the list".
    """

    def test_a_bounded_but_real_skills_change_still_trips_it(self, cfg):
        before = C.current()
        C.apply_patch({"candidate": {"skills": ["rust", "go", "kafka"]}},
                      path=cfg, actor="claude-desktop", confirm_widen=True)
        after = C.current()
        assert after.policy_hash != before.policy_hash
        assert P.requires_approval_cycle(after.policy_hash, before.policy_hash)

    def test_a_weights_change_within_bounds_still_trips_it(self, cfg):
        before = C.current()
        C.apply_patch({"scoring": {"weights": {"skills": 0.7, "experience": 0.3}}},
                      path=cfg, actor="claude-desktop")
        after = C.current()
        assert P.requires_approval_cycle(after.policy_hash, before.policy_hash)

    def test_a_HAND_EDIT_trips_it_too(self, cfg):
        before = C.current()
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "scoring": {"weights": {"skills": 0.9, "experience": 0.1}},
        }, indent=2), encoding="utf-8")
        C.invalidate_cache()
        after = C.current()
        assert P.requires_approval_cycle(after.policy_hash, before.policy_hash)
        assert after.external_edit is not None

    def test_a_cosmetic_change_does_NOT_trip_it(self, cfg):
        before = C.current()
        C.apply_patch({"candidate": {"headline": "Backend Engineer"}},
                      path=cfg, actor="claude-desktop")
        after = C.current()
        assert not P.requires_approval_cycle(after.policy_hash, before.policy_hash)


class TestWhatTheOperatorCanStillDo:
    """A guard that refuses everything is not a well-guarded surface.

    The operator asked for dynamic weights and a tailor-made profile. These
    assertions are the other half of the invariant: the feature still works.
    """

    def test_he_can_retune_the_weights_he_named(self, cfg):
        out = C.apply_patch(
            {"scoring": {"weights": {"skills": 0.75, "experience": 0.25}}},
            path=cfg, actor="claude-desktop")
        assert out["status"] == "ok", out
        assert C.current().policy.scoring.weights.skills == 0.75

    def test_he_can_retune_every_bonus_and_band(self, cfg):
        out = C.apply_patch({"scoring": {
            "bonuses": {"location_match": 3, "hybrid": 5, "remote": 4},
            "verdicts": [{"min": 85, "label": "Apply"},
                         {"min": 0, "label": "Skip"}],
            "experience": {"under_penalty_per_year": 10},
            "salary": {"meets_expectation_ratio": 0.9},
        }}, path=cfg, actor="claude-desktop")
        assert out["status"] == "ok", out

    def test_he_can_describe_himself(self, cfg):
        out = C.apply_patch({"candidate": {
            "name": "G. Sundeep",
            "headline": "Backend Software Engineer",
            "locations": ["Bangalore", "Hyderabad"],
            "work_mode_preference": ["remote", "hybrid", "office"],
            "notice_period_days": 0,
            "pay": {"inr_lakhs_per_year": {"expected": 24.0, "floor": 20.0},
                    "usd_per_year": {"expected": 30000.0, "floor": 20959.0}},
        }}, path=cfg, actor="claude-desktop")
        assert out["status"] == "ok", out
        cand = C.current().policy.candidate
        assert cand.locations == ("Bangalore", "Hyderabad")
        assert cand.pay.for_unit("usd_per_year").floor == 20959.0

    def test_he_can_add_vocabulary_the_shipped_table_lacks(self, cfg):
        out = C.apply_patch({"scoring": {"skills": {"extra_skills": {
            "trpc": ["trpc.io"], "temporal": ["temporal.io"]}}}},
            path=cfg, actor="claude-desktop")
        assert out["status"] == "ok", out

    def test_he_can_tighten_the_agent_without_ceremony(self, cfg):
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 3,
                                              "cycle_interval_hours": 12}}}},
            path=cfg, actor="claude-desktop",
            allowed_sections=("servers.naukri",))
        assert out["status"] == "ok", out

    def test_the_whole_thing_still_works_with_no_config_file_at_all(self,
                                                                    monkeypatch):
        monkeypatch.setenv(C.ENV_CONFIG, ":none:")
        C.invalidate_cache()
        loaded = C.current()
        assert loaded.policy == P.DEFAULT_POLICY
        assert loaded.source is None
