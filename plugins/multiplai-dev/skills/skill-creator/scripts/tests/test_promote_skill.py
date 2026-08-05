"""Tests for promote_skill.py.

The gate's job is to fail on broken drafts and stay silent on good ones. The
second half is the harder half: the first sweep across the real marketplace
produced ~120 failures, every one of them noise — vendored `.venv` modules,
package-internal files run by path, and third-party imports missing from the
gate's own interpreter. Each of those is pinned here so the calibration can't
regress.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import promote_skill  # noqa: E402
from promote_skill import (_bundled_scripts, parse_contract,  # noqa: E402
                           promote, python_command)

VALID_FRONTMATTER = textwrap.dedent("""\
    ---
    name: {name}
    description: Does a thing.
    ---

    # Thing
    """)


def make_skill(tmp_path: Path, name="alpha", scripts=None) -> Path:
    d = tmp_path / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(VALID_FRONTMATTER.format(name=name))
    for rel, body in (scripts or {}).items():
        p = d / "scripts" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return d


GOOD_PY = """\
    import argparse
    def main():
        argparse.ArgumentParser().parse_args()
    if __name__ == "__main__":
        main()
    """

GOOD_SH = """\
    #!/usr/bin/env bash
    case "${1:-}" in -h|--help) echo "usage: x"; exit 0 ;; esac
    """


class TestHappyPath:
    def test_clean_skill_passes(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"go.py": GOOD_PY, "go.sh": GOOD_SH})
        report = promote(skill, run_contract=False)
        assert report.ok, report.render()

    def test_skill_with_no_scripts_passes(self, tmp_path):
        skill = make_skill(tmp_path)
        assert promote(skill, run_contract=False).ok


class TestCatchesRealDefects:
    def test_script_that_rejects_help_fails(self, tmp_path):
        """The defect this gate was written for: usage text, exit 1."""
        skill = make_skill(tmp_path, scripts={"go.sh": """\
            #!/usr/bin/env bash
            echo "usage: go.sh <file>" >&2
            exit 1
            """})
        report = promote(skill, run_contract=False)
        assert not report.ok
        assert "--help exited 1" in report.render()

    def test_syntax_error_fails(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"go.py": """\
            def main(:
                pass
            if __name__ == "__main__":
                main()
            """})
        assert not promote(skill, run_contract=False).ok

    def test_bad_frontmatter_fails(self, tmp_path):
        skill = make_skill(tmp_path)
        (skill / "SKILL.md").write_text("# no frontmatter\n")
        report = promote(skill, run_contract=False)
        assert not report.ok
        assert "frontmatter" in report.render()

    def test_name_must_be_hyphen_case(self, tmp_path):
        skill = make_skill(tmp_path, name="Alpha_Skill")
        assert not promote(skill, run_contract=False).ok


class TestScriptCollectionCalibration:
    """Each case here is a false positive from the first real sweep."""

    def test_vendored_venv_is_skipped(self, tmp_path):
        """Two skills vendor a .venv; walking it found 70+ irrelevant modules."""
        skill = make_skill(tmp_path, scripts={
            "go.py": GOOD_PY,
            ".venv/lib/python3.12/site-packages/pygments/__main__.py": "raise SystemExit(1)\n",
        })
        found = {p.name for p in _bundled_scripts(skill)}
        assert found == {"go.py"}

    def test_package_internal_module_is_skipped(self, tmp_path):
        """`build_pipeline/gates.py` is reached as -m, not by path.

        Run directly it raises "attempted relative import with no known parent
        package" — a fact about how it was invoked, not about the skill.
        """
        skill = make_skill(tmp_path, scripts={
            "pkg/__init__.py": "",
            "pkg/mod.py": GOOD_PY,
        })
        assert _bundled_scripts(skill) == []

    def test_library_module_without_main_is_skipped(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"lib.py": "X = 1\n"})
        assert _bundled_scripts(skill) == []

    def test_tests_directory_is_skipped(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"tests/test_go.py": GOOD_PY})
        assert _bundled_scripts(skill) == []


class TestMissingDependencyIsAWarning:
    def test_undeclared_import_warns_but_does_not_block(self, tmp_path):
        """The script never reached its own argv handling, so this says nothing
        about its CLI shape — it says the gate's interpreter lacks a dep."""
        skill = make_skill(tmp_path, scripts={"go.py": """\
            import definitely_not_installed_xyz
            if __name__ == "__main__":
                pass
            """})
        report = promote(skill, run_contract=False)
        assert report.ok, report.render()
        assert any("definitely_not_installed_xyz" in w for w in report.warnings)
        assert any("PEP 723" in w for w in report.warnings)


class TestPythonCommand:
    def test_pep723_script_routed_through_uv(self, tmp_path):
        script = tmp_path / "s.py"
        script.write_text("# /// script\n# dependencies = []\n# ///\nprint(1)\n")
        cmd = python_command(script)
        if cmd[0] == "uv":
            assert cmd[:3] == ["uv", "run", "--script"]
        else:
            pytest.skip("uv not on PATH in this environment")

    def test_plain_script_uses_the_current_interpreter(self, tmp_path):
        script = tmp_path / "s.py"
        script.write_text("print(1)\n")
        assert python_command(script)[0] == sys.executable


CONTRACT = """\
# Contract

### help exits zero
```sh
bash scripts/go.sh --help
```
Expect: usage: x

### second case
```sh
echo hello
```
Expect: hello
"""


class TestContract:
    def test_parses_multiple_cases(self):
        cases = parse_contract(CONTRACT)
        assert [c[0] for c in cases] == ["help exits zero", "second case"]
        assert cases[1][1] == "echo hello"
        assert cases[1][2] == "hello"

    def test_prose_between_heading_and_fence_is_not_part_of_the_name(self):
        """The first parser used DOTALL on the name group, so a case that
        explained itself had the whole paragraph absorbed into its title."""
        text = ("### short name\n\n"
                "Some prose explaining why this assertion exists,\n"
                "over two lines.\n\n"
                "```sh\necho hi\n```\n\nExpect: hi\n")
        cases = parse_contract(text)
        assert [c[0] for c in cases] == ["short name"]
        assert cases[0][1] == "echo hi"

    def test_missing_contract_is_not_a_failure(self, tmp_path):
        skill = make_skill(tmp_path)
        assert promote(skill, run_contract=True).ok

    def test_satisfied_contract_passes(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"go.sh": GOOD_SH})
        (skill / "CONTRACT.md").write_text(CONTRACT)
        assert promote(skill, run_contract=True).ok

    def test_violated_contract_fails(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"go.sh": GOOD_SH})
        (skill / "CONTRACT.md").write_text(
            "### wrong\n```sh\necho hello\n```\nExpect: goodbye\n")
        report = promote(skill, run_contract=True)
        assert not report.ok
        assert "goodbye" in report.render()

    def test_unparseable_contract_is_a_failure_not_a_pass(self, tmp_path):
        """A contract nobody can parse is worse than no contract — it reads as
        coverage that isn't there."""
        skill = make_skill(tmp_path)
        (skill / "CONTRACT.md").write_text("some prose, no cases\n")
        assert not promote(skill, run_contract=True).ok


