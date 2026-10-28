"""Notes for later blocks (`carry_block_notes`): the slot parser, the saved
notes, the implementer prompt and the context section later blocks get."""

from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.gates import parse_handoff_note, parse_implementation_note
from build_pipeline.llm_steps.tdd_steps import run_implementer
from build_pipeline.models import AgentResult, BlockInfo, HandoffNote
from build_pipeline.prompts.implementation import NOTES_FOR_LATER_BLOCKS_REQUEST
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    HANDOFF_NOTE_MAX_CHARS,
    HANDOFF_SECTION_MAX_CHARS,
    _record_handoff_note,
    assemble_context,
)

HEADING = "## Notes from earlier blocks"

REPORT = """Implemented the parser.

NOTES_FOR_LATER_BLOCKS:
- Money is integer cents; use money.to_cents, which rounds half-even.
- Repo.get raises repo.NotFound, not KeyError.

STATUS: DONE
TESTS_RUN: pytest -q
GREEN: 3 passed
FILES: app.py
SURPRISES: none
SPEC_IMPACT: none
"""


def _config(tmp_path, **kw) -> BuildConfig:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    change = project / "specs" / "changes" / "test"
    change.mkdir(parents=True, exist_ok=True)
    (change / "implementation-plan.md").write_text("# Design")
    config = BuildConfig(
        project_dir=project,
        change_name="test",
        tier="advanced",
        test_command="pytest -xvs",
        config_dir=tmp_path / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
        **kw,
    )
    config.specs_dir = project / "specs"
    return config


def _blocks() -> list[BlockInfo]:
    first = BlockInfo(number=1, name="Money", description="cents")
    first.handoff_notes = [
        HandoffNote(role="implementer", round=0, text="use to_cents")
    ]
    second = BlockInfo(number=2, name="Repo", description="repo")
    second.handoff_notes = [
        HandoffNote(role="implementer", round=0, text="Repo.get raises NotFound"),
        HandoffNote(role="review-fix", round=1, text="ids are strings"),
    ]
    third = BlockInfo(number=3, name="Invoice", description="invoice")
    third.handoff_notes = [HandoffNote(role="implementer", round=0, text="later")]
    return [first, second, third]


class TestParseHandoffNote:
    def test_reads_the_section_up_to_the_next_slot(self):
        assert parse_handoff_note(REPORT) == (
            "- Money is integer cents; use money.to_cents, which rounds half-even.\n"
            "- Repo.get raises repo.NotFound, not KeyError."
        )

    def test_none_and_a_missing_slot_give_none(self):
        assert parse_handoff_note("NOTES_FOR_LATER_BLOCKS: none\nSTATUS: DONE") is None
        assert parse_handoff_note("STATUS: DONE") is None
        assert parse_handoff_note("") is None

    def test_an_echoed_placeholder_is_not_a_note(self):
        assert (
            parse_handoff_note("NOTES_FOR_LATER_BLOCKS: <what a later block needs>")
            is None
        )

    def test_the_last_section_wins(self):
        text = "NOTES_FOR_LATER_BLOCKS: first\nSTATUS: DONE\nNOTES_FOR_LATER_BLOCKS: second\n"
        assert parse_handoff_note(text) == "second"

    def test_surprises_stop_at_the_notes_slot(self):
        text = "SURPRISES: the clock is UTC\nNOTES_FOR_LATER_BLOCKS: use utcnow\n"
        note = parse_implementation_note(
            text, block_number=1, block_name="b", role="implementer"
        )
        assert note.surprises == "the clock is UTC"


class TestHandoffNoteState:
    def test_round_trips_through_the_checkpoint(self, tmp_path):
        path = tmp_path / "state.json"
        state = BuildState(
            change_name="c", tier="advanced", tdd=TDDState(blocks=_blocks())
        )
        state.checkpoint(path)
        loaded = BuildState.load(path)
        assert loaded.tdd.blocks[1].handoff_notes == _blocks()[1].handoff_notes

    def test_a_checkpoint_without_the_field_loads_with_no_notes(self, tmp_path):
        path = tmp_path / "state.json"
        state = BuildState(
            change_name="c", tier="advanced", tdd=TDDState(blocks=_blocks())
        )
        state.checkpoint(path)
        import json

        data = json.loads(path.read_text())
        for block in data["tdd"]["blocks"]:
            del block["handoff_notes"]
        path.write_text(json.dumps(data))
        loaded = BuildState.load(path)
        assert [b.handoff_notes for b in loaded.tdd.blocks] == [[], [], []]


