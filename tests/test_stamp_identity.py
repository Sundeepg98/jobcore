"""One name, one meaning: the stamp that says whether two scores are comparable.

WHY THIS FILE EXISTS -- the defect it was written to reproduce, verbatim:

    ``Policy.fingerprint()`` covers ``{scoring, candidate}`` and is what every
    ``*_config()`` tool, the ledger and instahyre's result stamp print under the
    key ``policy_hash``. ``FitScore.policy_hash`` covered ``{scoring}`` ONLY and
    was what naukri's result stamp printed -- under the same key. One identical
    policy produced ``65c29f6b041b`` from the first and ``7e664cb97b54`` from
    the second.

    Measured against the build before this file landed:

        FAILED tests/test_stamp_identity.py::TestOneNameOneMeaning::
               test_a_field_called_policy_hash_means_one_thing
        AssertionError: two hashes under one name: config says 65c29f6b041b,
        the result stamp says 7e664cb97b54

That is the whole bug. Both numbers were correct; both answered a real and
DIFFERENT question; neither name said which. This is the one field whose entire
job is to tell you whether two scores may be compared, so an ambiguous name
does not merely confuse -- it inverts the answer. Someone comparing a stored
naukri score against a config readout concludes the policy differed when it did
not; someone comparing two servers' stamps concludes they matched when the
candidate underneath had been rewritten.

THE MODEL THIS FILE PINS. Two questions, therefore two names:

  ``scoring_hash``  -- "was the same ARITHMETIC applied?"  Covers ``scoring``
      only. Belongs on a RESULT, because a result already carries its own
      inputs; what it cannot otherwise say is which weights, bonuses, caps and
      verdict bands turned those inputs into that number.

  ``policy_hash``   -- "was the same POLICY in effect?"  Covers ``scoring`` AND
      ``candidate``. Belongs on a CONFIG readout, the ledger, and the agent's
      approval gate, whose entire purpose is to catch an inflated
      ``candidate.skills`` -- a change that moves no arithmetic and would be
      invisible to a scoring-only hash.

Collapsing these into one hash was considered and rejected in both directions.
A result cannot carry the candidate half honestly: ``FitScore`` receives
``profile_skills`` as a call argument, and on uplers and instahyre those come
from the LIVE platform profile, not from the config file -- a candidate-covering
stamp on such a result would be a claim the result cannot vouch for. And a
config readout cannot shed the candidate half: ``requires_approval_cycle`` is
built on exactly that coverage.

So both hashes stay. What changes is that they now say which they are, and that
a config readout prints BOTH -- which is what finally lets a stored result be
matched back to the configuration that produced it. That bridge did not exist
before; the ambiguity was standing in for it.
"""

from __future__ import annotations

import pytest

from jobcore.fit import FitScore
from jobcore.policy import (
    DEFAULT_POLICY,
    DEFAULT_SCORING_POLICY,
    Policy,
    ScoringPolicy,
    fingerprint_hash,
)


# ── fixtures ────────────────────────────────────────────────────────────────

def _policy(**overrides) -> Policy:
    """A policy with BOTH halves non-default, so the two hashes can differ."""
    base = {
        "scoring": {"weights": {"skills": 0.8, "experience": 0.2}},
        "candidate": {
            "skills": ["node.js", "typescript", "react"],
            "years_experience": 5,
            "locations": ["bangalore"],
        },
    }
    for key, value in overrides.items():
        base[key] = {**base.get(key, {}), **value}
    return Policy.from_dict(base)


def _score(policy: Policy) -> FitScore:
    """One fixed job/profile pair, scored under *policy*'s scoring half.

    The inputs are frozen so that anything that moves in the stamps moved
    because of the policy, never because of the job.
    """
    return FitScore.compute(
        job_skills={"node.js", "typescript", "aws"},
        profile_skills={"node.js", "typescript", "react"},
        job_exp_str="3-5 years",
        profile_exp="5 years 0 months",
        policy=policy.scoring,
    )


# ── the reproduction ────────────────────────────────────────────────────────

class TestOneNameOneMeaning:
    """The check that was red before the rename. Kept as the regression."""

    def test_a_field_called_policy_hash_means_one_thing(self):
        """Every ``policy_hash`` in the system hashes the same thing.

        This is the assertion that failed. It passes now because the
        scoring-only hash is no longer CALLED ``policy_hash`` -- there is
        exactly one ``policy_hash`` left and it is the full one.
        """
        p = _policy()
        result = _score(p).to_dict(stamp=True)

        assert "policy_hash" not in result, (
            "a result stamp must not print `policy_hash`: it covers scoring "
            "only and cannot vouch for the candidate half, so borrowing the "
            "config tool's name is the ambiguity this file exists to stop"
        )

        # The full hash still exists, under its own name, and is unchanged.
        assert p.policy_hash == fingerprint_hash(p.fingerprint())

    def test_the_two_hashes_are_genuinely_different_numbers(self):
        """Not a naming quibble -- they disagree on a real policy.

        If these ever coincide the test above proves nothing, so pin it.
        """
        p = _policy()
        assert p.policy_hash != p.scoring_hash

    def test_each_name_says_its_own_scope(self):
        p = _policy()
        assert p.scoring_hash == fingerprint_hash({"scoring": p.scoring.to_dict()})
        assert p.policy_hash == fingerprint_hash(
            {"scoring": p.scoring.to_dict(),
             "candidate": p.fingerprint()["candidate"]}
        )


