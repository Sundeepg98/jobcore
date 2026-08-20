"""The guard that makes jobcore a library rather than a copy.

jobcore must import and run with no job board's package present. These tests
fail if anyone reintroduces a platform import, which is the only way the
extraction can silently rot back into coupling.

The checks parse the AST rather than scanning lines: a docstring that shows
``from jobcore import ...`` is documentation, not an import, and a checker
that cannot tell the difference produces false failures (it did, on its first
run, which is why it is written this way).
"""

import ast
import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import jobcore

SRC = Path(jobcore.__file__).parent

PLATFORM_PACKAGES = frozenset({
    "naukri_server",
    "uplers_server",
    "linkedin_server",
    "jobspy",
})


def _source_files():
    files = sorted(SRC.glob("*.py"))
    assert files, f"no source files found under {SRC}"
    return files


def _imported_top_levels(path: Path) -> list[tuple[str, int]]:
    """Every top-level package this module imports, as (name, lineno).

    Relative imports (``from . import x``) are reported as ``"."`` so callers
    can skip them; a bare ``from __future__`` counts as stdlib.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from . / from .. import
                found.append((".", node.lineno))
            elif node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


class TestCheckerIsWiredUp:
    """The checker must be able to see imports at all, or it certifies nothing."""

    def test_it_finds_the_imports_that_are_really_there(self):
        found = {name for name, _ in _imported_top_levels(SRC / "fit.py")}
        assert {"math", "re", "dataclasses", "typing"} <= found

    def test_it_flags_a_planted_platform_import(self, tmp_path):
        planted = tmp_path / "bad.py"
        planted.write_text(
            '"""from naukri_server.config import X — this line is a docstring."""\n'
            "from naukri_server.config import LAKHS_MULTIPLIER\n",
            encoding="utf-8",
        )
        names = [n for n, _ in _imported_top_levels(planted)]
        assert names == ["naukri_server"], "the docstring must not count, the import must"


class TestNoPlatformImportsInSource:
    @pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
    def test_file_imports_no_platform_package(self, path):
        offenders = [
            (name, lineno)
            for name, lineno in _imported_top_levels(path)
            if name in PLATFORM_PACKAGES
        ]
        assert offenders == [], f"{path.name} imports a platform package: {offenders}"


class TestNoThirdPartyRuntimeDependency:
    @pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
    def test_only_stdlib_imports(self, path):
        """jobcore declares zero dependencies; prove the source honours that."""
        allowed = set(sys.stdlib_module_names) | {".", "jobcore"}
        offenders = [
            (name, lineno)
            for name, lineno in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"{path.name} imports non-stdlib {offenders}; jobcore must stay "
            f"dependency-free"
        )


class TestImportsInACleanInterpreter:
    def test_import_succeeds_in_subprocess(self):
        """A fresh interpreter, cwd elsewhere, imports jobcore and scores a job."""
        code = (
            "import jobcore, sys;"
            "assert not [m for m in sys.modules if m.startswith('naukri')], "
            "sorted(sys.modules);"
            "r = jobcore.compute_fit_score("
            "  job_skills=jobcore.parse_skills('React, Node.js'),"
            "  profile_skills=jobcore.parse_skills('reactjs, nodejs'),"
            "  job_exp_str='3-5 years', profile_exp='4 years');"
            "print(r['overall_score'])"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(Path(sys.executable).parent),
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert proc.stdout.strip() == "100", proc.stdout


class TestEveryModuleImportable:
    def test_all_submodules_import(self):
        names = [m.name for m in pkgutil.iter_modules([str(SRC)])]
        assert set(names) >= {"fit", "salary", "scoring", "skills"}, names
        for name in names:
            importlib.import_module(f"jobcore.{name}")

    def test_public_api_is_complete(self):
        """Every name in __all__ actually resolves."""
        missing = [n for n in jobcore.__all__ if not hasattr(jobcore, n)]
        assert missing == [], missing

    def test_scoring_all_is_complete(self):
        from jobcore import scoring

        missing = [n for n in scoring.__all__ if not hasattr(scoring, n)]
        assert missing == [], missing