class TestCLI:
    def test_missing_skill_md_is_reported(self, tmp_path, capsys):
        (tmp_path / "empty").mkdir()
        assert promote_skill.main([str(tmp_path / "empty")]) == 1
        assert "no SKILL.md" in capsys.readouterr().out

    def test_exit_code_zero_on_clean_skill(self, tmp_path):
        skill = make_skill(tmp_path, scripts={"go.py": GOOD_PY})
        assert promote_skill.main([str(skill)]) == 0


class TestRealSkills:
    def test_every_shipped_skill_passes_the_gate(self):
        """The gate must pass on what we publish, or it gets switched off."""
        # tests → scripts → skill-creator → skills → multiplai-dev → plugins → repo
        repo = Path(__file__).resolve().parents[6]
        skills = sorted((repo / "plugins").glob("*/skills/*/"))
        if not skills:
            pytest.skip("not running inside the marketplace repo")
        blocked = [s.name for s in skills
                   if not promote(s, run_contract=False).ok]
        assert blocked == []


# ---------------------------------------------------------------------------
# The two budgets (#114)
# ---------------------------------------------------------------------------

class TestTheResolutionBudgetIsSpentOnce:
    """A cold uv cache is not a hung script, and must not be reported as one."""

    @pytest.fixture(autouse=True)
    def _fresh_run(self):
        promote_skill._resolution_budget_spent = False
        yield
        promote_skill._resolution_budget_spent = False

    def test_the_first_uv_command_may_pay_for_the_clone(self):
        assert promote_skill.timeout_for(
            "uv run --project ../../scripts ../../scripts/costs_report.py --help"
        ) == promote_skill.RESOLVE_TIMEOUT_SECONDS

    def test_every_later_command_is_held_to_the_tight_budget(self):
        promote_skill.timeout_for(["uv", "run", "x.py"])
        assert promote_skill.timeout_for(["uv", "run", "y.py"]) == \
            promote_skill.TIMEOUT_SECONDS
        assert promote_skill.timeout_for(
            "uv run --project . z.py --help") == promote_skill.TIMEOUT_SECONDS

    def test_a_plain_command_never_gets_the_allowance(self):
        """A hang in `python3 script.py --help` is a hang, not a cache miss —
        and it must not consume the allowance the uv command needs."""
        assert promote_skill.timeout_for(["python3", "x.py", "--help"]) == \
            promote_skill.TIMEOUT_SECONDS
        assert promote_skill.timeout_for(["bash", "x.sh", "--help"]) == \
            promote_skill.TIMEOUT_SECONDS
        # …and the uv command that follows still gets it.
        assert promote_skill.timeout_for(["uv", "run", "x.py"]) == \
            promote_skill.RESOLVE_TIMEOUT_SECONDS
