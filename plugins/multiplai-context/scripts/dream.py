"""Dream consolidation script for multiplai plugin.

Default mode (no flags): generates a human-readable change proposal and writes it
to .multiplai/dreams/ for review. Run /multiplai-context:dream-remember to apply.

--auto: fully autonomous — applies changes directly to memory files without review.
--check: report pending learnings count and exit.
"""

import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.config import load_yaml, save_yaml
from lib.log_utils import setup_logging
from generators.config import load_catalog_config
from generators.dispatcher import generate_catalogs

logger = setup_logging("dream")


# ---------------------------------------------------------------------------
# Learnings I/O
# ---------------------------------------------------------------------------

def _read_all_learnings(learnings_dir: Path) -> tuple[str, list[Path]]:
    """Read all pending learnings files. Returns (combined_text, source_files)."""
    if not learnings_dir.exists():
        return "", []
    files = sorted(learnings_dir.glob("*.md"))
    if not files:
        return "", []
    parts = []
    for f in files:
        content = f.read_text().strip()
        if content:
            parts.append(f"### File: {f.name}\n\n{content}")
    combined = "\n\n---\n\n".join(parts)
    return combined, files


def _read_memory_files(memory_dir: Path) -> dict[str, str]:
    """Return {filename: content} for all .md files in memory_dir."""
    if not memory_dir.exists():
        return {}
    return {
        f.name: f.read_text()
        for f in sorted(memory_dir.glob("*.md"))
        if f.name != "learnings.md"
    }


def _extract_headers(content: str) -> str:
    """Return H1–H3 headers from markdown content."""
    headers = [l for l in content.split("\n") if l.startswith("#")]
    return "\n".join(headers) if headers else content[:300]


# ---------------------------------------------------------------------------
# Report mode (default)
# ---------------------------------------------------------------------------

_PROPOSAL_SYSTEM = """\
You are a memory consolidation analyst for a personal Claude Code memory system.

## The one thing to understand

There are TWO memory systems. Knowing the difference IS the job:

- The DIARY already records WHAT HAPPENED — facts, events, decisions, fixes, in
  chronological order. You never duplicate it.
- MEMORY (what you write to) holds GENERALIZED, REUSABLE KNOWLEDGE — guidance that
  changes how a FUTURE, DIFFERENT task is done.

Your job is NOT to log this session. It is to DISTILL the pending learnings into
transferable lessons that will inform future decisions and actions. A learning that
only says "X happened" or "we decided Y" or "fixed Z" is diary material — drop it,
unless it contains a general lesson you can lift out.

## Generalization transform (apply to every candidate)

Strip the point-in-time scaffolding, keep the transferable rule:

- DROP: commit hashes / SHAs; "committed as ...", "fixed in ...", "decided on <date>";
  finished-task residue ("update file X", "rename Y now"); one-off absolute paths;
  specific project / repo / file names UNLESS the lesson is genuinely scoped to that
  project and useless elsewhere.
- KEEP, phrased as conditional guidance: "When <situation>, do <action>, because
  <outcome>." Prefer this shape over a narrated fact.

## Litmus gate (decide keep vs filter)

For each candidate ask: "Facing a DIFFERENT but similar situation in the future,
does this change what I'd do?"
- YES, and it reads as transferable guidance -> KEEP (generalized).
- It only records that something happened / was decided / was fixed -> FILTER OUT.
- A true general lesson wrapped in specifics -> KEEP THE LESSON, DROP THE SPECIFICS.

## Examples

RAW: "npm install -g @anthropic-ai/claude-code is deprecated. Use
curl -fsSL https://claude.ai/install.sh | bash. Update multiplai-container Dockerfile."
KEEP: "Claude Code is no longer installed via npm; official method is
`curl -fsSL https://claude.ai/install.sh | bash`."
(Dropped "update the Dockerfile" — a one-time task, now done.)

RAW: "pluginConfigs key must be plugin@marketplace compound form; wrong key silently
falls back to ~/.multiplai defaults with no error. Sideloaded plugins ignore
pluginConfigs — use CLAUDE_PLUGIN_OPTION_* env vars. Committed as a8cbec9."
KEEP: "`pluginConfigs` keys use the compound `plugin@marketplace` form; a wrong key
fails silently (falls back to defaults, no error). Sideloaded plugins (`--plugin-dir`)
ignore `pluginConfigs` — pass options via `CLAUDE_PLUGIN_OPTION_*` env vars instead."
(Dropped the specific fallback path and the commit SHA.)

RAW: "Decision (2026-06-15): multiplai-core, mktplace, and kit all going public.
Pre-public: scrub gho_ token from history + rotate; remove scalestack skill;
secret scan. De-personalization machinery deleted; identity moves to memory."
FILTER OUT as written — it's a dated decision + checklist (diary). If a reusable rule
exists, extract ONLY that: "Before making any repo public, scrub secrets from git
history AND rotate them, and strip employer-specific content."

RAW: "multiplai-plugin git fetch not run regularly; origin/main tracking ref weeks
stale. Always fetch before checking ahead/behind or assuming sync with remote."
KEEP: "Always `git fetch` before checking ahead/behind counts or assuming sync with a
remote — local tracking refs go stale."
(Dropped the multiplai-plugin framing; the lesson is general.)

## Output format

```
# Processed Learnings — {date}

**Sources:** {N} files, ~{M} entries
Generated by Dream. Review with `/multiplai-context:dream-remember` to apply.

---

## Updates for `{filename}` ({N} learnings)

### {N}. [{trust_level}[, seen {Nx}]] {short_title}
**Section:** {existing section or "New section"}
**Change:** add / update / replace
> Exact text to insert (generalized, concise, ideally "When X, do Y")

**Source:** {filename(s)}

---

## Filtered Out ({N} items)

- "{short description}": {reason} (diary/event-only / already applied / too specific /
  task residue / low trust / superseded)
```

## Rules

- Group updates by target memory file.
- Deduplicate: if the same lesson appears multiple times, merge into one entry and note the count ("seen 3x"). Repetition elevates priority.
- Resolve contradictions: keep the most recent high-trust version; note what was superseded.
- Trust hierarchy: authoritative > verified/high > unverified/low. Don't propose low-trust single-occurrence items.
- Mark RULE-PROPOSAL items (changes to CLAUDE.md behavioral rules) with **[RULE-PROPOSAL]** in the title — these require individual approval.
- Filter out: diary/event-only entries, finished-task residue, already-applied facts, one-time fixes with no general pattern, entries with no clear target file.
- Keep proposed text concise — one-line bullets over paragraphs. Memory costs tokens.
- Never invent changes not supported by the learnings.
"""


