"""Renaming the path FIELDS is not enough: the loader bakes paths into prose.

Found live on 2026-08-22 by the uplers slice. ``jobcore.config`` composes
messages like ``f"{path} is not valid JSON: {exc}"`` and stores them in
``config_error``; that string is then interpolated into ``config_status`` and
into the per-call notes every scoring tool appends. One unparseable
``jobhunt.json`` therefore published the machine's directory layout from tools
that render no path of their own -- while every path FIELD in the same payload
was already clean.

So the fix has to reach the prose, and the substitution has to be EXACT: only
strings the snapshot already knows are paths. Each test that asserts a path is
gone is paired with one asserting the message is still readable, because
deleting the prose would trade the leak for an unusable error.
"""

from __future__ import annotations

import json
import os
import re

from pathlib import Path

import pytest

from jobcore import config as jobcore_config
from jobcore.paths import display_path, relativise_known


#: A drive letter is ONE character. Without the lookbehind this matches the
#: "s:/" inside "https://" and fires on every correct URL a server emits --
#: measured today on two sibling servers, both of which had to tighten it.
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")


def _leaky(value) -> bool:
    """Windows-shaped leak detection. NOT the primary assertion -- see below."""
    if isinstance(value, str):
        return bool(DRIVE_PATH.search(value))
    if isinstance(value, dict):
        return any(_leaky(k) or _leaky(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_leaky(v) for v in value)
    return False


def _contains(value, needle: str) -> bool:
    """Does the actual path string appear anywhere in this payload?

    THIS is the primary detector, and :func:`_leaky` is only a second opinion.
    CI caught why on 2026-08-22: a drive-letter regex can only fire on Windows,
    so on the Linux half of the matrix every leak assertion here passed while
    detecting nothing, and the CONTROL that was supposed to prove them
    falsifiable failed outright -- correctly, because on a POSIX runner the
    leaked path is ``/tmp/pytest-of-runner/...`` and carries no drive letter.

    Checking for the exact path the fixture created is platform-independent and
    strictly stronger: it fails on a real leak on either OS, and it cannot pass
    by being unable to see.
    """
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(k, needle) or _contains(v, needle) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(v, needle) for v in value)
    return False


