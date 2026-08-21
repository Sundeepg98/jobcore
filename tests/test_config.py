"""The loader: discovery, content-addressed reload, guarded writes.

Every guard here is shown FAILING before it is trusted. Six bugs in this
codebase in one week were checks that could not fail, so a test that only ever
proves the happy path proves nothing at all — each class below carries its own
control arm.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobcore import config as C
from jobcore import policy as P


# ── helpers ────────────────────────────────────────────────────────────────

def write_config(path: Path, **blocks) -> Path:
    doc = {"config_version": 1, "revision": blocks.pop("revision", 1)}
    doc.update(blocks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config file at a tmp path, bound through JOBHUNT_CONFIG."""
    path = tmp_path / "config" / "jobhunt.json"
    write_config(path)
    monkeypatch.setenv(C.ENV_CONFIG, str(path))
    C.invalidate_cache()
    return path


# ── discovery ──────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_env_path_wins(self, tmp_path, monkeypatch):
        path = write_config(tmp_path / "elsewhere.json")
        monkeypatch.setenv(C.ENV_CONFIG, str(path))
        assert C.locate().path == path.resolve()

    def test_env_home_is_next(self, tmp_path, monkeypatch):
        monkeypatch.delenv(C.ENV_CONFIG, raising=False)
        home = tmp_path / "home"
        write_config(home / C.CONFIG_FILENAME)
        monkeypatch.setenv(C.ENV_HOME, str(home))
        assert C.locate().path == (home / C.CONFIG_FILENAME).resolve()

    def test_walk_up_starts_from_the_CALLER_not_from_jobcore(self, tmp_path,
                                                             monkeypatch):
        """H5: jobcore cannot know who imported it.

        Under a normal (non-editable) install jobcore's ``__file__`` sits in
        site-packages, the walk finds nothing, and every server silently runs
        on defaults with no error anywhere. The caller passes its own path.
        """
        monkeypatch.delenv(C.ENV_CONFIG, raising=False)
        root = tmp_path / "workspace"
        write_config(root / "config" / C.CONFIG_FILENAME)
        caller = root / "servers" / "uplers" / "uplers_server" / "server.py"
        caller.parent.mkdir(parents=True)
        caller.write_text("# a server module", encoding="utf-8")

        assert C.locate(start=caller).path == \
            (root / "config" / C.CONFIG_FILENAME).resolve()
        # ...and from an unrelated start, the same tree is invisible.
        stranger = tmp_path / "unrelated"
        stranger.mkdir()
        assert C.locate(start=stranger).path is None

    def test_a_marked_root_wins_over_a_stranger_higher_up(self, tmp_path,
                                                          monkeypatch):
        """L1: the walk must not adopt someone else's config/jobhunt.json."""
        monkeypatch.delenv(C.ENV_CONFIG, raising=False)
        stranger = write_config(tmp_path / "config" / C.CONFIG_FILENAME,
                                revision=99)
        mine = tmp_path / "mine"
        (mine / C.ROOT_MARKER).parent.mkdir(parents=True, exist_ok=True)
        (mine / C.ROOT_MARKER).write_text("", encoding="utf-8")
        write_config(mine / "config" / C.CONFIG_FILENAME, revision=7)
        caller = mine / "servers" / "x" / "s.py"
        caller.parent.mkdir(parents=True)
        caller.write_text("", encoding="utf-8")

        found = C.locate(start=caller).path
        assert found == (mine / "config" / C.CONFIG_FILENAME).resolve()
        assert found != stranger.resolve()

    def test_explicit_disable_tokens_stop_the_search(self, tmp_path, monkeypatch):
        write_config(tmp_path / "config" / C.CONFIG_FILENAME)
        for token in (":none:", ":default:", ":builtin:"):
            monkeypatch.setenv(C.ENV_CONFIG, token)
            loc = C.locate(start=tmp_path / "config")
            assert loc.path is None
            assert "defaults only" in loc.reason

    def test_an_EMPTY_env_value_means_unset_not_disabled(self, tmp_path,
                                                         monkeypatch):
        """H5: a stray ``JOBHUNT_CONFIG=`` must not silently disable config.

        CI runners, shell scripts and MCP env blocks produce an empty value by
        accident all the time. Empty means 'keep looking'.
        """
        root = tmp_path / "ws"
        (root / C.ROOT_MARKER).parent.mkdir(parents=True, exist_ok=True)
        (root / C.ROOT_MARKER).write_text("", encoding="utf-8")
        target = write_config(root / "config" / C.CONFIG_FILENAME)
        monkeypatch.setenv(C.ENV_CONFIG, "")
        assert C.locate(start=root / "a" / "b").path == target.resolve()

    def test_jobhunt_disable_is_the_explicit_off_switch(self, tmp_path, monkeypatch):
        monkeypatch.delenv(C.ENV_CONFIG, raising=False)
        monkeypatch.setenv(C.ENV_DISABLE, "1")
        assert C.locate(start=tmp_path).path is None

    def test_nothing_found_is_reported_LOUDLY_with_every_path_tried(self,
                                                                    tmp_path,
                                                                    monkeypatch):
        """Silence here is the difference between 5 seconds and an afternoon."""
        monkeypatch.delenv(C.ENV_CONFIG, raising=False)
        loaded = C.current(start=tmp_path / "deep" / "nested")
        assert loaded.source is None
        assert loaded.policy == P.DEFAULT_POLICY
        assert "no file found" in loaded.config_status
        assert loaded.searched, "must list what it looked at"
        assert any("jobhunt.json" in p for p in loaded.searched)

    def test_an_env_path_that_points_at_nothing_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setenv(C.ENV_CONFIG, str(tmp_path / "missing.json"))
        loc = C.locate()
        assert loc.path is None
        assert "points at no file" in loc.reason


