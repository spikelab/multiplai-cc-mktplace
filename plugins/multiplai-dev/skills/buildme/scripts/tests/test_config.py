"""Tests for config — tier detection, test command discovery, config loading."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from build_pipeline.config import conf_effort, detect_tier, BuildConfig, GateToggles


class TestTierDetection:
    def test_opus_46_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-6"}):
            tier, name = detect_tier()
            assert tier == "advanced"
            assert "opus-4-6" in name

    def test_opus_45_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-5"}):
            tier, _ = detect_tier()
            assert tier == "advanced"

    def test_sonnet_is_standard(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-sonnet-4-6"}):
            tier, _ = detect_tier()
            assert tier == "standard"

    def test_haiku_is_standard(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-haiku-4-5"}):
            tier, _ = detect_tier()
            assert tier == "standard"

    def test_no_env_model_derives_from_default_model(self):
        """DEV-3 fix: with CLAUDE_MODEL unset, tier derives from the resolved
        DEFAULT_MODEL (the model buildme actually runs), not a hardcoded
        'standard'/'unknown'. An opus DEFAULT_MODEL → advanced."""
        import build_pipeline.config as cfg
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(cfg, "DEFAULT_MODEL", "claude-opus-4-8"):
            tier, name = detect_tier()
            assert tier == "advanced"
            assert name == "claude-opus-4-8"

    def test_no_env_model_sonnet_default_is_standard(self):
        """A sonnet DEFAULT_MODEL (e.g. under a sonnet ceiling) → standard."""
        import build_pipeline.config as cfg
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(cfg, "DEFAULT_MODEL", "claude-sonnet-5"):
            tier, name = detect_tier()
            assert tier == "standard"
            assert name == "claude-sonnet-5"

    def test_unknown_model_defaults_standard(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "gpt-4-turbo"}):
            tier, _ = detect_tier()
            assert tier == "standard"

    def test_future_opus_5_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-5-0"}):
            tier, _ = detect_tier()
            assert tier == "advanced"

    def test_opus_47_is_advanced(self):
        """The skill pins claude-opus-4-7 — the version-range check must accept it
        (the old literal allowlist would have silently downgraded it to standard)."""
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-7"}):
            tier, name = detect_tier()
            assert tier == "advanced"
            assert "opus-4-7" in name

    def test_opus_48_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-8"}):
            tier, _ = detect_tier()
            assert tier == "advanced"

    def test_opus_44_is_standard(self):
        """Below the 4.5 floor stays standard."""
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-4"}):
            tier, _ = detect_tier()
            assert tier == "standard"


class TestTestCommandDiscovery:
    def test_discovers_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == "pytest -xvs"

    def test_discovers_swift_test(self, tmp_path):
        (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == "swift test"

    def test_discovers_npm_test(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}\n')
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == "npm test"

    def test_no_test_command_if_no_markers(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == ""


class TestGateToggles:
    def test_defaults_all_enabled(self):
        g = GateToggles()
        assert g.code_review_per_block
        assert g.security_review_per_block
        assert g.test_quality_enabled
        assert g.e2e_test_entry_point_check

    def test_toggle_off(self):
        g = GateToggles(security_review_per_block=False)
        assert not g.security_review_per_block


class TestTierProperties:
    def test_advanced_task_granularity(self):
        c = BuildConfig(tier="advanced")
        assert c.task_granularity == "blocks"

    def test_standard_task_granularity(self):
        c = BuildConfig(tier="standard")
        assert c.task_granularity == "checkboxes"

    def test_advanced_agent_scope(self):
        c = BuildConfig(tier="advanced")
        assert c.agent_scope == "per_block"

    def test_standard_agent_scope(self):
        c = BuildConfig(tier="standard")
        assert c.agent_scope == "per_task"

    def test_advanced_no_refactor_phase(self):
        c = BuildConfig(tier="advanced")
        assert not c.refactor_phase
        assert c.tdd_phases == ["test", "implement"]

    def test_standard_has_refactor_phase(self):
        c = BuildConfig(tier="standard")
        assert c.refactor_phase
        assert c.tdd_phases == ["test", "implement", "refactor"]

    def test_advanced_implementer_prompt_clean(self):
        c = BuildConfig(tier="advanced")
        assert c.implementer_prompt_style == "clean"

    def test_standard_implementer_prompt_minimum(self):
        c = BuildConfig(tier="standard")
        assert c.implementer_prompt_style == "minimum"


class TestReviewModel:
    def test_defaults_to_none(self):
        assert BuildConfig().review_model is None

    def test_loaded_from_specs_config_yaml(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            "code_review:\n  per_block: true\n  model: claude-sonnet-4-6\n"
        )
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.review_model == "claude-sonnet-4-6"

    def test_yaml_does_not_override_existing_value(self, tmp_path):
        """An already-set review_model (e.g. from env) wins over config.yaml."""
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("code_review:\n  model: claude-haiku-4-5\n")
        config = BuildConfig(project_dir=tmp_path, review_model="claude-sonnet-4-6")
        config.specs_dir = specs
        config._load_specs_config()
        assert config.review_model == "claude-sonnet-4-6"

    def test_env_override_wins(self, tmp_path):
        import argparse
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("code_review:\n  model: claude-haiku-4-5\n")
        args = argparse.Namespace(project_dir=str(tmp_path), mode="only", change="feat")
        with patch.dict(os.environ, {"BUILDME_REVIEW_MODEL": "claude-sonnet-4-6"}):
            config = BuildConfig.from_cli_args(args)
        assert config.review_model == "claude-sonnet-4-6"

    def test_review_model_is_ceiling_capped(self, tmp_path):
        """The MULTIPLAI_MODEL ceiling applies to the reviewer model too."""
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("code_review:\n  model: claude-opus-4-6\n")
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        with patch.dict(os.environ, {"MULTIPLAI_MODEL": "claude-sonnet-4-6"}):
            config._load_specs_config()
        assert config.review_model == "claude-sonnet-4-6"


class TestStandardsFiles:
    def test_defaults_empty(self):
        config = BuildConfig()
        assert config.standards_files == []
        assert config.standards_text() == ""

    def test_parsed_from_specs_config_yaml(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            "standards_files:\n  - docs/standards.md\n  - python-style.md\n"
        )
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.standards_files == ["docs/standards.md", "python-style.md"]

    def test_standards_text_reads_project_relative_files(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "standards.md").write_text("Never use bare except.")
        config = BuildConfig(
            project_dir=tmp_path,
            config_dir=tmp_path / "no-such-config",
            standards_files=["docs/standards.md"],
        )
        text = config.standards_text()
        assert "Never use bare except." in text
        assert "standards.md" in text

    def test_standards_text_reads_reference_dev_files(self, tmp_path):
        config_dir = tmp_path / "claude-config"
        ref_dir = config_dir / "reference" / "dev"
        ref_dir.mkdir(parents=True)
        (ref_dir / "python-style.md").write_text("Use type hints everywhere.")
        config = BuildConfig(
            project_dir=tmp_path / "project",
            config_dir=config_dir,
            standards_files=["python-style.md"],
        )
        assert "Use type hints everywhere." in config.standards_text()

    def test_missing_standards_file_skipped(self, tmp_path):
        config = BuildConfig(
            project_dir=tmp_path,
            config_dir=tmp_path / "no-such-config",
            standards_files=["does-not-exist.md"],
        )
        assert config.standards_text() == ""

    def test_unreadable_standards_file_skipped_not_fatal(self, tmp_path, caplog):
        """One bad standards doc (here: invalid UTF-8 → UnicodeDecodeError)
        is logged and skipped, per the docstring — it must not fail the block.
        Readable siblings still land in the output."""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "good.md").write_text("Never use bare except.")
        (docs / "binary.md").write_bytes(b"\xff\xfe\x00garbage\x80\x81")
        config = BuildConfig(
            project_dir=tmp_path,
            config_dir=tmp_path / "no-such-config",
            standards_files=["docs/binary.md", "docs/good.md"],
        )
        with caplog.at_level("WARNING"):
            text = config.standards_text()
        assert "Never use bare except." in text
        assert "binary.md" not in text
        assert any("unreadable" in r.getMessage() for r in caplog.records)


class TestConfigPaths:
    def test_change_dir(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="my-feature")
        config.specs_dir = tmp_path / "specs"
        assert config.change_dir == tmp_path / "specs" / "changes" / "my-feature"

    def test_tasks_path(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="feat")
        config.specs_dir = tmp_path / "specs"
        assert config.tasks_path == tmp_path / "specs" / "changes" / "feat" / "tasks.md"

    def test_change_dir_normalizes_traversal(self, tmp_path):
        """A --change value that tries to escape specs/changes/ is neutralized,
        so archive()'s shutil.move can never target an out-of-tree directory."""
        config = BuildConfig(project_dir=tmp_path, change_name="../../etc/passwd")
        config.specs_dir = tmp_path / "specs"
        cd = config.change_dir
        assert ".." not in cd.parts
        assert cd.parent == tmp_path / "specs" / "changes"
        assert cd == tmp_path / "specs" / "changes" / "etcpasswd"

    def test_change_dir_normalizes_case_and_spaces(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="My Feature")
        config.specs_dir = tmp_path / "specs"
        assert config.change_dir == tmp_path / "specs" / "changes" / "my-feature"