@pytest.fixture
def broken_config(tmp_path, monkeypatch):
    """A checkout with a sibling config/ holding unparseable JSON."""
    checkout = tmp_path / "mcp-servers" / "someserver"
    checkout.mkdir(parents=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = cfg_dir / "jobhunt.json"
    cfg.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv(jobcore_config.ENV_CONFIG, str(cfg))
    jobcore_config.invalidate_cache()
    yield checkout, cfg
    jobcore_config.invalidate_cache()


class TestTheProseLeaksWithoutDisplay:

    def test_the_raw_report_carries_the_absolute_path_in_its_prose(self, broken_config):
        """Baseline. This is the defect, stated as a measurement."""
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        raw = loaded.report()
        assert str(cfg) in (raw["config_error"] or "")
        assert str(cfg) in raw["config_status"]

    def test_clearing_only_the_path_fields_would_not_have_been_enough(
        self, broken_config
    ):
        """CONTROL. The obvious fix, shown insufficient rather than argued about."""
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        raw = loaded.report()
        raw["source"] = None
        raw["searched"] = []
        assert _contains(raw, str(cfg)), (
            "the path survives in prose after both fields are cleared"
        )


class TestDisplayReachesTheProse:

    def test_no_absolute_path_survives_anywhere_in_the_report(self, broken_config):
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        out = loaded.report(display=lambda p: display_path(p, anchor=checkout))
        assert not _contains(out, str(cfg)), out
        assert not _contains(out, str(cfg.parent)), out
        assert not _leaky(out), out

    def test_the_error_still_says_which_file_and_why(self, broken_config):
        """Leak-free is not enough; the message has to remain an answer."""
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        out = loaded.report(display=lambda p: display_path(p, anchor=checkout))
        assert "jobhunt.json" in out["config_error"]
        assert "not valid JSON" in out["config_error"]
        assert out["config_error"].startswith("../../config/jobhunt.json")

    def test_status_for_renders_the_same_way(self, broken_config):
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        status = loaded.status_for(lambda p: display_path(p, anchor=checkout))
        assert not _contains(status, str(cfg))
        assert not _leaky(status)
        assert "jobhunt.json" in status

    def test_a_healthy_config_reports_a_relative_source(self, tmp_path, monkeypatch):
        checkout = tmp_path / "mcp-servers" / "someserver"
        checkout.mkdir(parents=True)
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        cfg = cfg_dir / "jobhunt.json"
        cfg.write_text(json.dumps({"revision": 1}), encoding="utf-8")
        monkeypatch.setenv(jobcore_config.ENV_CONFIG, str(cfg))
        jobcore_config.invalidate_cache()
        loaded = jobcore_config.current(start=checkout)
        out = loaded.report(display=lambda p: display_path(p, anchor=checkout))
        assert out["source"] == "../../config/jobhunt.json"
        assert out["searched"] == ["../../config/jobhunt.json"]
        assert not _contains(out, str(cfg))
        assert not _leaky(out)

    def test_omitting_display_changes_nothing(self, broken_config):
        """The library must not guess an anchor. Opt in, or get raw paths."""
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        assert loaded.report() == loaded.report(display=None)


class TestKnownPaths:

    def test_known_paths_includes_the_config_directory(self, broken_config):
        """The ledger and the lock file are named from it and never equal source."""
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        known = loaded.known_paths
        assert str(cfg) in known
        assert str(cfg.parent) in known

    def test_a_derived_sibling_file_is_relativised_too(self, broken_config):
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        message = "could not append to %s: disk full" % (cfg.parent / "jobhunt.history.jsonl")
        out = relativise_known(
            message,
            known=loaded.known_paths,
            render=lambda p: display_path(p, anchor=checkout),
        )
        assert not _contains(out, str(cfg.parent))
        assert not _leaky(out)
        assert "jobhunt.history.jsonl" in out


class TestSubstitutionIsExactNotHeuristic:

    def test_a_path_the_snapshot_does_not_know_is_left_alone(self, tmp_path):
        """A heuristic would eat an API route or a URL. This one cannot."""
        text = "GET /jobapi/v3/search returned 403 from https://www.naukri.com/x"
        out = relativise_known(
            text, known=[str(tmp_path / "jobhunt.json")], render=lambda p: "REL"
        )
        assert out == text

    def test_a_longer_path_is_substituted_before_a_prefix_of_it(self):
        """Shortest-first would leave a mangled tail behind."""
        known = ["/a/b", "/a/b/c/jobhunt.json"]
        out = relativise_known(
            "tried /a/b/c/jobhunt.json under /a/b",
            known=known,
            render=lambda p: {"/a/b": "DIR", "/a/b/c/jobhunt.json": "FILE"}[p],
        )
        assert out == "tried FILE under DIR"

    def test_non_strings_pass_through(self, tmp_path):
        for value in (None, 3, [1, 2], {"a": 1}):
            assert relativise_known(value, known=["/x"], render=str) is value

    def test_the_loose_regex_this_family_used_matches_https(self):
        """CONTROL for the regex at the top of this file, not for the code.

        Two sibling servers independently hit this today. Pinning it here stops
        the loose literal being copied into a third suite.
        """
        loose = re.compile(r"[A-Za-z]:[\\/]")
        assert loose.search("https://www.naukri.com/x")
        assert not DRIVE_PATH.search("https://www.naukri.com/x")
        assert DRIVE_PATH.search(r"D:\workspace\projects")


class TestTheExactDetectorIsOnlyExactForThisGeometry:
    """The primary detector has a precondition, and it is not free.

    Found by the instahyre slice after I told three slices to make
    exact-substring matching primary. It is only EXACT if a CORRECT rendering
    cannot contain the needle -- and against the real CI anchor on Linux it
    can. A naive port would have false-positived on every green run.

    The fixture above dodges it by GEOMETRY, not by luck: checkout at
    ``<tmp>/mcp-servers/someserver`` with the config two levels up, which is
    what production looks like, so the correct answer is the literal
    ``../../config/jobhunt.json`` and shares no substring with the absolute
    path. The geometry is part of the instrument, so it gets an assertion.
    """

    def test_the_correct_rendering_shares_no_substring_with_the_absolute_path(
        self, broken_config
    ):
        checkout, cfg = broken_config
        rendered = display_path(cfg, anchor=checkout)
        assert rendered == "../../config/jobhunt.json"
        assert str(cfg) not in rendered, (
            "the fixture geometry has drifted: a correct rendering now contains "
            "the needle, so _contains would fire on a green run"
        )

    def test_a_cross_root_geometry_makes_the_substring_detector_unsound(self):
        """CONTROL. The case the geometry avoids, demonstrated rather than asserted.

        Uses posixpath directly so it runs identically on every OS -- the
        failure is POSIX-shaped and would otherwise be invisible from Windows,
        which is the same blindness this whole file exists to close.
        """
        import posixpath

        anchor = "/home/runner/work/repo/repo"
        target = "/tmp/pytest-of-runner/pytest-0/x.json"
        rendered = posixpath.relpath(target, anchor)

        # Five hops: home/runner/work/repo/repo. Derived, not hand-counted --
        # a wrong literal here would make the control pass for the wrong reason.
        assert rendered == "../" * 5 + target.lstrip("/")
        assert not DRIVE_PATH.search(rendered), "no drive letter: correctly rendered"
        assert target in rendered, (
            "a PERFECTLY CORRECT rendering contains the absolute path, so an "
            "exact-substring detector fires on it. This is why the fixture "
            "puts the checkout and the config under a shared root."
        )

    def test_tightening_the_regex_does_not_rescue_a_leading_drive_letter(self):
        """A second thing I told a slice that was not the fix.

        The lookbehind stops ``https://`` matching, and nothing else. Against a
        string that BEGINS with a drive letter the lookbehind succeeds at
        position 0, so both forms match. The uplers slice measured this: the
        defect there was a Windows-shaped test fixture, and no regex change
        could have fixed it.
        """
        leading = "Z:/opt/one/jobhunt.json"
        loose = re.compile(r"[A-Za-z]:[\/]")
        assert loose.search(leading)
        assert DRIVE_PATH.search(leading), (
            "the lookbehind is not a fix for a leading drive letter; it only "
            "stops the s:/ inside https:// from matching"
        )


class TestAPathSpelledByReprIsTheSamePath:
    """The needle has two spellings and the scrubber knew one.

    Found live by the uplers slice, 2026-08-22. ``OSError.__str__`` renders its
    filename through ``repr()``, so on Windows the path arrives in an error
    message with DOUBLED separators. Every exact-substring detector in this
    family -- including the one this file made primary an hour earlier -- looked
    for the single-separator form, found nothing, and called the payload clean.

    So there was a window in which nothing was looking: the drive-letter regex
    is blind on Linux, and the exact detector was blind on the repr form.
    """

    @pytest.fixture
    def unreadable_config(self, tmp_path, monkeypatch):
        """A config file that EXISTS but cannot be read.

        The OSError is constructed rather than provoked, because provoking a
        real permission failure is not portable -- but it is constructed with
        the real 3-argument form, so ``str()`` of it is byte-identical to what
        a real failed open produces. Verified: both render the filename via
        repr().
        """
        checkout = tmp_path / "mcp-servers" / "someserver"
        checkout.mkdir(parents=True)
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        cfg = cfg_dir / "jobhunt.json"
        cfg.write_text("{}", encoding="utf-8")

        real = Path.read_bytes

        def boom(self):
            if self == cfg:
                raise OSError(13, "Permission denied", str(cfg))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", boom)
        monkeypatch.setenv(jobcore_config.ENV_CONFIG, str(cfg))
        jobcore_config.invalidate_cache()
        yield checkout, cfg
        jobcore_config.invalidate_cache()

    def test_the_repr_spelling_is_what_actually_appears(self, unreadable_config):
        """Baseline. Pins WHY the single-spelling detector saw nothing."""
        checkout, cfg = unreadable_config
        raw = jobcore_config.current(start=checkout).report()
        err = raw["config_error"] or ""
        assert "cannot read" in err, err
        if os.sep == "\\":
            # BOTH spellings appear, one per half, which is the finding stated
            # exactly: f"cannot read {path}: {exc}" writes {path} with single
            # separators and {exc} -- an OSError, whose __str__ uses repr() --
            # with doubled ones.
            assert str(cfg) in err, (
                "the {path} half of the message uses single separators"
            )
            assert str(cfg).replace("\\", "\\\\") in err, (
                "the {exc} half uses repr() separators -- THIS is the half a "
                "single-spelling detector could not see, which is how one "
                "sentence ended up with two opposite verdicts"
            )

    def test_both_spellings_are_relativised(self, unreadable_config):
        checkout, cfg = unreadable_config
        out = jobcore_config.current(start=checkout).report(
            display=lambda p: display_path(p, anchor=checkout)
        )
        err = out["config_error"] or ""
        assert not _contains(err, str(cfg))
        assert not _contains(err, str(cfg).replace("\\", "\\\\"))
        assert not _leaky(err), err
        assert "jobhunt.json" in err, "still has to say WHICH file"
        assert "Permission denied" in err, "and still has to say why"

    def test_the_single_spelling_still_works(self):
        """The common case must not regress while fixing the rare one."""
        out = relativise_known(
            "cannot read /a/b/c.json: boom",
            known=["/a/b/c.json"],
            render=lambda p: "REL",
        )
        assert out == "cannot read REL: boom"

    def test_spellings_collapse_to_one_on_posix(self):
        """CONTROL. This adds nothing where separators are not escaped."""
        from jobcore.paths import _spellings

        assert _spellings("/a/b/c.json") == ["/a/b/c.json"]
        # Derived, never hand-escaped: a wrong literal here would make this
        # pass or fail for reasons unrelated to the behaviour it checks.
        win = r"D:\a\b.json"
        assert _spellings(win) == [win, win.replace("\\", "\\\\")]

    def test_it_is_still_a_spelling_list_and_not_a_hunt(self):
        """The exactness is the safety property, so it gets its own test."""
        text = "GET /jobapi/v3/search failed via https://www.naukri.com/x"
        assert relativise_known(text, known=[r"D:\some\path"], render=lambda p: "REL") == text