async def _generate_proposal(
    client,
    all_learnings: str,
    memory_contents: dict[str, str],
    source_files: list[Path],
) -> str:
    """Call LLM to produce a structured change proposal from learnings."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source_names = ", ".join(f.name for f in source_files)

    memory_context = "\n\n".join(
        f"### {name} (structure):\n{_extract_headers(content)}"
        for name, content in memory_contents.items()
    )

    messages = [
        {
            "role": "user",
            "content": (
                f"Today's date: {today}\n"
                f"Source files: {source_names}\n\n"
                f"## Current memory file structure:\n\n{memory_context}\n\n"
                f"## Pending learnings:\n\n{all_learnings}"
            ),
        }
    ]

    response = await client.query(system=_PROPOSAL_SYSTEM, messages=messages)
    return response.content


async def dream_report() -> None:
    """Generate a change proposal and write it to .multiplai/dreams/ for review."""
    paths = get_paths()
    learnings_dir = paths.learnings_dir
    memory_dir = paths.memory_dir()
    dreams_dir = paths.dreams_dir()

    all_learnings, source_files = _read_all_learnings(learnings_dir)
    if not all_learnings:
        print("No pending learnings — nothing to propose.")
        return

    learnings_bytes = len(all_learnings.encode("utf-8"))
    learnings_lines = len(all_learnings.splitlines())
    logger.info(
        "Source learnings: %d files (%d lines, %d bytes): %s",
        len(source_files), learnings_lines, learnings_bytes,
        ", ".join(f.name for f in source_files),
    )

    client = await create_client()
    logger.info("Dream using %s", type(client).__name__)

    memory_contents = _read_memory_files(memory_dir)
    logger.info(
        "Loaded %d memory files for context: %s",
        len(memory_contents), ", ".join(sorted(memory_contents)),
    )

    proposal = await _generate_proposal(client, all_learnings, memory_contents, source_files)

    # Quick structural digest so the log answers "what did the model decide?"
    # without having to open the proposal file.
    proposal_lines = proposal.splitlines()
    target_files = [
        l.split("`")[1] for l in proposal_lines
        if l.startswith("## Updates for `") and "`" in l[15:]
    ]
    has_filtered = any(l.startswith("## Filtered Out") for l in proposal_lines)
    logger.info(
        "Proposal generated: %d bytes, %d target files (%s), filtered-out section=%s",
        len(proposal.encode("utf-8")),
        len(target_files),
        ", ".join(target_files) if target_files else "none",
        has_filtered,
    )
    if not has_filtered or not target_files:
        logger.warning(
            "Proposal looks incomplete — missing target updates or Filtered Out section"
        )

    dreams_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = dreams_dir / f"processed-learnings-{today}.md"
    output_file.write_text(proposal)
    logger.info("Proposal written to %s", output_file)

    print(f"Proposal written to {output_file}")
    print(f"Sources: {len(source_files)} files, ~{learnings_lines} lines")
    print(f"Targets: {len(target_files)} files ({', '.join(target_files) or 'none'})")
    print("Review with: /multiplai-context:dream-remember")


# ---------------------------------------------------------------------------
# Auto mode (--auto)
# ---------------------------------------------------------------------------

def _memory_dir_is_git_repo(memory_dir: Path) -> bool:
    if not memory_dir.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(memory_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _commit_memory_changes(memory_dir: Path) -> bool:
    """Stage and commit memory changes. Returns True on commit, False otherwise."""
    if not _memory_dir_is_git_repo(memory_dir):
        logger.warning(
            "Memory auto-commit skipped — %s is not a git repository.", memory_dir
        )
        return False

    try:
        # Stage only memory markdown files. Staging the whole tree would
        # sweep in unrelated dirty work when memory_dir lives inside a
        # larger repo (dotfiles/workspace) and record it in the snapshot.
        subprocess.run(
            ["git", "-C", str(memory_dir), "add", "--", "*.md"],
            check=True, timeout=15, capture_output=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(memory_dir), "diff", "--cached", "--quiet"],
            timeout=10, capture_output=True,
        )
        if diff.returncode == 0:
            logger.info("Memory auto-commit skipped — no changes to commit")
            return False

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit", "-m", f"dream: consolidate {today}"],
            check=True, timeout=30, capture_output=True,
        )
        logger.info("Memory auto-committed in %s", memory_dir)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(
            "Memory auto-commit failed: git %s exited %d (stderr: %s)",
            e.cmd, e.returncode, e.stderr.decode("utf-8", "replace") if e.stderr else "",
        )
        return False
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        logger.warning("Memory auto-commit failed: %s", e)
        return False


# Mechanical applier — executes an already-generalized proposal. It does NOT decide
# what is or isn't a memory; all that judgment lives in _PROPOSAL_SYSTEM. This keeps
# --auto using the exact same generalization brain as report mode (just no human gate).
_APPLIER_SYSTEM = (
    "You apply an approved set of memory updates to a memory file. Make ONLY the "
    "changes the proposal specifies (add / update / replace at the named sections). "
    "Match the file's existing style and formatting exactly. Do not generalize, "
    "re-judge, invent, or add anything not in the proposal. If a 'Last Updated' line "
    "exists, refresh its date. Return the full updated file content and nothing else."
)


def _split_proposal_by_file(proposal: str) -> dict[str, str]:
    """Split a proposal into {filename: section_text} by '## Updates for `file`' headers.

    'Filtered Out' and any preamble are not target sections and are dropped — only the
    per-file update blocks become applier instructions.
    """
    sections: dict[str, str] = {}
    current_file: str | None = None
    buf: list[str] = []

    def _flush():
        if current_file is not None:
            sections[current_file] = "\n".join(buf).strip()

    for line in proposal.splitlines():
        if line.startswith("## Updates for `") and "`" in line[15:]:
            _flush()
            current_file = line.split("`")[1]
            buf = [line]
        elif line.startswith("## "):
            # any other H2 (e.g. "## Filtered Out") ends the current file section
            _flush()
            current_file = None
            buf = []
        elif current_file is not None:
            buf.append(line)
    _flush()
    return sections


async def _apply_proposal_to_file(client, memory_file: Path, proposal_section: str) -> str | None:
    """Apply one file's slice of the proposal to that memory file. Returns new content."""
    if not memory_file.exists():
        return None

    current_content = memory_file.read_text()
    messages = [
        {
            "role": "user",
            "content": (
                f"## Approved updates for {memory_file.name}:\n{proposal_section}\n\n"
                f"## Current file content:\n{current_content}"
            ),
        }
    ]

    try:
        response = await client.query(system=_APPLIER_SYSTEM, messages=messages)
        return response.content
    except Exception:
        logger.exception("Failed to apply updates to %s", memory_file.name)
        return None


