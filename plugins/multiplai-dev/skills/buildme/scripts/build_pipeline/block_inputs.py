"""What each block's agents get besides the block's own text, written into
the block's section of tasks.md so a person reading the plan sees it.

`tdd_engine.assemble_context` builds each agent's prompt from the block, the
plan, the requirements, earlier blocks' signatures, the rubric, the
prototype, the memory files and the reference docs; under `tdd.context:
sliced` it cuts the plan and the requirements to what the block cites. Which
of them a block gets was visible only in the prompt. `write_inputs` lists
them at the end of every block's section, between `START` and `END`, after
the plan check passed.

The list is buildme's, rewritten whole every time: `strip` takes it out
again, and every reader that must not see it reads tasks.md through `strip`
(`tdd_engine.parse_blocks`, so the agents never get it twice, and the plan
revision's hash, so writing it changes no revision). Never raises on a
tasks.md it cannot place a list in: that block gets none.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

START = "<!-- buildme:inputs: written by buildme after the plan check; edits here are replaced -->"
END = "<!-- /buildme:inputs -->"
# A list exactly as `render` puts it in: two newlines, then the list.
_REGION_RE = re.compile(r"\n\n" + re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
_HEADER_RE = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)


def strip(text: str) -> str:
    """*text* without the input lists `write_inputs` put in it: byte for
    byte the text before they were written."""
    return _REGION_RE.sub("", text) if START in text else text


def lines_for(block, config, blocks: list) -> list[str]:
    """The bullet lines of *block*'s list, as `assemble_context` decides
    them for this config."""
    from .change_manager import requirement_sources
    from .tdd_engine import (
        _DESIGN_SECTION_RE,
        _sliced_design,
        _sliced_requirements,
        prototype_section,
    )

    sliced = config.tdd_context == "sliced"
    out: list[str] = []

    sources = requirement_sources(config.change_dir)
    if sliced and sources:
        kept = _sliced_requirements(block, sources)
        named = (
            "every requirement"
            if kept == sources
            else ", ".join(f"`{k}`" for k in kept)
        )
    else:
        named = "every requirement"
    out.append(
        f"- Requirements: {named} of this component's specs (not the test writer, which gets them as its specs)"
    )

    plan = config.design_path
    if plan.is_file():
        shown = os.path.relpath(plan, config.change_dir)
        text = plan.read_text()
        cut = _sliced_design(block, text) if sliced else text
        if cut == text:
            out.append(f"- Implementation plan: the whole of `{shown}`")
        else:
            heads = [m.group(0).strip() for m in _DESIGN_SECTION_RE.finditer(cut)]
            out.append(
                f"- Implementation plan: from `{shown}`, its opening and "
                + ", ".join(f"`{h}`" for h in heads)
            )

    earlier = [
        f"block {b.number} ({b.name}): {sig}"
        for b in blocks
        if b.number < block.number
        for sig in b.produces
    ]
    if earlier:
        out.append(
            "- Signatures from earlier blocks, to use as written: " + "; ".join(earlier)
        )

    if config.rubric_path.exists():
        out.append(
            "- `rubric.md`: the reviewer only"
            if sliced
            else "- `rubric.md`: every agent"
        )
    if config.unknowns_path.exists():
        out.append("- `unknowns.md`: the test writer")
    if prototype_section(config):
        out.append("- The prototype's files: the test writer and the implementer")
    out.append(
        "- The test writer: signatures of the existing code this block names, read when it runs"
    )
    memory = (
        config.tdd_memory_files
        if sliced
        else config.core_memory_files
        + config.stack_memory_files
        + config.additional_memory_files
    )
    present = [m for m in memory if (config.config_dir / "memory" / m).exists()]
    if present:
        out.append("- Memory files: " + ", ".join(f"`{m}`" for m in present))
    docs = [d.name for d in config.stack_reference_docs()]
    if docs:
        out.append("- Reference docs: " + ", ".join(f"`{d}`" for d in docs))
    return out


def render(text: str, config, blocks: list) -> str:
    """tasks.md *text* with one input list at the end of each block's
    section, any earlier list taken out first."""
    text = strip(text)
    by_number = {b.number: b for b in blocks}
    heads = list(_HEADER_RE.finditer(text))
    if not heads:
        return text
    parts = [text[: heads[0].start()]]
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        section = text[head.start() : end]
        block = by_number.get(int(head.group(1)))
        if block is None:
            parts.append(section)
            continue
        body = section.rstrip("\n")
        listed = "\n".join(
            [
                START,
                "**What this block's agents also get**",
                "",
                *lines_for(block, config, blocks),
                END,
            ]
        )
        parts.append(f"{body}\n\n{listed}{section[len(body) :]}")
    return "".join(parts)


def write_inputs(config) -> bool:
    """Write each block's input list into tasks.md. True when the file
    changed."""
    from .io_utils import atomic_write
    from .tdd_engine import parse_blocks

    path = Path(config.tasks_path)
    if not path.is_file():
        return False
    before = path.read_text()
    after = render(before, config, parse_blocks(path))
    if after == before:
        return False
    atomic_write(path, after)
    return True