def _config_from_yaml(tmp_path, yaml_text):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "config.yaml").write_text(yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = specs
    config._load_specs_config()
    return config


class TestReviewPanelConfig:
    def test_defaults_to_no_panel(self):
        assert BuildConfig().review_panel == []

    def test_accepts_dict_and_bare_string_entries(self, tmp_path):
        config = _config_from_yaml(
            tmp_path, "code_review:\n  panel:\n    - model: opus\n    - sonnet\n"
        )
        assert len(config.review_panel) == 2

    def test_provider_qualified_entries_bypass_the_ceiling(self, tmp_path):
        """The point of a cross-family member is that it is not in the family
        the Claude ceiling ranks."""
        config = _config_from_yaml(
            tmp_path, "code_review:\n  panel:\n    - openai:gpt-5\n"
        )
        assert config.review_panel == ["openai:gpt-5"]

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path):
        config = _config_from_yaml(
            tmp_path, "code_review:\n  panel:\n    - {}\n    - opus\n"
        )
        assert len(config.review_panel) == 1

    def test_non_list_panel_is_ignored(self, tmp_path):
        config = _config_from_yaml(tmp_path, "code_review:\n  panel: opus\n")
        assert config.review_panel == []


class TestReviewGateConfig:
    def test_defaults_match_the_previous_hardcoded_thresholds(self):
        gate = BuildConfig().review_gate
        assert gate.min_weighted_average == 3.5
        assert gate.critical_score == 1.0

    def test_thresholds_load_from_yaml(self, tmp_path):
        config = _config_from_yaml(
            tmp_path,
            "code_review:\n  gate:\n    min_weighted_average: 4.0\n    critical_score: 2\n",
        )
        assert config.review_gate.min_weighted_average == 4.0
        assert config.review_gate.critical_score == 2

    def test_invalid_thresholds_keep_the_defaults(self, tmp_path):
        """A typo'd threshold must not silently loosen the gate."""
        config = _config_from_yaml(
            tmp_path, "code_review:\n  gate:\n    min_weighted_average: not-a-number\n"
        )
        assert config.review_gate.min_weighted_average == 3.5

    def test_unknown_keys_are_ignored(self, tmp_path):
        config = _config_from_yaml(
            tmp_path, "code_review:\n  gate:\n    nonsense: 1\n    critical_score: 2\n"
        )
        assert config.review_gate.critical_score == 2


