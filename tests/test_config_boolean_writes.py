"""A boolean key takes a real bool on write -- never 0, 1, "true" or "false".

THE DEFECT, measured by lane naukri-bool28 on 2026-11-28 (its record, section 7):

* ``apply_patch`` checked a written value with ``value in spec.choices``, and
  ``in`` compares with ``==``. So ``servers.naukri.agent.enabled``, whose
  choices are ``(False, True)``, took 0, 1, 0.0 and 1.0 -- ``1 == True`` -- and
  ``apply_patch({"enabled": 1}, confirm_widen=True)`` armed the agent. The
  other boolean keys carry no ``choices`` at all, so they took any type.
* The tier-B ratchet read the OLD value TRUTHILY. With ``enabled: "false"``
  already in the file -- which naukri now reads, correctly, as NOT armed --
  writing True counted as no loosening at all and landed without
  ``confirm_widen``. With ``enabled: 1`` there, ``_same(1, True)`` held and the
  arming write was not even seen as a change.

THE RULE pinned here:

* A key whose shipped default is a bool is a BOOLEAN key. The default is the
  one type witness the schema carries, and every one of the measured boolean
  keys has one.
* A write to a boolean key must be ``type(value) is bool``. ``None`` keeps its
  meaning: revert to the default.
* Its ratchet reads the old value the way the consumers do. An ARMING key
  (direction "down") is armed only by True. A RESTRICTING key (direction
  "up") is lifted only by False. Anything else in the file is read as the
  safe state, so moving off it is loosening.
* Choice membership never takes a bool for a number or a number for a bool.

Every test here was run against the pre-fix module first; the record names
which went red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobcore import config as C
from jobcore import policy as P

#: Every boolean key, derived the way the write path derives it.
BOOLEAN_KEYS = sorted(s.path for s in P.SCHEMA.values() if isinstance(s.default, bool))

#: The values the defect let through, and the two strings it most invites.
NOT_A_BOOL = [0, 1, 1.0, "true", "false"]


def _write_config(path: Path, servers=None) -> Path:
    doc = {"config_version": 1, "revision": 1}
    if servers is not None:
        doc["servers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    C.invalidate_cache()
    return path


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = _write_config(tmp_path / "config" / "jobhunt.json")
    monkeypatch.setenv(C.ENV_CONFIG, str(path))
    return path


def _nested(dotted: str, value):
    out: dict = {}
    node = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def _write(cfg, key, value, *, confirm=False):
    section = ".".join(key.split(".")[:2])
    return C.apply_patch(_nested(key, value), path=cfg, actor="test",
                         allowed_sections=(section,), confirm_widen=confirm)


def _refusals(out) -> str:
    return " ".join(out.get("refusals", []))


def test_the_boolean_keys_are_found_and_include_the_three_ratchets():
    """The derivation reaches the keys the defect was measured on."""
    assert {"servers.naukri.agent.enabled",
            "servers.naukri.agent.quiet_hours.enabled",
            "servers.naukri.agent.blocklist.enabled"} <= set(BOOLEAN_KEYS)
    # Every key whose choices are all bools has a bool default too, so the
    # default-is-the-witness rule cannot miss one by being handed choices.
    for spec in P.SCHEMA.values():
        if spec.choices and all(isinstance(c, bool) for c in spec.choices):
            assert isinstance(spec.default, bool), spec.path


@pytest.mark.parametrize("value", NOT_A_BOOL, ids=repr)
@pytest.mark.parametrize("key", BOOLEAN_KEYS)
def test_a_boolean_key_refuses_anything_but_a_bool(cfg, key, value):
    """Confirmed, so the ratchet cannot be the reason; the TYPE must be."""
    before = cfg.read_bytes()
    out = _write(cfg, key, value, confirm=True)
    assert out["status"] == "refused", out
    assert "must be true or false" in _refusals(out), out
    assert cfg.read_bytes() == before, "a refused write changed the file"


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("key", BOOLEAN_KEYS)
def test_a_boolean_key_takes_a_real_bool(cfg, key, value):
    """CONTROL. The rule refuses the wrong TYPE, not booleans."""
    out = _write(cfg, key, value, confirm=True)
    assert out["status"] == "ok", out


@pytest.mark.parametrize("stored", [1, "false", "true", None, 0])
def test_an_arming_key_is_armed_only_by_true(cfg, stored):
    """Whatever non-True value the file holds, writing True is arming."""
    _write_config(cfg, {"naukri": {"agent": {"enabled": stored}}})
    out = _write(cfg, "servers.naukri.agent.enabled", True)
    assert out["status"] == "refused", out
    assert "confirm_widen" in _refusals(out), out


@pytest.mark.parametrize("stored", [0, "", "no", None, 1])
def test_a_restricting_key_is_lifted_only_by_false(cfg, stored):
    """Whatever non-False value the file holds, writing False is lifting."""
    _write_config(cfg, {"naukri": {"agent": {"quiet_hours": {"enabled": stored}}}})
    out = _write(cfg, "servers.naukri.agent.quiet_hours.enabled", False)
    assert out["status"] == "refused", out
    assert "confirm_widen" in _refusals(out), out


def test_moving_to_the_safe_state_is_free_from_any_stored_value(cfg):
    """CONTROL. Strictness costs nothing in the tightening direction."""
    _write_config(cfg, {"naukri": {"agent": {"enabled": "true",
                                             "quiet_hours": {"enabled": 0}}}})
    assert _write(cfg, "servers.naukri.agent.enabled", False)["status"] == "ok"
    assert _write(cfg, "servers.naukri.agent.quiet_hours.enabled", True)["status"] == "ok"


def test_a_real_flip_still_needs_confirm_and_lands_with_it(cfg):
    """CONTROL. The ratchet itself is unchanged for real booleans."""
    key = "servers.naukri.agent.enabled"
    assert _write(cfg, key, True)["status"] == "refused"
    assert _write(cfg, key, True, confirm=True)["status"] == "ok"
    key = "servers.naukri.agent.quiet_hours.enabled"
    assert _write(cfg, key, False)["status"] == "refused"
    assert _write(cfg, key, False, confirm=True)["status"] == "ok"


def test_reverting_a_restricting_key_to_its_default_is_free(cfg):
    """None means "revert to the default", and the default IS the safe state.

    The truthy ratchet read ``not bool(None)`` as switching quiet hours OFF,
    and refused the revert to them being ON.
    """
    _write_config(cfg, {"naukri": {"agent": {"quiet_hours": {"enabled": True}}}})
    out = _write(cfg, "servers.naukri.agent.quiet_hours.enabled", None)
    assert out["status"] == "ok", out


def test_choice_membership_never_takes_a_bool_for_a_number():
    """The mechanism itself: ``True in (0, 1, 2)`` is True in Python."""
    numbers = P.KeySpec(path="servers.naukri.synthetic", tier=P.TIER_A, doc="test",
                        readers=("naukri",), default=1, choices=(0, 1, 2))
    assert C._check_choices(numbers, "k", True) is not None
    assert C._check_choices(numbers, "k", 1) is None
    flags = P.KeySpec(path="servers.naukri.synthetic", tier=P.TIER_A, doc="test",
                      readers=("naukri",), default=False, choices=(False, True))
    assert C._check_choices(flags, "k", 1) is not None
    assert C._check_choices(flags, "k", True) is None
