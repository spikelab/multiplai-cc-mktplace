"""Tests for the apply module — block parsing and change selection."""

import pytest
from pathlib import Path

from build_pipeline.apply import parse_blocks_for_apply
from build_pipeline.models import BlockStatus


SAMPLE_TASKS_COARSE = """\
## 1. Shared Infrastructure

Package scaffold, configuration, state management, Pydantic models.
Foundation for all other modules.

Satisfies: shared-infrastructure/state-persistence, shared-infrastructure/sdk-wrapper

## 2. Change Manager

Manages spec-driven change directories.

Satisfies: change-management/create-change, change-management/artifact-status

## 3. Model Adaptation [DONE]

Tier detection from environment.

Satisfies: model-adaptation/tier-detection
"""

SAMPLE_TASKS_CHECKBOXES = """\
## 1. Setup

- [ ] 1.1 Create project skeleton
- [ ] 1.2 Add dependencies

## 2. Core [DONE]

- [x] 2.1 Implement parser
- [x] 2.2 Add validation

## 3. Tests

- [ ] 3.1 Unit tests
- [ ] 3.2 Integration tests
"""


class TestParseBlocksCoarse:
    def test_parses_block_count(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_COARSE)
        assert len(blocks) == 3

    def test_block_names(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_COARSE)
        assert blocks[0].name == "Shared Infrastructure"
        assert blocks[1].name == "Change Manager"
        assert blocks[2].name == "Model Adaptation"

    def test_block_numbers(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_COARSE)
        assert blocks[0].number == 1
        assert blocks[1].number == 2
        assert blocks[2].number == 3

    def test_done_block_detected(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_COARSE)
        assert blocks[2].status == BlockStatus.DONE
        assert blocks[0].status == BlockStatus.PENDING

    def test_satisfies_parsed(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_COARSE)
        assert "shared-infrastructure/state-persistence" in blocks[0].satisfies

    def test_description_parsed(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_COARSE)
        assert "Package scaffold" in blocks[0].description


class TestParseBlocksCheckboxes:
    def test_parses_checkbox_format(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_CHECKBOXES)
        assert len(blocks) == 3

    def test_done_block_from_header(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_CHECKBOXES)
        assert blocks[1].status == BlockStatus.DONE
        assert blocks[1].name == "Core"

    def test_pending_blocks(self):
        blocks = parse_blocks_for_apply(SAMPLE_TASKS_CHECKBOXES)
        assert blocks[0].status == BlockStatus.PENDING
        assert blocks[2].status == BlockStatus.PENDING
