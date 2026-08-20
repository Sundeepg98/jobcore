"""Skill taxonomy — canonical skill names with alias normalisation.

Platform-agnostic. ``"react"`` means the same thing on Naukri, LinkedIn, Uplers
and every other board that exists, which is why this table is the one piece of
job-matching logic worth sharing rather than re-deriving per platform.

The table below is the extraction source of truth. It was lifted verbatim from
``naukri_server/domain/skill_taxonomy.py`` at commit 0021d82 (2026-08-20);
that module is now a re-export shim over this one.

Two kinds of variant, handled in two different places
-----------------------------------------------------
A board writes one skill many ways. Some of those ways are **semantic** —
``"aws"`` for Amazon Web Services, ``"k8s"`` for Kubernetes — and nothing but a
lookup table can know them, so they live in ``SKILL_ALIASES`` below.

The rest are **mechanical**: the same letters with different spacing,
punctuation or number. ``"postgre sql"``, ``"restapi"``, ``"Rest APIs;"``,
``"next js"``. Enumerating those one string at a time is a losing game — a real
235-requisition corpus produced a fresh batch of them — so ``normalize()``
derives them instead (see ``_derived_key``).

Deriving costs something, and the cost is **false merges**. Stripping every
non-alphanumeric character would turn both ``"c#"`` and ``"c++"`` into ``"c"``
and quietly declare two different languages the same skill. So the derivation
is narrow on purpose:

* only a fixed set of separator characters is removed, and ``+`` and ``#`` are
  never among them;
* a derived key that two canonical skills would both claim is **refused**, not
  guessed — it resolves to neither;
* exact lookup always wins, so nothing that resolved before can change;
* the plural/singular step applies only to words long enough for a trailing
  ``s`` to be an inflection rather than a coincidence (``"sas"`` is not the
  singular of ``"sass"``).

Every derived case is pinned in ``tests/test_normalisation.py`` alongside the
false merges it must not make.
"""

from __future__ import annotations

import unicodedata


# ── Skill Alias Map ──────────────────────────────────────────────────────────
# Canonical skill name -> set of known aliases.
# SkillTaxonomy normalizes all inputs through this map.

SKILL_ALIASES: dict[str, set[str]] = {
    "javascript": {"js", "vanilla js", "es6", "es2015", "ecmascript"},
    "typescript": {"ts"},
    "python": {"py", "python3"},
    "java": {"core java", "java8", "java11", "java17"},
    "c#": {"csharp", "c sharp"},
    "c++": {"cpp", "cplusplus"},
    "golang": {"go lang", "go"},
    "ruby": {"rb"},
    "rust": {"rustlang"},
    "react": {"reactjs", "react.js", "react js"},
    "angular": {"angularjs", "angular.js", "angular js"},
    "vue": {"vuejs", "vue.js", "vue js"},
    "next.js": {"nextjs", "next"},
    "node.js": {"nodejs", "node", "node js"},
    "express": {"expressjs", "express.js"},
    "django": {"django rest framework", "drf"},
    "flask": {"flask api"},
    "spring boot": {"springboot", "spring-boot", "spring"},
    "fastapi": {"fast api"},
    ".net": {"dotnet", "dot net", "asp.net"},
    "kubernetes": {"k8s", "k8"},
    "docker": {"containerization", "containers"},
    "terraform": {"tf", "iac", "infrastructure as code"},
    "ansible": {"configuration management"},
    "jenkins": {"ci server"},
    "postgresql": {"postgres", "psql", "pgsql"},
    "mongodb": {"mongo", "mongo db"},
    "mysql": {"my sql"},
    "redis": {"redis cache"},
    "elasticsearch": {"elastic", "elk", "elastic search"},
    "apache kafka": {"kafka"},
    "rabbitmq": {"rabbit mq", "amqp"},
    "amazon web services": {"aws"},
    "microsoft azure": {"azure"},
    "google cloud platform": {"gcp", "google cloud"},
    "ci/cd": {"cicd", "ci cd", "continuous integration", "continuous deployment"},
    "rest api": {"rest", "restful", "restful api", "rest apis"},
    "graphql": {"graph ql"},
    "machine learning": {"ml"},
    "deep learning": {"dl"},
    "natural language processing": {"nlp"},
    "computer vision": {"cv"},
    "artificial intelligence": {"ai"},
    "data science": {"data analytics"},
    "microservices": {"micro services", "micro-services"},
    "devops": {"dev ops", "dev-ops"},
    "agile": {"scrum", "kanban"},
    "html": {"html5"},
    "css": {"css3", "scss", "sass", "less"},
    "linux": {"unix", "ubuntu", "centos", "rhel"},
    "git": {"version control"},
    "sql": {"structured query language"},
    "nosql": {"no sql", "non-relational"},
    "power bi": {"powerbi"},
    "tableau": {"data visualization"},
    "apache spark": {"spark", "pyspark"},
    "hadoop": {"hdfs", "mapreduce"},
    "snowflake": {"snowflake db"},
    "nestjs": {"nest", "nest.js", "nest js"},
    "react native": {"reactnative", "react-native"},
    "maven": {"apache maven"},
    "gradle": {"gradle build"},
    "pytest": {"py.test"},
    "junit": {"junit5", "junit4"},
    "k3s": {"k3"},
    # Mobile
    "kotlin": {"kt"},
    "swift": {"swiftui"},
    "flutter": {"dart"},
    # ML/AI
    "tensorflow": {"keras"},
    "pytorch": {"torch"},
    "scikit-learn": {"sklearn"},
    # Testing
    "selenium": {"webdriver"},
    "cypress": {"cypress.io"},
    "playwright": {"pw"},
    # Data
    "airflow": {"apache airflow"},
    "dbt": {"data build tool"},
    # Frontend
    "svelte": {"sveltekit"},
    "nuxt": {"nuxtjs", "nuxt.js"},
    "remix": {"remix.run"},
    # BaaS/Cloud
    "supabase": {"supa"},
    "firebase": {"firestore"},
    # Observability
    "datadog": {"dd"},
    "grafana": {"grafana cloud"},
    "prometheus": {"prom"},
    "splunk": {"splunk cloud"},
    # AWS Messaging
    "sqs": {"amazon sqs", "aws sqs"},
    "sns": {"amazon sns", "aws sns"},
    # Design
    "figma": {"figma design"},
}


