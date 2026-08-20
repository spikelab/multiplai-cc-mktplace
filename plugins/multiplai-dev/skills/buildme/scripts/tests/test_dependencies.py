"""Tests for dependency detection — what is NEW TO THIS PROJECT.

The precision bar these tests defend: an explainer must fire for a library the
specs name and the manifests do not, and must NOT fire for a stdlib module, a
generic language word, the project's own modules, or anything already declared.
"""

import json

import pytest

from build_pipeline.dependencies import (
    NewDependency,
    declared_dependencies,
    detect_new_dependencies,
    existing_import_names,
    project_module_names,
)


def _write_change(change_dir, *, impact: str = "", decisions: str = ""):
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text(
        "## Why\nBecause.\n\n## What Changes\nStuff.\n\n## Impact\n" + impact + "\n"
    )
    (change_dir / "design.md").write_text(
        "## Context\nc\n\n## Decisions\n" + decisions + "\n\n## Risks / Trade-offs\nr\n"
    )
    return change_dir


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


# --- The two headline cases (Done-means criterion 2) --------------------------

class TestDetectNewDependencies:
    def test_returns_dependency_named_only_in_specs(self, tmp_path, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["httpx>=0.27"]\n'
        )
        change = _write_change(
            tmp_path / "change",
            impact="Adds audio transcription via `mlx-whisper`.",
        )
        deps = detect_new_dependencies(change, project)
        assert [d.name for d in deps] == ["mlx-whisper"]
        assert deps[0].mentioned_in == ["proposal.md § Impact"]
        assert "pyproject.toml" in deps[0].evidence

    def test_returns_nothing_for_dependency_already_in_pyproject(self, tmp_path, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["httpx>=0.27", "mlx-whisper"]\n'
        )
        change = _write_change(
            tmp_path / "change",
            impact="Adds audio transcription via `mlx-whisper` over `httpx`.",
        )
        assert detect_new_dependencies(change, project) == []

    def test_underscore_and_case_variants_match_the_manifest(self, tmp_path, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["python-dotenv"]\n'
        )
        change = _write_change(tmp_path / "change", impact="Reads .env with `python_dotenv`.")
        assert detect_new_dependencies(change, project) == []

    def test_extras_and_version_specifiers_are_stripped(self, tmp_path, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["uvicorn[standard]>=0.30"]\n'
        )
        change = _write_change(tmp_path / "change", decisions="Serve with `uvicorn`.")
        assert detect_new_dependencies(change, project) == []


# --- Precision: what must NOT fire an explainer -------------------------------

class TestPrecision:
    def test_stdlib_mentions_never_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            impact="Uses `pathlib`, `asyncio`, `json`, `dataclasses`, `subprocess`, `re`.",
            decisions="State is serialized with `json` and paths handled by `pathlib`.",
        )
        assert detect_new_dependencies(change, project) == []

    def test_language_and_format_words_never_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            impact="Written in `python`, packaged with `uv`, config in `yaml`, CI via `github`.",
        )
        assert detect_new_dependencies(change, project) == []

    def test_file_paths_and_filenames_never_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            impact="Touches `build_pipeline/gates.py`, `README.md`, and `specs/config.yaml`.",
        )
        assert detect_new_dependencies(change, project) == []

    def test_projects_own_modules_never_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        (project / "build_pipeline").mkdir()
        change = _write_change(
            tmp_path / "change",
            decisions="Extend `build_pipeline` with a new gate.",
        )
        assert detect_new_dependencies(change, project) == []

    def test_only_impact_and_decisions_sections_are_scanned(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = tmp_path / "change"
        change.mkdir()
        (change / "proposal.md").write_text(
            "## Why\nWe like `polars`.\n\n## Impact\nNothing new.\n"
        )
        (change / "design.md").write_text(
            "## Context\nToday we use `duckdb`.\n\n## Decisions\nNo new libraries.\n"
        )
        assert detect_new_dependencies(change, project) == []

    def test_unbackticked_prose_names_never_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            impact="We will call Whisper through MLX and store results in Postgres.",
        )
        assert detect_new_dependencies(change, project) == []


