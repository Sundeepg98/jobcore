"""Import pays for file reads, never for git -- and the stamp stays true.

WHY THIS FILE EXISTS. On 2026-11-28, after a logon with the D: hard disk cold,
the uplers and instahyre MCP servers took longer than the client's 30 s connect
timeout to start. Part of what they waited for was this module: two stamps per
server, three ``git`` processes per stamp, all on the importing thread, each
capped at 5 s.

THE PROPERTY THAT MUST SURVIVE THE FIX. The stamp is frozen at import. A
per-call ``git rev-parse`` from a stale process reports the commit on DISK and
so confirms its own freshness. So ``commit`` and ``branch`` are still read at
import -- from git's own files now, not from a git process -- and everything a
git process is still needed for (``dirty``, ``dirty_files``, ``committed_at``)
is resolved once, in the background, and then frozen.

What each class here pins, and how it was proven able to fail: every test in
this file was run against the pre-fix module (jobcore 93217c3) and the
handoff records which went red and how.

  (a) TestImportRunsNoGit          a fake git that sleeps 5 s cannot slow import
  (b) TestFileReadParity           commit/branch equal what git itself says
  (c) TestFrozenAtImport           a later commit or checkout moves nothing
  (d) TestTheBackgroundHalf        the slow fields arrive once, then freeze
  (e) TestTheFallback              unreadable files fall back to git, off-thread
"""

from __future__ import annotations

import importlib.util
import json
import os
import pickle
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from jobcore import buildinfo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="these tests drive a real git repository"
)

#: The documented payload value for a field still resolving, as a LITERAL. A
#: test that read it off the module would fail on a build without the constant
#: for the wrong reason -- a missing name -- instead of on what it reports.
PENDING = "pending"

#: The variables `git rev-parse --local-env-vars` names. A suite run from inside
#: a git hook inherits them, and the helpers below must drive the fixture repo
#: they are pointed at rather than the hook's.
_LOCAL_GIT_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)


# ---------------------------------------------------------------- helpers ---


def _clean_env():
    return {k: v for k, v in os.environ.items() if k.upper() not in _LOCAL_GIT_ENV}


def _git(cwd, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=_clean_env()
    )
    if check:
        assert proc.returncode == 0, "git %s failed: %s" % (" ".join(args), proc.stderr)
    return proc.stdout.strip()


def _try_git(cwd, *args):
    """``True`` if git accepted the command -- for features older gits lack."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=_clean_env()
    ).returncode == 0


def _init(path, *extra):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", *extra)
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo, name="one.txt", text="1"):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "add %s" % name)
    return _git(repo, "rev-parse", "HEAD")


def _git_says(repo):
    """What git itself reports -- the instrument the file reader must agree with."""
    full = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return full, (None if branch == "HEAD" else branch)


def _join_background(timeout=30.0):
    """Wait out every background stamp thread, so tests cannot see each other's."""
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name.startswith("jobcore-buildinfo"):
            thread.join(max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), "a background stamp thread never finished"


@pytest.fixture(autouse=True)
def _isolated():
    _join_background()
    buildinfo.invalidate_cache()
    yield
    buildinfo.invalidate_cache()
    _join_background()


class _Gate:
    """Stands in for the background launcher: records calls, runs them on release.

    Holding the gate shut is what makes "pending" observable deterministically
    rather than by racing a real git.
    """

    def __init__(self):
        self.calls = []
        self.opened = threading.Event()

    def __call__(self, cmd, **kwargs):
        self.calls.append((threading.current_thread().name, list(cmd)))
        if not self.opened.wait(30):
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.run(cmd, **kwargs)


@pytest.fixture
def gate(monkeypatch):
    g = _Gate()
    # raising=False: on the pre-fix module there is no `_spawn` to replace, and
    # the test must then fail on its ASSERTIONS, not on this line.
    monkeypatch.setattr(buildinfo, "_spawn", g, raising=False)
    yield g
    g.opened.set()  # never strand a background thread on a failed test


def _forbid_git_on_this_thread(monkeypatch):
    """Make every git call from the CALLING thread an error.

    ``subprocess.run`` is what the pre-fix module used at import. RuntimeError,
    not OSError: the old ``_run_git`` swallowed OSError into an "unknown" stamp,
    which would make this a test that a broken build can pass.
    """
    caller = threading.get_ident()
    real = subprocess.run

    def guarded(*args, **kwargs):
        if threading.get_ident() == caller:
            raise RuntimeError("git was run on the importing thread")
        return real(*args, **kwargs)

    monkeypatch.setattr(buildinfo.subprocess, "run", guarded)


