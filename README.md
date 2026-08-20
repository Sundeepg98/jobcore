# jobcore

The job/profile scoring engine, with no job board in it.

Everything here is true on **every** board that exists: what `"react"` is a name
for, how a 3–5 year requirement scores against a 7-year candidate, and what a
salary string means once you know how many rupees make a lakh. Nothing here
knows about a specific site — no HTTP, no browser, no database, no config module
import, and **zero runtime dependencies**.

It was extracted from the Naukri MCP server, which now consumes it through
re-export shims rather than owning a private copy.

---

## Install

```bash
pip install -e ../jobcore      # from a sibling MCP server's venv
```

Python ≥ 3.10.

## Use

```python
from jobcore import compute_fit_score, parse_skills

result = compute_fit_score(
    job_skills=parse_skills("React, Node.js, AWS"),
    profile_skills=parse_skills(["reactjs", "nodejs", "typescript"]),
    job_exp_str="3-5 years",
    profile_exp="4 years 0 months",
)
result["overall_score"]     # 80
result["recommendation"]    # "Strong match — apply confidently"
result["skill_match"]
# {"score": 67, "matched": ["node.js", "react"], "missing": ["amazon web services"]}
```

Note `"AWS"` came back as `"amazon web services"` and `"reactjs"` matched
`"React"` — that is the taxonomy doing its job.

The dict is deliberately small and flat — it is designed to be an MCP tool
result, where every key costs tokens on every call.

Want the typed object instead of the dict:

```python
from jobcore import ScoringEngine

fit = ScoringEngine().fit_score(...)     # -> FitScore
fit.skill_match.missing                  # frozenset({"amazon web services"})
fit.overall_score                        # 80
```

## What's in it

| Piece | What it is |
|---|---|
| `SkillTaxonomy` / `SKILL_ALIASES` | **88 canonical skills, 150 aliases.** `"react"` ← `reactjs`, `react.js`, `react js`. The crown jewel. |
| `SkillMatch` | job ∩ profile, and what's missing |
| `ExperienceScore` | sqrt over-qualification penalty (floor 60), linear −20/yr under-qualification |
| `BonusScore` | additive +5 location, +5 remote, +5 salary fit, +5 agent-eligible |
| `FitScore` | the aggregate: 60% skills + 40% experience + bonuses, capped at 100 |
| `Salary` / `SalaryConfig` | parsing, unit detection, market position, CTC comparison |
| `ScoringEngine` | binds a taxonomy + salary type and gives you the lot |

## Configuration, not imports

The only thing that is *not* universal is currency units, so they are injected
rather than imported from somebody's `config.py`:

```python
from jobcore import ScoringEngine, SalaryConfig

engine = ScoringEngine(salary_config=SalaryConfig(lakhs_multiplier=100_000))
```

Same for vocabulary — a board with its own skill names extends the shared table
without mutating it:

```python
from jobcore import DEFAULT_TAXONOMY

mine = DEFAULT_TAXONOMY.extended({"cobol": {"cobol85", "ibm cobol"}})
```

## Empty is never the same as broken

An unparseable salary returns an **undisclosed** `Salary` — `min_lakhs is None`,
`is_disclosed is False`, `raw` preserved — never `0.0`, which would read as a
genuine offer of nothing. An unknown skill is passed through lowercased, never
dropped, because dropping it would make a job look like a *better* match than it
is. A wrong `salary_cls` raises `TypeError` rather than quietly scoring every
job 0 on the salary bonus.

## Tests

```bash
pytest
```

221 tests, ~2s, no network.

- **`test_parity_golden.py`** — `golden_scores.json` holds 179 inputs plus the
  full 88/150 alias table, captured by running Naukri's **pristine** scoring
  modules before the extraction. If any number or string drifts, these fail.
  Regenerating the corpus is not a way to make a failure go away.
- **`test_independence.py`** — AST-walks every source file and fails if a
  platform package or any third-party import appears; boots a clean subprocess
  interpreter and scores a job in it.
- **`test_engine.py`** — the behaviour that is new: injected units, injected
  taxonomy, and the loud-failure guarantees above.

Both the parity and independence checks have been shown failing (a mutated
weighting and a single removed alias both go red), which is the only reason to
trust them when they are green.

## Consumers

| Server | How it consumes jobcore |
|---|---|
| `mcp-servers/naukri` | `naukri_server/scoring.py`, `domain/fit_score.py`, `domain/skill_taxonomy.py` and `domain/salary.py` are thin re-export shims. `Salary` is subclassed to bind `naukri_server.config.LAKHS_MULTIPLIER`. |
| `mcp-servers/uplers` | Installed and ready; scoring not yet wired into its tools. |

**Editing jobcore changes what a live job server scores.** Run both suites.
