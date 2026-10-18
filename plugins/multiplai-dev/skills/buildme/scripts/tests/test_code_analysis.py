"""The codebase analysis through code_facts, and the facts handed to a block's agents.

No model is called: each test passes a fake call function that answers from
the prompt, the way `agent_call_structured` would after reading the files.
"""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
from code_facts import ExploreOutput

from build_pipeline import code_analysis
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.spec_steps import run_codebase_analysis
from build_pipeline.models import BlockInfo
from build_pipeline.tdd_engine import assemble_context

SCHEMA = Path(__file__).resolve().parents[3] / "review" / "schema" / "facts.v1.schema.json"

CALC = """\
class CalcError(ValueError):
    \"\"\"Raised for input the calculator refuses.\"\"\"


def add(a: int, b: int) -> int:
    if a is None or b is None:
        raise CalcError("missing operand")
    return a + b
"""

CLI = """\
from calc import add


def main(argv):
    return add(int(argv[0]), int(argv[1]))
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


def _config(project: Path, **kw) -> BuildConfig:
    config = BuildConfig(project_dir=project, change_name="feat", config_dir=project / ".no-config",
                         core_memory_files=[], **kw)
    config.specs_dir = project / "specs"
    config.change_dir.mkdir(parents=True, exist_ok=True)
    return config


def _cite(path: str, line: int, quote: str) -> dict:
    return {"path": path, "line_start": line, "line_end": line, "quote": quote}


def _answer_for(prompt: str) -> ExploreOutput:
    """What an agent that read the listed files would answer, with one bad citation."""
    out = {"facts": [], "files": [], "interfaces": [], "gotchas": [], "conventions": []}
    if "- src/calc.py" in prompt:
        out["files"].append({"path": "src/calc.py", "purpose": "Adds two numbers.",
                          "citation": _cite("src/calc.py", 5, "def add(a: int, b: int) -> int:")})
        out["facts"].append({"claim": "add returns the sum of its operands.",
                          "citations": [_cite("src/calc.py", 8, "return a + b")]})
        out["gotchas"].append({"claim": "A missing operand raises CalcError, not TypeError.",
                            "citations": [_cite("src/calc.py", 7, 'raise CalcError("missing operand")')]})
        out["interfaces"].append({"claim": "add(a, b) is the module's entry point.",
                               "citations": [_cite("src/calc.py", 5, "def add(a: int, b: int) -> int:")]})
        out["conventions"].append({"claim": "Errors subclass ValueError.",
                                "citations": [_cite("src/calc.py", 1, "class CalcError(ValueError):")]})
        # The agent misremembers a line: this claim's only citation is not at HEAD.
        out["facts"].append({"claim": "add logs every call.",
                          "citations": [_cite("src/calc.py", 3, "log.info('add')")]})
    if "- src/cli.py" in prompt:
        out["facts"].append({"claim": "main parses two integers and calls add.",
                          "citations": [_cite("src/cli.py", 5, "return add(int(argv[0]), int(argv[1]))")]})
    return ExploreOutput.model_validate(out)


def _fake_call(seen: list | None = None):
    async def call(prompt, schema, **kwargs):
        if seen is not None:
            seen.append((prompt, kwargs))
        return _answer_for(prompt)
    return call


@pytest.fixture
def project(tmp_path):
    return _repo(tmp_path / "proj", {
        "src/calc.py": CALC, "src/cli.py": CLI,
        "tests/test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "src/test_cli.py": "def test_main():\n    pass\n",
        "vendor/lib.py": "x = 1\n", "node_modules/m/index.js": "module.exports = 1\n",
        "build/out.py": "y = 2\n", "web/app.min.js": "a()\n", "README.md": "# calc\n",
    })


class TestScope:
    def test_only_source_files_at_head_without_tests_vendored_or_generated(self, project):
        git = code_analysis.project_git(project)
        assert sorted(code_analysis.source_files(project, git)) == ["src/calc.py", "src/cli.py"]

    def test_a_file_changed_after_head_is_read_as_committed(self, project):
        (project / "src" / "calc.py").write_text("changed\n")
        git = code_analysis.project_git(project)
        assert code_analysis.source_files(project, git)["src/calc.py"] == CALC

    def test_a_subdirectory_project_reads_its_own_files_through_the_repo_prefix(self, tmp_path):
        repo = _repo(tmp_path / "mono", {"packages/api/src/calc.py": CALC, "other/x.py": "x = 1\n"})
        project = repo / "packages" / "api"
        git = code_analysis.project_git(project)
        assert git.prefix == "packages/api/"
        assert list(code_analysis.source_files(project, git)) == ["src/calc.py"]
        assert git.resolver()("src/calc.py", "head") == (str(repo), git.sha, "packages/api/src/calc.py")


class TestRunCodebaseAnalysis:
    @pytest.mark.asyncio
    async def test_writes_cited_facts_under_each_heading_and_a_valid_facts_json(self, project):
        config = _config(project)
        md = await run_codebase_analysis(project, config, call=_fake_call())
        config.codebase_analysis_path.write_text(md)

        text = config.codebase_analysis_path.read_text()
        sections = re.split(r"^## ", text, flags=re.M)
        by_heading = {s.split("\n", 1)[0]: s for s in sections[1:]}
        for heading in ("Architecture", "Patterns", "Dependencies", "Conventions"):
            assert re.search(r"\[`src/(calc|cli)\.py:\d+(-\d+)?`\]\([^)]*src/(calc|cli)\.py#L\d+\)",
                             by_heading[heading]), heading
        # The link resolves from the report's directory to the real file.
        link = re.search(r"\]\(([^)#]*src/calc\.py)#L8\)", text).group(1)
        assert (config.codebase_analysis_path.parent / link).resolve() == (project / "src" / "calc.py").resolve()

        facts = json.loads((config.change_dir / "facts.json").read_text())
        jsonschema.validate(facts, json.loads(SCHEMA.read_text()))
        assert facts["kind"] == "tree" and facts["base_sha"] == code_analysis.EMPTY_TREE
        assert facts["head_sha"] == _git(project, "rev-parse", "HEAD").strip()
        assert [c["claim"] for c in facts["conventions"]] == ["Errors subclass ValueError."]

    @pytest.mark.asyncio
    async def test_a_fact_whose_citation_fails_is_dropped_and_counted(self, project):
        config = _config(project)
        md = await run_codebase_analysis(project, config, call=_fake_call())
        assert "add logs every call" not in md
        assert "1 claim(s) whose citations were not found there were dropped" in md
        facts = json.loads((config.change_dir / "facts.json").read_text())
        assert [d["what"] for d in facts["dropped"]] == ["add logs every call."]
        assert all("add logs" not in f["claim"] for f in facts["facts"])

    @pytest.mark.asyncio
    async def test_each_agent_is_read_only_in_the_project_and_asked_for_conventions(self, project):
        seen: list = []
        await run_codebase_analysis(project, _config(project), call=_fake_call(seen))
        assert len(seen) == 1
        prompt, kwargs = seen[0]
        assert "`conventions`" in prompt
        assert kwargs["allowed_tools"] == ["Read", "Glob", "Grep"]
        assert kwargs["cwd"] == str(project)
        assert "vendor/lib.py" not in prompt
        # The test file is only pointed at for test style, never listed as a file to explain.
        listing = prompt.split("Files in your part of the module:", 1)[1].split("\n\n", 1)[0]
        assert "tests/test_calc.py" not in listing

    @pytest.mark.asyncio
    async def test_the_size_limit_keeps_named_files_first_and_lists_the_rest(self, project, monkeypatch):
        monkeypatch.setattr(code_analysis, "UNIT_LINES", 9)   # one file per unit
        config = _config(project, codebase_analysis_max_units=1)
        (config.change_dir / "interview.md").write_text("The CLI in src/cli.py must accept floats.\n")
        seen: list = []
        md = await run_codebase_analysis(project, config, call=_fake_call(seen))
        assert len(seen) == 1 and "- src/cli.py" in seen[0][0] and "src/calc.py" not in seen[0][0]
        not_analysed = md.split("## Not analysed", 1)[1]
        assert "`codebase_analysis_max_units`" in not_analysed and "- `src/calc.py`" in not_analysed

    @pytest.mark.asyncio
    async def test_without_a_named_file_the_most_imported_one_is_kept(self, project, monkeypatch):
        monkeypatch.setattr(code_analysis, "UNIT_LINES", 9)
        seen: list = []
        md = await run_codebase_analysis(project, _config(project, codebase_analysis_max_units=1),
                                         call=_fake_call(seen))
        # cli.py imports calc; nothing imports cli.
        assert "- src/calc.py" in seen[0][0]
        assert "- `src/cli.py`" in md.split("## Not analysed", 1)[1]

    @pytest.mark.asyncio
    async def test_a_failed_slice_is_a_labelled_line_and_the_others_still_count(self, project, monkeypatch):
        monkeypatch.setattr(code_analysis, "UNIT_LINES", 9)

        async def call(prompt, schema, **kwargs):
            if "- src/cli.py" in prompt:
                raise RuntimeError("agent died")
            return _answer_for(prompt)

        md = await run_codebase_analysis(project, _config(project), call=call)
        assert "(analysis failed: `src/cli.py`: agent died)" in md
        assert "add returns the sum of its operands." in md

    @pytest.mark.asyncio
    async def test_the_default_call_runs_buildmes_structured_agent_under_one_budget_label(self, project):
        config = _config(project)
        fake = AsyncMock(side_effect=lambda prompt, schema, **kw: _answer_for(prompt))
        with patch("build_pipeline.llm_steps.spec_steps.agent_call_structured", fake):
            await run_codebase_analysis(project, config)
        kwargs = fake.await_args.kwargs
        assert kwargs["budget_label"] == "codebase_analysis"
        assert kwargs["model"] == config.model_for("codebase_analysis")
        assert kwargs["effort"] == config.spec_effort

    @pytest.mark.asyncio
    async def test_a_project_without_a_commit_says_so(self, tmp_path):
        project = tmp_path / "bare"
        project.mkdir()
        (project / "app.py").write_text("x = 1\n")
        md = await run_codebase_analysis(project, _config(project), call=_fake_call())
        assert md.startswith(code_analysis.NOT_ANALYSED_HEADING)
        assert "(analysis failed" not in md
        assert "- `app.py`" in md


class TestNoCommit:
    """A `--no-worktree` build runs `git init` without committing (orchestrator
    `_run_bootstrap`), so the project can hold source files and no commit."""

    @pytest.mark.asyncio
    async def test_the_phase_calls_no_model_and_labels_the_report_as_not_an_analysis(self, tmp_path):
        from build_pipeline.orchestrator import _run_codebase_analysis_phase
        from build_pipeline.progress import ProgressWriter
        from build_pipeline.spec_generator import read_codebase_analysis
        from build_pipeline.state import BuildState

        project = tmp_path / "inited"
        (project / "src").mkdir(parents=True)
        (project / "src" / "app.py").write_text("x = 1\n")
        (project / "node_modules" / "m").mkdir(parents=True)
        (project / "node_modules" / "m" / "index.js").write_text("module.exports = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        config = _config(project)
        state = BuildState(change_name="feat", mode="scratch", tier="advanced")
        call = AsyncMock()
        progress = ProgressWriter(tmp_path / "p.md")
        with patch("build_pipeline.llm_steps.spec_steps._codebase_analysis_call", call):
            await _run_codebase_analysis_phase(config, state, progress)
        call.assert_not_awaited()
        assert not (config.change_dir / "facts.json").exists()
        text = read_codebase_analysis(state)
        assert text.startswith(code_analysis.NOT_ANALYSED_HEADING)
        assert "no git commit" in text and "- `src/app.py`" in text
        assert "node_modules" not in text
        assert "(analysis failed" not in text
        assert "skipped" in (tmp_path / "p.md").read_text()


class TestGreenfield:
    @pytest.mark.asyncio
    async def test_a_project_with_no_source_still_skips_without_a_call(self, tmp_path):
        from build_pipeline.orchestrator import _run_codebase_analysis_phase
        from build_pipeline.progress import ProgressWriter
        from build_pipeline.state import BuildState

        project = _repo(tmp_path / "green", {"README.md": "# new\n"})
        config = _config(project)
        call = AsyncMock()
        state = BuildState(change_name="feat", mode="scratch", tier="advanced")
        with patch("build_pipeline.llm_steps.spec_steps._codebase_analysis_call", call):
            await _run_codebase_analysis_phase(config, state, ProgressWriter(tmp_path / "p.md"))
        call.assert_not_awaited()
        assert not config.codebase_analysis_path.exists()
        assert not (config.change_dir / "facts.json").exists()

    @pytest.mark.asyncio
    async def test_the_phase_writes_the_report_and_facts_json_side_by_side(self, project, tmp_path):
        from build_pipeline.orchestrator import _run_codebase_analysis_phase
        from build_pipeline.progress import ProgressWriter
        from build_pipeline.state import BuildState

        config = _config(project)
        state = BuildState(change_name="feat", mode="scratch", tier="advanced")
        with patch("build_pipeline.llm_steps.spec_steps._codebase_analysis_call",
                   AsyncMock(side_effect=lambda prompt, schema, **kw: _answer_for(prompt))):
            await _run_codebase_analysis_phase(config, state, ProgressWriter(tmp_path / "p.md"))
        assert "## Conventions" in config.codebase_analysis_path.read_text()
        assert (config.codebase_analysis_path.parent / "facts.json").exists()
        assert state.spec_gen.codebase_analysis_path == str(config.codebase_analysis_path)


class TestConfigKeys:
    def _load(self, tmp_path, yaml_text: str) -> BuildConfig:
        (tmp_path / "specs").mkdir(exist_ok=True)
        (tmp_path / "specs" / "config.yaml").write_text(yaml_text)
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config._load_specs_config()
        return config

    def test_defaults(self, tmp_path):
        c = self._load(tmp_path, "context: x\n")
        assert (c.codebase_analysis_max_units, c.block_facts, c.regenerate_walkthroughs,
                c.walkthrough_max_usd) == (12, False, False, 10.0)

    def test_values(self, tmp_path):
        c = self._load(tmp_path, "codebase_analysis_max_units: 3\nblock_facts: true\n"
                                 "regenerate_walkthroughs: true\nwalkthrough_max_usd: 2.5\n")
        assert (c.codebase_analysis_max_units, c.block_facts, c.regenerate_walkthroughs,
                c.walkthrough_max_usd) == (3, True, True, 2.5)

    def test_invalid_values_keep_the_defaults(self, tmp_path, caplog):
        c = self._load(tmp_path, "codebase_analysis_max_units: 0\nblock_facts: 'yes please'\n"
                                 "regenerate_walkthroughs: 1\nwalkthrough_max_usd: -1\n")
        assert (c.codebase_analysis_max_units, c.block_facts, c.regenerate_walkthroughs,
                c.walkthrough_max_usd) == (12, False, False, 10.0)
        assert sum("Invalid" in r.getMessage() for r in caplog.records) == 4


class TestBlockFacts:
    @pytest.fixture
    def analysed(self, project):
        """A project whose analysis wrote facts.json, then a later commit that moved a cited line."""
        import asyncio

        config = _config(project, block_facts=True)
        asyncio.run(run_codebase_analysis(project, config, call=_fake_call()))
        # An earlier block rewrote add's body: the "return a + b" quote is gone from line 8.
        (project / "src" / "calc.py").write_text(CALC.replace("return a + b", "return int(a) + int(b)"))
        _git(project, "commit", "-qam", "block 1")
        return config

    def _block(self) -> BlockInfo:
        return BlockInfo(number=2, name="Floats", description="- [ ] 2.1 Accept floats in `src/calc.py`")

    def test_the_test_writer_gets_the_section_without_the_fact_head_no_longer_holds(self, analysed):
        ctx = assemble_context(self._block(), analysed, "test_writer")
        section = ctx.split(code_analysis.BLOCK_FACTS_HEADING, 1)[1]
        assert "1 dropped because their quoted lines changed" in section.split("\n", 1)[0]
        assert "A missing operand raises CalcError" in section
        assert "`src/calc.py:7`" in section
        assert "add returns the sum of its operands." not in section
        # Facts about a file the block does not name stay out.
        assert "main parses two integers" not in section

    def test_the_implementer_gets_it_and_the_refactorer_does_not(self, analysed):
        assert code_analysis.BLOCK_FACTS_HEADING in assemble_context(self._block(), analysed, "implementer")
        assert code_analysis.BLOCK_FACTS_HEADING not in assemble_context(self._block(), analysed, "refactorer")

    def test_off_by_default(self, analysed):
        analysed.block_facts = False
        assert code_analysis.BLOCK_FACTS_HEADING not in assemble_context(self._block(), analysed, "test_writer")
        assert BuildConfig().block_facts is False

    def test_a_block_naming_no_analysed_file_gets_no_section(self, analysed):
        block = BlockInfo(number=3, name="Docs", description="Write the README.")
        assert code_analysis.BLOCK_FACTS_HEADING not in assemble_context(block, analysed, "test_writer")

    def test_no_facts_json_means_no_section(self, project):
        config = _config(project, block_facts=True)
        assert code_analysis.BLOCK_FACTS_HEADING not in assemble_context(self._block(), config, "test_writer")


class TestInboundImports:
    def test_counts_other_files_whose_import_lines_name_the_module(self):
        counts = code_analysis.inbound_imports({
            "src/calc.py": "x = 1\n",
            "src/cli.py": "from calc import add\n",
            "b.py": "import pkg.calc\n# calc is mentioned here, outside an import line\n",
            "pkg/__init__.py": "",
            "web/my-mod.js": "module.exports = 1\n",
            "web/app.js": "const m = require('./my-mod')\n",
        })
        assert counts == {"src/calc.py": 2, "src/cli.py": 0, "b.py": 0, "pkg/__init__.py": 1,
                          "web/my-mod.js": 1, "web/app.js": 0}


class TestNamedIn:
    # This repo's own paths: one stages/trace.py, three render.py.
    TRACE = "plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py"
    RENDERS = ["plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/render.py",
               "plugins/multiplai-dev/skills/review/scripts/review_pipeline/render.py",
               "plugins/multiplai-dev/skills/spec-extract/scripts/spec_extract_pipeline/render.py"]

    def test_a_partial_path_names_the_one_file_ending_with_it(self):
        assert code_analysis._named_in("edit `stages/trace.py`", [self.TRACE, *self.RENDERS]) == {self.TRACE}

    def test_a_partial_path_picks_one_of_several_files_with_the_same_name(self):
        assert code_analysis._named_in("change walkthrough_pipeline/render.py.", self.RENDERS) == {self.RENDERS[0]}

    def test_a_file_name_three_files_share_names_nothing(self):
        assert code_analysis._named_in("edit `render.py`", self.RENDERS) == set()

    def test_a_partial_path_two_files_end_with_names_neither(self):
        paths = ["a/stages/trace.py", "b/stages/trace.py"]
        assert code_analysis._named_in("edit stages/trace.py", paths) == set()


def _section(md: str, heading: str) -> str:
    for s in re.split(r"^## ", md, flags=re.M)[1:]:
        if s.split("\n", 1)[0] == heading:
            return s
    raise AssertionError(f"no ## {heading} in the report")


class TestTestStyle:
    """The conventions agent reads some of the project's tests for test style,
    without the tests joining the files it explains."""

    @staticmethod
    def _call(seen: list):
        async def call(prompt, schema, **kwargs):
            seen.append(prompt)
            out = _answer_for(prompt)
            if "- tests/test_calc.py" in prompt:
                out.conventions.append(ExploreOutput.model_validate({"conventions": [{
                    "claim": "Tests are plain functions named test_<behaviour> that assert directly.",
                    "citations": [_cite("tests/test_calc.py", 3, "def test_add():"),
                                  _cite("tests/test_calc.py", 4, "assert add(1, 2) == 3")]}]}).conventions[0])
            return out
        return call

    @pytest.mark.asyncio
    async def test_a_test_importing_the_slices_module_is_given_for_conventions_only(self, project):
        seen: list = []
        md = await run_codebase_analysis(project, _config(project), call=self._call(seen))
        prompt = seen[0]
        note = prompt.split(code_analysis.TEST_STYLE_INTRO, 1)[1]
        assert "- tests/test_calc.py" in note.split("Return ONE JSON object", 1)[0]
        assert "Tests are plain functions named test_<behaviour>" in _section(md, "Conventions")
        assert "tests/test_calc.py:3" in _section(md, "Conventions")
        # Still not analysed as a file: no purpose, not reported as uncovered.
        facts = json.loads((_config(project).change_dir / "facts.json").read_text())
        assert "tests/test_calc.py" not in {f["path"] for f in facts["files"]}
        assert "tests/test_calc.py" not in facts["uncovered"]

    @pytest.mark.asyncio
    async def test_each_slice_gets_the_tests_that_import_its_own_modules(self, project, monkeypatch):
        monkeypatch.setattr(code_analysis, "UNIT_LINES", 9)   # one file per slice
        seen: list = []
        await run_codebase_analysis(project, _config(project), call=self._call(seen))
        calc = next(p for p in seen if "- src/calc.py (" in p)
        cli = next(p for p in seen if "- src/cli.py (" in p)
        assert code_analysis.TEST_STYLE_INTRO in calc and "- tests/test_calc.py" in calc
        assert code_analysis.TEST_STYLE_INTRO not in cli

    @pytest.mark.asyncio
    async def test_when_no_test_imports_an_analysed_module_the_first_slice_still_gets_some(self, tmp_path):
        project = _repo(tmp_path / "p", {"src/calc.py": CALC,
                                         "tests/test_a.py": "def test_a():\n    assert True\n",
                                         "tests/test_b.py": "def test_b():\n    assert True\n",
                                         "tests/test_c.py": "def test_c():\n    assert True\n"})
        seen: list = []
        await run_codebase_analysis(project, _config(project), call=self._call(seen))
        note = seen[0].split(code_analysis.TEST_STYLE_INTRO, 1)[1]
        assert "- tests/test_a.py" in note and "- tests/test_b.py" in note and "tests/test_c.py" not in note

    @pytest.mark.asyncio
    async def test_a_project_without_tests_gets_no_test_note(self, tmp_path):
        project = _repo(tmp_path / "p", {"src/calc.py": CALC})
        seen: list = []
        await run_codebase_analysis(project, _config(project), call=self._call(seen))
        assert code_analysis.TEST_STYLE_INTRO not in seen[0]


PYPROJECT = """\
[project]
name = "calc"
dependencies = ["requests>=2.31"]
"""

SETTINGS = """\
api:
  base_url: https://api.example.com