# ------------------------------------------- (a) import runs no git -------


def _distlib_launcher():
    """The console-script launcher pip ships on Windows, or ``None``.

    CreateProcess finds only ``.exe`` files on PATH, so the fake git has to BE
    one. pip builds every ``Scripts\\*.exe`` the same way: this launcher, a
    shebang naming the interpreter, then a zip whose ``__main__`` runs.
    """
    spec = importlib.util.find_spec("pip")
    if spec is None or not spec.submodule_search_locations:
        return None
    machine = platform.machine().lower()
    name = "t64-arm.exe" if "arm" in machine else (
        "t64.exe" if struct.calcsize("P") == 8 else "t32.exe"
    )
    for location in spec.submodule_search_locations:
        candidate = Path(location) / "_vendor" / "distlib" / name
        if candidate.is_file():
            return candidate.read_bytes()
    return None


def _write_slow_git(bindir, seconds):
    """A ``git`` on PATH that sleeps, then hands the call to the real git."""
    real = shutil.which("git")
    bindir.mkdir()
    if os.name == "nt":
        launcher = _distlib_launcher()
        if launcher is None:
            pytest.skip("no console-script launcher to build a fake git.exe from")
        main = (
            "import subprocess, sys, time\n"
            "time.sleep(%r)\n"
            "sys.exit(subprocess.run([%r, *sys.argv[1:]]).returncode)\n" % (seconds, real)
        )
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("__main__.py", main)
        shebang = b'#!"' + os.fsencode(sys.executable) + b'"\r\n'
        (bindir / "git.exe").write_bytes(launcher + shebang + archive.getvalue())
    else:
        script = bindir / "git"
        script.write_text('#!/bin/sh\nsleep %s\nexec "%s" "$@"\n' % (seconds, real))
        script.chmod(0o755)


_CHILD = r'''
import json, sys, threading, time
MAIN = threading.get_ident()
on_main = []
def hook(event, args):
    if event == "subprocess.Popen" and threading.get_ident() == MAIN:
        on_main.append(str(args[1])[:80])
sys.addaudithook(hook)
import jobcore  # the package a server already holds; buildinfo is NOT imported yet
sys.path.insert(0, sys.argv[1])
t0 = time.perf_counter()
import consumer  # imports jobcore.buildinfo and takes both stamps, as servers do
elapsed = time.perf_counter() - t0
print(json.dumps({
    "import_s": elapsed,
    "git_on_import_thread": on_main,
    "payload": consumer.BUILD.as_dict(),
}))
'''