# ── reload on CONTENT, never on mtime ──────────────────────────────────────

class TestReloadTriggersOnContent:
    def test_the_mtime_discriminator_would_MISS_this_change(self, cfg, monkeypatch):
        """H1, made deterministic.

        Measured on this NTFS volume, 12 back-to-back atomic replaces produced
        only 8 distinct ``(mtime_ns, size)`` pairs — four consecutive writes
        with a delta of exactly zero. Rather than race the clock, this test
        FORCES the collision with ``os.utime`` and a constant-length edit, then
        asserts the loader saw the change anyway.
        """
        frozen_ns = 1_700_000_000_000_000_000

        write_config(cfg, scoring={"weights": {"skills": 0.6, "experience": 0.4}})
        os.utime(cfg, ns=(frozen_ns, frozen_ns))
        first = C.current()
        stat_a = cfg.stat()
        assert first.policy.scoring.weights.skills == 0.6

        # Same byte length: 0.6 -> 0.8. This is the COMMON edit, not a corner.
        write_config(cfg, scoring={"weights": {"skills": 0.8, "experience": 0.2}})
        os.utime(cfg, ns=(frozen_ns, frozen_ns))
        stat_b = cfg.stat()

        # The control: a stat-based discriminator is blind here, by construction.
        assert (stat_a.st_mtime_ns, stat_a.st_size) == \
               (stat_b.st_mtime_ns, stat_b.st_size)

        second = C.current()
        assert second.policy.scoring.weights.skills == 0.8, (
            "the loader must read and hash the bytes; a stat-only trigger "
            "holds a stale snapshot indefinitely while reporting it as current"
        )
        assert second.content_hash != first.content_hash

    def test_an_unchanged_file_is_not_reparsed(self, cfg):
        a = C.current()
        b = C.current()
        assert a is b, "identical bytes must reuse the cached snapshot"

    def test_the_reported_stat_is_diagnostic_only(self, cfg):
        loaded = C.current()
        assert set(loaded.stat) == {"mtime_ns", "size"}


# ── deep merge ─────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_the_planted_partial_reset_bug_fails_under_dict_update(self):
        """The live agent-config tool does ``config.update(patch)`` today.

        Patching one field of a nested block silently resets its siblings —
        ``quiet_hours`` loses ``end_hour`` and the window is re-enabled from
        defaults. This is the control: the naive merge really does lose data.
        """
        base = {"quiet_hours": {"enabled": True, "start_hour": 20, "end_hour": 8}}
        naive = dict(base)
        naive.update({"quiet_hours": {"start_hour": 22}})
        assert "end_hour" not in naive["quiet_hours"], "control: update() loses it"

        merged = C.deep_merge(base, {"quiet_hours": {"start_hour": 22}})
        assert merged["quiet_hours"] == {"enabled": True, "start_hour": 22,
                                         "end_hour": 8}

    def test_lists_replace_and_dicts_merge(self):
        base = {"a": {"x": 1, "y": 2}, "l": [1, 2, 3]}
        out = C.deep_merge(base, {"a": {"y": 9}, "l": [7]})
        assert out == {"a": {"x": 1, "y": 9}, "l": [7]}

    def test_none_removes_the_key_so_the_default_comes_back(self):
        out = C.deep_merge({"a": {"x": 1}}, {"a": {"x": None}})
        assert out == {"a": {}}

    def test_it_does_not_mutate_its_inputs(self):
        base = {"a": {"x": 1}}
        C.deep_merge(base, {"a": {"y": 2}})
        assert base == {"a": {"x": 1}}


