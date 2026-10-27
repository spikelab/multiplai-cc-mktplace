"""Where the reviewer's standards and the spec writer's reference docs come from.

Three options — `standards_files`, `reference_docs`, `reference_dirs` — each
settable in five places. The highest place that sets an option wins and
replaces the lower ones for it; lists are never merged, so the progress line
can say exactly which docs a run used:

  1. env       BUILDME_STANDARDS_FILES / BUILDME_REFERENCE_DOCS / BUILDME_REFERENCE_DIRS
  2. config    specs/config.yaml
  3. plugin    multiplai-dev plugin options (CLAUDE_PLUGIN_OPTION_*)
  4. conf      multiplai.conf [buildme]
  5. built-in  $CLAUDE_CONFIG_DIR/reference/dev/ and _DEFAULT_REFERENCE_DOCS
"""

import json
from pathlib import Path

import pytest
from build_pipeline.config import BuildConfig

PLUGIN_JSON = Path(__file__).resolve().parents[4] / ".claude-plugin" / "plugin.json"


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or f"# {path.name}\nbody of {path.name}\n")
    return path


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "claude-config"
    (d / "reference" / "dev").mkdir(parents=True)
    return d


@pytest.fixture
def conf(tmp_path, monkeypatch):
    """Write a multiplai.conf and point the loader at it."""

    def _conf(body: str) -> None:
        home = tmp_path / "multiplai-home"
        home.mkdir(exist_ok=True)
        (home / "multiplai.conf").write_text(body)
        monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(home))

    return _conf


def _config(project, config_dir, **kwargs) -> BuildConfig:
    config = BuildConfig(project_dir=project, config_dir=config_dir, **kwargs)
    config.stack = "pyproject"
    return config


def _load_yaml(config: BuildConfig, body: str) -> None:
    specs = config.project_dir / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "config.yaml").write_text(body)
    config.specs_dir = specs
    config._load_specs_config()


# --- standards_files: the reviewer's list -----------------------------------