class TestAdjudicationConfig:
    def test_on_by_default(self):
        assert BuildConfig().adjudicate_findings is True

    def test_can_be_disabled(self, tmp_path):
        config = _config_from_yaml(tmp_path, "code_review:\n  adjudicate: false\n")
        assert config.adjudicate_findings is False


class TestBudgetConfig:
    def test_unlimited_by_default(self):
        config = BuildConfig()
        assert config.budget_max_tokens is None
        assert config.budget_max_usd is None

    def test_ceilings_load_from_yaml(self, tmp_path):
        config = _config_from_yaml(tmp_path, "budget:\n  max_tokens: 5000000\n  max_usd: 25\n")
        assert config.budget_max_tokens == 5_000_000
        assert config.budget_max_usd == 25.0

    def test_unparseable_ceiling_falls_back_to_unlimited(self, tmp_path):
        config = _config_from_yaml(tmp_path, "budget:\n  max_tokens: lots\n")
        assert config.budget_max_tokens is None

    def test_absent_budget_section_is_unlimited(self, tmp_path):
        config = _config_from_yaml(tmp_path, "tdd:\n  test_command: pytest\n")
        assert config.budget_max_tokens is None
class TestExplainerToggle:
    """B1 explainers default ON; the CLI flag wins over specs/config.yaml."""

    def test_default_is_on(self):
        assert GateToggles().explainers_enabled
        assert BuildConfig().explainers_active

    def test_config_yaml_can_disable(self, tmp_path):
        import yaml
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            yaml.dump({"context": "demo", "explainers": {"enabled": False}})
        )
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert not config.gates.explainers_enabled
        assert not config.explainers_active

    def test_cli_flag_disables_even_when_config_enables(self, tmp_path):
        import yaml
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            yaml.dump({"context": "demo", "explainers": {"enabled": True}})
        )
        config = BuildConfig(project_dir=tmp_path, skip_explainers=True)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.gates.explainers_enabled
        assert not config.explainers_active

    def test_unknowns_path_lives_in_the_change_dir(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="my-change")
        config.specs_dir = tmp_path / "specs"
        assert config.unknowns_path == (
            tmp_path / "specs" / "changes" / "my-change" / "unknowns.md"
        )


