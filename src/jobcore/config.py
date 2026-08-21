"""The config file loader — discovery, content-addressed reload, guarded writes.

**This module is never imported by the scoring path.** :mod:`jobcore.fit`,
:mod:`jobcore.salary` and :mod:`jobcore.scoring` import :mod:`jobcore.policy`
and nothing else; a server imports *this*, gets a :class:`~jobcore.policy.Policy`,
and injects it. That separation is not aesthetic — ``test_independence.py``
runs a clean interpreter with cwd elsewhere and asserts a score of exactly
``100``. A scoring path that auto-discovered a file would make that number
machine-dependent and jobcore would stop being a library.

Four things here are deliberately not what the first design said, because each
was measured or traced to be wrong:

**Reload triggers on CONTENT, never on mtime.** Twelve back-to-back atomic
replaces on this NTFS volume produced only 8 distinct ``(mtime_ns, size)``
pairs — four consecutive writes with a delta of exactly zero — and the common
edits (``0.6`` -> ``0.8``, ``15`` -> ``25``, ``"revision": 7`` -> ``8``)
preserve byte length. A stat-only discriminator holds a stale snapshot
indefinitely while reporting its stale revision as current. The file is a few
kilobytes; it is read and hashed every call, and parsed only on change.

**A hand edit is DETECTED, not prevented.** Notepad takes no lock and honours
no compare-and-swap, and "open it in Notepad" is the workflow this whole thing
exists to serve. So the loader compares the observed fingerprint against the
tail of the ledger: a fingerprint it has not seen becomes a first-class
history row with ``source: "external_edit"``, and a ``revision`` that went
backwards becomes a visible ``revision_regression`` rather than a silent lost
update.

**``policy_rev`` is content-derived.** The file's ``revision`` integer stays
purely the compare-and-swap token for agent writes. What gets stamped on a
score is a number the LOADER maintains, incremented whenever it observes a
scoring fingerprint that is not already the tail of the ledger — so "the score
was 80 yesterday and 72 today" resolves to two rows and one diff even when
nothing ever called :func:`apply_patch`.

**The lock is PID + liveness, not a wall clock.** Copied in design from
``naukri_server/profile_lock.py``, which is already proven in this tree. A
ten-second staleness rule on a box where a virus scan can stall a write lets a
second writer steal the lock mid-write.

Tier enforcement lives here and is described in :mod:`jobcore.policy`. The one
rule that governs everything else:

    **No sequence of config writes, from any server, may grant autonomous
    apply authority.**

Tier C is therefore not "refused on write" — it is **not loadable at all**. A
Tier C key present in the file with a value differing from the Python one is
refused loudly (logged at ERROR, reported in ``tier_c_refusals``) and the
Python value is used. The file may display it; the file may not decide it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

from .policy import (
    DEFAULT_POLICY,
    HARD_LIMITS,
    KeySpec,
    Policy,
    PolicyError,
    TIER_B,
    TIER_C,
    canonical_json,
    iter_specs,
    schema_defaults,
    spec_for,
)

__all__ = [
    "ENV_CONFIG",
    "ENV_HOME",
    "ENV_DISABLE",
    "DISABLE_TOKENS",
    "CONFIG_FILENAME",
    "LEDGER_FILENAME",
    "ROOT_MARKER",
    "Location",
    "Loaded",
    "ConfigLockedError",
    "locate",
    "current",
    "invalidate_cache",
    "apply_patch",
    "deep_merge",
    "default_document",
    "flatten",
]

logger = logging.getLogger("jobcore.config")

ENV_CONFIG = "JOBHUNT_CONFIG"
ENV_HOME = "JOBHUNT_HOME"
ENV_DISABLE = "JOBHUNT_DISABLE"

#: Explicit tokens meaning "built-in defaults only; do not search". An EMPTY
#: ``JOBHUNT_CONFIG`` means *unset*, never *disabled*: CI runners, shell
#: scripts and ``env`` blocks produce an empty value by accident all the time,
#: and one stray ``JOBHUNT_CONFIG=`` silently running every server on defaults
#: is the worst possible failure for a config system.
DISABLE_TOKENS = frozenset({":none:", ":default:", ":builtin:", ":disabled:"})

CONFIG_FILENAME = "jobhunt.json"
LEDGER_FILENAME = "policy_history.jsonl"
ROOT_MARKER = ".jobhunt-root"
MAX_WALK_UP = 6

#: Sections a patch may name. ``servers.<name>`` is scoped to that server.
SHARED_SECTIONS = ("candidate", "scoring")


# ── Discovery ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Location:
    """Where the config file is, and every path that was tried getting there."""

    path: Optional[Path]
    searched: tuple[str, ...]
    reason: str

    @property
    def found(self) -> bool:
        return self.path is not None


def _start_dir(start: Optional[Path]) -> Path:
    """Where the walk-up begins.

    The caller passes its own ``Path(__file__)``. jobcore cannot know who
    imported it, and walking up from jobcore's own location only works by
    luck: it is true under an editable install and false under a normal
    ``pip install``, where the walk finds nothing and every server silently
    runs on defaults.
    """
    if start is None:
        return Path(__file__).resolve().parent
    p = Path(start).resolve()
    return p.parent if p.is_file() else p


def locate(start: Optional[Path] = None, env: Optional[Mapping[str, str]] = None) -> Location:
    """Find the config file. Order: env path, env home, walk up, user home.

    Args:
        start: the CALLER's ``__file__`` (or a directory). See :func:`_start_dir`.
        env: environment mapping; defaults to ``os.environ``.
    """
    env = os.environ if env is None else env
    searched: list[str] = []

    if str(env.get(ENV_DISABLE, "")).strip().lower() in ("1", "true", "yes"):
        return Location(None, (), f"{ENV_DISABLE} is set — built-in defaults only")

    raw = env.get(ENV_CONFIG)
    if raw is not None:
        token = raw.strip()
        if token.lower() in DISABLE_TOKENS:
            return Location(None, (), f"{ENV_CONFIG}={token} — built-in defaults only")
        if token:
            p = Path(token).expanduser()
            searched.append(str(p))
            if p.is_file():
                return Location(p.resolve(), tuple(searched), f"{ENV_CONFIG}")
            return Location(
                None, tuple(searched),
                f"{ENV_CONFIG}={token} points at no file",
            )
        # Empty value: treat as UNSET and keep searching (see DISABLE_TOKENS).

    home = env.get(ENV_HOME)
    if home and home.strip():
        p = Path(home.strip()).expanduser() / CONFIG_FILENAME
        searched.append(str(p))
        if p.is_file():
            return Location(p.resolve(), tuple(searched), ENV_HOME)

    base = _start_dir(start)
    ancestors = [base, *base.parents][:MAX_WALK_UP + 1]
    # A marked root wins outright, so the walk cannot adopt a stranger's
    # ``config/jobhunt.json`` sitting higher up the drive.
    for parent in ancestors:
        if (parent / ROOT_MARKER).is_file():
            candidate = parent / "config" / CONFIG_FILENAME
            searched.append(str(candidate))
            if candidate.is_file():
                return Location(candidate.resolve(), tuple(searched),
                                f"walk-up to {ROOT_MARKER}")
            return Location(None, tuple(searched),
                            f"{ROOT_MARKER} found at {parent} but no "
                            f"config/{CONFIG_FILENAME} beside it")
    for parent in ancestors:
        candidate = parent / "config" / CONFIG_FILENAME
        searched.append(str(candidate))
        if candidate.is_file():
            return Location(candidate.resolve(), tuple(searched), "walk-up")

    candidate = Path.home() / ".jobhunt" / CONFIG_FILENAME
    searched.append(str(candidate))
    if candidate.is_file():
        return Location(candidate.resolve(), tuple(searched), "user home")

    return Location(None, tuple(searched), "no config file found")


# ── The loaded snapshot ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Loaded:
    """One immutable snapshot of the effective policy, plus its provenance.

    Bind this ONCE at tool entry and hold it for the whole call. A change that
    lands mid-call must not be seen by that call: half a ranking scored with
    old weights and half with new is worse than either.
    """

    policy: Policy
    source: Optional[str]
    revision: int
    policy_rev: int
    policy_hash: str
    content_hash: Optional[str]
    provenance: Mapping[str, str] = field(default_factory=dict)
    unknown_keys: tuple[str, ...] = ()
    tier_c_refusals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    config_error: Optional[str] = None
    ledger_error: Optional[str] = None
    external_edit: Optional[Mapping[str, Any]] = None
    revision_regression: Optional[Mapping[str, Any]] = None
    searched: tuple[str, ...] = ()
    stat: Optional[Mapping[str, int]] = None

    @property
    def scoring(self):
        return self.policy.scoring

    @property
    def candidate(self):
        return self.policy.candidate

    @property
    def config_status(self) -> str:
        if self.config_error:
            return f"error: {self.config_error}"
        if self.source is None:
            return (
                "no file found; built-in defaults in use. searched: "
                + (", ".join(self.searched) if self.searched else "(nothing)")
            )
        return f"loaded from {self.source}"

    def server(self, name: str) -> dict:
        return self.policy.server(name)

    def report(self, server: Optional[str] = None) -> dict:
        """The payload a ``<server>_config()`` tool returns."""
        out = {
            "revision": self.revision,
            "policy_rev": self.policy_rev,
            "policy_hash": self.policy_hash,
            "source": self.source,
            "config_status": self.config_status,
            "candidate": self.policy.candidate.to_dict(),
            "scoring": self.policy.scoring.to_dict(),
            "provenance": dict(self.provenance),
            "unknown_keys": list(self.unknown_keys),
            "tier_c_refusals": list(self.tier_c_refusals),
            "warnings": list(self.warnings),
            "config_error": self.config_error,
            "ledger_error": self.ledger_error,
            "external_edit": dict(self.external_edit) if self.external_edit else None,
            "revision_regression": (
                dict(self.revision_regression) if self.revision_regression else None
            ),
            "searched": list(self.searched),
        }
        if server is not None:
            out["server"] = self.policy.server(server)
        return out


def _defaults_snapshot(location: Location, note: Optional[str] = None) -> Loaded:
    policy = DEFAULT_POLICY
    return Loaded(
        policy=policy,
        source=None,
        revision=0,
        policy_rev=0,
        policy_hash=policy.policy_hash,
        content_hash=None,
        provenance={},
        config_error=note,
        searched=location.searched,
    )


# ── Cache: content-addressed, never mtime-addressed ────────────────────────

_CACHE: dict[str, tuple[str, Loaded]] = {}
_DEFAULTS_CACHE: dict[str, Loaded] = {}


def invalidate_cache() -> None:
    """Drop every cached snapshot. Tests and an explicit reload use this."""
    _CACHE.clear()
    _DEFAULTS_CACHE.clear()


def current(
    start: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    location: Optional[Location] = None,
) -> Loaded:
    """The effective policy right now, re-read only when the bytes changed.

    Never raises for a bad file: a malformed or out-of-range config yields
    built-in defaults plus a prominent ``config_error``. Half-applying a
    policy would be worse than ignoring it, and dying at import — which is
    what an unguarded ``float(os.environ[...])`` does one repo over — is
    worse than both.
    """
    loc = location or locate(start, env=env)
    if not loc.found:
        key = "|".join(loc.searched) + f"#{loc.reason}"
        hit = _DEFAULTS_CACHE.get(key)
        if hit is None:
            hit = _defaults_snapshot(loc)
            _DEFAULTS_CACHE[key] = hit
        return hit

    path = loc.path
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _defaults_snapshot(loc, f"cannot read {path}: {exc}")

    digest = sha256(raw).hexdigest()
    cached = _CACHE.get(str(path))
    if cached is not None and cached[0] == digest:
        return cached[1]

    loaded = _parse(raw, digest, loc)
    _CACHE[str(path)] = (digest, loaded)
    return loaded


def _stat_of(path: Path) -> Optional[dict]:
    try:
        st = path.stat()
    except OSError:
        return None
    # Reported for diagnostics only. It is NEVER the reload discriminator:
    # measured on this volume, 12 consecutive atomic replaces produced 8
    # distinct (mtime_ns, size) pairs.
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _parse(raw: bytes, digest: str, loc: Location) -> Loaded:
    path = loc.path
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("jobcore.config: %s is not valid JSON (%s); using defaults",
                     path, exc)
        snap = _defaults_snapshot(loc, f"{path} is not valid JSON: {exc}")
        return Loaded(**{**snap.__dict__, "content_hash": digest,
                         "source": str(path), "stat": _stat_of(path)})
    if not isinstance(data, Mapping):
        snap = _defaults_snapshot(loc, f"{path} must contain a JSON object")
        return Loaded(**{**snap.__dict__, "content_hash": digest,
                         "source": str(path), "stat": _stat_of(path)})

    data = dict(data)
    leaves = flatten(data, schema_aware=True)

    # ── Tier C: refuse loudly, never silently ignore ────────────────────────
    refusals: list[str] = []
    refused_paths: list[str] = []
    for key, value in leaves.items():
        spec = spec_for(key)
        if spec is None or spec.tier != TIER_C:
            continue
        shipped = spec.default if spec.path == key else None
        if _same(value, shipped):
            continue  # a display echo of the Python value; harmless
        refused_paths.append(key)
        refusals.append(
            f"{key}={value!r} REFUSED (tier C, not loadable from the file; "
            f"the value in use is {shipped!r}). {spec.doc}"
        )
    if refusals:
        for line in refusals:
            logger.error("jobcore.config: %s", line)
        data = _strip_paths(data, refused_paths)

    unknown = tuple(sorted(k for k in leaves if spec_for(k) is None))
    if unknown:
        logger.warning(
            "jobcore.config: %s declares keys nothing reads: %s",
            path, ", ".join(unknown),
        )

    try:
        policy = Policy.from_dict(data)
        policy.validate()
    except (PolicyError, TypeError, ValueError, KeyError) as exc:
        logger.error("jobcore.config: %s is invalid (%s); using defaults", path, exc)
        snap = _defaults_snapshot(loc, f"{path} is invalid: {exc}")
        return Loaded(**{**snap.__dict__, "content_hash": digest,
                         "source": str(path), "stat": _stat_of(path),
                         "unknown_keys": unknown,
                         "tier_c_refusals": tuple(refusals)})

    warnings: list[str] = []
    fx = policy.candidate.pay.fx_warning()
    if fx:
        warnings.append(fx)
        logger.warning("jobcore.config: %s", fx)

    provenance = _provenance(leaves)
    phash = policy.policy_hash
    revision = policy.revision

    rev, ledger_error, external_edit, regression = _observe(
        path, phash, revision, policy
    )

    return Loaded(
        policy=policy,
        source=str(path),
        revision=revision,
        policy_rev=rev,
        policy_hash=phash,
        content_hash=digest,
        provenance=provenance,
        unknown_keys=unknown,
        tier_c_refusals=tuple(refusals),
        warnings=tuple(warnings),
        config_error=None,
        ledger_error=ledger_error,
        external_edit=external_edit,
        revision_regression=regression,
        searched=loc.searched,
        stat=_stat_of(path),
    )


def _same(a, b) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def _provenance(leaves: Mapping[str, Any]) -> dict[str, str]:
    """Which keys came from the file and which are shipped defaults."""
    out: dict[str, str] = {}
    for spec in _concrete_specs():
        out[spec.path] = "file" if spec.path in leaves else "default"
    return out


#: Every wildcard-free spec, in declaration order. Built eagerly: it is a
#: comprehension over ~100 frozen dataclasses and a lazy memo would need a
#: ``global`` statement, which a sibling repo's AST-walking guard chokes on
#: (``ast.Global.names`` holds plain strings, not alias objects).
_CONCRETE: tuple[KeySpec, ...] = tuple(s for s in iter_specs() if not s.is_pattern)


def _concrete_specs() -> tuple[KeySpec, ...]:
    return _CONCRETE


# ── flatten / deep merge ───────────────────────────────────────────────────

def _is_declared_leaf(path: str) -> bool:
    """True for a path the schema declares EXACTLY (not via a pattern).

    Some declared keys are free-form maps — ``scoring.skills.weights`` is
    ``{skill: weight}`` and ``scoring.skills.extra_skills`` is
    ``{canonical: [aliases]}``. Their entries are data, not schema keys, so
    flattening must stop at the map. Pattern matches deliberately do NOT stop
    it: ``servers.*.agent.**`` matches ``…agent.blocklist``, and stopping
    there would swallow the tier-B ``blocklist.companies`` into a tier-C leaf.
    """
    from .policy import SCHEMA
    return path in SCHEMA


def flatten(data: Mapping, prefix: str = "", *, schema_aware: bool = False
            ) -> dict[str, Any]:
    """Every leaf as a dotted path. Lists are LEAVES, never descended into.

    With *schema_aware*, a declared free-form map is a leaf too — see
    :func:`_is_declared_leaf`.
    """
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and not (schema_aware and _is_declared_leaf(path)):
            if value:
                out.update(flatten(value, path, schema_aware=schema_aware))
            else:
                out[path] = {}
        else:
            out[path] = value
    return out


def deep_merge(base: Mapping, patch: Mapping) -> dict:
    """Dicts MERGE, lists REPLACE, ``None`` means revert to the shipped default.

    ``dict.update`` is what the live agent-config tool does today, and it is a
    partial RESET: patching ``{"quiet_hours": {"start_hour": 22}}`` silently
    drops ``end_hour`` back to its default and re-enables the window. Stated
    once here, tested once in ``test_config.py``.
    """
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)          # revert to default
        elif isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _strip_paths(data: Mapping, paths: Sequence[str]) -> dict:
    """Remove dotted *paths* from a nested mapping (used for tier-C refusals)."""
    out = json.loads(json.dumps(data, default=str))
    for path in paths:
        parts = path.split(".")
        node = out
        ok = True
        for p in parts[:-1]:
            nxt = node.get(p) if isinstance(node, Mapping) else None
            if not isinstance(nxt, Mapping):
                ok = False
                break
            node = nxt
        if ok and isinstance(node, dict):
            node.pop(parts[-1], None)
    return out


# ── The ledger: history rows written on LOAD, not only on write ────────────

def _ledger_path(config_path: Path) -> Path:
    return config_path.parent / LEDGER_FILENAME


def _read_tail(ledger: Path) -> Optional[dict]:
    try:
        with ledger.open("r", encoding="utf-8") as fh:
            tail = None
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    tail = json.loads(line)
                except json.JSONDecodeError:
                    continue
            return tail
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _observe(path: Path, policy_hash: str, revision: int, policy: Policy):
    """Record a newly-observed fingerprint and derive ``policy_rev``.

    Returns ``(policy_rev, ledger_error, external_edit, revision_regression)``.

    This is what makes the explainability answer real. The headline workflow
    is *open the file, change a weight, save* — which calls
    :func:`apply_patch` never. Writing the history row here means a hand edit
    is a first-class ledger row with a diff, instead of two materially
    different scoring policies sharing one stamp.
    """
    ledger = _ledger_path(path)
    tail = _read_tail(ledger)

    if tail is not None and tail.get("hash") == policy_hash:
        regression = None
        if isinstance(tail.get("revision"), int) and revision < tail["revision"]:
            regression = {
                "file_revision": revision,
                "ledger_revision": tail["revision"],
                "detail": (
                    "the file's revision went BACKWARDS relative to the "
                    "ledger — an external save almost certainly overwrote an "
                    "agent write with a stale buffer"
                ),
            }
        return int(tail.get("rev") or 0), None, None, regression

    prev_rev = int(tail.get("rev") or 0) if tail else 0
    prev_fp = tail.get("fingerprint") if tail else None
    rev = prev_rev + 1
    diff = _diff_fingerprints(prev_fp, policy.fingerprint())

    regression = None
    if tail is not None and isinstance(tail.get("revision"), int) \
            and revision < tail["revision"]:
        regression = {
            "file_revision": revision,
            "ledger_revision": tail["revision"],
            "detail": "revision regressed; a stale external save is the usual cause",
        }

    external = None
    if tail is not None:
        external = {
            "policy_rev": rev,
            "previous_policy_rev": prev_rev,
            "previous_hash": tail.get("hash"),
            "hash": policy_hash,
            "file_revision": revision,
            "diff": diff,
            "detail": (
                "the scoring fingerprint changed without a tool write "
                "(a text-editor save is the normal cause). Scores produced "
                "before and after are NOT comparable."
            ),
        }
        logger.warning(
            "jobcore.config: external edit detected — fingerprint %s -> %s, %d "
            "field(s) changed", tail.get("hash"), policy_hash, len(diff),
        )

    row = {
        "rev": rev,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": "loader",
        "source": "initial" if tail is None else "external_edit",
        "hash": policy_hash,
        "revision": revision,
        "diff": diff,
        "fingerprint": policy.fingerprint(),
    }
    err = _append_row(ledger, row)
    if err:
        # No durable ledger: fall back to the file's own integer so a stamp
        # still exists, and say so rather than pretending.
        return revision, err, external, regression
    return rev, None, external, regression


def _append_row(ledger: Path, row: Mapping) -> Optional[str]:
    try:
        with _ConfigLock(ledger.parent / (LEDGER_FILENAME + ".lock")):
            existing = _read_tail(ledger)
            if existing is not None and existing.get("hash") == row.get("hash"):
                return None  # another process got there first
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(canonical_json(row) + "\n")
        return None
    except (OSError, ConfigLockedError) as exc:
        logger.warning("jobcore.config: could not append to %s: %s", ledger, exc)
        return f"could not append to {ledger}: {exc}"


def _diff_fingerprints(before: Optional[Mapping], after: Mapping) -> dict:
    """``{dotted_path: [old, new]}`` over the two fingerprints."""
    a = flatten(before or {})
    b = flatten(after or {})
    out: dict[str, list] = {}
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            out[key] = [a.get(key), b.get(key)]
    return out


# ── Cross-process lock: PID + liveness, reclaim a corpse, never a corpse's ─

class ConfigLockedError(RuntimeError):
    """Another LIVE process holds the config lock."""

    def __init__(self, holder_pid: int, lock_file: Path):
        self.holder_pid = holder_pid
        self.lock_file = lock_file
        super().__init__(
            f"config file locked by live PID {holder_pid} (lock: {lock_file}). "
            f"Retry, or stop that process."
        )


def _pid_is_alive(pid: int) -> bool:
    """Does a process with *pid* exist? Cross-platform, side-effect free.

    A PID that cannot be positively confirmed alive is treated as dead on
    POSIX (so a stale lock is reclaimable) and as alive on Windows when the
    handle opens but cannot be introspected (so a live holder is never robbed).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _ConfigLock:
    """Re-entrant for this PID; reclaims a dead holder; releases only if owner."""

    def __init__(self, lock_file: Path, attempts: int = 40, delay: float = 0.05):
        self.lock_file = Path(lock_file)
        self.attempts = attempts
        self.delay = delay
        self._acquired = False

    def _holder(self) -> Optional[int]:
        try:
            raw = self.lock_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        if not raw:
            return None
        try:
            return int(raw.splitlines()[0].strip())
        except ValueError:
            return None  # garbage == stale

    def _write(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.lock_file.with_suffix(self.lock_file.suffix + ".tmp")
        tmp.write_text(f"{os.getpid()}\n", encoding="utf-8")
        os.replace(tmp, self.lock_file)

    def acquire(self) -> None:
        last: Optional[int] = None
        for _ in range(self.attempts):
            holder = self._holder()
            if holder is None or holder == os.getpid() or not _pid_is_alive(holder):
                self._write()
                self._acquired = True
                return
            last = holder
            time.sleep(self.delay)
        raise ConfigLockedError(last or -1, self.lock_file)

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self._holder() == os.getpid():
                self.lock_file.unlink()
        except (FileNotFoundError, OSError):
            pass
        finally:
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ── Writes ─────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, document: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False)
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_raw(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def default_document() -> dict:
    """The full default file — what ``jobhunt.example.json`` should contain."""
    doc = DEFAULT_POLICY.to_dict()
    doc["revision"] = 0
    doc["updated_at"] = None
    doc["updated_by"] = None
    doc["servers"] = schema_defaults("servers")
    return doc


def _section_of(path: str) -> str:
    parts = path.split(".")
    if parts[0] == "servers" and len(parts) >= 2:
        return f"servers.{parts[1]}"
    return parts[0]


def _check_ratchet(spec: KeySpec, key: str, old, new, confirm_widen: bool
                   ) -> Optional[str]:
    """Tier B: tighten freely; loosen only with confirm AND under the ceiling."""
    if spec.max_items is not None and isinstance(new, (list, tuple)) \
            and len(new) > spec.max_items:
        return (
            f"{key}: {len(new)} entries exceeds the Python-side maximum of "
            f"{spec.max_items}. That bound is not in the config file and no "
            f"write, confirmed or not, can raise it."
        )
    if isinstance(new, (int, float)) and not isinstance(new, bool):
        if spec.ceiling is not None and new > spec.ceiling:
            return (
                f"{key}={new} exceeds the ceiling {spec.ceiling}, which lives "
                f"in Python (jobcore.policy.HARD_LIMITS), not in the file. "
                f"Edit the source and restart if this is really intended."
            )
        if spec.floor is not None and new < spec.floor:
            return (
                f"{key}={new} is below the floor {spec.floor}, which lives in "
                f"Python, not in the file."
            )

    loosening = _is_loosening(spec, old, new)
    if loosening and not confirm_widen:
        return (
            f"{key}: {old!r} -> {new!r} loosens a tier-B guard. Tightening is "
            f"free; loosening needs confirm_widen=True. ({spec.doc})"
        )
    return None


def _is_loosening(spec: KeySpec, old, new) -> bool:
    if old is None or _same(old, new):
        return False
    direction = spec.direction
    if direction in ("grow", "shrink"):
        old_set = set(old or ()) if isinstance(old, (list, tuple, set)) else set()
        new_set = set(new or ()) if isinstance(new, (list, tuple, set)) else set()
        if direction == "grow":       # a longer list is tighter (a blocklist)
            return not new_set >= old_set
        return not new_set <= old_set  # a shorter list is tighter (a skill list)
    if isinstance(old, bool) or isinstance(new, bool):
        # direction "up" means True is the safe state.
        return bool(old) and not bool(new) if direction == "up" else \
            (not bool(old)) and bool(new)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return new > old if direction == "down" else new < old
    return True  # any other change to a guarded key is treated as loosening


def _check_choices(spec: KeySpec, key: str, new) -> Optional[str]:
    if spec.choices and new not in spec.choices:
        return f"{key}={new!r} is not one of {list(spec.choices)}"
    if spec.tier != TIER_B:
        if isinstance(new, (int, float)) and not isinstance(new, bool):
            if spec.ceiling is not None and new > spec.ceiling:
                return f"{key}={new} exceeds the maximum {spec.ceiling}"
            if spec.floor is not None and new < spec.floor:
                return f"{key}={new} is below the minimum {spec.floor}"
        if spec.max_items is not None and isinstance(new, (list, tuple)) \
                and len(new) > spec.max_items:
            return f"{key}: {len(new)} entries exceeds the maximum {spec.max_items}"
    return None


def apply_patch(
    patch: Mapping,
    *,
    path: Optional[Path] = None,
    start: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    base_revision: Optional[int] = None,
    actor: str = "unknown",
    allowed_sections: Sequence[str] = SHARED_SECTIONS,
    confirm_widen: bool = False,
) -> dict:
    """Merge *patch* into the config file under lock, or refuse and say why.

    Args:
        patch: nested dict of changes. ``None`` at a leaf reverts to default.
        allowed_sections: which top-level sections the CALLER may write.
            ``"candidate"``, ``"scoring"``, and ``"servers.<own-name>"``. A
            patch touching another server's section is refused by name — no
            server can widen a sibling's caps, and there is no authority
            process whose being down makes config unwritable.
        base_revision: compare-and-swap token. A stale one returns
            ``{"status": "conflict"}`` and the caller re-reads.
        confirm_widen: required to loosen a tier-B guard.

    Returns a dict with ``status`` in ``ok`` / ``refused`` / ``conflict`` /
    ``no_config_file`` / ``error``. It never raises for a policy problem —
    a refusal is data the calling agent can read and act on.
    """
    target = Path(path) if path else None
    if target is None:
        loc = locate(start, env=env)
        if not loc.found:
            return {
                "status": "no_config_file",
                "detail": loc.reason,
                "searched": list(loc.searched),
                "hint": (
                    f"create the file, or set {ENV_CONFIG} to its path in the "
                    f"MCP host's env block (a stdio child inherits nothing "
                    f"else), then restart the host."
                ),
            }
        target = loc.path

    leaves = flatten(patch, schema_aware=True)
    if not leaves:
        return {"status": "refused", "refusals": ["empty patch"]}

    allowed = set(allowed_sections)
    refusals: list[str] = []

    # 1. Foreign sections, by name.
    for key in leaves:
        section = _section_of(key)
        if section not in allowed:
            owner = section.split(".", 1)[-1] if section.startswith("servers.") else None
            refusals.append(
                f"{key}: {section} is not writable from here"
                + (f" — call {owner}'s own set_config" if owner else "")
            )

    # 2. Undeclared keys. Unknown keys already IN the file are preserved and
    #    reported; a patch that introduces one is refused, because nothing
    #    reads it and a knob wired to nothing must not ship.
    for key in leaves:
        if spec_for(key) is None:
            refusals.append(
                f"{key}: not a declared key. Nothing reads it, so writing it "
                f"would create a decoy."
            )

    # 3. Tiers.
    current_raw = _read_raw(target)
    current_leaves = flatten(current_raw, schema_aware=True)
    for key, new in leaves.items():
        spec = spec_for(key)
        if spec is None:
            continue
        if spec.tier == TIER_C:
            refusals.append(
                f"{key}: REFUSED — tier C. Not writable and not loadable from "
                f"the config file at any tier. {spec.doc}"
            )
            continue
        problem = _check_choices(spec, key, new)
        if problem:
            refusals.append(problem)
            continue
        if spec.tier == TIER_B:
            old = current_leaves.get(key, spec.default)
            problem = _check_ratchet(spec, key, old, new, confirm_widen)
            if problem:
                refusals.append(problem)

    if refusals:
        for line in refusals:
            logger.error("jobcore.config: patch refused — %s", line)
        return {"status": "refused", "refusals": refusals}

    lock = _ConfigLock(target.with_suffix(target.suffix + ".lock"))
    try:
        with lock:
            fresh = _read_raw(target)
            fresh_revision = int(fresh.get("revision") or 0)
            if base_revision is not None and base_revision != fresh_revision:
                return {
                    "status": "conflict",
                    "revision": fresh_revision,
                    "detail": (
                        f"base_revision={base_revision} but the file is at "
                        f"{fresh_revision}; re-read and retry"
                    ),
                }

            before_policy = _safe_policy(fresh)
            merged = deep_merge(fresh, dict(patch))

            try:
                after_policy = Policy.from_dict(merged)
                after_policy.validate()
            except (PolicyError, TypeError, ValueError, KeyError) as exc:
                return {"status": "refused", "refusals": [str(exc)]}

            ambiguity = _taxonomy_conflicts(after_policy)
            if ambiguity:
                return {
                    "status": "refused",
                    "refusals": [
                        f"scoring.skills.extra_skills would make these derived "
                        f"forms ambiguous: {sorted(ambiguity)}. An ambiguous "
                        f"derived form resolves to NEITHER canonical, so an "
                        f"EXISTING skill silently stops matching and every "
                        f"score involving it changes."
                    ],
                }

            merged["revision"] = fresh_revision + 1
            merged["config_version"] = merged.get("config_version") or 1
            merged["updated_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            merged["updated_by"] = actor
            _atomic_write(target, merged)

            after_policy = after_policy.with_revision(merged["revision"])
            new_hash = after_policy.policy_hash
            diff = _diff_fingerprints(before_policy.fingerprint(),
                                      after_policy.fingerprint())
            ledger = _ledger_path(target)
            tail = _read_tail(ledger)
            rev = int(tail.get("rev") or 0) + 1 if tail else 1
            row = {
                "rev": rev,
                "ts": merged["updated_at"],
                "actor": actor,
                "source": "apply_patch",
                "hash": new_hash,
                "revision": merged["revision"],
                "diff": diff,
                "fingerprint": after_policy.fingerprint(),
            }
            try:
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("a", encoding="utf-8") as fh:
                    fh.write(canonical_json(row) + "\n")
                ledger_error = None
            except OSError as exc:
                ledger_error = f"could not append to {ledger}: {exc}"
    except ConfigLockedError as exc:
        return {"status": "error", "detail": str(exc), "holder_pid": exc.holder_pid}
    except OSError as exc:
        return {"status": "error", "detail": f"write failed: {exc}"}

    invalidate_cache()
    return {
        "status": "ok",
        "revision": merged["revision"],
        "policy_rev": rev,
        "policy_hash": new_hash,
        "changed": _changed(flatten(fresh, schema_aware=True),
                            flatten(merged, schema_aware=True)),
        "scoring_changed": diff,
        "ledger_error": ledger_error,
        "path": str(target),
    }


def _safe_policy(raw: Mapping) -> Policy:
    try:
        return Policy.from_dict(raw)
    except Exception:
        return DEFAULT_POLICY


def _changed(before: Mapping, after: Mapping) -> dict:
    out: dict[str, list] = {}
    for key in sorted(set(before) | set(after)):
        if key in ("updated_at", "updated_by"):
            continue
        if before.get(key) != after.get(key):
            out[key] = [before.get(key), after.get(key)]
    return out


def _taxonomy_conflicts(policy: Policy) -> frozenset:
    """Derived forms two canonicals would both claim under the extension."""
    extension = policy.scoring.skills.taxonomy_extension()
    if not extension:
        return frozenset()
    from .skills import DEFAULT_TAXONOMY
    return DEFAULT_TAXONOMY.extended(extension).ambiguous_derived_keys


# Re-exported so a consumer can bound a value without importing policy too.
MIN_AGENT_FIT_FLOOR = int(HARD_LIMITS["min_agent_fit_floor"])


# ── A shell-level answer to "what is it actually reading?" ─────────────────

def _main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m jobcore.config`` — status, or ``--example`` for a template.

    The difference between a five-second diagnosis and an afternoon. It prints
    where the file was found (or every path tried when it was not), the
    content-derived stamp, and anything the loader refused.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m jobcore.config")
    parser.add_argument("--example", action="store_true",
                        help="print a fully-populated default config document")
    parser.add_argument("--start", default=None,
                        help="walk up from this path instead of jobcore's own")
    args = parser.parse_args(argv)

    if args.example:
        print(json.dumps(default_document(), indent=2, ensure_ascii=False))
        return 0

    loaded = current(start=Path(args.start) if args.start else None)
    print(json.dumps(loaded.report(), indent=2, ensure_ascii=False, default=str))
    return 0 if loaded.config_error is None else 1


if __name__ == "__main__":  # pragma: no cover - exercised by hand
    raise SystemExit(_main())