class TestImportRunsNoGit:
    """(a) The lead's instrument, run as a real process with a real slow git.

    Before: every git call ran on the importing thread, so a git that takes
    5 s per call made import wait for each of them. After: none runs there.
    """

    def test_a_5_second_git_on_path_cannot_slow_import(self, tmp_path):
        repo = _init(tmp_path / "repo")
        _commit(repo)
        (repo / "consumer.py").write_text(
            "from jobcore import buildinfo as _bi\n"
            "BUILD = _bi.stamp(__file__)\n"
            "JOBCORE_BUILD = _bi.self_stamp()\n",
            encoding="utf-8",
        )
        child = tmp_path / "child.py"
        child.write_text(_CHILD, encoding="utf-8")
        bindir = tmp_path / "slowbin"
        _write_slow_git(bindir, 5)

        env = _clean_env()
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        src = str(Path(buildinfo.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, str(child), str(repo)],
            capture_output=True, text=True, env=env, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        # The background threads' fake gits outlive this child by at most the
        # 5 s they sleep; nothing waits on them and they end by themselves.
        result = json.loads(proc.stdout.strip().splitlines()[-1])

        on_thread = result["git_on_import_thread"]
        print("import_s=%.4f git_on_import_thread=%d" % (result["import_s"], len(on_thread)))
        assert on_thread == [] and result["import_s"] < 0.5, (
            "with a 5 s git on PATH, import took %.2f s and ran %d git "
            "process(es) on its own thread: %s"
            % (result["import_s"], len(on_thread), on_thread)
        )
        payload = result["payload"]
        assert payload["commit_full"] == _git(repo, "rev-parse", "HEAD")
        # The slow git cannot have answered yet, so the payload says so.
        assert payload["dirty"] == PENDING
        assert payload["committed_at"] == PENDING


# ---------------------------------------- (b) parity with git itself -------


def _plain(tmp_path):
    repo = _init(tmp_path / "plain")
    _commit(repo)
    return repo


def _slashed_branch(tmp_path):
    repo = _plain(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature/deep/name")
    _commit(repo, "two.txt", "2")
    return repo


def _detached(tmp_path):
    repo = _plain(tmp_path)
    _commit(repo, "two.txt", "2")
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    return repo


def _packed(tmp_path):
    repo = _plain(tmp_path)
    _git(repo, "pack-refs", "--all")
    branch = _git(repo, "symbolic-ref", "HEAD")
    assert not (repo / ".git" / Path(branch)).exists(), "the ref is still loose"
    return repo


def _packed_then_loose(tmp_path):
    """A commit after pack-refs writes a loose ref that must win over the packed."""
    repo = _packed(tmp_path)
    _commit(repo, "two.txt", "2")
    return repo


def _worktree(tmp_path):
    main = _plain(tmp_path)
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "wt-branch", str(linked))
    _commit(linked, "in-worktree.txt", "w")
    return linked


def _packed_worktree(tmp_path):
    linked = _worktree(tmp_path)
    _git(linked, "pack-refs", "--all")
    return linked


def _symref_chain(tmp_path):
    repo = _plain(tmp_path)
    branch = _git(repo, "symbolic-ref", "HEAD")
    _git(repo, "symbolic-ref", "refs/heads/alias", branch)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/alias")
    return repo


def _tag_named_like_the_branch(tmp_path):
    """git abbreviates to ``heads/<name>`` here, and a naive reader would not."""
    repo = _plain(tmp_path)
    _git(repo, "checkout", "-q", "-b", "release")
    _git(repo, "tag", "release")
    return repo


def _relative_gitdir_file(tmp_path):
    """A submodule's shape: ``.git`` is a FILE with a relative ``gitdir:``."""
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--separate-git-dir", str(tmp_path / "store.git"), str(work))
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "commit.gpgsign", "false")
    # Delete, then write: Git for Windows marks .git HIDDEN, and Windows refuses
    # to open a hidden file for truncation (PermissionError) -- not to delete it.
    (work / ".git").unlink()
    (work / ".git").write_text("gitdir: ../store.git\n", encoding="utf-8")
    _commit(work)
    return work


def _sha256(tmp_path):
    repo = tmp_path / "sha256"
    repo.mkdir()
    if not _try_git(repo, "init", "-q", "--object-format=sha256"):
        pytest.skip("this git cannot create a SHA-256 repository")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _commit(repo)
    return repo


PARITY_CASES = {
    "plain": _plain,
    "branch-with-slashes": _slashed_branch,
    "detached-head": _detached,
    "packed-ref": _packed,
    "packed-then-loose": _packed_then_loose,
    "worktree": _worktree,
    "packed-worktree": _packed_worktree,
    "symref-chain": _symref_chain,
    "tag-named-like-branch": _tag_named_like_the_branch,
    "relative-gitdir-file": _relative_gitdir_file,
    "sha256": _sha256,
}


class TestFileReadParity:
    """(b) The file reader answers exactly what ``git rev-parse`` answers.

    The expectation is taken from git itself, so a reader bug cannot agree with
    a test bug. Git is then FORBIDDEN on the calling thread and the background
    launcher is made to fail: whatever ``stamp`` returns for commit and branch
    can only have come from the files.
    """

    @pytest.mark.parametrize("case", sorted(PARITY_CASES))
    def test_commit_and_branch_match_git(self, case, tmp_path, monkeypatch):
        repo = PARITY_CASES[case](tmp_path)
        full, branch = _git_says(repo)

        _forbid_git_on_this_thread(monkeypatch)

        def no_background_git(*args, **kwargs):
            raise RuntimeError("git refused")

        monkeypatch.setattr(buildinfo, "_spawn", no_background_git, raising=False)

        held = buildinfo.stamp(repo)
        assert held.source == "git", held
        assert held.commit_full == full
        assert held.commit == full[: buildinfo.SHORT_HASH_LENGTH]
        assert held.branch == branch

    def test_a_repository_with_no_commits_is_known_without_git(self, tmp_path, monkeypatch):
        repo = _init(tmp_path / "empty")
        _forbid_git_on_this_thread(monkeypatch)
        held = buildinfo.stamp(repo)
        assert held.source == "unknown"
        assert held.commit is None
        assert "no commits" in held.detail

    def test_a_directory_outside_any_repository_is_known_without_git(
        self, tmp_path, monkeypatch
    ):
        plain = tmp_path / "plain-dir"
        plain.mkdir()
        _forbid_git_on_this_thread(monkeypatch)
        held = buildinfo.stamp(plain)
        assert held.source == "unknown"
        assert "work tree" in held.detail


