"""Salary value object — parsing, normalisation, and market positioning.

Platform-agnostic. The one thing that is NOT universal — how many rupees make
a lakh, and above what figure a number must be raw currency rather than lakhs —
is pushed into :class:`SalaryConfig` and injected, so no consumer of this
library has to import another server's config module to get a salary parsed.

Extracted from ``naukri_server/domain/salary.py`` at commit 0021d82; that
module is now a shim binding ``SalaryConfig`` to Naukri's ``LAKHS_MULTIPLIER``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Optional

from .policy import DEFAULT_SCORING_POLICY, ScoringPolicy


@dataclass(frozen=True)
class SalaryConfig:
    """Unit conventions for salary parsing.

    lakhs_multiplier:
        Rupees per lakh. 1 lakh = 100,000.
    raw_amount_threshold:
        A figure above this cannot plausibly be "lakhs per annum", so it is
        treated as raw currency and divided by *lakhs_multiplier*. It is also
        the sanity ceiling for CTC comparison — a max above it scores 0 rather
        than pretending a 1,500-lakh package is a great match.
    """

    lakhs_multiplier: float = 100_000.0
    raw_amount_threshold: float = 200.0


DEFAULT_SALARY_CONFIG = SalaryConfig()

# Scoring policy. These ARE judgement calls — which is why they moved into
# :mod:`jobcore.policy` and are injected per call. The module constants stay as
# the documented defaults and as the values ``DEFAULT_SCORING_POLICY`` carries;
# they are what every unmigrated caller still gets.
_MEETS_EXPECTATION_RATIO = 0.8   # within 20% of expected CTC still scores
_BELOW_MARKET_RATIO = 0.85
_ABOVE_MARKET_RATIO = 1.15


@dataclass(frozen=True)
class Salary:
    """Immutable value object for salary in lakhs per annum.

    Encapsulates:
    - String parsing with unit detection (> raw_amount_threshold = raw currency)
    - Market positioning (below/at/above)
    - CTC comparison scoring

    To bind a different unit convention, subclass and override ``CONFIG``::

        class MySalary(Salary):
            CONFIG = SalaryConfig(lakhs_multiplier=100_000)

    ``from_string`` also accepts a per-call ``config=`` override.
    """

    min_lakhs: float | None
    max_lakhs: float | None
    raw: str

    CONFIG: ClassVar[SalaryConfig] = DEFAULT_SALARY_CONFIG

    @classmethod
    def from_string(cls, salary_str: str, config: SalaryConfig | None = None) -> "Salary":
        """Parse salary strings like '10-15 Lacs', 'Not disclosed'.

        Unit detection rule: if any value > config.raw_amount_threshold,
        divide by config.lakhs_multiplier.

        An unparseable string yields an *undisclosed* Salary (min/max None) with
        ``raw`` preserved — never a zero, which would silently read as a real
        offer of nothing. Callers must check :attr:`is_disclosed`.
        """
        cfg = config or cls.CONFIG
        if not salary_str or not isinstance(salary_str, str):
            return cls(None, None, salary_str or "")
        s = salary_str.lower().strip()
        if "not disclosed" in s or "confidential" in s:
            return cls(None, None, salary_str)
        nums = re.findall(r'(\d+(?:\.\d+)?)', s.replace(",", ""))
        if not nums:
            return cls(None, None, salary_str)
        vals = [float(n) for n in nums[:2]]
        if any(v > cfg.raw_amount_threshold for v in vals):
            vals = [v / cfg.lakhs_multiplier for v in vals]
        if len(vals) >= 2:
            return cls(round(vals[0], 1), round(vals[1], 1), salary_str)
        return cls(round(vals[0], 1), round(vals[0], 1), salary_str)

    @property
    def is_disclosed(self) -> bool:
        return self.min_lakhs is not None

    @property
    def midpoint(self) -> float | None:
        if self.min_lakhs is not None and self.max_lakhs is not None:
            return (self.min_lakhs + self.max_lakhs) / 2
        return None

    def compare_to_ctc(self, expected_ctc,
                       policy: Optional[ScoringPolicy] = None) -> int:
        """Score salary fit against expected CTC. Returns 0, 3, or 5 by default.

        5 = meets expectation, 3 = within 20%, 0 = below threshold.

        ``expected_ctc`` MUST be denominated the same way this Salary type is.
        That is why ``candidate.pay`` in the config schema is split per unit
        system rather than carrying one scalar and a tag: a 24-lakh figure
        compared against a $150,000 job clears everything, and a $20,959
        figure compared against a 25-lakh job clears nothing — and both look
        exactly like "no salary data", which is the failure this class's
        ``is_disclosed`` contract exists to make impossible.

        The sanity ceiling defaults to this type's ``raw_amount_threshold``,
        which is what the code has always done — uplers deliberately binds
        that to 10,000,000 for USD/year, so a concrete default in the policy
        would silently re-impose a 200-lakh ceiling on a dollar board.
        """
        pol = policy or DEFAULT_SCORING_POLICY
        # Defensive: ensure float (callers may pass string like "25")
        try:
            expected_ctc = float(expected_ctc)
        except (ValueError, TypeError):
            return 0
        ceiling = pol.salary.ceiling_for(self.CONFIG.raw_amount_threshold)
        if self.max_lakhs is None or self.max_lakhs > ceiling:
            return 0
        if self.max_lakhs >= expected_ctc:
            return pol.bonuses.salary_meets
        if self.max_lakhs >= expected_ctc * pol.salary.meets_expectation_ratio:
            return pol.bonuses.salary_near
        return 0

    def market_position(self, market_avg: float,
                        policy: Optional[ScoringPolicy] = None) -> str:
        """Categorize as below/at/above market.

        Returns ``"unknown"`` — never a guess — when the salary is undisclosed.
        """
        pol = policy or DEFAULT_SCORING_POLICY
        if self.midpoint is None:
            return "unknown"
        if self.midpoint < market_avg * pol.salary.below_market_ratio:
            return "below"
        if self.midpoint > market_avg * pol.salary.above_market_ratio:
            return "above"
        return "at_market"