# --- Manifest coverage --------------------------------------------------------

class TestManifests:
    def test_package_json_declarations(self, tmp_path, project):
        (project / "package.json").write_text(json.dumps({
            "dependencies": {"react": "^18", "@tanstack/react-query": "^5"},
            "devDependencies": {"vitest": "^2"},
        }))
        change = _write_change(
            tmp_path / "change",
            decisions="Use `react`, `@tanstack/react-query`, `vitest`, and `zustand`.",
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == ["zustand"]

    def test_cargo_declarations(self, tmp_path, project):
        (project / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n\n[dependencies]\nserde = "1"\n\n'
            '[dev-dependencies]\nproptest = "1"\n'
        )
        change = _write_change(
            tmp_path / "change", decisions="Use `serde`, `proptest`, and `tokio`."
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == ["tokio"]

    def test_go_mod_declarations_match_by_last_segment(self, tmp_path, project):
        (project / "go.mod").write_text(
            "module example.com/demo\n\ngo 1.22\n\nrequire (\n"
            "\tgithub.com/stretchr/testify v1.9.0\n)\n"
        )
        change = _write_change(tmp_path / "change", decisions="Assertions via `testify`.")
        assert detect_new_dependencies(change, project) == []

    def test_package_swift_declarations(self, tmp_path, project):
        (project / "Package.swift").write_text(
            'let package = Package(name: "Demo", dependencies: [\n'
            '    .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.0.0"),\n'
            '])\n'
        )
        change = _write_change(
            tmp_path / "change", decisions="CLI parsing by `swift-argument-parser`."
        )
        assert detect_new_dependencies(change, project) == []

    def test_requirements_txt_declarations(self, tmp_path, project):
        (project / "requirements.txt").write_text("# comment\nrequests==2.32.0\nrich\n")
        change = _write_change(tmp_path / "change", impact="Uses `requests` and `rich`.")
        assert detect_new_dependencies(change, project) == []

    def test_no_manifest_records_that_in_the_evidence(self, tmp_path, project):
        change = _write_change(tmp_path / "change", impact="Adds `polars`.")
        deps = detect_new_dependencies(change, project)
        assert [d.name for d in deps] == ["polars"]
        assert "no dependency manifest" in deps[0].evidence

    def test_declared_dependencies_reports_manifests_found(self, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["httpx"]\n'
        )
        declared, manifests = declared_dependencies(project)
        assert "httpx" in declared
        assert manifests == ["pyproject.toml"]

    def test_unparseable_manifest_does_not_crash(self, tmp_path, project):
        (project / "pyproject.toml").write_text("this is not toml {{{")
        change = _write_change(tmp_path / "change", impact="Adds `polars`.")
        assert [d.name for d in detect_new_dependencies(change, project)] == ["polars"]


# --- Misc ---------------------------------------------------------------------

class TestMisc:
    def test_mentions_from_both_sections_are_merged(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            impact="Adds `polars`.",
            decisions="Dataframes handled by `polars`.",
        )
        deps = detect_new_dependencies(change, project)
        assert len(deps) == 1
        assert deps[0].mentioned_in == [
            "proposal.md § Impact",
            "design.md § Decisions",
        ]

    def test_missing_spec_files_return_nothing(self, tmp_path, project):
        assert detect_new_dependencies(tmp_path / "nope", project) == []

    def test_results_are_sorted_by_name(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change", impact="Adds `zstandard`, `polars`, and `duckdb`."
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == [
            "duckdb", "polars", "zstandard",
        ]

    def test_project_module_names_includes_src_layout(self, project):
        (project / "src").mkdir()
        (project / "src" / "demo_pkg").mkdir()
        (project / "helper.py").write_text("")
        names = project_module_names(project)
        assert "demo-pkg" in names
        assert "helper" in names

    def test_new_dependency_is_a_model_with_defaults(self):
        dep = NewDependency(name="polars")
        assert dep.mentioned_in == []
        assert dep.evidence == ""


# --- Precision filters added after the real-change smoke test -----------------

class TestAlreadyImportedNames:
    """A design doc naming `BaseModel` or `log_utils` is naming code the
    project already uses. Only names the project has never imported are new."""

    def test_imported_symbol_does_not_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        (project / "models.py").write_text("from pydantic import BaseModel\n")
        change = _write_change(
            tmp_path / "change", decisions="Model the finding with a `BaseModel`."
        )
        assert detect_new_dependencies(change, project) == []

    def test_imported_submodule_does_not_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        (project / "app.py").write_text(
            "from multiplai_core.log_utils import setup_logging\n"
        )
        change = _write_change(
            tmp_path / "change", decisions="Logging via the shared `log_utils` helper."
        )
        assert detect_new_dependencies(change, project) == []

    def test_never_imported_name_still_fires(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        (project / "app.py").write_text("import httpx\n")
        change = _write_change(tmp_path / "change", decisions="Frames via `polars`.")
        assert [d.name for d in detect_new_dependencies(change, project)] == ["polars"]

    def test_js_and_swift_import_forms(self, project):
        (project / "app.ts").write_text("import { z } from 'zod';\n")
        (project / "View.swift").write_text("import Alamofire\n")
        names = existing_import_names(project)
        assert "zod" in names
        assert "alamofire" in names

    def test_skips_vendor_directories(self, project):
        (project / "node_modules").mkdir()
        (project / "node_modules" / "x.js").write_text("import 'left-pad';\n")
        assert "left-pad" not in existing_import_names(project)

    def test_missing_project_dir_is_empty(self, tmp_path):
        assert existing_import_names(tmp_path / "nope") == set()


class TestRejectedAlternatives:
    """A dependency the design explicitly decided against is not one we depend
    on — no explainer for the road not taken."""

    def test_rather_than_suppresses_the_alternative(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            decisions="- Use `tomllib` (stdlib) rather than adding `tomli`.",
        )
        assert detect_new_dependencies(change, project) == []

    def test_instead_of_suppresses_the_alternative(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            decisions="- Frames with `polars` instead of `pandas`.",
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == ["polars"]

    def test_negation_after_the_token_does_not_suppress_it(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            decisions="- Adopt `polars`; we avoid heavyweight alternatives.",
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == ["polars"]

    def test_instead_of_leading_the_sentence_keeps_the_adopted_dep(self, tmp_path, project):
        """`instead of X` negates only X — the adopted dependency named later
        in the same sentence must still fire."""
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            decisions="- Instead of `pandas`, we use `polars`.",
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == ["polars"]

    def test_distant_negation_cue_does_not_govern_the_token(self, tmp_path, project):
        """A cue many words back is commentary, not a rejection of this token."""
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            decisions="- We ruled out building our own frame library and adopt `polars`.",
        )
        assert [d.name for d in detect_new_dependencies(change, project)] == ["polars"]


class TestStdlibFilterIsPythonOnly:
    """`secrets` / `queue` are Python stdlib names AND real npm/cargo package
    names — the stdlib subtraction only applies where Python is plausible."""

    def test_stdlib_name_fires_in_a_pure_js_project(self, tmp_path, project):
        (project / "package.json").write_text(json.dumps({"dependencies": {}}))
        change = _write_change(tmp_path / "change", decisions="Store tokens via `secrets`.")
        assert [d.name for d in detect_new_dependencies(change, project)] == ["secrets"]

    def test_stdlib_name_never_fires_in_a_python_project(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(tmp_path / "change", decisions="Store tokens via `secrets`.")
        assert detect_new_dependencies(change, project) == []

    def test_mixed_python_and_js_project_keeps_the_filter(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        (project / "package.json").write_text(json.dumps({"dependencies": {}}))
        change = _write_change(tmp_path / "change", decisions="Store tokens via `secrets`.")
        assert detect_new_dependencies(change, project) == []

    def test_no_manifest_keeps_the_conservative_filter(self, tmp_path, project):
        change = _write_change(tmp_path / "change", decisions="Store tokens via `secrets`.")
        assert detect_new_dependencies(change, project) == []


class TestManifestFilenamesAreNotDependencies:
    def test_go_mod_and_go_sum_never_fire(self, tmp_path, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        change = _write_change(
            tmp_path / "change",
            impact="Manifests covered: `pyproject.toml`, `package.json`, "
                   "`Package.swift`, `Cargo.toml`, `go.mod`, `go.sum`, `uv.lock`.",
        )
        assert detect_new_dependencies(change, project) == []


class TestDottedPathsCollapseToDistributions:
    """`rich.console.Console` is the library `rich`, not a fourth dependency."""

    @pytest.fixture
    def py_project(self, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        return project

    def test_dotted_mentions_collapse_to_one_candidate(self, tmp_path, py_project):
        change = _write_change(
            tmp_path / "change",
            impact="Renders via `rich`, `rich.print`, `rich.console.Console` "
                   "and `rich.table.Table`.",
        )
        assert [d.name for d in detect_new_dependencies(change, py_project)] == ["rich"]

    def test_declared_dependency_is_matched_through_its_dotted_form(self, tmp_path, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["rich"]\n'
        )
        change = _write_change(tmp_path / "change", impact="Uses `rich.console.Console`.")
        assert detect_new_dependencies(change, project) == []

    def test_stdlib_member_access_never_fires(self, tmp_path, py_project):
        change = _write_change(
            tmp_path / "change",
            impact="Walks with `pathlib.Path.rglob` and formats `datetime.datetime`.",
        )
        assert detect_new_dependencies(change, py_project) == []

    def test_npm_dotted_package_survives_in_a_js_project(self, tmp_path, project):
        (project / "package.json").write_text(json.dumps({"dependencies": {}}))
        change = _write_change(tmp_path / "change", impact="Adds `lodash.debounce`.")
        assert [d.name for d in detect_new_dependencies(change, project)] == ["lodash.debounce"]

    def test_a_declared_dotted_distribution_is_not_renamed_away_from_itself(
            self, tmp_path, project):
        """`ruamel.yaml` is a real dotted PyPI distribution. Collapsing it to
        `ruamel` would un-match it from its own declaration and fire a bogus
        explainer named `ruamel`."""
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["ruamel.yaml"]\n'
        )
        change = _write_change(tmp_path / "change", impact="Parses via `ruamel.yaml`.")
        assert detect_new_dependencies(change, project) == []

    def test_member_access_on_a_declared_dotted_distribution_never_fires(
            self, tmp_path, project):
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["zope.interface"]\n'
        )
        change = _write_change(
            tmp_path / "change", impact="Implements `zope.interface.Interface`.")
        assert detect_new_dependencies(change, project) == []

    def test_an_undeclared_dotted_distribution_still_fires_as_its_head(
            self, tmp_path, py_project):
        """With nothing declared there is no way to know `zope.interface` is
        the distribution name, so the head is the candidate — one explainer,
        slightly under-named, beats none. (`ruamel.yaml` can't get even that:
        it is indistinguishable from a YAML filename, and the extension rule
        wins.)"""
        change = _write_change(tmp_path / "change", impact="Uses `zope.interface`.")
        assert [d.name for d in detect_new_dependencies(change, py_project)] == ["zope"]


class TestBuiltinsAndMembersNeverFire:
    @pytest.fixture
    def py_project(self, project):
        (project / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = []\n')
        return project

    @pytest.mark.parametrize("token", ["open", "print", "sorted", "isinstance", "property"])
    def test_builtin_names_never_fire(self, tmp_path, py_project, token):
        change = _write_change(tmp_path / "change", impact=f"Calls `{token}` directly.")
        assert detect_new_dependencies(change, py_project) == []

    @pytest.mark.parametrize("token", ["ljust", "rglob", "unlink", "iterdir"])
    def test_bare_stdlib_member_names_never_fire(self, tmp_path, py_project, token):
        change = _write_change(tmp_path / "change", impact=f"Uses `{token}` on the result.")
        assert detect_new_dependencies(change, py_project) == []

    @pytest.mark.parametrize("token", ["st_mtime", "st_size", "st_mode"])
    def test_stat_struct_fields_never_fire(self, tmp_path, py_project, token):
        change = _write_change(tmp_path / "change", impact=f"Compares `{token}` values.")
        assert detect_new_dependencies(change, py_project) == []

    @pytest.mark.parametrize("token", ["skip-explainer", "log-warning", "return-early"])
    def test_hyphenated_prose_phrases_never_fire(self, tmp_path, py_project, token):
        change = _write_change(tmp_path / "change", impact=f"We `{token}` in that case.")
        assert detect_new_dependencies(change, py_project) == []

    def test_builtin_named_package_still_fires_in_a_js_project(self, tmp_path, project):
        """`open` is a real npm package — the builtin filter is Python-only."""
        (project / "package.json").write_text(json.dumps({"dependencies": {}}))
        change = _write_change(tmp_path / "change", impact="Adds `open` for launching URLs.")
        assert [d.name for d in detect_new_dependencies(change, project)] == ["open"]

    @pytest.mark.parametrize("token", ["make-dir", "get-port", "create-react-app"])
    def test_verb_headed_npm_package_still_fires_in_a_js_project(
            self, tmp_path, project, token):
        """npm is full of real verb-headed distributions — the prose-verb
        filter is Python-only, like every other Python-shaped heuristic."""
        (project / "package.json").write_text(json.dumps({"dependencies": {}}))
        change = _write_change(tmp_path / "change", impact=f"Adds `{token}`.")
        assert [d.name for d in detect_new_dependencies(change, project)] == [token]

    def test_st_prefixed_npm_package_still_fires_in_a_js_project(self, tmp_path, project):
        """The stat-struct-field filter is Python-only too."""
        (project / "package.json").write_text(json.dumps({"dependencies": {}}))
        change = _write_change(tmp_path / "change", impact="Serves files via `st-cache`.")
        assert [d.name for d in detect_new_dependencies(change, project)] == ["st-cache"]


class TestTemplateHeadingsFeedTheScan:
    """The scan reads two heading strings out of two spec templates. Reshaping
    either template renames the section the scan looks for and turns dependency
    detection into a silent no-op — so the coupling is asserted against the
    shipped templates, not against handwritten markdown."""

    def test_proposal_template_still_carries_the_impact_heading(self):
        from build_pipeline.change_manager import TEMPLATES
        assert "\n## Impact\n" in TEMPLATES["proposal"]

    def test_design_template_still_carries_the_decisions_heading(self):
        from build_pipeline.change_manager import TEMPLATES
        assert "\n## Decisions\n" in TEMPLATES["design"]

    def test_scan_finds_a_dependency_written_into_the_shipped_templates(
        self, tmp_path, project,
    ):
        """End to end over the real templates: a backticked name under the
        proposal's Impact and the design's Decisions is detected."""
        from build_pipeline.change_manager import TEMPLATES

        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = []\n'
        )
        change = tmp_path / "change"
        change.mkdir(parents=True)
        (change / "proposal.md").write_text(
            TEMPLATES["proposal"].replace(
                "## Impact\n", "## Impact\n\nAudio goes through `mlx-whisper`.\n",
            )
        )
        (change / "design.md").write_text(
            TEMPLATES["design"].replace(
                "## Decisions\n", "## Decisions\n\nChart with `altair`.\n",
            )
        )
        found = {d.name: d.mentioned_in for d in detect_new_dependencies(change, project)}
        assert found["mlx-whisper"] == ["proposal.md § Impact"]
        assert found["altair"] == ["design.md § Decisions"]

    def test_use_cases_document_is_not_scanned_for_dependencies(
        self, tmp_path, project,
    ):
        """Personas and outcomes name people and behaviour, not libraries —
        use-cases.md is not one of the scan's two sources."""
        (project / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = []\n'
        )
        change = _write_change(tmp_path / "change")
        (change / "use-cases.md").write_text(
            "## Personas\n\n### Analyst\n- **Who they are:** uses `polars` daily\n\n"
            "## Use cases\n- Analyst wants a chart so that they can see the trend\n"
        )
        assert detect_new_dependencies(change, project) == []