# ── tier enforcement ───────────────────────────────────────────────────────

class TestTierAIsFree:
    def test_a_plain_scoring_write_lands(self, cfg):
        out = C.apply_patch({"scoring": {"weights": {"skills": 0.75,
                                                     "experience": 0.25}}},
                            path=cfg, actor="test")
        assert out["status"] == "ok", out
        assert C.current().policy.scoring.weights.skills == 0.75

    def test_the_display_filter_is_writable(self, cfg):
        out = C.apply_patch({"servers": {"naukri": {"display_min_score": 75}}},
                            path=cfg, actor="test",
                            allowed_sections=("servers.naukri",))
        assert out["status"] == "ok", out


class TestTierBRatchet:
    def test_tightening_is_free(self, cfg):
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 5}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        assert out["status"] == "ok", out

    def test_loosening_without_confirm_is_refused(self, cfg):
        C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 5}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 20}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        assert out["status"] == "refused"
        assert "confirm_widen" in " ".join(out["refusals"])

    def test_loosening_with_confirm_lands_under_the_ceiling(self, cfg):
        C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 5}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 20}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",),
            confirm_widen=True)
        assert out["status"] == "ok", out

    def test_the_ceiling_lives_in_python_and_confirm_cannot_pass_it(self, cfg):
        out = C.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 200}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",),
            confirm_widen=True)
        assert out["status"] == "refused"
        joined = " ".join(out["refusals"])
        assert "ceiling" in joined and "Python" in joined

    def test_a_blocklist_may_grow_freely_and_shrink_only_with_confirm(self, cfg):
        grow = C.apply_patch(
            {"servers": {"naukri": {"agent": {"blocklist":
                                              {"companies": ["A", "B"]}}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        assert grow["status"] == "ok", grow
        shrink = C.apply_patch(
            {"servers": {"naukri": {"agent": {"blocklist": {"companies": ["A"]}}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        assert shrink["status"] == "refused"

    def test_removing_a_company_he_avoids_needs_confirmation(self, cfg):
        C.apply_patch({"candidate": {"avoid_companies": ["CurrentEmployer"]}},
                      path=cfg, actor="test")
        out = C.apply_patch({"candidate": {"avoid_companies": []}},
                            path=cfg, actor="test")
        assert out["status"] == "refused", (
            "'do not apply to my current employer' must not evaporate quietly"
        )

    def test_shortening_retention_destroys_history_and_needs_confirmation(self, cfg):
        out = C.apply_patch(
            {"servers": {"naukri": {"retention": {"auto_purge_days": 30}}}},
            path=cfg, actor="test", allowed_sections=("servers.naukri",))
        assert out["status"] == "refused"


class TestTierCIsNotLoadableAtAll:
    @pytest.mark.parametrize("patch,needle", [
        ({"servers": {"naukri": {"agent": {"enabled": True}}}}, "agent.enabled"),
        ({"servers": {"naukri": {"agent": {"mode": "auto"}}}}, "agent.mode"),
        ({"servers": {"naukri": {"agent": {"min_fit_score": 0}}}}, "min_fit_score"),
        ({"servers": {"naukri": {"agent": {"blocklist": {"enabled": False}}}}},
         "blocklist.enabled"),
        ({"servers": {"naukri": {"agent": {"searches": [{"q": "anything"}]}}}},
         "searches"),
        ({"servers": {"naukri": {"agent": {"per_search_limit": 500}}}},
         "per_search_limit"),
    ])
    def test_a_write_is_refused_by_name(self, cfg, patch, needle):
        out = C.apply_patch(patch, path=cfg, actor="test",
                            allowed_sections=("servers.naukri",))
        assert out["status"] == "refused"
        joined = " ".join(out["refusals"])
        assert needle in joined and "tier C" in joined

    def test_confirm_widen_does_not_unlock_tier_c(self, cfg):
        out = C.apply_patch({"servers": {"naukri": {"agent": {"mode": "auto"}}}},
                            path=cfg, actor="test",
                            allowed_sections=("servers.naukri",),
                            confirm_widen=True)
        assert out["status"] == "refused"

    def test_a_HAND_EDITED_tier_c_value_is_refused_on_LOAD(self, cfg, caplog):
        """The whole point of tier C: not "refused on write", NOT LOADABLE.

        A write path guard is worthless if the file is the attack surface, and
        the file is exactly the surface a text editor reaches.
        """
        write_config(cfg, servers={"naukri": {"agent": {
            "enabled": True, "mode": "auto", "min_fit_score": 0,
            "blocklist": {"enabled": False},
        }}})
        loaded = C.current()
        agent = loaded.policy.server("naukri")["agent"]
        assert agent["enabled"] is False
        assert agent["mode"] == "dry_run"
        assert agent["min_fit_score"] == 70
        assert agent["blocklist"]["enabled"] is True
        assert len(loaded.tier_c_refusals) == 4
        assert all("REFUSED" in r for r in loaded.tier_c_refusals)

    def test_the_refusal_is_LOUD_not_silent(self, cfg, caplog):
        write_config(cfg, servers={"naukri": {"agent": {"mode": "auto"}}})
        with caplog.at_level("ERROR", logger="jobcore.config"):
            loaded = C.current()
        assert loaded.tier_c_refusals
        assert any("REFUSED" in rec.message or "REFUSED" in rec.getMessage()
                   for rec in caplog.records), caplog.text

    def test_a_tier_c_value_EQUAL_to_the_python_one_is_a_harmless_echo(self, cfg):
        """The file may DISPLAY them for transparency. It may not decide them."""
        write_config(cfg, servers={"naukri": {"agent": {
            "enabled": False, "mode": "dry_run", "min_fit_score": 70}}})
        loaded = C.current()
        assert loaded.tier_c_refusals == ()


class TestSectionScoping:
    def test_a_server_cannot_write_a_siblings_section(self, cfg):
        out = C.apply_patch({"servers": {"naukri": {"display_min_score": 10}}},
                            path=cfg, actor="uplers",
                            allowed_sections=("candidate", "scoring",
                                              "servers.uplers"))
        assert out["status"] == "refused"
        assert "naukri" in " ".join(out["refusals"])

    def test_shared_sections_are_writable_from_anywhere(self, cfg):
        out = C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}},
                            path=cfg, actor="uplers",
                            allowed_sections=("candidate", "scoring",
                                              "servers.uplers"))
        assert out["status"] == "ok", out


class TestUndeclaredKeys:
    def test_a_patch_introducing_an_undeclared_key_is_refused(self, cfg):
        out = C.apply_patch({"scoring": {"invented_knob": 7}}, path=cfg,
                            actor="test")
        assert out["status"] == "refused"
        assert "decoy" in " ".join(out["refusals"])

    def test_unknown_keys_already_in_the_file_are_preserved_and_REPORTED(self, cfg):
        write_config(cfg, servers={"futureserver": {"some_setting": 1}})
        loaded = C.current()
        assert "servers.futureserver.some_setting" in loaded.unknown_keys
        # preserved, so a newer server's section is not destroyed by an older one
        assert loaded.policy.servers["futureserver"]["some_setting"] == 1


class TestValidationAtRead:
    def test_malformed_json_falls_back_to_defaults_and_says_so(self, cfg):
        cfg.write_text("{ this is not json", encoding="utf-8")
        loaded = C.current()
        assert loaded.policy == P.DEFAULT_POLICY
        assert "not valid JSON" in loaded.config_error
        assert "error" in loaded.config_status

    def test_an_out_of_range_value_falls_back_WHOLE_never_half_applied(self, cfg):
        write_config(cfg, scoring={"weights": {"skills": 2.0, "experience": 0.4},
                                   "bonuses": {"hybrid": 4}})
        loaded = C.current()
        assert loaded.policy.scoring.weights.skills == 0.6
        assert loaded.policy.scoring.bonuses.hybrid == 3, (
            "a half-applied policy is worse than an ignored one"
        )
        assert loaded.config_error is not None

    def test_it_never_raises_at_import_or_read(self, cfg):
        """linkedin_own's bare float(os.environ[...]) dies at import.

        That is the failure mode to design away from: a malformed value must
        never take the server down.
        """
        for content in ("", "null", "[]", '{"scoring": 5}', '{"candidate": 7}'):
            cfg.write_text(content, encoding="utf-8")
            C.invalidate_cache()
            loaded = C.current()
            assert loaded.policy.scoring.weights.skills == 0.6


class TestProvenance:
    def test_it_says_which_values_came_from_the_file(self, cfg):
        write_config(cfg, scoring={"weights": {"skills": 0.7, "experience": 0.3}})
        prov = C.current().provenance
        assert prov["scoring.weights.skills"] == "file"
        assert prov["scoring.bonuses.hybrid"] == "default"

    def test_the_report_carries_the_whole_diagnostic_surface(self, cfg):
        report = C.current().report(server="naukri")
        for key in ("revision", "policy_rev", "policy_hash", "source",
                    "config_status", "provenance", "unknown_keys",
                    "tier_c_refusals", "config_error", "server", "searched"):
            assert key in report, key


# ── compare-and-swap, lock, atomicity ──────────────────────────────────────

class TestCompareAndSwap:
    def test_a_stale_base_revision_conflicts_and_loses_nothing(self, cfg):
        first = C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}},
                              path=cfg, actor="a", base_revision=1)
        assert first["status"] == "ok", first
        stale = C.apply_patch({"scoring": {"bonuses": {"hybrid": 1}}},
                              path=cfg, actor="b", base_revision=1)
        assert stale["status"] == "conflict"
        assert stale["revision"] == first["revision"]
        assert C.current().policy.scoring.bonuses.hybrid == 4, "no lost update"

    def test_a_fresh_base_revision_succeeds(self, cfg):
        first = C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}},
                              path=cfg, actor="a", base_revision=1)
        second = C.apply_patch({"scoring": {"bonuses": {"hybrid": 1}}},
                               path=cfg, actor="b",
                               base_revision=first["revision"])
        assert second["status"] == "ok", second

    def test_revision_advances_by_one_per_write(self, cfg):
        out = C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}},
                            path=cfg, actor="a")
        assert out["revision"] == 2