async def dream_auto() -> None:
    """Apply learnings directly to memory files without review (autonomous mode)."""
    paths = get_paths()
    learnings_dir = paths.learnings_dir
    memory_dir = paths.memory_dir()
    dream_state_file = paths.dream_state_file()

    all_learnings, source_files = _read_all_learnings(learnings_dir)
    if not all_learnings:
        logger.info("No pending learnings to consolidate")
    else:
        try:
            client = await create_client()
            logger.info("Dream (auto) using %s", type(client).__name__)

            # Stage 1 — generalize. IDENTICAL to report mode: same _PROPOSAL_SYSTEM,
            # same call. All the diary-vs-memory judgment happens here. The only
            # difference from report mode is that we apply the result instead of
            # waiting for /dream-remember approval.
            memory_contents = _read_memory_files(memory_dir)
            proposal = await _generate_proposal(
                client, all_learnings, memory_contents, source_files
            )

            # Audit trail: write the same proposal artifact report mode would.
            dreams_dir = paths.dreams_dir()
            dreams_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            (dreams_dir / f"processed-learnings-{today}.md").write_text(proposal)

            # Stage 2 — mechanically apply each file's slice of the proposal.
            # Files are independent, so apply them concurrently.
            per_file = _split_proposal_by_file(proposal)
            logger.info("Dream (auto) proposal targets %d files: %s",
                        len(per_file), ", ".join(sorted(per_file)) or "none")

            targets = []
            for filename, section in per_file.items():
                memory_file = memory_dir / filename
                if not memory_file.exists():
                    logger.warning("Proposal targets unknown file %s — skipped", filename)
                    continue
                targets.append((filename, memory_file, section))

            results = await asyncio.gather(*(
                _apply_proposal_to_file(client, mf, section)
                for _, mf, section in targets
            ))

            updated_count = 0
            for (filename, memory_file, _), updated_content in zip(targets, results):
                if updated_content:
                    memory_file.write_text(updated_content)
                    updated_count += 1
                    logger.info("Applied updates to %s", filename)

            # Delete processed learnings files
            for f in source_files:
                f.unlink(missing_ok=True)
                logger.info("Deleted processed learnings: %s", f.name)

            state = load_yaml(dream_state_file)
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            state["learnings_processed"] = sum(1 for _ in all_learnings.splitlines())
            state["files_updated"] = updated_count
            save_yaml(dream_state_file, state)

            logger.info("Dream (auto) complete: %d files updated", updated_count)
        except Exception:
            logger.exception("Dream (auto) consolidation failed")
            raise

    try:
        config = load_catalog_config()
        catalog_results = await generate_catalogs(config=config)
        logger.info("Catalog regeneration complete: %d generators ran", len(catalog_results))
    except Exception:
        logger.exception("Catalog generation failed (dream still complete)")

    _commit_memory_changes(memory_dir)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Dream — learnings consolidation")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report pending learnings count and exit",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Apply changes directly to memory files without review (autonomous mode)",
    )
    # --run kept as deprecated alias for --auto
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="Record that a consolidation was applied (updates dream_state). "
             "Used by /dream-remember after the human-in-the-loop apply so the "
             "dream gate stops nudging.",
    )
    parser.add_argument("--files-updated", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--learnings-processed", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.stamp:
        paths = get_paths()
        dream_state_file = paths.dream_state_file()
        state = load_yaml(dream_state_file) or {}
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["files_updated"] = args.files_updated
        state["learnings_processed"] = args.learnings_processed
        save_yaml(dream_state_file, state)
        print(f"Stamped dream_state: last_run={state['last_run']}")
        return

    if args.check:
        paths = get_paths()
        _, files = _read_all_learnings(paths.learnings_dir)
        if not files:
            print("No pending learnings")
            return
        total_lines = sum(
            len(f.read_text().splitlines()) for f in files
        )
        print(f"Pending learnings: {len(files)} files, ~{total_lines} lines")
        return

    if args.auto or args.run:
        asyncio.run(dream_auto())
    else:
        asyncio.run(dream_report())


if __name__ == "__main__":
    main()