"""


class TestDependencySlice:
    """Manifests and config files are read in a slice of their own, whose facts
    go under Dependencies (the declared dependencies, configuration and
    external services), as the old Dependencies agent's did."""

    @pytest.fixture
    def configured(self, tmp_path):
        return _repo(tmp_path / "c", {
            "src/calc.py": CALC, "src/cli.py": CLI, "pyproject.toml": PYPROJECT,
            "config/settings.yaml": SETTINGS, "pnpm-lock.yaml": "lockfileVersion: 6\n",
            "tests/fixtures.yaml": "a: 1\n", "node_modules/m/package.json": "{}\n",
        })

    @staticmethod
    def _call(seen: list):
        async def call(prompt, schema, **kwargs):
            seen.append(prompt)
            if "- pyproject.toml (" not in prompt:
                return _answer_for(prompt)
            return ExploreOutput.model_validate({
                "facts": [{"claim": "The project depends on requests 2.31 or later.",
                           "citations": [_cite("pyproject.toml", 3, 'dependencies = ["requests>=2.31"]')]},
                          {"claim": "The API base URL is set in config/settings.yaml.",
                           "citations": [_cite("config/settings.yaml", 2, "base_url: https://api.example.com")]}],
                "files": [{"path": "pyproject.toml", "purpose": "Declares the package and its dependencies.",
                           "citation": _cite("pyproject.toml", 2, 'name = "calc"')}],
            })
        return call

    def test_the_scope_holds_manifests_and_config_but_no_lock_test_or_vendored_file(self, configured):
        git = code_analysis.project_git(configured)
        assert sorted(code_analysis.config_files(configured, git)) == ["config/settings.yaml", "pyproject.toml"]
        assert sorted(code_analysis.source_files(configured, git)) == ["src/calc.py", "src/cli.py"]

    @pytest.mark.asyncio
    async def test_the_slice_is_asked_for_dependencies_and_its_facts_go_under_dependencies(self, configured):
        seen: list = []
        md = await run_codebase_analysis(configured, _config(configured), call=self._call(seen))
        deps_prompts = [p for p in seen if "- pyproject.toml (" in p]
        assert len(deps_prompts) == 1 and len(seen) == 2
        assert code_analysis.DEPENDENCY_NOTE.strip() in deps_prompts[0]
        assert "- config/settings.yaml (" in deps_prompts[0] and "- src/calc.py (" not in deps_prompts[0]
        dependencies = _section(md, "Dependencies")
        assert "depends on requests 2.31 or later" in dependencies and "pyproject.toml:3" in dependencies
        assert "API base URL is set" in dependencies
        assert "pyproject.toml: Declares the package" in dependencies
        architecture = _section(md, "Architecture")
        assert "requests" not in architecture and "add returns the sum" in architecture
        # The interfaces other code calls stay under Dependencies, after the declared ones.
        assert dependencies.index("requests") < dependencies.index("add(a, b) is the module's entry point")

    @pytest.mark.asyncio
    async def test_the_slice_counts_towards_the_size_limit(self, configured, monkeypatch):
        monkeypatch.setattr(code_analysis, "UNIT_LINES", 9)   # one source file per slice
        seen: list = []
        md = await run_codebase_analysis(configured, _config(configured, codebase_analysis_max_units=2),
                                         call=self._call(seen))
        assert len(seen) == 2 and sum("- pyproject.toml (" in p for p in seen) == 1
        assert "- `src/cli.py`" in _section(md, "Not analysed")

    @pytest.mark.asyncio
    async def test_with_one_unit_the_source_wins_and_the_config_files_are_listed(self, configured):
        seen: list = []
        md = await run_codebase_analysis(configured, _config(configured, codebase_analysis_max_units=1),
                                         call=self._call(seen))
        assert len(seen) == 1 and "- pyproject.toml (" not in seen[0]
        not_analysed = _section(md, "Not analysed")
        assert "- `pyproject.toml`" in not_analysed and "- `config/settings.yaml`" in not_analysed

    @pytest.mark.asyncio
    async def test_manifests_come_first_and_the_files_over_one_slice_are_listed(self, configured, monkeypatch):
        monkeypatch.setattr(code_analysis, "UNIT_LINES", 3)   # pyproject.toml alone fills the slice
        seen: list = []
        md = await run_codebase_analysis(configured, _config(configured, codebase_analysis_max_units=12),
                                         call=self._call(seen))
        deps_prompt = next(p for p in seen if "- pyproject.toml (" in p)
        assert "config/settings.yaml" not in deps_prompt
        assert "- `config/settings.yaml`" in _section(md, "Not analysed")

    def test_a_config_file_over_the_line_limit_is_data_not_config(self, configured, monkeypatch):
        monkeypatch.setattr(code_analysis, "MAX_CONFIG_LINES", 2)
        git = code_analysis.project_git(configured)
        assert sorted(code_analysis.config_files(configured, git)) == ["config/settings.yaml"]