class TestTheLock:
    def test_a_live_holder_is_refused_by_pid(self, cfg):
        lock_file = cfg.with_suffix(cfg.suffix + ".lock")
        lock_file.write_text(f"{os.getpid() if False else 1}\n", encoding="utf-8")
        # Write OUR pid via a held lock instead, then try from a "different" pid.
        held = C._ConfigLock(lock_file)
        held.acquire()
        try:
            assert held._holder() == os.getpid()
        finally:
            held.release()
        assert not lock_file.exists(), "release must remove a lock we own"

    def test_a_dead_holders_lock_is_reclaimed_never_deadlocked_on(self, tmp_path):
        lock_file = tmp_path / "x.lock"
        # A PID that cannot exist. Windows and POSIX both treat it as dead.
        lock_file.write_text("999999999\n", encoding="utf-8")
        with C._ConfigLock(lock_file, attempts=2, delay=0.01):
            assert C._ConfigLock(lock_file)._holder() == os.getpid()

    def test_garbage_in_the_lock_file_is_treated_as_stale(self, tmp_path):
        lock_file = tmp_path / "x.lock"
        lock_file.write_text("not-a-pid\n", encoding="utf-8")
        with C._ConfigLock(lock_file, attempts=2, delay=0.01):
            pass

    def test_release_never_removes_someone_elses_lock(self, tmp_path):
        lock_file = tmp_path / "x.lock"
        lock = C._ConfigLock(lock_file)
        lock.acquire()
        lock_file.write_text("999999999\n", encoding="utf-8")   # someone reclaimed
        lock.release()
        assert lock_file.exists(), "we must not delete a lock we no longer own"

    def test_it_is_reentrant_for_this_process(self, tmp_path):
        lock_file = tmp_path / "x.lock"
        with C._ConfigLock(lock_file):
            with C._ConfigLock(lock_file, attempts=2, delay=0.01):
                pass

    def test_a_live_foreign_holder_raises_rather_than_stealing(self, tmp_path,
                                                               monkeypatch):
        """A wall-clock staleness rule would steal the lock mid-write.

        On a box where a virus scan can stall a write past ten seconds, that is
        exactly how two writers end up interleaved.
        """
        lock_file = tmp_path / "x.lock"
        lock_file.write_text("4242\n", encoding="utf-8")
        monkeypatch.setattr(C, "_pid_is_alive", lambda pid: pid == 4242)
        with pytest.raises(C.ConfigLockedError) as exc:
            C._ConfigLock(lock_file, attempts=2, delay=0.001).acquire()
        assert exc.value.holder_pid == 4242


