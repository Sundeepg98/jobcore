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
| `SkillTaxonomy` / `SKILL_ALIASES` | **88 canonical skills, 155 aliases**, plus derived variants. `"react"` ← `reactjs`, `react.js`, `react js`, `react-js`. The crown jewel. |
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

## Two kinds of variant, and only one of them is a table

A board writes one skill many ways. Some of those ways are **semantic** —
`"aws"` for Amazon Web Services, `"k8s"` for Kubernetes — and nothing but a
lookup table can know them. Those are `SKILL_ALIASES`.

The rest are **mechanical**: the same letters with different spacing,
punctuation or number.

```python
normalize_skill("restapi")       # "rest api"     concatenation
normalize_skill("postgre sql")   # "postgresql"   spurious space
normalize_skill("Rest APIs;")    # "rest api"     trailing punctuation
normalize_skill("microservice")  # "microservices" singular
normalize_skill("ci-cd")         # "ci/cd"        different separator
```

Enumerating those one string at a time is a losing game — a real
235-requisition corpus produced a fresh batch of them, and the next board will
produce another — so `normalize()` **derives** them: it retries the lookup with
separator characters removed, then once more with a trailing `s` added or
dropped.

Deriving costs something, and the cost is **false merges**. The obvious rule —
strip everything that is not alphanumeric — turns both `"c#"` and `"c++"` into
`"c"` and declares two different languages the same skill. So the derivation is
narrow on purpose:

- only a fixed separator set is removed (`` .-_/\;:,'` `` and whitespace);
  `+` and `#` are never among them;
- a derived key that two canonical skills would both claim is **refused**, not
  awarded to whichever was inserted last — it resolves to neither, and
  `DEFAULT_TAXONOMY.ambiguous_derived_keys` names any such case (empty on the
  shipped table);
- **exact lookup always wins**, so derivation can only ever turn a
  previously-unresolved string into a canonical one. It cannot change an answer
  the table already had. This is why the 179-input parity corpus still passes
  byte-for-byte;
- the plural step applies only from six characters up, because a trailing `s`
  is an inflection on a long word and a coincidence on a short one. `"sas"` is
  not the singular of `"sass"`, and `"cvs"` is version control, not computer
  vision.

`ms sql` stays distinct from `mysql`, and `github` from `git`. Those, and the
`c#`/`c++` case, are pinned in `tests/test_normalisation.py`.

Misspellings cannot be derived — no rule turns `"kubernates"` into
`"kubernetes"` without also turning real words into each other — so they stay
data, in `CORPUS_MISSPELLINGS`. Each one earns its place by having been seen on
a live requisition.

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

269 tests, ~2s, no network.

- **`test_parity_golden.py`** — `golden_scores.json` holds 179 inputs plus the
  full 88/150 alias table, captured by running Naukri's **pristine** scoring
  modules before the extraction. If any number or string drifts, these fail.
  Regenerating the corpus is not a way to make a failure go away. The captured
  alias table is asserted as a **floor**, not a snapshot: an alias removed,
  re-pointed or renamed still fails entry by entry, while a new alias is
  allowed only if it is declared in `CORPUS_MISSPELLINGS` — so undeclared
  growth fails too.
- **`test_normalisation.py`** — the mechanical variants above, every case taken
  from a live board rather than invented, sitting next to the false merges the
  derivation must not make.
- **`test_independence.py`** — AST-walks every source file and fails if a
  platform package or any third-party import appears; boots a clean subprocess
  interpreter and scores a job in it.
- **`test_engine.py`** — the behaviour that is new: injected units, injected
  taxonomy, and the loud-failure guarantees above.

Every one of these has been shown failing, which is the only reason to trust
them when they are green: a mutated weighting and a single removed alias both
go red; the variant tests were red on 23 of 47 cases before the derivation
landed; and the taxonomy floor was smoked against five separate mutations
(alias removed, alias re-pointed, canonical renamed, undeclared alias added,
declared alias never applied) and went red on all five.

## Consumers

| Server | How it consumes jobcore |
|---|---|
| `mcp-servers/naukri` | `naukri_server/scoring.py`, `domain/fit_score.py`, `domain/skill_taxonomy.py` and `domain/salary.py` are thin re-export shims. `Salary` is subclassed to bind `naukri_server.config.LAKHS_MULTIPLIER`. |
| `mcp-servers/uplers` | Installed and ready; scoring not yet wired into its tools. |

**Editing jobcore changes what a live job server scores.** Run both suites.
