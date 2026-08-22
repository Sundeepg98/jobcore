"""The build stamp must describe the code that is LOADED, not the code on disk.

The test that matters here is :func:`test_stamp_is_frozen_after_a_later_commit`:
it simulates the exact failure the module exists to prevent by committing to a
real temporary repository after the stamp was taken, and asserting the stamp
still names the old commit. Asserting the hash is merely non-empty would pass
against a per-call ``git rev-parse``, which is the broken implementation.

Every freeze assertion is paired with a CONTROL that runs the same steps against
a deliberately naive per-call implementation and asserts the control DISAGREES.
A check that cannot fail certifies nothing; the controls are the proof that
these can.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from jobcore import buildinfo


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="these tests drive a real git repository"
)


# ---------------------------------------------------------------- helpers ---


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert proc.returncode == 0, "git %s failed: %s" % (" ".join(args), proc.stderr)
    return proc.stdout.strip()


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo, filename, text):
    (repo / filename).write_text(text, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", "add %s" % filename)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _clean_memo():
    buildinfo.invalidate_cache()
    yield
    buildinfo.invalidate_cache()


# ------------------------------------------------- the staleness contract ---


class TestFrozenAtImport:

    def test_stamp_is_frozen_after_a_later_commit(self, tmp_path):
        """A commit made after the stamp was taken does not change the stamp.

        This IS the stale-process simulation. `held` stands for the module
        constant a server captured at import; the second commit stands for the
        fix someone lands while that server keeps running.
        """
        repo = _init_repo(tmp_path)
        first = _commit(repo, "one.txt", "1")

        held = buildinfo.stamp(repo)
        assert held.source == "git"
        assert held.commit_full == first

        second = _commit(repo, "two.txt", "2")
        assert second != first

        assert buildinfo.stamp(repo).commit_full == first, (
            "the stamp followed the working tree; a stale process would now "
            "claim to be running code it has never loaded"
        )
        assert buildinfo.stamp(repo) is held

    def test_a_per_call_implementation_fails_that_assertion(self, tmp_path):
        """CONTROL. Proves the assertion above is not vacuous.

        Runs the identical steps against `resolve`, which is honest about
        reading the tree right now, and asserts it DOES follow the new commit.
        If this ever passes with equality, the test above has stopped measuring
        anything.
        """
        repo = _init_repo(tmp_path)
        first = _commit(repo, "one.txt", "1")
        naive = buildinfo.resolve(repo)
        assert naive.commit_full == first

        second = _commit(repo, "two.txt", "2")
        assert buildinfo.resolve(repo).commit_full == second
        assert buildinfo.resolve(repo).commit_full != naive.commit_full

    def test_a_held_stamp_and_a_fresh_resolve_disagree_when_the_process_is_stale(
        self, tmp_path
    ):
        """The disagreement IS the diagnosis, and it is machine-readable."""
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        held = buildinfo.stamp(repo)
        second = _commit(repo, "two.txt", "2")

        fresh = buildinfo.resolve(repo)
        assert held.commit_full != fresh.commit_full
        assert fresh.commit_full == second

    def test_the_second_call_does_not_shell_out(self, tmp_path, monkeypatch):
        """Frozen means frozen: no git process runs on a memo hit.

        A `stamp` that shelled out every time would report the CURRENT commit
        from a STALE process, which reads as confirmation of a fix that is not
        loaded. Blowing up on the second subprocess call is how that is caught
        even if a future implementation happens to return the right value.
        """
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        buildinfo.stamp(repo)

        def explode(*args, **kwargs):
            raise AssertionError("stamp() ran git again on a cached repo")

        monkeypatch.setattr(buildinfo.subprocess, "run", explode)
        assert buildinfo.stamp(repo).source == "git"

    def test_resolved_at_is_the_moment_of_resolution_not_of_reading(self, tmp_path):
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        held = buildinfo.stamp(repo)
        again = buildinfo.stamp(repo)
        assert held.resolved_at == again.resolved_at


# ------------------------------------------------------ what it reports -----


class TestWhatItReports:

    def test_short_and_full_hash_agree(self, tmp_path):
        repo = _init_repo(tmp_path)
        full = _commit(repo, "one.txt", "1")
        s = buildinfo.stamp(repo)
        assert s.commit_full == full
        assert s.commit == full[: buildinfo.SHORT_HASH_LENGTH]
        assert len(s.commit) == buildinfo.SHORT_HASH_LENGTH

    def test_a_clean_tree_is_not_dirty(self, tmp_path):
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        s = buildinfo.stamp(repo)
        assert s.dirty is False
        assert s.dirty_files == 0

    def test_a_modified_tracked_file_makes_it_dirty(self, tmp_path):
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        (repo / "one.txt").write_text("changed", encoding="utf-8")
        s = buildinfo.stamp(repo)
        assert s.dirty is True
        assert s.dirty_files == 1

    def test_an_untracked_file_also_counts_as_dirty(self, tmp_path):
        """An untracked module that gets imported differs from the commit too."""
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        (repo / "scratch.py").write_text("x = 1", encoding="utf-8")
        s = buildinfo.stamp(repo)
        assert s.dirty is True
        assert s.dirty_files == 1

    def test_committed_at_is_iso8601(self, tmp_path):
        from datetime import datetime

        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        s = buildinfo.stamp(repo)
        assert datetime.fromisoformat(s.committed_at) is not None

    def test_a_file_start_path_resolves_via_its_directory(self, tmp_path):
        """Servers pass ``__file__``; that must work, not fall back to unknown."""
        repo = _init_repo(tmp_path)
        full = _commit(repo, "mod.py", "x = 1")
        s = buildinfo.stamp(repo / "mod.py")
        assert s.source == "git"
        assert s.commit_full == full


# ---------------------------------------------- unknown is never a guess ----


class TestUnknownIsAValue:

    def test_a_directory_that_is_not_a_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        s = buildinfo.stamp(plain)
        assert s.source == "unknown"
        assert s.commit is None
        assert s.commit_full is None
        assert "work tree" in s.detail

    def test_a_repository_with_no_commits_says_so_specifically(self, tmp_path):
        repo = _init_repo(tmp_path)
        s = buildinfo.stamp(repo)
        assert s.source == "unknown"
        assert s.commit is None
        assert "no commits" in s.detail

    def test_no_git_executable_says_so_rather_than_guessing(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        monkeypatch.setattr(buildinfo.shutil, "which", lambda name: None)
        s = buildinfo.resolve(repo)
        assert s.source == "unknown"
        assert s.commit is None
        assert "git executable" in s.detail

    def test_a_hanging_git_degrades_instead_of_wedging_import(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=buildinfo.GIT_TIMEOUT_SECONDS)

        monkeypatch.setattr(buildinfo.subprocess, "run", timeout)
        s = buildinfo.resolve(repo)
        assert s.source == "unknown"
        assert s.commit is None

    def test_resolution_never_raises(self, tmp_path, monkeypatch):
        """Import of a server must not die because git misbehaved."""

        def boom(*args, **kwargs):
            raise OSError("git blew up")

        monkeypatch.setattr(buildinfo.subprocess, "run", boom)
        s = buildinfo.resolve(tmp_path)
        assert s.source == "unknown"

    def test_an_unresolvable_start_is_unknown_not_an_exception(self):
        s = buildinfo.stamp(None)
        assert s.source == "unknown"


# ----------------------------------------------------- the payload block ----


class TestBuildBlock:

    def test_block_carries_the_frozen_code_stamp(self, tmp_path):
        repo = _init_repo(tmp_path)
        full = _commit(repo, "one.txt", "1")
        block = buildinfo.build_block(repo)
        assert block["code"]["commit_full"] == full
        assert block["code"]["source"] == "git"

    def test_process_timing_is_derived_fresh_not_frozen(self):
        """A cached uptime is a lie that grows, so uptime is recomputed."""
        clock = buildinfo.ProcessClock()
        first = clock.as_dict()["uptime_seconds"]
        for _ in range(200000):
            pass
        second = clock.as_dict()["uptime_seconds"]
        assert second >= first

    def test_started_at_does_not_move(self):
        clock = buildinfo.ProcessClock()
        assert clock.as_dict()["started_at"] == clock.as_dict()["started_at"]

    def test_extra_keys_merge_without_clobbering_code(self, tmp_path):
        repo = _init_repo(tmp_path)
        _commit(repo, "one.txt", "1")
        block = buildinfo.build_block(
            repo, clock=buildinfo.ProcessClock(), extra={"config": {"policy_hash": "abc"}}
        )
        assert set(block) == {"code", "process", "config"}
        assert block["config"]["policy_hash"] == "abc"
        assert block["process"]["pid"] > 0
