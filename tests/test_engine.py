"""Behaviour of the pieces that are NEW in the extraction.

Golden parity covers everything carried over from naukri unchanged. What is
new is the configurability — the salary units and the taxonomy became
injectable — plus the failure modes that must stay loud.
"""

import pytest

from jobcore import (
    DEFAULT_SALARY_CONFIG,
    DEFAULT_TAXONOMY,
    FitScore,
    Salary,
    SalaryConfig,
    ScoringEngine,
    SkillMatch,
    SkillTaxonomy,
)


class TestSalaryConfigIsInjected:
    def test_default_multiplier_is_one_lakh(self):
        assert DEFAULT_SALARY_CONFIG.lakhs_multiplier == 100_000.0
        assert DEFAULT_SALARY_CONFIG.raw_amount_threshold == 200.0

    def test_raw_rupees_are_converted_with_the_configured_multiplier(self):
        assert Salary.from_string("1500000").max_lakhs == 15.0

    def test_a_different_multiplier_changes_the_answer(self):
        """The whole point of the extraction: units are data, not an import."""
        thousands = SalaryConfig(lakhs_multiplier=1_000.0)
        s = Salary.from_string("1500000", config=thousands)
        assert s.max_lakhs == 1500.0
        # and the shared default is untouched
        assert Salary.from_string("1500000").max_lakhs == 15.0

    def test_subclass_binds_config_for_every_call(self):
        class ThousandsSalary(Salary):
            CONFIG = SalaryConfig(lakhs_multiplier=1_000.0)

        assert ThousandsSalary.from_string("1500000").max_lakhs == 1500.0
        assert isinstance(ThousandsSalary.from_string("10-15 Lacs"), ThousandsSalary)

    def test_threshold_governs_unit_detection(self):
        low = SalaryConfig(raw_amount_threshold=10.0)
        # 15 is now "too big to be lakhs", so it converts
        assert Salary.from_string("15", config=low).max_lakhs == 0.0
        assert Salary.from_string("15").max_lakhs == 15.0


class TestSalaryFailsLoudlyNotQuietly:
    @pytest.mark.parametrize(
        "raw", ["Not disclosed", "Confidential", "", "   ", "abc", None, 12345]
    )
    def test_unparseable_is_undisclosed_never_zero(self, raw):
        """A zero would read as a real offer of nothing. It must be None."""
        s = Salary.from_string(raw)
        assert s.min_lakhs is None
        assert s.max_lakhs is None
        assert s.is_disclosed is False
        assert s.midpoint is None
        assert s.market_position(15.0) == "unknown"

    def test_raw_input_is_preserved_for_the_caller_to_show(self):
        assert Salary.from_string("Negotiable, DOE").raw == "Negotiable, DOE"


class TestScoringEngineConfiguration:
    def test_engine_uses_its_salary_type(self):
        class ThousandsSalary(Salary):
            CONFIG = SalaryConfig(lakhs_multiplier=1_000.0)

        default_engine = ScoringEngine()
        odd_engine = ScoringEngine(salary_cls=ThousandsSalary)
        # "1500000" is 15 LPA by default (meets a 15 expectation -> 5)
        assert default_engine.score_salary("1500000", 15) == 5
        # ...and 1500 LPA under the odd config, which trips the sanity ceiling
        assert odd_engine.score_salary("1500000", 15) == 0

    def test_engine_accepts_a_config_instead_of_a_class(self):
        engine = ScoringEngine(salary_config=SalaryConfig(lakhs_multiplier=1_000.0))
        assert engine.score_salary("1500000", 15) == 0

    def test_bad_salary_type_raises_rather_than_scoring_zero(self):
        """A wrong type must not degrade into 'no salary data'."""
        with pytest.raises(TypeError, match="Salary subclass"):
            ScoringEngine(salary_cls=str)
        with pytest.raises(TypeError, match="Salary subclass"):
            ScoringEngine(salary_cls=object())

    def test_engine_uses_its_taxonomy(self):
        custom = SkillTaxonomy({"cobol": {"cobol85", "ibm cobol"}})
        engine = ScoringEngine(taxonomy=custom)
        assert engine.normalize_skill("IBM COBOL") == "cobol"
        # the shared default does not know that alias
        assert DEFAULT_TAXONOMY.normalize("IBM COBOL") == "ibm cobol"

    def test_fit_score_returns_the_typed_aggregate(self):
        engine = ScoringEngine()
        fit = engine.fit_score(
            job_skills={"react"}, profile_skills={"react"},
            job_exp_str="3-5 years", profile_exp="4 years",
        )
        assert isinstance(fit, FitScore)
        assert isinstance(fit.skill_match, SkillMatch)
        assert fit.to_dict() == engine.compute_fit_score(
            job_skills={"react"}, profile_skills={"react"},
            job_exp_str="3-5 years", profile_exp="4 years",
        )


class TestTaxonomyExtension:
    def test_extended_adds_without_mutating_the_original(self):
        before = DEFAULT_TAXONOMY.canonical_count
        extended = DEFAULT_TAXONOMY.extended({"cobol": {"cobol85"}})
        assert extended.normalize("cobol85") == "cobol"
        assert extended.canonical_count == before + 1
        assert DEFAULT_TAXONOMY.canonical_count == before
        assert DEFAULT_TAXONOMY.normalize("cobol85") == "cobol85"

    def test_extended_unions_aliases_for_an_existing_skill(self):
        extended = DEFAULT_TAXONOMY.extended({"react": {"react18"}})
        assert extended.normalize("react18") == "react"
        assert extended.normalize("reactjs") == "react"  # original alias survives
        assert extended.canonical_count == DEFAULT_TAXONOMY.canonical_count

    def test_unknown_skill_passes_through_never_dropped(self):
        """Dropping a skill would make a job look like a better match than it is."""
        parsed = DEFAULT_TAXONOMY.parse_set("react, some-bespoke-inhouse-thing")
        assert "some-bespoke-inhouse-thing" in parsed
        assert "react" in parsed


class TestSkillMatchEdges:
    def test_no_job_skills_scores_fifty_not_zero_and_not_hundred(self):
        m = SkillMatch.compute(set(), {"react"})
        assert m.score == 50.0

    def test_match_helper_agrees_with_skillmatch(self):
        job = frozenset({"react", "aws"})
        prof = frozenset({"react"})
        matched, missing = DEFAULT_TAXONOMY.match(job, prof)
        sm = SkillMatch.compute(set(job), set(prof))
        assert matched == sm.matched
        assert missing == sm.missing