class TestRecordHandoffNote:
    def _state(self, tmp_path, block):
        return BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )

    def test_flag_off_records_nothing(self, tmp_path):
        config = _config(tmp_path)
        block = BlockInfo(number=1, name="b", description="d")
        _record_handoff_note(
            block, config, self._state(tmp_path, block), "implementer", 0, REPORT
        )
        assert block.handoff_notes == []

    def test_records_and_checkpoints(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        block = BlockInfo(number=1, name="b", description="d")
        state = self._state(tmp_path, block)
        _record_handoff_note(block, config, state, "implementer", 0, REPORT)
        assert [(n.role, n.round) for n in block.handoff_notes] == [("implementer", 0)]
        saved = BuildState.load(config.state_file_path())
        assert saved.tdd.blocks[0].handoff_notes == block.handoff_notes

    def test_same_role_and_round_replaces(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        block = BlockInfo(number=1, name="b", description="d")
        state = self._state(tmp_path, block)
        _record_handoff_note(
            block, config, state, "implementer", 0, "NOTES_FOR_LATER_BLOCKS: a"
        )
        _record_handoff_note(
            block, config, state, "review-fix", 1, "NOTES_FOR_LATER_BLOCKS: b"
        )
        _record_handoff_note(
            block, config, state, "implementer", 0, "NOTES_FOR_LATER_BLOCKS: c"
        )
        assert [(n.role, n.round, n.text) for n in block.handoff_notes] == [
            ("implementer", 0, "c"),
            ("review-fix", 1, "b"),
        ]

    def test_no_section_records_nothing(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        block = BlockInfo(number=1, name="b", description="d")
        _record_handoff_note(
            block,
            config,
            self._state(tmp_path, block),
            "implementer",
            0,
            "STATUS: DONE",
        )
        assert block.handoff_notes == []

    def test_a_long_note_keeps_its_end(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        block = BlockInfo(number=1, name="b", description="d")
        text = "x" * HANDOFF_NOTE_MAX_CHARS + "END"
        _record_handoff_note(
            block,
            config,
            self._state(tmp_path, block),
            "implementer",
            0,
            f"NOTES_FOR_LATER_BLOCKS: {text}",
        )
        kept = block.handoff_notes[0].text
        assert len(kept) == HANDOFF_NOTE_MAX_CHARS
        assert kept.endswith("END")


class TestContextSection:
    ROLES = ("test_writer", "implementer", "refactorer")

    def test_flag_off_adds_nothing_for_any_role(self, tmp_path):
        config = _config(tmp_path)
        blocks = _blocks()
        for role in self.ROLES:
            assert HEADING not in assemble_context(
                blocks[2], config, role, blocks=blocks
            )

    def test_flag_off_matches_a_build_with_no_notes(self, tmp_path):
        config = _config(tmp_path)
        noted = _blocks()
        bare = _blocks()
        for b in bare:
            b.handoff_notes = []
        for role in self.ROLES:
            assert assemble_context(
                noted[2], config, role, blocks=noted
            ) == assemble_context(bare[2], config, role, blocks=bare)

    def test_only_the_implementer_role_gets_earlier_notes(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        blocks = _blocks()
        implementer = assemble_context(blocks[2], config, "implementer", blocks=blocks)
        assert HEADING in implementer
        assert "use to_cents" in implementer
        assert "Repo.get raises NotFound" in implementer
        assert "ids are strings" in implementer
        assert "later" not in implementer.split(HEADING, 1)[1]
        for role in ("test_writer", "refactorer"):
            assert HEADING not in assemble_context(
                blocks[2], config, role, blocks=blocks
            )

    def test_the_first_block_gets_no_section(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        blocks = _blocks()
        assert HEADING not in assemble_context(
            blocks[0], config, "implementer", blocks=blocks
        )

    def test_the_oldest_notes_are_dropped_first(self, tmp_path):
        config = _config(tmp_path, carry_block_notes=True)
        blocks = _blocks()
        blocks[0].handoff_notes[0].text = "OLD " + "a" * (
            HANDOFF_SECTION_MAX_CHARS // 2
        )
        blocks[1].handoff_notes = [
            HandoffNote(
                role="implementer",
                round=0,
                text="NEW " + "b" * (HANDOFF_SECTION_MAX_CHARS // 2 + 100),
            )
        ]
        section = assemble_context(
            blocks[2], config, "implementer", blocks=blocks
        ).split(HEADING, 1)[1]
        assert "NEW " in section
        assert "OLD " not in section
        assert "Block 1" in section  # the line saying which blocks' notes were left out


class TestImplementerPrompt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("style", ["clean", "minimum"])
    async def test_the_request_is_added_only_when_asked(self, style):
        prompts = {}
        for carry in (False, True):
            with patch(
                "build_pipeline.llm_steps.tdd_steps.agent_call",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True),
            ) as call:
                await run_implementer(
                    "b",
                    "d",
                    "t",
                    "ctx",
                    "pytest",
                    prompt_style=style,
                    carry_notes=carry,
                )
            prompts[carry] = call.await_args.args[0]
        assert NOTES_FOR_LATER_BLOCKS_REQUEST not in prompts[False]
        assert prompts[True] == prompts[False] + NOTES_FOR_LATER_BLOCKS_REQUEST
