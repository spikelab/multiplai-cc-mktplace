"""Each block's section of tasks.md lists what its agents get besides the
block's text (`block_inputs`), written after the plan check. The list is
buildme's: it changes no plan revision, never reaches the agents as block
text, and is rewritten whole on every run."""

from __future__ import annotations

import pytest
from build_pipeline import block_inputs
from build_pipeline.spec_generator import _plan_hashes
from build_pipeline.tdd_engine import parse_blocks

from .test_nested_components import _config, write_nested

PLAN = (
    "# Implementation plan\n\nOpening.\n\n## Component: a\n\nStore.\n\n"
    "## Component: b\n\nForm.\n\n## Global Constraints\n\nNone.\n"
)
TASKS = (
    "# Tasks: b\n\nDepartures: none.\n\n"
    "## 1. Keep the form\n\nThe Component: b form keeps typed text.\n\n"
    "Satisfies: cap-b\n\nInterfaces:\n- Produces: `keep(form) -> None`\n\n"
    "## 2. Restore the form\n\nRestore it after a login.\n\n"
    "Satisfies: cap-shared\n\nInterfaces:\n- Consumes: `keep(form) -> None`\n"
)


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "specs" / "changes" / "e"
    write_nested(root)
    (root / "implementation-plan.md").write_text(PLAN)
    config = _config(tmp_path, "e/b")
    (config.change_dir / "tasks.md").write_text(TASKS)
    (config.change_dir / "rubric.md").write_text("# Rubric\n")
    return config


def test_each_block_gets_its_list_and_strip_gives_the_text_back(config):
    assert block_inputs.write_inputs(config)
    text = config.tasks_path.read_text()
    assert text.count(block_inputs.START) == 2
    first, second = text.split("## 2. Restore the form")
    assert "the whole of `../../implementation-plan.md`" in first
    assert "Signatures from earlier blocks" not in first
    assert "block 1 (Keep the form): `keep(form) -> None`" in second
    assert "- `rubric.md`: every agent" in second
    assert block_inputs.strip(text) == TASKS
    # Rewritten whole, never twice.
    assert not block_inputs.write_inputs(config)
    assert config.tasks_path.read_text() == text


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"])
def test_strip_undoes_render_whatever_the_file_ends_with(config, ending):
    original = TASKS.rstrip("\n") + ending
    rendered = block_inputs.render(
        original, config, parse_blocks(_write(config, original))
    )
    assert block_inputs.strip(rendered) == original


def _write(config, text: str):
    config.tasks_path.write_text(text)
    return config.tasks_path


def test_the_agents_never_get_the_list_and_no_revision_moves(config):
    before_blocks = parse_blocks(config.tasks_path)
    before_hashes = _plan_hashes(config)
    block_inputs.write_inputs(config)
    assert parse_blocks(config.tasks_path) == before_blocks
    assert _plan_hashes(config) == before_hashes


def test_sliced_context_names_the_sections_and_requirements_a_block_gets(config):
    config.tdd_context = "sliced"
    block_inputs.write_inputs(config)
    first = config.tasks_path.read_text().split("## 2.")[0]
    assert (
        "- Implementation plan: from `../../implementation-plan.md`, its opening and "
        "`## Component: b`" in first
    )
    assert "- Requirements: `specs.md#cap-b` of this component's specs" in first
    assert "- `rubric.md`: the reviewer only" in first


def test_a_tasks_md_with_no_blocks_is_left_alone(config):
    _write(config, "# Tasks\n\nNothing yet.\n")
    assert not block_inputs.write_inputs(config)
    assert config.tasks_path.read_text() == "# Tasks\n\nNothing yet.\n"