# ------------------------------------------------ (c) frozen at import -----


class TestFrozenAtImport:
    """(c) What was read at import does not move -- on either read path."""

    def test_a_commit_and_a_checkout_after_import_change_nothing(self, tmp_path):
        repo = _init(tmp_path / "repo")
        first = _commit(repo)
        first_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

        held = buildinfo.stamp(repo)
        payload_at_import = {k: held.as_dict()[k] for k in ("commit", "commit_full", "branch")}

        _commit(repo, "two.txt", "2")
        _git(repo, "checkout", "-q", "-b", "elsewhere")
        third = _commit(repo, "three.txt", "3")
        assert third != first

        assert buildinfo.stamp(repo) is held
        assert held.commit_full == first
        assert held.branch == first_branch
        assert {k: held.as_dict()[k] for k in payload_at_import} == payload_at_import

    def test_control_a_fresh_resolve_follows_the_tree(self, tmp_path):
        """CONTROL: the same steps through ``resolve`` DO move, so (c) can fail."""
        repo = _init(tmp_path / "repo")
        first = _commit(repo)
        assert buildinfo.resolve(repo).commit_full == first
        _git(repo, "checkout", "-q", "-b", "elsewhere")
        third = _commit(repo, "three.txt", "3")
        fresh = buildinfo.resolve(repo)
        assert fresh.commit_full == third
        assert fresh.branch == "elsewhere"


# ----------------------------------------- (d) the background half --------


class TestTheBackgroundHalf:
    """(d) ``dirty``, ``dirty_files`` and ``committed_at`` arrive once, then freeze."""

    def test_pending_then_delivered_then_frozen(self, tmp_path, gate):
        repo = _init(tmp_path / "repo")
        full = _commit(repo)
        committed_at = _git(repo, "show", "-s", "--format=%cI", "HEAD")

        held = buildinfo.stamp(repo)

        # 1. Not delivered yet: the payload says so, and does not wait to say it.
        t0 = time.perf_counter()
        early = held.as_dict()
        assert time.perf_counter() - t0 < 0.5, "the payload waited on git"
        for name in ("committed_at", "dirty", "dirty_files"):
            assert early[name] == PENDING, (name, early)
        assert early["commit_full"] == full, "the identity must not be pending"
        assert early["detail"].startswith("still resolving")

        # 2. An ATTRIBUTE read waits for the value instead of inventing one.
        seen = []
        reader = threading.Thread(target=lambda: seen.append(held.dirty), daemon=True)
        reader.start()
        reader.join(0.3)
        assert reader.is_alive() and seen == [], "the attribute did not wait"
        with pytest.raises(TimeoutError):
            held.settled(timeout=0.05)

        # 3. Released: it arrives, with the values git gives.
        gate.opened.set()
        reader.join(30)
        assert seen == [False]
        done = held.settled()
        assert (done.dirty, done.dirty_files) == (False, 0)
        assert done.committed_at == committed_at
        assert done.detail is None
        assert held.as_dict() == done.as_dict()

        # The background ran on its own thread, and asked about the IMPORTED
        # commit by hash -- never HEAD, which may have moved by then.
        assert [name for name, _ in gate.calls] == ["jobcore-buildinfo"] * 2
        assert "status" in gate.calls[0][1]
        assert full in gate.calls[1][1] and "HEAD" not in gate.calls[1][1]

        # 4. Frozen: the tree changes, the stamp does not, and git is not asked again.
        (repo / "scratch.py").write_text("x = 1", encoding="utf-8")
        _commit(repo, "two.txt", "2")
        for _ in range(3):
            assert held.as_dict() == done.as_dict()
            assert held.dirty is False and held.dirty_files == 0
        assert len(gate.calls) == 2

    def test_a_dirty_tree_is_reported_dirty(self, tmp_path):
        """Modified AND untracked both count -- through the waiting attribute."""
        repo = _init(tmp_path / "repo")
        _commit(repo)
        (repo / "one.txt").write_text("changed", encoding="utf-8")
        (repo / "untracked.py").write_text("x = 1", encoding="utf-8")
        held = buildinfo.stamp(repo)
        assert held.dirty is True
        assert held.dirty_files == 2

    def test_a_background_that_never_answers_is_given_up_on(
        self, tmp_path, gate, monkeypatch
    ):
        """Never wait forever: past its deadline the stamp freezes as unknown."""
        repo = _init(tmp_path / "repo")
        _commit(repo)
        monkeypatch.setattr(buildinfo, "GIT_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(buildinfo, "_BACKGROUND_SLACK_SECONDS", 0.2, raising=False)

        held = buildinfo.stamp(repo)
        t0 = time.perf_counter()
        assert held.dirty is None  # waited out the deadline, then gave up
        assert time.perf_counter() - t0 < 5
        assert "did not finish" in held.detail
        gate.opened.set()
        _join_background()
        assert held.dirty is None, "a late answer moved a frozen stamp"

    def test_the_environment_cannot_point_git_at_another_repository(
        self, tmp_path, monkeypatch
    ):
        """A suite run from inside a git hook has GIT_DIR and GIT_INDEX_FILE set.

        Inherited, they would make git describe the HOOK's repository. Here the
        other repository is dirty and on another commit; neither may leak in.
        """
        repo = _init(tmp_path / "repo")
        full = _commit(repo)
        other = _init(tmp_path / "other")
        _commit(other, "o.txt", "o")
        (other / "o.txt").write_text("changed", encoding="utf-8")

        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))
        monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
        held = buildinfo.stamp(repo)
        assert held.commit_full == full
        assert (held.dirty, held.dirty_files) == (False, 0)