class TestSkipExplainersFlag:
    def test_flag_is_declared_on_build_and_spec_generate(self):
        from build_pipeline.__main__ import build_parser

        parser = build_parser()
        for argv in (
            ["build", "--skip-explainers"],
            ["spec-generate", "--change", "x", "--skip-explainers"],
        ):
            assert parser.parse_args(argv).skip_explainers is True

    def test_default_is_false(self):
        from build_pipeline.__main__ import build_parser

        assert build_parser().parse_args(["build"]).skip_explainers is False


class TestPrototypeToggle:
    """`prototype: {enabled: auto|true|false}` in specs/config.yaml, with the
    --prototype / --no-prototype CLI flags overriding it."""

    def _write_config(self, tmp_path, body):
        specs = tmp_path / "specs"
        specs.mkdir(parents=True, exist_ok=True)
        (specs / "config.yaml").write_text(body)
        return specs

    def test_defaults_to_auto(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config._load_specs_config()
        assert config.gates.prototype == "auto"

    def test_reads_auto_from_config_yaml(self, tmp_path):
        self._write_config(tmp_path, "prototype:\n  enabled: auto\n")
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config._load_specs_config()
        assert config.gates.prototype == "auto"

    def test_reads_boolean_from_config_yaml(self, tmp_path):
        self._write_config(tmp_path, "prototype:\n  enabled: false\n")
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config._load_specs_config()
        assert config.gates.prototype == "false"

    def test_unrecognized_value_falls_back_to_auto(self, tmp_path):
        self._write_config(tmp_path, "prototype:\n  enabled: sometimes\n")
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config._load_specs_config()
        assert config.gates.prototype == "auto"

    def test_cli_flag_overrides_config_yaml(self, tmp_path):
        import argparse
        self._write_config(tmp_path, "prototype:\n  enabled: true\n")
        args = argparse.Namespace(
            mode="scratch", project_dir=str(tmp_path), change="c",
            auto=False, spec_only=False, skip_research=False,
            lenient_review=False, prototype=False, no_prototype=True,
        )
        config = BuildConfig.from_cli_args(args)
        assert config.prototype_mode == "false"

    def test_no_flags_takes_the_config_value(self, tmp_path):
        import argparse
        self._write_config(tmp_path, "prototype:\n  enabled: true\n")
        args = argparse.Namespace(
            mode="scratch", project_dir=str(tmp_path), change="c",
            auto=False, spec_only=False, skip_research=False,
            lenient_review=False, prototype=False, no_prototype=False,
        )
        config = BuildConfig.from_cli_args(args)
        assert config.prototype_mode == "true"

    def test_prototype_dir_is_inside_the_change(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="my-change")
        config.specs_dir = tmp_path / "specs"
        assert config.prototype_dir == config.change_dir / "prototype"

    def test_cli_parser_exposes_the_flags(self):
        from build_pipeline.__main__ import build_parser
        parser = build_parser()
        assert parser.parse_args(["build", "--no-prototype"]).no_prototype is True
        assert parser.parse_args(["build", "--prototype"]).prototype is True
        with pytest.raises(SystemExit):
            parser.parse_args(["build", "--prototype", "--no-prototype"])


class TestRespecToggle:
    """`respec: {halt_on_contradiction: ...}` in specs/config.yaml, default off."""

    def test_defaults_to_false(self):
        assert BuildConfig().gates.respec_halt_on_contradiction is False

    def test_absent_config_section_keeps_the_default(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("context: demo\n")
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.gates.respec_halt_on_contradiction is False

    def test_enabled_from_specs_config_yaml(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("respec:\n  halt_on_contradiction: true\n")
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.gates.respec_halt_on_contradiction is True


# --- Git lifecycle toggles (work item 4) ---------------------------------

class TestGitToggles:
    def test_defaults_are_worktree_on_push_on_pr_draft(self):
        from build_pipeline.config import BuildConfig, GitToggles

        assert GitToggles() == GitToggles(worktree=True, push=True, pr="draft")
        assert BuildConfig().git.worktree is True
        assert BuildConfig().pipeline_branch is None

    def test_loaded_from_config_yaml(self, tmp_path):
        import yaml
        from build_pipeline.config import BuildConfig

        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(yaml.dump({
            "schema": "spec-driven",
            "git": {"worktree": False, "push": False, "pr": "ready"},
        }))
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.git.worktree is False
        assert config.git.push is False
        assert config.git.pr == "ready"

    def test_invalid_pr_mode_falls_back_to_draft(self, tmp_path):
        import yaml
        from build_pipeline.config import BuildConfig

        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(yaml.dump({"git": {"pr": "merge-it"}}))
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = specs
        config._load_specs_config()
        assert config.git.pr == "draft"

    def test_cli_flags_beat_config_yaml(self, tmp_path):
        import argparse
        from build_pipeline.config import BuildConfig

        config = BuildConfig(project_dir=tmp_path)
        config._apply_git_cli_overrides(argparse.Namespace(
            no_worktree=True, no_push=True, no_pr=True, pr_ready=False,
        ))
        assert config.git.worktree is False
        assert config.git.push is False
        assert config.git.pr == "none"

    def test_pr_ready_flag(self, tmp_path):
        import argparse
        from build_pipeline.config import BuildConfig

        config = BuildConfig(project_dir=tmp_path)
        config._apply_git_cli_overrides(argparse.Namespace(
            no_worktree=False, no_push=False, no_pr=False, pr_ready=True,
        ))
        assert config.git.pr == "ready"

    def test_absent_flags_leave_defaults(self, tmp_path):
        import argparse
        from build_pipeline.config import BuildConfig

        config = BuildConfig(project_dir=tmp_path)
        config._apply_git_cli_overrides(argparse.Namespace())
        assert config.git == type(config.git)(worktree=True, push=True, pr="draft")

    def test_cli_parser_exposes_the_flags(self):
        from build_pipeline.__main__ import build_parser

        args = build_parser().parse_args(
            ["build", "--no-worktree", "--no-push", "--no-pr", "--pr-ready"]
        )
        assert args.no_worktree and args.no_push and args.no_pr and args.pr_ready
        plain = build_parser().parse_args(["build"])
        assert not plain.no_worktree and not plain.no_push and not plain.no_pr


class TestRebindProjectDir:
    def test_rebinding_moves_every_derived_path(self, tmp_path):
        from build_pipeline.config import BuildConfig

        config = BuildConfig(project_dir=tmp_path / "src", change_name="c")
        config.specs_dir = tmp_path / "src" / "specs"
        wt = tmp_path / "wt"
        config.rebind_project_dir(wt)
        assert config.project_dir == wt
        assert config.specs_dir == wt / "specs"
        assert config.change_dir == wt / "specs" / "changes" / "c"
        assert config.state_file_path() == wt / "specs" / "changes" / "c" / ".build-state.json"
        assert config.progress_file_path() == wt / "build-progress.md"


class TestConfEffort:
    """Effort is the second axis of the same tuning decision as MODEL=; it
    used to be the half you could not reach from multiplai.conf."""

    @staticmethod
    def _conf(tmp_path: Path, body: str) -> dict:
        (tmp_path / "multiplai.conf").write_text(body)
        return {"CLAUDE_MULTIPLAI_HOME": str(tmp_path), "MULTIPLAI_EFFORT": "high"}

    def test_absent_conf_leaves_effort_unset(self, tmp_path):
        with patch.dict(os.environ, self._conf(tmp_path, ""), clear=True):
            assert conf_effort("buildme") is None
            assert conf_effort("buildme", "low") == "low"

    def test_section_effort_is_returned(self, tmp_path):
        with patch.dict(os.environ, self._conf(tmp_path, "[buildme]\nEFFORT=medium\n"), clear=True):
            assert conf_effort("buildme") == "medium"

    def test_effort_is_capped_by_the_ceiling(self, tmp_path):
        """A budget run forces every step down — a conf override must not be
        the one thing that escapes the ceiling."""
        env = self._conf(tmp_path, "[buildme]\nEFFORT=high\n")
        env["MULTIPLAI_EFFORT"] = "low"
        with patch.dict(os.environ, env, clear=True):
            assert conf_effort("buildme") == "low"

    def test_blank_value_is_treated_as_unset(self, tmp_path):
        with patch.dict(os.environ, self._conf(tmp_path, "[buildme]\nEFFORT=\n"), clear=True):
            assert conf_effort("buildme", "medium") == "medium"

    def test_step_section_is_independent_of_the_pipeline_wide_one(self, tmp_path):
        body = "[buildme]\nEFFORT=low\n[buildme.review]\nEFFORT=high\n"
        with patch.dict(os.environ, self._conf(tmp_path, body), clear=True):
            assert conf_effort("buildme") == "low"
            assert conf_effort("buildme.review", conf_effort("buildme")) == "high"

    def test_step_falls_back_to_the_pipeline_wide_value(self, tmp_path):
        with patch.dict(os.environ, self._conf(tmp_path, "[buildme]\nEFFORT=medium\n"), clear=True):
            assert conf_effort("buildme.review", conf_effort("buildme")) == "medium"

    def test_unknown_effort_is_ignored_not_passed_through(self, tmp_path):
        """`resolve_effort` ranks an unknown name at the high tier, so the
        ceiling never trips and the typo would reach the SDK verbatim."""
        with patch.dict(os.environ, self._conf(tmp_path, "[buildme]\nEFFORT=turbo\n"), clear=True):
            assert conf_effort("buildme") is None
            assert conf_effort("buildme", "medium") == "medium"


class TestBuildConfigEffortFields:
    def test_defaults_are_unset_without_a_conf(self, tmp_path):
        with patch.dict(os.environ, {"CLAUDE_MULTIPLAI_HOME": str(tmp_path)}, clear=True):
            config = BuildConfig()
            assert config.spec_effort is None
            assert config.review_effort is None
            assert config.agent_effort is None

    def test_step_fields_pick_up_their_conf_sections(self, tmp_path):
        (tmp_path / "multiplai.conf").write_text(
            "[buildme]\nEFFORT=low\n[buildme.review]\nEFFORT=high\n")
        with patch.dict(os.environ, {"CLAUDE_MULTIPLAI_HOME": str(tmp_path),
                                     "MULTIPLAI_EFFORT": "high"}, clear=True):
            config = BuildConfig()
            assert config.review_effort == "high"
            # spec/agent inherit the pipeline-wide value
            assert config.spec_effort == "low"
            assert config.agent_effort == "low"

    def test_explicit_root_effort_reaches_every_step(self, tmp_path):
        """The fallback lives in __post_init__, so a directly-constructed
        config propagates too — per-field conf reads would have ignored it."""
        with patch.dict(os.environ, {"CLAUDE_MULTIPLAI_HOME": str(tmp_path)}, clear=True):
            config = BuildConfig(effort="low")
            assert (config.spec_effort, config.review_effort, config.agent_effort) == \
                ("low", "low", "low")

    def test_an_explicit_step_effort_still_wins_over_the_root(self, tmp_path):
        with patch.dict(os.environ, {"CLAUDE_MULTIPLAI_HOME": str(tmp_path)}, clear=True):
            config = BuildConfig(effort="low", review_effort="max")
            assert config.review_effort == "max"
            assert config.spec_effort == "low"
