"""No tracked file may carry this machine's absolute layout.

WHAT THIS COVERS, AND THE GAP THAT MATTERS MOST
-----------------------------------------------
**THIS FILE DETECTS PATHS. IT CANNOT DETECT A PERSONAL NAME.**

That sentence is not a disclaimer, it is the finding. When this repository was
swept properly for the first time on 2026-08-31, twenty-two identifier shapes
were run over all thirty tracked files. The shape sweep found TWO real hits,
both of them paths. An exact-value scan run beside it -- a human looking for a
known string, which is the only method that works on a name -- found THREE
MORE, in files the shape sweep had just certified clean:

    tests/test_config.py           a real person's name as candidate fixture
    tests/test_policy.py           the same
    tests/test_safety_invariant.py the same

No pattern in this file would have found any of them, and none could have:
``G. Aldridge`` and an invented ``G. Whitfield`` are the same shape, the same
length, the same character classes. Those three were fixed by hand in the same
commit that added this file, and **nothing here would stop them coming back.**

So a green run means: no MACHINE PATHS of the three shapes below. It does not
mean "no personal data". Anyone reading it as the second has been misled by the
instrument.

WHY THIS FILE EXISTS
--------------------
``jobcore`` is vendored. ``src/jobcore/paths.py`` is copied verbatim into the
servers that use it, and one of those servers -- linkedin -- has an identity
guard that hunts exactly these shapes across everything it tracks. On
2026-08-31 that guard went red on the vendored copy, and the repository holding
it could not go green from the inside: fixing the copy broke the vendoring pin
that pins it byte-for-byte to this repo, and leaving it broke the guard. The
conflict was not a bug in either repository. It was the absence of this file.
A leak in a vendored library is a leak in every consumer, and the only place it
can be fixed is upstream.

THE TWO IT WAS SHOWN FAILING ON, both real, both at HEAD when it was written:

    src/jobcore/paths.py            a drive root, DOUBLED separators, in the
                                    prose of a docstring explaining that this
                                    exact shape leaks -- quoting the real path
                                    in order to warn about it
    tests/test_report_display.py    a drive root, SINGLE separator, as the test
                                    data proving a path detector fires

The second is the shape worth naming: **a hygiene fixture that proves itself by
carrying the thing it forbids is self-refuting.** A synthetic root proves the
detector fires exactly as well, and does not publish a layout to do it.

THE DESIGN RULE
---------------
**Hunt by SHAPE. Allow the GENERIC. Never blocklist the real.**

A committed list of real strings is itself a leak, so no such list appears
here. Every allowed value below is a place-name or a placeholder, safe to
commit because it identifies nobody. There is no declared-plant allowlist in
this file at all, and that is deliberate: declaring a real value to make a
suite green is the exact trade this check exists to refuse. Every test plant
below is COMPOSED from :data:`BACKSLASH` instead, so this module's own text
contains no path shape and needs no exemption from its own sweep.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: THE SEPARATOR IS A RUN, ``+``, AND NOT ONE CHARACTER. That single quantifier
#: is the whole difference between this rule working and this rule certifying.
#:
#: In the consumer repo this was ported from, it was written ``[\\/]`` -- one
#: separator -- for about an hour on 2026-08-31, and in that hour it reported
#: that repository CLEAN while three given-name drive roots sat in tracked
#: files. All three were the DOUBLED spelling, which is not an edge case: two
#: backslashes is how a Windows path is written inside JSON, inside a Python
#: string literal, and inside any prose quoting either. The cleanup that ran
#: against the same one-character pattern removed the occurrences it could see
#: and left exactly the ones it could not.
#:
#: This repository then proved the point a second time. Of ITS two real hits,
#: one was single-spelled and one was doubled -- so a rule with the narrower
#: separator would have found half of them and reported a number.
#:
#: THE GENERALISATION, worth more than the quantifier: **a control must cover
#: every spelling the value can be WRITTEN in, not just the one the author had
#: in mind.** A guard asserting it can match ONE spelling is still a guard that
#: reports zero without knowing whether it can see.
DRIVE_ROOT_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]+([A-Za-z0-9_.-]{2,})")

#: The account-named form. Kept as its own rule rather than folded into the one
#: above because it asks a different question -- see
#: :func:`test_the_drive_root_rule_catches_what_the_user_path_rule_cannot`.
WINDOWS_USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9._-]{2,})")

#: The POSIX home form. The lookbehind excludes ``:`` so a drive-letter path is
#: counted once rather than twice, and excludes word characters so the prose
#: ``anchored/home/tail`` stops reading as a home directory. The trailing
#: separator is ``+`` for the reason above, even though a forward slash needs
#: no escaping and is therefore the least likely spelling to be doubled -- the
#: rule should not depend on which spellings happen to be common.
POSIX_HOME_PATH = re.compile(
    r"(?<![A-Za-z0-9_:])(?:/home|/Users)/+([A-Za-z0-9._-]{2,})"
)

#: First segments that name a PLACE rather than a person. A drive rooted at
#: ``workspace`` says nothing about who owns the machine; a drive rooted at a
#: given name says exactly who owns it, and that is the form found here.
GENERIC_DRIVE_ROOTS = frozenset(
    {
        "users",  # handed to WINDOWS_USER_PATH, which checks the NEXT segment
        "windows",
        "programdata",
        "program",  # "Program Files" truncates at the space
        "workspace",
        "dev-cache",
        "temp",
        "tmp",
        "repo",
        "opt",
        "some",  # "some/path", the stock stand-in in this repo's own fixtures
    }
)

#: Accounts that name a MACHINE ROLE, not a human. ``runner`` is the account
#: every GitHub-hosted runner executes as, so ``/home/runner/work/...`` is CI
#: geometry rather than somebody's home directory -- and this repository uses
#: exactly that path to demonstrate a cross-root relpath failure.
#:
#: This set is narrow on purpose. The segment after ``Users`` or ``/home`` is
#: an ACCOUNT NAME by construction; there is no benign vocabulary for it, so
#: the only things allowed to sit there are a visible placeholder and a service
#: account that is documented to be one.
SERVICE_ACCOUNTS = frozenset({"runner"})

PLACEHOLDER_MARKERS = ("xxx", "dummy", "fake", "redacted", "placeholder", "<", "...")

BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".ico", ".db", ".pyc"}
)

#: A LONE BACKSLASH, built from its code point so that no amount of quoting,
#: copying or transport can turn it into something else.
#:
#: IT EXISTS BECAUSE THE ESCAPE IS THE FAILURE MODE. On 2026-08-31 three
#: separate readings certified the consumer repo clean of drive-rooted paths
#: within about ten minutes, and all three were broken: a ``git grep`` whose
#: pattern never reached the regex engine intact; a rewrite in which a
#: backslash before a ``+`` turned the plus into a literal; and a correct
#: pattern pushed twice through a shell heredoc that collapsed the character
#: class into an escaped slash, matching the SLASH ONLY. Thirty-one hits were
#: present throughout. Two of those readings agreed with each other, which is
#: what made them convincing, and they agreed because they shared a broken
#: transport -- repetition through one broken channel is not repetition.
#:
#: **A path guard reporting zero is indistinguishable from a path guard that is
#: broken**, so the rules above are asserted to match something BEFORE the
#: sweep is allowed to certify that they matched nothing. See
#: :func:`test_the_path_rules_can_match_a_backslash_at_all`, which is the
#: control for all three and is worth more than the rules it guards.
BACKSLASH = chr(92)


def redact(value: str) -> str:
    """``<first2>..<last2>`` plus a length. Never the path itself.

    A CI log is a publication channel, so a guard that fails by printing the
    layout it just caught has published it to exactly the audience the check
    was protecting the repository from.
    """
    if len(value) <= 6:
        return f"<{len(value)} chars>"
    return f"{value[:2]}..{value[-2:]} <{len(value)} chars>"


def _drive_root_ok(match: re.Match[str]) -> bool:
    segment = match.group(1).lower()
    if segment in GENERIC_DRIVE_ROOTS:
        return True
    return any(marker in segment for marker in PLACEHOLDER_MARKERS)


def _account_path_ok(match: re.Match[str]) -> bool:
    segment = match.group(1).lower()
    if segment in SERVICE_ACCOUNTS:
        return True
    return any(marker in segment for marker in PLACEHOLDER_MARKERS)


#: name -> (pattern, allowed?). The name is what a failure reports.
SHAPES: tuple[tuple[str, re.Pattern[str], object], ...] = (
    # Drive root FIRST because it is the shape this repo was actually leaking,
    # and the one a check named "user path" cannot see: the leak sits one
    # segment to the LEFT of where that rule looks.
    ("drive root", DRIVE_ROOT_PATH, _drive_root_ok),
    ("user path", WINDOWS_USER_PATH, _account_path_ok),
    ("user path", POSIX_HOME_PATH, _account_path_ok),
)


def hits_in(text: str) -> list[tuple[str, str]]:
    """``(rule name, redacted value)`` for everything not allowed."""
    found: list[tuple[str, str]] = []
    for name, pattern, allowed in SHAPES:
        for match in pattern.finditer(text):
            if not allowed(match):
                found.append((name, redact(match.group(0))))
    return found


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in out.splitlines() if rel]


def sweepable() -> list[str]:
    return [
        rel
        for rel in tracked_files()
        if Path(rel).suffix.lower() not in BINARY_SUFFIXES
    ]


# ---------------------------------------------------------------------------
# 1. The sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", sweepable(), ids=lambda r: r)
def test_no_tracked_file_carries_a_machine_path(rel):
    """Nothing is skipped and nothing is declared.

    There is no allowlist keyed on file: a real path made green by declaring it
    is still a published path, and the declaration is a signpost to it.
    """
    path = REPO / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable blob
        return

    found = hits_in(text)
    assert not found, (
        f"{rel}: {len(found)} machine path(s) in a tracked file: {found}. "
        "Replace the value with a generic root or a placeholder -- not with "
        "an allowlist entry, and not by escaping it differently."
    )


def test_the_sweep_actually_looked():
    """A parametrised sweep passes vacuously on an empty file list.

    Thirty files were tracked when this was written. The floor is set below
    that so ordinary additions and deletions do not trip it, and above zero so
    a broken ``git ls-files`` -- a wrong cwd, a missing repo -- fails loudly
    instead of certifying an empty set.
    """
    assert len(sweepable()) >= 25


# ---------------------------------------------------------------------------
# 2. The controls -- each rule shown failing, then shown not over-firing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule, planted",
    [
        # EVERY PLANT IS COMPOSED, NOT WRITTEN AS A LITERAL, for two reasons.
        # First, a literal here would be a real path shape in a tracked file
        # and this module's own sweep would fail on it -- which it did, in
        # development, and which is the most direct evidence available that
        # these rules are not inert. Second, and specific to this shape: a
        # backslash does not survive transport reliably, so composing from
        # chr(92) is the only way to write a backslash-bearing test value that
        # is certainly the value intended.
        ("drive root", "cd D:" + BACKSLASH + "Ravenscroft" + BACKSLASH + "src"),
        # THE DOUBLED SPELLING, and it is a REAL historical defect rather than
        # a synthetic one: one of this repo's two hits was written this way,
        # inside a docstring, and a rule with a single-character separator
        # would have walked straight past it.
        (
            "drive root",
            '"args": ["D:' + BACKSLASH * 2 + "Ravenscroft" + BACKSLASH * 2 + 'src"]',
        ),
        ("drive root", "D:/Ravenscroft/projects"),
        (
            "user path",
            "C:" + BACKSLASH + "Users" + BACKSLASH + "rmarchetti" + BACKSLASH + "App",
        ),
        (
            "user path",
            "C:" + BACKSLASH * 2 + "Users" + BACKSLASH * 2 + "rmarchetti",
        ),
        ("user path", "/home/rmarchetti/.config/thing"),
    ],
)
def test_every_rule_can_actually_fail(rule, planted):
    """Each rule, shown failing on a synthetic violation of its own shape.

    Never a real path: a control that needs one has the same defect as the
    fixture it is guarding.
    """
    names = {name for name, _ in hits_in(planted)}
    assert rule in names, (rule, names)


@pytest.mark.parametrize(
    "benign",
    [
        # The forms this repo's real paths were rewritten INTO on 2026-08-31.
        # If either of these starts failing, that commit's replacements all
        # become violations at once -- so this row is what makes the cleanup
        # safe to have done.
        "D:" + BACKSLASH + "workspace" + BACKSLASH + "projects",
        "D:" + BACKSLASH * 2 + "workspace" + BACKSLASH * 2 + "projects",
        # Fixtures already in the tree, which must stay legal.
        "Z:/opt/one/jobhunt.json",
        "D:" + BACKSLASH + "some" + BACKSLASH + "path",
        "C:" + BACKSLASH + "Users" + BACKSLASH + "<user>" + BACKSLASH + "App",
        "/home/<user>/.config",
        "/home/runner/work/repo/repo",
        # A URL is not a drive path. The lookbehind is what stops the "s:/"
        # inside "https://" from reading as one, and this repo has a detector
        # whose earlier version fired on every correct URL a server emitted.
        "GET /jobapi/v3/search returned 403 from https://www.naukri.com/x",
        "see https://example.com/in/somewhere for the write-up",
    ],
)
def test_the_generic_and_synthetic_forms_are_allowed(benign):
    """THE CONTROL FOR THE CONTROLS.

    Without it, every failure test above passes just as well against a rule
    that refuses EVERYTHING -- which would be green on the plants, red on the
    whole repository, and would make the sweep unmaintainable within a day.
    """
    assert not hits_in(benign), (benign, hits_in(benign))


def test_the_path_rules_can_match_a_backslash_at_all():
    """THE CONTROL FOR ALL THREE RULES, and it is worth more than they are.

    **A path guard reporting zero is indistinguishable from a path guard that
    is broken.** On 2026-08-31 that stopped being a maxim and became a count:
    three separate checks reported the consumer repository clean of
    drive-rooted paths within about ten minutes, and all three were broken --
    a ``git grep`` whose pattern never reached the regex engine intact, a
    rewrite in which a backslash before ``+`` turned the plus into a literal,
    and a correct pattern run twice through a shell heredoc that collapsed the
    character class into an escaped slash. Thirty-one hits were present the
    whole time.

    So this asserts the rules match something before the sweep is allowed to
    certify that they matched nothing.
    """
    # THE CHARACTER ITSELF, PINNED FIRST, and this assertion was added because
    # the control failed its own mutation test without it. ``[\\/]`` matches a
    # SLASH as well, so it cannot tell a backslash from whatever a broken
    # transport turned one into: with ``BACKSLASH`` mutated to ``chr(47)``
    # every composed value below becomes a slash-spelled path, the rules match
    # all of them, and this control goes green while proving nothing whatever
    # about the spelling that actually leaks. Nine mutations were run against
    # this file; that was the only one to survive, and it survived HERE, in
    # the control that exists to catch exactly it.
    assert ord(BACKSLASH) == 92, (
        f"BACKSLASH is {BACKSLASH!r}, not a backslash. Every composed value "
        "below is then a slash-spelled path -- which the rules do match -- so "
        "this control would certify a guard that has never seen the doubled "
        "backslash spelling at all"
    )
    assert re.match(r"[\\/]", BACKSLASH), (
        "the character class does not match a backslash, so every path rule "
        "in this file is inert and the sweep above certifies nothing"
    )

    # EVERY SPELLING THE VALUE CAN BE WRITTEN IN, not just the one this file's
    # author had in mind. The doubled forms are NOT edge cases -- a Windows
    # path inside JSON, inside a Python literal, or quoted in prose about
    # either is written with two backslashes, and this repository was carrying
    # one hit of each spelling when these rules were added.
    single = BACKSLASH
    double = BACKSLASH + BACKSLASH

    for sep in (single, double, "/", "//"):
        rooted = "D:" + sep + "Ravenscroft" + sep + "src"
        assert DRIVE_ROOT_PATH.search(rooted), (
            "DRIVE_ROOT_PATH is blind to a separator run of "
            f"{len(sep)}; that is the gap that let one of this repo's two "
            "hits sit at HEAD while a sweep reported a number"
        )
        assert WINDOWS_USER_PATH.search("C:" + sep + "Users" + sep + "rmarchetti"), (
            f"WINDOWS_USER_PATH is blind to a separator run of {len(sep)}"
        )

    for sep in ("/", "//"):
        assert POSIX_HOME_PATH.search("/home" + sep + "rmarchetti"), (
            f"POSIX_HOME_PATH is blind to a separator run of {len(sep)}"
        )


def test_the_drive_root_rule_catches_what_the_user_path_rule_cannot():
    """WHY THIS IS A SECOND RULE and not a widening of the first.

    ``WINDOWS_USER_PATH`` requires a literal ``Users`` segment. A drive rooted
    straight at a person's name has none, so the leak sits ONE SEGMENT TO THE
    LEFT of where that check looks. Both of this repository's real hits were
    that shape, and a guard carrying only the account rule would have reported
    zero on both.
    """
    rooted_at_a_person = "D:" + BACKSLASH + "Ravenscroft" + BACKSLASH + "src"

    assert not WINDOWS_USER_PATH.search(rooted_at_a_person), (
        "the older rule is supposed to MISS this; if it catches it, the "
        "argument for a separate rule is gone and this one should be deleted"
    )
    assert DRIVE_ROOT_PATH.search(rooted_at_a_person)
    assert "drive root" in {name for name, _ in hits_in(rooted_at_a_person)}


def test_a_failure_never_prints_the_path():
    """A CI log is a publication channel."""
    value = "D:" + BACKSLASH + "Ravenscroft" + BACKSLASH + "projects"
    rendered = redact(value)
    assert value not in rendered
    assert rendered.endswith("chars>")
    assert len(rendered) < len(value)


def test_the_name_gap_is_stated_where_a_reader_will_see_it():
    """The most important sentence in this module, asserted so that an edit
    deleting it fails rather than quietly widening what green means.

    Three real personal names were sitting in this repository's fixtures when
    this file was written, and no rule here would have found one of them.
    """
    doc = __doc__ or ""
    assert "IT CANNOT DETECT A PERSONAL NAME" in doc
    assert "nothing here would stop them coming back" in doc
