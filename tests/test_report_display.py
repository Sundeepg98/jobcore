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
import re

import pytest

from jobcore import config as jobcore_config
from jobcore.paths import display_path, relativise_known


#: A drive letter is ONE character. Without the lookbehind this matches the
#: "s:/" inside "https://" and fires on every correct URL a server emits --
#: measured today on two sibling servers, both of which had to tighten it.
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")


def _leaky(value) -> bool:
    if isinstance(value, str):
        return bool(DRIVE_PATH.search(value))
    if isinstance(value, dict):
        return any(_leaky(k) or _leaky(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_leaky(v) for v in value)
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
        assert _leaky(raw), "the path survives in prose after both fields are cleared"


class TestDisplayReachesTheProse:

    def test_no_absolute_path_survives_anywhere_in_the_report(self, broken_config):
        checkout, cfg = broken_config
        loaded = jobcore_config.current(start=checkout)
        out = loaded.report(display=lambda p: display_path(p, anchor=checkout))
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
        assert DRIVE_PATH.search(r"D:\Sundeep\projects")
