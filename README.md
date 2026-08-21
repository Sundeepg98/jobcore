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
| `ScoringEngine` | binds a taxonomy + salary type + policy and gives you the lot |
| `ScoringPolicy` / `Policy` | the numbers above as **values**, with defaults equal to the literals they replaced |
| `jobcore.config` | the file loader — discovery, content-addressed reload, tiered writes. **Never on the scoring path.** |

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

## Policy: the numbers are values, not literals

The weights, bonuses, penalties and verdict bands were constants welded into
`fit.py`, `salary.py` and `scoring.py`. They are now a `ScoringPolicy`, and it
is **injected** — never read from a file by this package.

```python
from jobcore import ScoringEngine, ScoringPolicy, Weights

engine = ScoringEngine(policy=ScoringPolicy(weights=Weights(0.75, 0.25)))
engine.compute_fit_score(..., explain=True)["explain"]
# {"weights": {...}, "base": {...}, "bonuses": {...}, "verdict_band": {...}}
```

**Every default equals the literal it replaced**, so a caller that passes
nothing gets today's number, byte for byte — which is what lets the 179 golden
parity cases pass untouched.

`policy=` is a parameter of `FitScore.compute`, of `SkillMatch`,
`ExperienceScore` and `BonusScore`, and of the flat module functions — not only
of `ScoringEngine`. That is deliberate: three of naukri's four scoring call
sites build `FitScore` directly and never touch an engine, and one consumer
(instahyre) imports the flat `compute_fit_score` and never builds one either.
An engine-only seam would have produced a split-brain in which the daily brief
honoured his weights and the agent's own scorer did not.

A result stamps itself with a `policy_hash` whenever the policy is not the
shipped default — so two scores are comparable exactly when their hashes match,
and you can tell.

## Reading the file: `jobcore.config`

A separate module, imported by the *server*, never by the scoring path:

```python
from jobcore import ScoringEngine
from jobcore import config

loaded = config.current(start=__file__)       # pass YOUR path, not jobcore's
engine = ScoringEngine(policy=loaded.scoring, candidate=loaded.candidate)
```

`python -m jobcore.config` prints where the file was found — or every path it
tried when it was not — plus the stamp and anything it refused.

Four decisions worth knowing before changing it:

- **Reload triggers on content, not mtime.** Measured on the target NTFS
  volume, 12 back-to-back atomic replaces produced 8 distinct
  `(mtime_ns, size)` pairs, and the common edits (`0.6`→`0.8`, `15`→`25`)
  preserve byte length. The file is read and hashed every call; parsed only on
  change.
- **A hand edit is detected, not prevented.** A text editor takes no lock and
  honours no compare-and-swap. The loader compares the observed fingerprint
  against the ledger tail, writes a history row for anything new, and reports
  a `revision` that went backwards as a `revision_regression`.
- **`policy_rev` is content-derived.** The file's `revision` integer is the
  compare-and-swap token; what gets stamped is a number the loader maintains.
- **`locate(start)` takes the CALLER's path.** jobcore cannot know who
  imported it, and walking up from its own `__file__` works under an editable
  install and silently finds nothing under a normal one.

No file anywhere → built-in defaults → today's behaviour. That is the
independence guarantee, and `test_independence.py` enforces it.

## The invariant, and the three trust tiers

> **No sequence of config writes, from any server, may grant autonomous apply
> authority.**

Every key carries its tier as data (`jobcore.policy.SCHEMA`), and the tier is
derived from what the key **gates in the call graph**, never from what it is
called.

| Tier | Rule |
|---|---|
| **A** | free. Changes what he *sees*; reversible, visible, no outward effect. |
| **B** | one-way ratchet. Tightening is free; loosening needs an explicit `confirm_widen` **and** must land under a ceiling that lives in Python. The file can never raise the ceiling. |
| **C** | **not loadable from the file at all.** The file may display the value; a differing one is refused loudly and the Python value is used. A write is refused by name. |

Tier C holds `agent.enabled`, `agent.mode`, `min_fit_score` (wherever it
appears — it is the autonomous-apply *selector*, not a display filter),
`agent.searches`, `blocklist.enabled`, and **anything else under an `agent`
subtree that the schema does not explicitly name** — deny by default, because
the escalation the tier table exists to stop opened through two keys that had
no tier at all.

`false → true` and `dry_run → auto` have no "tighter" direction, so they cannot
be ratchets. Env plus a restart is the right friction, for the same reason it
is right for a kill switch: the party asking to widen the guard is the agent
whose behaviour it bounds.

Two levers reach the same selector without touching the agent block —
inflating `candidate.skills` until every job scores 100, and collapsing
`scoring.weights` onto whichever component a job maxes out. Neither can be
Tier C (they are the point of the feature). They are bounded instead, by
`HARD_LIMITS` and by `requires_approval_cycle`: any cycle that observes a
scoring fingerprint it has not seen runs in approval mode regardless of the
configured mode.

`test_safety_invariant.py` does not assert that the guards exist — it runs the
attack. Both traced paths, all six writes, plus a hand-edited file carrying the
whole escalation. Every guarded assertion has a **control** that runs the same
attack against a permissive build and asserts it *succeeds*; without that, a
refusal could be a typo rather than a guard.

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

554 tests, ~2s, no network.

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
- **`test_policy.py`** — every default is asserted to equal today's literal, and
  the schema's declared default is compared against the dataclass's so the two
  can never drift apart silently.
- **`test_policy_effects.py`** — a knob that is read and ignored is the decoy
  class this design exists to kill, so every knob is asserted to move a score
  by the arithmetically predicted amount. It also pins the property the golden
  corpus structurally cannot see: **under a non-default policy, every entry
  point returns the same number** — engine, flat API, and a direct
  `FitScore.compute`, which is how three of naukri's four call sites score.
- **`test_config.py`** — discovery, the content-hash reload (with the mtime
  collision forced deterministically via `os.utime`, because it is real and
  intermittent in the wild), deep merge against the planted `dict.update`
  partial-reset bug, compare-and-swap, the PID+liveness lock, and hand-edit
  detection.
- **`test_safety_invariant.py`** — see below.

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