class TestAtomicWrite:
    def test_no_tmp_file_is_left_behind(self, cfg):
        C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}}, path=cfg, actor="a")
        leftovers = list(cfg.parent.glob("*.tmp"))
        assert leftovers == []

    def test_the_file_stays_valid_json_after_a_write(self, cfg):
        C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}}, path=cfg, actor="a")
        json.loads(cfg.read_text(encoding="utf-8"))


# ── the ledger, hand-edit detection, content-derived policy_rev ────────────

class TestTheLedgerMakesHandEditsFirstClass:
    def test_a_HAND_EDIT_produces_a_history_row_with_a_diff(self, cfg):
        """H3: the headline workflow calls apply_patch NEVER.

        'Open the file, change a weight, save' is the scenario the whole design
        is built around. If history is written only on the write path, that
        edit produces a changed score under an unchanged stamp and no row —
        which is exactly where he is today.
        """
        C.current()
        write_config(cfg, scoring={"weights": {"skills": 0.8, "experience": 0.2}})
        loaded = C.current()

        assert loaded.external_edit is not None
        assert loaded.external_edit["diff"]["scoring.weights.skills"] == [0.6, 0.8]
        rows = [json.loads(line) for line in
                (cfg.parent / C.LEDGER_FILENAME).read_text(encoding="utf-8").splitlines()
                if line.strip()]
        assert rows[-1]["source"] == "external_edit"
        assert rows[-1]["hash"] == loaded.policy_hash

    def test_policy_rev_is_content_derived_not_hand_maintained(self, cfg):
        first = C.current()
        write_config(cfg, scoring={"weights": {"skills": 0.8, "experience": 0.2}})
        second = C.current()
        assert second.policy_rev == first.policy_rev + 1
        assert second.revision == first.revision, (
            "the file's own integer is the CAS token and he never touched it — "
            "which is exactly why the stamp cannot be that integer"
        )

    def test_two_scores_under_different_policies_carry_different_stamps(self, cfg):
        a = C.current()
        write_config(cfg, scoring={"weights": {"skills": 0.8, "experience": 0.2}})
        b = C.current()
        assert a.policy_hash != b.policy_hash
        assert a.policy_rev != b.policy_rev

    def test_an_edit_that_cannot_move_a_score_does_not_churn_the_stamp(self, cfg):
        a = C.current()
        write_config(cfg, candidate={"name": "G. Sundeep",
                                     "headline": "Backend Engineer"})
        b = C.current()
        assert b.policy_hash == a.policy_hash
        assert b.policy_rev == a.policy_rev
        assert b.external_edit is None

    def test_a_revision_that_went_BACKWARDS_is_surfaced(self, cfg):
        """H2: Notepad writes its whole buffer and CAS is not in the room.

        The agent's write is gone, the revision regressed, and today nothing
        anywhere would report it.
        """
        C.apply_patch({"scoring": {"bonuses": {"hybrid": 4}}}, path=cfg, actor="agent")
        C.current()
        # ...he saves a stale buffer from before the agent's write.
        write_config(cfg, revision=1,
                     scoring={"weights": {"skills": 0.7, "experience": 0.3}})
        loaded = C.current()
        assert loaded.revision_regression is not None
        assert loaded.revision_regression["file_revision"] == 1
        assert loaded.revision_regression["ledger_revision"] > 1

    def test_a_tool_write_also_lands_in_the_ledger(self, cfg):
        C.current()
        out = C.apply_patch({"scoring": {"weights": {"skills": 0.7,
                                                     "experience": 0.3}}},
                            path=cfg, actor="claude-desktop")
        rows = [json.loads(line) for line in
                (cfg.parent / C.LEDGER_FILENAME).read_text(encoding="utf-8").splitlines()
                if line.strip()]
        assert rows[-1]["actor"] == "claude-desktop"
        assert rows[-1]["source"] == "apply_patch"
        assert rows[-1]["hash"] == out["policy_hash"]

    def test_the_ledger_is_append_only_and_small(self, cfg):
        C.current()
        for skills in (0.7, 0.75, 0.8):
            write_config(cfg, scoring={"weights": {"skills": skills,
                                                   "experience": 1 - skills}})
            C.current()
        text = (cfg.parent / C.LEDGER_FILENAME).read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line.strip()]
        assert len(rows) == 4          # initial + three edits
        assert all(json.loads(r) for r in rows)

    def test_an_unwritable_ledger_degrades_visibly_rather_than_crashing(self, cfg,
                                                                       monkeypatch):
        monkeypatch.setattr(C, "_append_row",
                            lambda ledger, row: "simulated: read-only volume")
        loaded = C.current()
        assert loaded.ledger_error is not None
        assert loaded.policy.scoring.weights.skills == 0.6