class TestStandardsFilesRanks:
    def test_env_replaces_config(self, project, config_dir, monkeypatch):
        _write(project / "a.md", "rule A")
        _write(project / "b.md", "rule B")
        _write(project / "c.md", "rule C")
        monkeypatch.setenv("BUILDME_STANDARDS_FILES", "b.md:c.md")
        config = _config(project, config_dir, standards_files=["a.md"])
        text = config.standards_text()
        assert "rule B" in text and "rule C" in text
        assert "rule A" not in text
        assert config.standards_source() == (["b.md", "c.md"], "env")

    def test_config_yaml_replaces_plugin_option(self, project, config_dir, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STANDARDS_FILES", "p.md")
        config = _config(project, config_dir)
        _load_yaml(config, "standards_files:\n  - a.md\n")
        assert config.standards_source() == (["a.md"], "config")

    def test_plugin_option_replaces_conf(self, project, config_dir, monkeypatch, conf):
        conf("[buildme]\nSTANDARDS_FILES=c.md\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STANDARDS_FILES", "p.md:q.md")
        config = _config(project, config_dir)
        assert config.standards_source() == (["p.md", "q.md"], "plugin")

    def test_conf_used_when_nothing_above_sets_it(self, project, config_dir, conf):
        conf("[buildme]\nSTANDARDS_FILES=c.md:d.md\n")
        config = _config(project, config_dir)
        assert config.standards_source() == (["c.md", "d.md"], "conf")

    def test_nothing_set_is_the_empty_built_in(self, project, config_dir):
        config = _config(project, config_dir)
        assert config.standards_source() == ([], "built-in")

    def test_blank_env_counts_as_unset(self, project, config_dir, monkeypatch):
        monkeypatch.setenv("BUILDME_STANDARDS_FILES", "  ")
        config = _config(project, config_dir, standards_files=["a.md"])
        assert config.standards_source() == (["a.md"], "config")

    def test_standards_file_resolves_through_a_configured_reference_dir(
        self, project, config_dir, tmp_path, monkeypatch
    ):
        house = tmp_path / "house-standards"
        _write(house / "style.md", "house rule")
        monkeypatch.setenv("BUILDME_REFERENCE_DIRS", str(house))
        config = _config(project, config_dir, standards_files=["style.md"])
        assert "house rule" in config.standards_text()

    def test_standards_line_names_its_rank_once(
        self, project, config_dir, monkeypatch, capsys
    ):
        _write(project / "b.md")
        monkeypatch.setenv("BUILDME_STANDARDS_FILES", "b.md")
        config = _config(project, config_dir)
        config.standards_text()
        config.standards_text()
        out = capsys.readouterr().out
        assert out.count("STANDARDS(") == 1
        assert "STANDARDS(env):b.md" in out

    def test_standards_line_says_none_when_nothing_is_configured(
        self, project, config_dir, capsys
    ):
        _config(project, config_dir).standards_text()
        assert "STANDARDS(built-in):(none)" in capsys.readouterr().out


# --- reference_dirs: where reference doc names are looked up -----------------


class TestReferenceDirsRanks:
    def test_env_dir_replaces_the_default_dir(
        self, project, config_dir, tmp_path, monkeypatch
    ):
        """Replace, not merge: a doc only in the default dir stops resolving."""
        _write(config_dir / "reference" / "dev" / "python-project-structure.md")
        custom = tmp_path / "custom"
        _write(custom / "uv-python-best-practices.md")
        monkeypatch.setenv("BUILDME_REFERENCE_DIRS", str(custom))
        config = _config(project, config_dir)
        assert [p.name for p in config.stack_reference_docs()] == [
            "uv-python-best-practices.md"
        ]
        assert config.reference_dirs_source() == ([custom], "env")

    def test_dirs_are_searched_in_order(
        self, project, config_dir, tmp_path, monkeypatch
    ):
        first, second = tmp_path / "first", tmp_path / "second"
        _write(first / "uv-python-best-practices.md")
        _write(second / "uv-python-best-practices.md")
        monkeypatch.setenv("BUILDME_REFERENCE_DIRS", f"{first}:{second}")
        config = _config(project, config_dir)
        assert config.stack_reference_docs()[0].parent == first

    def test_config_yaml_dir_is_relative_to_the_project(self, project, config_dir):
        _write(project / "docs" / "standards" / "uv-python-best-practices.md")
        config = _config(project, config_dir)
        _load_yaml(config, "reference_dirs:\n  - docs/standards\n")
        assert config.reference_dirs_source() == (
            [project / "docs" / "standards"],
            "config",
        )
        assert [p.name for p in config.stack_reference_docs()] == [
            "uv-python-best-practices.md"
        ]

    def test_plugin_option_dir(self, project, config_dir, tmp_path, monkeypatch, conf):
        conf("[buildme]\nREFERENCE_DIRS=/from/conf\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_REFERENCE_DIRS", str(tmp_path / "p"))
        config = _config(project, config_dir)
        assert config.reference_dirs_source() == ([tmp_path / "p"], "plugin")

    def test_conf_dir(self, project, config_dir, tmp_path, conf):
        conf(f"[buildme]\nREFERENCE_DIRS={tmp_path / 'c'}\n")
        config = _config(project, config_dir)
        assert config.reference_dirs_source() == ([tmp_path / "c"], "conf")

    def test_default_is_reference_dev(self, project, config_dir):
        config = _config(project, config_dir)
        assert config.reference_dirs_source() == (
            [config_dir / "reference" / "dev"],
            "built-in",
        )


# --- reference_docs: the spec writer's list ----------------------------------


class TestReferenceDocsRanks:
    def test_config_entry_may_be_a_repo_relative_path(self, project, config_dir):
        _write(project / "docs" / "standards" / "testing.md", "test rules")
        config = _config(project, config_dir)
        _load_yaml(
            config, "reference_docs:\n  pyproject: [docs/standards/testing.md]\n"
        )
        assert config.stack_reference_docs() == [
            project / "docs" / "standards" / "testing.md"
        ]
        assert "test rules" in config.reference_docs_text()

    def test_env_flat_list_replaces_the_whole_mapping(
        self, project, config_dir, monkeypatch
    ):
        _write(config_dir / "reference" / "dev" / "uv-python-best-practices.md")
        _write(project / "docs" / "a.md")
        _write(project / "docs" / "b.md")
        monkeypatch.setenv("BUILDME_REFERENCE_DOCS", "docs/b.md")
        config = _config(project, config_dir)
        _load_yaml(config, "reference_docs:\n  pyproject: [docs/a.md]\n")
        assert config.stack_reference_docs() == [project / "docs" / "b.md"]

    def test_references_line_names_env(self, project, config_dir, monkeypatch, capsys):
        _write(project / "x.md")
        monkeypatch.setenv("BUILDME_REFERENCE_DOCS", "x.md")
        _config(project, config_dir).stack_reference_docs()
        assert "REFERENCES(env):x.md" in capsys.readouterr().out

    def test_references_line_names_config(self, project, config_dir, capsys):
        _write(project / "docs" / "a.md")
        config = _config(project, config_dir)
        _load_yaml(config, "reference_docs:\n  pyproject: [docs/a.md]\n")
        config.stack_reference_docs()
        assert "REFERENCES(config):a.md" in capsys.readouterr().out

    def test_references_line_names_built_in(self, project, config_dir, capsys):
        _write(config_dir / "reference" / "dev" / "python-project-structure.md")
        _config(project, config_dir).stack_reference_docs()
        assert (
            "REFERENCES(built-in):python-project-structure.md"
            in capsys.readouterr().out
        )

    def test_a_named_doc_that_is_missing_warns(
        self, project, config_dir, monkeypatch, caplog
    ):
        monkeypatch.setenv("BUILDME_REFERENCE_DOCS", "docs/typo.md")
        with caplog.at_level("INFO"):
            _config(project, config_dir).stack_reference_docs()
        misses = [r for r in caplog.records if "docs/typo.md" in r.getMessage()]
        assert misses and all(r.levelname == "WARNING" for r in misses)

    def test_a_config_named_doc_that_is_missing_warns(
        self, project, config_dir, caplog
    ):
        config = _config(project, config_dir)
        _load_yaml(config, "reference_docs:\n  pyproject: [docs/typo.md]\n")
        with caplog.at_level("INFO"):
            config.stack_reference_docs()
        misses = [r for r in caplog.records if "docs/typo.md" in r.getMessage()]
        assert misses and all(r.levelname == "WARNING" for r in misses)

    def test_a_missing_built_in_doc_stays_info(self, project, config_dir, caplog):
        """The built-in map may name a doc nobody has written yet."""
        _write(config_dir / "reference" / "dev" / "python-project-structure.md")
        with caplog.at_level("INFO"):
            _config(project, config_dir).stack_reference_docs()
        misses = [
            r
            for r in caplog.records
            if "uv-python-best-practices.md" in r.getMessage()
            and "not found" in r.getMessage()
        ]
        assert misses and all(r.levelname == "INFO" for r in misses)


# --- the plugin manifest ------------------------------------------------------


def test_plugin_json_declares_both_options():
    user_config = json.loads(PLUGIN_JSON.read_text())["userConfig"]
    for key in ("standards_files", "reference_dirs"):
        assert user_config[key]["type"] == "string"
        assert user_config[key]["default"] == ""
        assert user_config[key]["description"]