class TestALiveStampIsStillABuildStamp:
    """It must behave as the value type it stands in for."""

    def test_the_pending_value_is_the_documented_literal(self):
        assert buildinfo.PENDING == PENDING

    def test_value_semantics(self, tmp_path):
        repo = _init(tmp_path / "repo")
        _commit(repo)
        held = buildinfo.stamp(repo)
        plain = held.settled()
        assert isinstance(held, buildinfo.BuildStamp)
        assert type(plain) is buildinfo.BuildStamp
        assert held == plain and plain == held
        assert hash(held) == hash(plain)
        assert pickle.loads(pickle.dumps(held)) == plain
        assert set(held.as_dict()) == set(plain.as_dict())
        with pytest.raises(Exception):
            held.commit = "x" * 12  # still frozen


# ---------------------------------------------------- (e) the fallback -----


class TestTheFallback:
    """(e) When the files cannot be read, git answers -- still off the import thread."""

    def test_an_unreadable_git_dir_falls_back_to_git_in_the_background(
        self, tmp_path, gate, monkeypatch
    ):
        repo = _init(tmp_path / "repo")
        _commit(repo)
        full, branch = _git_says(repo)

        def unreadable(*args, **kwargs):
            raise PermissionError("simulated: .git cannot be read")

        monkeypatch.setattr(buildinfo, "_read_head", unreadable, raising=False)
        _forbid_git_on_this_thread(monkeypatch)

        held = buildinfo.stamp(repo)
        early = held.as_dict()
        assert early["commit"] == PENDING
        assert early["source"] == PENDING
        assert "resolving through git in the background" in early["detail"]

        gate.opened.set()
        done = held.settled()
        assert done.source == "git"
        assert done.commit_full == full
        assert done.branch == branch
        assert (done.dirty, done.dirty_files) == (False, 0)
        assert gate.calls and all(name == "jobcore-buildinfo" for name, _ in gate.calls)

    def test_a_ref_format_the_reader_does_not_know_falls_back(
        self, tmp_path, gate, monkeypatch
    ):
        """The real-world case: a reftable repository has no loose refs to read."""
        repo = tmp_path / "reftable"
        repo.mkdir()
        if not _try_git(repo, "init", "-q", "--ref-format=reftable"):
            pytest.skip("this git cannot create a reftable repository")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "commit.gpgsign", "false")
        _commit(repo)
        full, branch = _git_says(repo)
        _forbid_git_on_this_thread(monkeypatch)

        held = buildinfo.stamp(repo)
        assert held.as_dict()["commit"] == PENDING
        gate.opened.set()
        done = held.settled()
        assert (done.source, done.commit_full, done.branch) == ("git", full, branch)