# Misspellings seen in live job posts. A typo cannot be derived — no rule turns
# "kubernates" into "kubernetes" without also turning real words into each
# other — so these are data, and each one earns its place by having been
# observed on a real requisition rather than imagined.
#
# Receipts: all five appear in the 235 native Uplers requisitions captured
# 2026-08-20 ("kubernates" 1, "kubernets" 1, "typescrpit" 1, "googel cloud" 1,
# "contenarization" 1).
CORPUS_MISSPELLINGS: dict[str, set[str]] = {
    "kubernetes": {"kubernates", "kubernets"},
    "typescript": {"typescrpit"},
    "google cloud platform": {"googel cloud"},
    "docker": {"contenarization"},
}


def _merge_misspellings() -> None:
    """Fold ``CORPUS_MISSPELLINGS`` into the public table, additively."""
    for canonical, variants in CORPUS_MISSPELLINGS.items():
        SKILL_ALIASES[canonical] = SKILL_ALIASES.get(canonical, set()) | variants


_merge_misspellings()


# ── Surface hygiene ──────────────────────────────────

# Characters a word processor substitutes for the plain ASCII ones. None of them
# is ever part of a skill NAME -- they are artefacts of where the text was
# typed, and they arrive whenever a human drafts a search string in Word or
# Google Docs and pastes it into a board. In one live capture of 704 recruiter
# keywords, 20 carried one of these; each became its own taxonomy entry, so the
# same skill was counted twice and the decorated copy matched nothing.
_QUOTE_CHARS = "“”‘’„‚′″«»\""
_DASH_CHARS = "‐‑‒–—―−"

_SURFACE_TABLE: dict[int, object] = {ord(ch): None for ch in _QUOTE_CHARS}
# Dashes FOLD onto the ASCII hyphen rather than being deleted: "-" is already a
# separator for derived lookup, so "type-script" and the prettified "type–script"
# become the same string for free. Deleting them outright would also silently
# join words that nobody joined.
_SURFACE_TABLE.update({ord(ch): "-" for ch in _DASH_CHARS})


def _clean_surface(skill: str) -> str:
    """Lowercase a raw skill string and drop the artefacts of human typing.

    Runs BEFORE every lookup, so it must be a no-op on anything already in the
    table -- and it is: NFKC is the identity on ASCII, no table entry contains a
    quote or a non-ASCII dash, and collapsing whitespace runs cannot change a
    string that has none. ``test_cleaning_is_a_no_op_for_every_string_already_in_the_table``
    asserts exactly that over the real table rather than a sample, because the
    failure mode here is silent: a cleaner that altered a known alias would stop
    it resolving for some skills only.

    NFKC also folds U+00A0 NO-BREAK SPACE to an ordinary space, which
    ``str.strip()`` does not -- two of those 704 live keywords had every space
    replaced by one, so they could never match anything.
    """
    text = unicodedata.normalize("NFKC", skill)
    text = text.translate(_SURFACE_TABLE)
    return " ".join(text.split()).lower()


# ── Mechanical variant derivation ────────────────────────────────────────────

# Characters dropped before the fallback ("derived") lookup. Spelled out rather
# than expressed as "everything that is not alphanumeric", because that rule
# collapses BOTH "c#" and "c++" onto "c". "+" and "#" are load-bearing.
_SEPARATORS = " \t\r\n.-_/\\;:,'`"
_SEPARATOR_TABLE = {ord(ch): None for ch in _SEPARATORS}