# ── comparability: the property the stamp exists to deliver ─────────────────

class TestIdenticalPolicyIdenticalStamp:
    """Same policy in, same stamp out -- everywhere the two are compared."""

    def test_two_results_under_one_policy_carry_one_scoring_hash(self):
        p = _policy()
        a = _score(p).to_dict(stamp=True)
        b = _score(p).to_dict(stamp=True)
        assert a["scoring_hash"] == b["scoring_hash"]

    def test_a_policy_rebuilt_from_its_own_dict_stamps_identically(self):
        """Round-tripping the config file must not move the stamp."""
        p = _policy()
        rebuilt = Policy.from_dict(p.to_dict())
        assert rebuilt.scoring_hash == p.scoring_hash
        assert rebuilt.policy_hash == p.policy_hash
        assert _score(rebuilt).to_dict(stamp=True)["scoring_hash"] == \
            _score(p).to_dict(stamp=True)["scoring_hash"]

    def test_the_config_readout_and_the_result_stamp_can_be_matched_up(self):
        """THE BRIDGE. The thing the ambiguity was standing in for.

        A stored score prints ``scoring_hash``. A config readout prints
        ``policy_hash`` AND ``scoring_hash``. Comparing the shared field is how
        you answer "was this score produced under my current configuration?" --
        a question that had no correct answer before, because the two fields
        were named the same and hashed different things.
        """
        p = _policy()
        result = _score(p).to_dict(stamp=True)
        assert result["scoring_hash"] == p.scoring_hash

    def test_two_different_policy_objects_with_equal_values_agree(self):
        """Identity is by VALUE, not by object -- two servers, one file."""
        a, b = _policy(), _policy()
        assert a is not b
        assert a.scoring_hash == b.scoring_hash
        assert a.policy_hash == b.policy_hash


class TestChangedPolicyChangedStamp:
    """A stamp that never moves certifies nothing."""

    def test_a_scoring_change_moves_both_hashes(self):
        before = _policy()
        after = _policy(scoring={"weights": {"skills": 0.5, "experience": 0.5}})
        assert after.scoring_hash != before.scoring_hash
        assert after.policy_hash != before.policy_hash

    def test_a_scoring_change_moves_the_result_stamp(self):
        before = _score(_policy()).to_dict(stamp=True)
        after = _score(
            _policy(scoring={"weights": {"skills": 0.5, "experience": 0.5}})
        ).to_dict(stamp=True)
        assert after["scoring_hash"] != before["scoring_hash"]

    @pytest.mark.parametrize("patch", [
        {"bonuses": {"cap": 25}},
        {"experience": {"under_penalty_per_year": 30}},
        {"skills": {"unknown_job_skills_default": 40}},
        {"verdicts": [{"min": 70, "label": "Strong"}, {"min": 0, "label": "No"}]},
    ])
    def test_every_scoring_lever_moves_the_scoring_hash(self, patch):
        """Not just weights: anything that can move a number must show up."""
        assert _policy(scoring=patch).scoring_hash != _policy().scoring_hash

    def test_a_candidate_change_moves_policy_hash_and_NOT_scoring_hash(self):
        """The asymmetry, pinned deliberately, because it looks like a bug.

        Inflating ``candidate.skills`` changes WHO is being scored, not HOW.
        The arithmetic is untouched, so ``scoring_hash`` is right not to move --
        and ``policy_hash`` is right to move, which is exactly why the agent's
        approval gate is built on the full hash and not the scoring one.
        """
        before = _policy()
        after = _policy(candidate={"skills": ["node.js", "typescript",
                                              "react", "kubernetes", "aws"]})
        assert after.scoring_hash == before.scoring_hash
        assert after.policy_hash != before.policy_hash

    def test_a_candidate_change_does_not_move_the_result_stamp(self):
        before = _score(_policy()).to_dict(stamp=True)
        after = _score(_policy(candidate={"years_experience": 9})).to_dict(stamp=True)
        assert after["scoring_hash"] == before["scoring_hash"]


class TestDefaultsAreUntouched:
    """Shipped behaviour, byte for byte, when no config file exists."""

    def test_the_default_policy_still_auto_stamps_nothing(self):
        result = FitScore.compute(
            job_skills={"node.js"}, profile_skills={"node.js"},
            job_exp_str="3-5 years", profile_exp="5 years",
        ).to_dict()
        assert "scoring_hash" not in result
        assert "policy_hash" not in result

    def test_default_scoring_hash_is_stable(self):
        """Two ways of naming the shipped defaults hash the same."""
        assert DEFAULT_POLICY.scoring_hash == fingerprint_hash(
            {"scoring": DEFAULT_SCORING_POLICY.to_dict()}
        )
        assert ScoringPolicy().scoring_hash == DEFAULT_POLICY.scoring_hash

    def test_the_explain_block_names_the_scoring_hash(self):
        """``explain`` reports the arithmetic, so it carries the arithmetic's
        hash -- not the config tool's."""
        block = _score(_policy()).explain()
        assert "policy_hash" not in block
        assert block["scoring_hash"] == _policy().scoring_hash
