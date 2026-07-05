"""Manual single-agent change application.

Iterates through pending blocks/tasks and implements them one at a time.
Supports pause/resume via state checkpointing.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from .change_manager import ChangeManager
from .config import BuildConfig
from .models import BlockInfo, BlockStatus, BuildPhase
from .state import BuildState, TDDState

log = logging.getLogger(__name__)


async def run_apply(config: BuildConfig, args) -> int:
    """Main entry point for manual change application."""
    cm = ChangeManager(config.specs_dir)

    # Auto-select change if not specified
    if not config.change_name:
        changes = cm.list_changes()
        if len(changes) == 0:
            print("ERROR:No active changes found", file=sys.stderr, flush=True)
            return 2
        if len(changes) == 1:
            config.change_name = changes[0]["name"]
            log.info("Auto-selected change: %s", config.change_name)
        else:
            names = [c["name"] for c in changes]
            print(f"ERROR:Multiple changes found, specify --change: {names}", file=sys.stderr, flush=True)
            return 2

    change_dir = config.change_dir
    if not change_dir.exists():
        print(f"ERROR:Change not found: {change_dir}", file=sys.stderr, flush=True)
        return 2

    # Check artifacts are complete
    status = cm.artifact_status(change_dir)
    from .models import ArtifactStatus
    if status.get("tasks") != ArtifactStatus.DONE:
        print("ERROR:tasks.md not found — run spec generation first", file=sys.stderr, flush=True)
        return 2

    # Load or create state
    state_path = config.state_file_path()
    if state_path.exists():
        state = BuildState.load(state_path)
    else:
        tasks_content = config.tasks_path.read_text()
        blocks = parse_blocks_for_apply(tasks_content)
        state = BuildState(
            change_name=config.change_name,
            mode="apply",
            tier=config.tier,
            state_file=str(state_path),
            tdd=TDDState(blocks=blocks),
        )
        state.checkpoint(state_path)

    # Load context for apply agent
    context_parts = []
    for artifact in ["proposal.md", "design.md", "tasks.md"]:
        p = change_dir / artifact
        if p.exists():
            context_parts.append(f"# {artifact}\n\n{p.read_text()}")
    # Load requirements
    for req_file in sorted(change_dir.glob("requirements/*.md")):
        context_parts.append(f"# {req_file.relative_to(change_dir)}\n\n{req_file.read_text()}")
    if config.research_path.exists():
        context_parts.append(f"# research.md\n\n{config.research_path.read_text()}")

    context = "\n\n---\n\n".join(context_parts)

    # Iterate through pending blocks
    if not state.tdd:
        print("ERROR:No blocks found in state", file=sys.stderr, flush=True)
        return 1

    start_block = getattr(args, "block", None) or state.tdd.current_block
    for i in range(start_block, len(state.tdd.blocks)):
        block = state.tdd.blocks[i]
        if block.status == BlockStatus.DONE:
            continue

        state.tdd.current_block = i
        log.info("Applying block %d/%d: %s", i + 1, len(state.tdd.blocks), block.name)
        print(f"BLOCK:{i+1}/{len(state.tdd.blocks)}:{block.name}:STARTED", flush=True)

        # The actual implementation happens via agent_call in the SKILL.md wrapper
        # or directly here if running autonomously
        from .sdk import agent_call
        from .prompts.implementation import APPLY_PROMPT

        prompt = APPLY_PROMPT.format(
            block_name=block.name,
            block_description=block.description,
            block_number=block.number,
            context=context,
        )

        result = await agent_call(
            prompt=prompt,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            model=config.model,
            max_turns=50,
            cwd=str(config.project_dir),
        )

        if result.success:
            state.mark_block_status(i, BlockStatus.DONE, state_path)
            print(f"BLOCK:{i+1}/{len(state.tdd.blocks)}:{block.name}:COMPLETE", flush=True)
        else:
            log.error("Block %d failed: %s", i + 1, result.error)
            state.mark_block_status(i, BlockStatus.FAILED, state_path)
            return 1

    state.cleanup(state_path)
    print("RESULT:SUCCESS", flush=True)
    return 0


def parse_blocks_for_apply(tasks_content: str) -> list[BlockInfo]:
    """Parse tasks.md into blocks for the apply module."""
    blocks: list[BlockInfo] = []
    block_pattern = re.compile(r"^##\s+(\d+)\.\s+(.+?)(?:\s+\[DONE\])?\s*$", re.MULTILINE)
    matches = list(block_pattern.finditer(tasks_content))

    for i, match in enumerate(matches):
        number = int(match.group(1))
        name = match.group(2).strip()
        # Extract description (text between this header and next header or end)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tasks_content)
        body = tasks_content[start:end].strip()

        # Check if block is already done
        is_done = "[DONE]" in match.group(0)

        # Extract Satisfies line
        satisfies: list[str] = []
        sat_match = re.search(r"Satisfies:\s*(.+?)(?:\n\n|\Z)", body, re.DOTALL)
        if sat_match:
            satisfies = [s.strip() for s in sat_match.group(1).replace("\n", " ").split(",")]

        # Get description (first paragraph before Satisfies or checkboxes)
        desc_lines = []
        for line in body.split("\n"):
            if line.startswith("- [") or line.startswith("Satisfies:"):
                break
            desc_lines.append(line)
        description = "\n".join(desc_lines).strip()

        blocks.append(BlockInfo(
            number=number,
            name=name,
            description=description,
            satisfies=satisfies,
            status=BlockStatus.DONE if is_done else BlockStatus.PENDING,
        ))

    return blocks