# A trailing "s" is an inflection on a long word and a coincidence on a short
# one. "microservice"/"microservices" is one skill; "sas"/"sass" is SAS the
# analytics language and Sass the stylesheet syntax, and "cvs"/"cv" is a version
# control system and computer vision. Six characters is where the corpus stops
# producing accidents.
INFLECTION_MIN_LENGTH = 6


def _derived_key(surface: str) -> str:
    """The separator-free form of a skill string, used only as a fallback."""
    return surface.translate(_SEPARATOR_TABLE)


class SkillTaxonomy:
    """Domain object for skill normalization.

    88 canonical skills, 155 aliases, case-insensitive normalization, plus
    derived handling of mechanical variants (spacing, separators, plural).

    An unknown skill is NOT dropped — it is returned lowercased and stripped.
    Losing a skill silently would make a job look like a better match than it
    is, so the taxonomy is additive-only by design.

    Lookup happens in three passes, and the order is the safety property:
    an **exact** table hit always wins, so no string that resolved before this
    class learned to derive anything can resolve differently now. Derivation
    only ever converts a previously-unresolved string into a canonical one.
    """

    def __init__(self, aliases: dict[str, set[str]]):
        self._aliases = aliases
        self._lookup: dict[str, str] = {}
        for canonical, alias_set in aliases.items():
            self._lookup[canonical] = canonical
            for alias in alias_set:
                self._lookup[alias] = canonical

        # Derived index. A key two different canonicals would both claim is
        # REFUSED rather than awarded to whichever was inserted last — a
        # coin-flip merge of two real skills is worse than no merge at all.
        self._derived: dict[str, str] = {}
        ambiguous: set[str] = set()
        for surface, canonical in self._lookup.items():
            key = _derived_key(surface)
            if not key:
                continue
            claimed = self._derived.get(key)
            if claimed is None:
                self._derived[key] = canonical
            elif claimed != canonical:
                ambiguous.add(key)
        for key in ambiguous:
            del self._derived[key]
        self._ambiguous = frozenset(ambiguous)

    def normalize(self, skill: str) -> str:
        """Normalize a skill string to its canonical form.

        Unknown input is returned lowercased and stripped, never dropped.
        """
        raw = _clean_surface(skill)

        canonical = self._lookup.get(raw)
        if canonical is not None:
            return canonical

        key = _derived_key(raw)
        if key:
            canonical = self._derived.get(key)
            if canonical is not None:
                return canonical
            if len(key) >= INFLECTION_MIN_LENGTH:
                inflected = key[:-1] if key.endswith("s") else key + "s"
                canonical = self._derived.get(inflected)
                if canonical is not None:
                    return canonical

        return raw

    def parse_set(self, raw) -> frozenset[str]:
        """Parse skills from any format (set/str/list/tuple) to normalized frozenset."""
        if isinstance(raw, set):
            items = {s for s in raw if s}
        elif isinstance(raw, str):
            items = {s.strip() for s in raw.split(",") if s.strip()}
        elif isinstance(raw, (list, tuple)):
            items = {s.strip() for s in raw if isinstance(s, str) and s.strip()}
        else:
            return frozenset()
        return frozenset(self.normalize(s) for s in items)

    def match(self, job_skills: frozenset[str], profile_skills: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
        """Return (matched, missing) skills."""
        matched = job_skills & profile_skills
        missing = job_skills - profile_skills
        return matched, missing

    def extended(self, extra_aliases: dict[str, set[str]]) -> "SkillTaxonomy":
        """Return a NEW taxonomy with *extra_aliases* merged in.

        The seam for platform-specific vocabulary: a board with its own skill
        names gets its own taxonomy without mutating the shared one. Aliases for
        an existing canonical skill are unioned, not replaced.
        """
        merged = {k: set(v) for k, v in self._aliases.items()}
        for canonical, aliases in extra_aliases.items():
            merged.setdefault(canonical, set()).update(aliases)
        return SkillTaxonomy(merged)

    @property
    def canonical_count(self) -> int:
        return len(self._aliases)

    @property
    def alias_count(self) -> int:
        """Explicit aliases only. Derived forms are computed, never stored as
        vocabulary — counting them would inflate the number that documents how
        much this table actually knows."""
        return sum(len(v) for v in self._aliases.values())

    @property
    def ambiguous_derived_keys(self) -> frozenset[str]:
        """Derived forms two canonicals would both claim, and which therefore
        resolve to neither. Empty on the shipped table; non-empty is a signal
        that a newly added skill collides with an existing one."""
        return self._ambiguous


# Module-level singleton
DEFAULT_TAXONOMY = SkillTaxonomy(SKILL_ALIASES)
