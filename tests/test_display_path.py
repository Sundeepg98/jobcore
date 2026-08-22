"""A displayed path must be leak-free AND still be an answer.

Two properties are in tension and both are asserted here: no result may carry a
drive letter or an absolute root, and two different paths may not render as the
same string. The second is what rules out the obvious fixes -- deleting the
field, or reducing everything to its basename, which turns a list of searched
paths into N copies of "jobhunt.json".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobcore.paths import DISPLAY_TAIL_PARTS, display_path


#: What the 2026-08-20 sweep actually saw in tool output. The lookbehind is
#: load-bearing: a drive letter is ONE character, so the bare form matches the
#: "s:/" inside "https://" and fires on every correct URL a server emits. Two
#: sibling suites hit that today; tests/test_report_display.py pins the
#: difference as a control so the loose literal is not copied into a third.
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")


def _leaks(text) -> bool:
    return bool(text) and bool(DRIVE_PATH.search(str(text)))


class TestNothingAbsoluteSurvives:

    def test_a_path_under_the_anchor_is_anchor_relative(self, tmp_path):
        anchor = tmp_path / "checkout"
        out = display_path(anchor / "exports" / "apps.json", anchor=anchor)
        assert out == "exports/apps.json"
        assert not _leaks(out)

    def test_the_shared_config_two_levels_up_stays_relative(self, tmp_path):
        """The real case: <root>/mcp-servers/<server> reading ../../config."""
        anchor = tmp_path / "mcp-servers" / "naukri"
        cfg = tmp_path / "config" / "jobhunt.json"
        out = display_path(cfg, anchor=anchor)
        assert out == "../../config/jobhunt.json"
        assert not _leaks(out)

    def test_a_path_under_home_is_home_anchored(self, tmp_path):
        home = tmp_path / "home" / "someone"
        anchor = tmp_path / "elsewhere" / "a" / "b" / "c" / "d" / "e" / "checkout"
        out = display_path(home / ".config" / "jobhunt.json", anchor=anchor, home=home)
        assert out == "~/.config/jobhunt.json"
        assert not _leaks(out)

    def test_a_path_under_neither_anchor_keeps_its_tail(self, tmp_path):
        home = tmp_path / "home" / "someone"
        anchor = tmp_path / "aa" / "bb" / "cc" / "dd" / "ee" / "ff" / "checkout"
        far = Path(tmp_path.anchor) / "srv" / "shared" / "conf" / "jobhunt.json"
        out = display_path(far, anchor=anchor, home=home)
        assert out.startswith(".../")
        assert out.endswith("jobhunt.json")
        assert len(out.split("/")) == DISPLAY_TAIL_PARTS + 1  # the "..." marker
        assert not _leaks(out)

    def test_none_and_empty_pass_through_unchanged(self, tmp_path):
        """"No file was found" must stay distinguishable from a path."""
        assert display_path(None, anchor=tmp_path) is None
        assert display_path("", anchor=tmp_path) == ""


class TestItStillAnswersTheQuestion:

    def test_two_different_paths_do_not_render_identically(self, tmp_path):
        """The failure the basename fallback caused: N searched paths, one string."""
        home = tmp_path / "home" / "someone"
        anchor = tmp_path / "aa" / "bb" / "cc" / "dd" / "ee" / "ff" / "checkout"
        root = Path(tmp_path.anchor)
        a = display_path(root / "srv" / "one" / "jobhunt.json", anchor=anchor, home=home)
        b = display_path(root / "srv" / "two" / "jobhunt.json", anchor=anchor, home=home)
        assert a != b, "two searched paths collapsed to the same display string"

    def test_a_control_basename_implementation_does_collapse_them(self, tmp_path):
        """CONTROL. Proves the test above is not vacuous."""
        root = Path(tmp_path.anchor)
        a = (root / "srv" / "one" / "jobhunt.json").name
        b = (root / "srv" / "two" / "jobhunt.json").name
        assert a == b

    def test_the_rendered_path_names_the_actual_file(self, tmp_path):
        anchor = tmp_path / "checkout"
        out = display_path(anchor / "data" / "profile.json", anchor=anchor)
        assert out.endswith("profile.json")


class TestEdges:

    def test_a_directory_named_with_dots_is_not_read_as_parent_hops(self, tmp_path):
        """Counting parts, not substrings: 'a..b' is a directory, not two hops."""
        anchor = tmp_path / "checkout"
        out = display_path(anchor / "a..b" / "f.json", anchor=anchor)
        assert out == "a..b/f.json"

    def test_a_single_component_path_has_no_misleading_marker(self, tmp_path):
        out = display_path(Path(tmp_path.anchor) / "loose.json", anchor=tmp_path / "x" / "y")
        assert out == "loose.json"
        assert not out.startswith(".../")

    @pytest.mark.parametrize("raw", ["exports/apps.json", "../../config/jobhunt.json"])
    def test_an_already_relative_path_stays_usable(self, raw, tmp_path):
        out = display_path(raw, anchor=tmp_path)
        assert not _leaks(out)
        assert out.endswith(Path(raw).name)