# ── the taxonomy-ambiguity guard ───────────────────────────────────────────

class TestTaxonomyExtensionGuard:
    def test_the_guard_can_fail(self):
        """Control: prove a collision is really detectable before trusting it."""
        from jobcore.skills import DEFAULT_TAXONOMY
        colliding = DEFAULT_TAXONOMY.extended({"reac": {"reacts"}, "react": set()})
        # A derived form both canonicals would claim resolves to NEITHER.
        assert isinstance(colliding.ambiguous_derived_keys, frozenset)

    def test_a_well_formed_extension_is_accepted(self, cfg):
        out = C.apply_patch(
            {"scoring": {"skills": {"extra_skills": {"trpc": ["trpc.io"]}}}},
            path=cfg, actor="test")
        assert out["status"] == "ok", out
        loaded = C.current()
        assert loaded.policy.scoring.skills.taxonomy_extension() == \
            {"trpc": {"trpc.io"}}

    def test_an_inverted_map_is_refused_with_the_real_signature_named(self, cfg):
        out = C.apply_patch(
            {"scoring": {"skills": {"extra_skills": {"trpc.io": "trpc"}}}},
            path=cfg, actor="test")
        assert out["status"] == "refused"
        assert "canonical" in " ".join(out["refusals"])


# ── the independence guarantee ─────────────────────────────────────────────

class TestScoringPathNeverReadsAFile:
    def test_importing_jobcore_does_not_import_the_loader(self):
        """If the scoring path could read a file, the same job would score
        differently on two machines and this would stop being a library."""
        code = (
            "import jobcore, sys;"
            "print('jobcore.config' in sys.modules)"
        )
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              cwd=str(Path(sys.executable).parent))
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False", proc.stdout

    def test_scoring_modules_import_policy_but_never_config(self):
        import ast
        src = Path(__import__("jobcore").__file__).parent
        for name in ("fit.py", "salary.py", "scoring.py", "skills.py",
                     "policy.py", "__init__.py"):
            tree = ast.parse((src / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "config":
                    pytest.fail(f"{name} imports .config at line {node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name != "jobcore.config", name

    def test_a_config_file_present_does_not_change_an_uninjected_score(self, cfg):
        """Policy is INJECTED. Nothing ambient reaches the default engine."""
        from jobcore import compute_fit_score, parse_skills
        write_config(cfg, scoring={"weights": {"skills": 0.9, "experience": 0.1}})
        C.current()   # the loader has definitely seen it
        result = compute_fit_score(
            job_skills=parse_skills("React, Node.js"),
            profile_skills=parse_skills("reactjs"),
            job_exp_str="3-5 years", profile_exp="4 years",
        )
        # 50 * 0.6 + 100 * 0.4 = 70, the DEFAULT weights.
        assert result["overall_score"] == 70


class TestDefaultDocument:
    def test_it_is_json_serialisable_and_round_trips(self):
        doc = C.default_document()
        again = json.loads(json.dumps(doc))
        assert P.Policy.from_dict(again).scoring == P.DEFAULT_SCORING_POLICY

    def test_it_displays_the_tier_c_values_without_making_them_loadable(self):
        doc = C.default_document()
        agent = doc["servers"]["naukri"]["agent"]
        assert agent["enabled"] is False and agent["mode"] == "dry_run"
        assert P.tier_for("servers.naukri.agent.mode") == P.TIER_C
