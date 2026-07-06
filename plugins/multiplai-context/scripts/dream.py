# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core[sdk] @ git+https://github.com/spikelab/multiplai-core@v0.6.0"]
# ///
"""Dream consolidation script for multiplai plugin.

Default mode (no flags): generates a human-readable change proposal and writes it
to .multiplai/dreams/ for review. Run /multiplai-context:dream-remember to apply.

--auto: fully autonomous — applies changes directly to memory files without review.
--check: report pending learnings count and exit.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Dream runs over a large backlog (40+ learnings, 20+ memory files) regularly
# exceed model_client's 600s default per-call ceiling and time out (observed
# repeatedly through 2026-06-26), forcing a manual env override each run. dream
# is a separate, long-lived process from the per-prompt hooks, so a generous
# 30-min default is safe here without loosening the global 600s default that
# keeps interactive callers (context_manager, session_start) snappy. setdefault
# preserves an explicit override. Must run before model_client is imported —
# the timeout is read into a module constant at import time.
os.environ.setdefault("MULTIPLAI_SDK_CALL_TIMEOUT_S", "1800")

from multiplai_core.paths import get_paths
from multiplai_core.model_client import create_client
from multiplai_core.config import load_yaml, save_yaml
from multiplai_core.log_utils import setup_logging
from generators.config import load_catalog_config
from generators.dispatcher import generate_catalogs

logger = setup_logging("dream")


# ---------------------------------------------------------------------------
# Learnings I/O
# ---------------------------------------------------------------------------

def _read_all_learnings(learnings_dir: Path) -> tuple[str, list[Path]]:
    """Read all pending learnings files. Returns (combined_text, source_files).

    Each content line is prefixed with its 1-indexed line number (matching what an editor
    shows for that file) so the model can cite `filename:line` provenance accurately rather
    than guessing — line numbers it can't see are line numbers it would fabricate.
    """
    if not learnings_dir.exists():
        return "", []
    files = sorted(learnings_dir.glob("*.md"))
    if not files:
        return "", []
    parts = []
    for f in files:
        raw = f.read_text()
        if not raw.strip():
            continue
        numbered = "\n".join(
            f"{i}: {line}" for i, line in enumerate(raw.splitlines(), start=1)
        )
        parts.append(f"### File: {f.name}\n\n{numbered}")
    combined = "\n\n---\n\n".join(parts)
    return combined, files


def _proposal_output_path(dreams_dir: Path, today: str) -> Path:
    """Return a non-colliding path for today's proposal.

    A same-day dream run (scheduled, or kicked off in parallel) must never
    silently overwrite a proposal that may be mid-review in dream-remember —
    that's silent data loss and forces a full re-generation (observed
    2026-06-21). If the base name is free, use it; otherwise append an
    incrementing counter so the prior proposal survives untouched.
    dream-remember globs `processed-learnings-*.md` and takes the most recent
    by mtime, so the versioned name is still discovered first.
    """
    base = dreams_dir / f"processed-learnings-{today}.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = dreams_dir / f"processed-learnings-{today}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


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


def _load_memory_catalog(catalogs_dir: Path) -> dict[str, dict]:
    """Return {filename: {summary, intent_domains}} from the memory catalog.

    The catalog (built by the router) carries each memory file's domain — its
    summary and intent_domains. Routing by that domain is far more reliable than
    guessing from section-header names, which makes broadly-named files (e.g.
    ai-agent-patterns.md) act as catch-alls. Returns {} if the catalog is absent
    or unreadable — the proposal then falls back to headers-only routing.
    """
    import json

    catalog_file = catalogs_dir / "memory.json"
    if not catalog_file.exists():
        return {}
    try:
        data = json.loads(catalog_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for entry in data.get("entries", []):
        src = entry.get("source")
        if src:
            out[src] = {
                "summary": entry.get("summary", ""),
                "intent_domains": entry.get("intent_domains", []),
            }
    return out


# ---------------------------------------------------------------------------
# Report mode (default)
# ---------------------------------------------------------------------------

_PROPOSAL_SYSTEM = """\
You are a memory consolidation analyst for a personal Claude Code memory system.

## The one thing to understand

Every learning has exactly ONE of three dispositions. Choosing correctly IS the job:

- DIARY (already recorded elsewhere) — WHAT HAPPENED: facts, events, decisions, fixes, in
  chronological order. Never duplicate it → these get FILTERED OUT.
- MEMORY (what you mostly write to) — GENERALIZED, REUSABLE KNOWLEDGE: guidance that changes
  how a FUTURE, DIFFERENT task is done.
- ACTION ITEM — a concrete change the TOOLCHAIN ITSELF should make to its own code, config,
  or structure (the memory/dream/plugin system, file layout, scripts). This is engineering
  work, NOT knowledge to remember. It goes in its own section and does NOT become memory.

Your job is NOT to log this session. It is to DISTILL the pending learnings: most are diary
(drop), some are reusable knowledge (memory), a few are change-requests to the system (action
items). A learning that only says "X happened" / "we decided Y" / "fixed Z" is diary — drop
it unless it contains a general lesson you can lift out.

The MEMORY-vs-ACTION-ITEM cut: if a learning says the system *should be changed* ("split these
files", "delete this orphan", "this script should also check X"), the change is an ACTION ITEM.
Then ask whether it ALSO carries a general principle that outlives the change — one that would
guide a DIFFERENT future situation after this task is done and forgotten:
- NO — pure cleanup with no transferable rule (e.g. "delete this stale orphan file", "remove
  this dead catalog reference") -> Action Item only, no memory.
- YES — e.g. "before making ANY repo public, scrub+rotate secrets and strip employer content"
  (useful for the next repo), or "split memory files by retrieval domain, not topic affinity"
  (a design heuristic for the next file) -> BOTH: the principle as a memory entry AND the
  concrete change as an action item.
Memory is for knowledge that informs work, not a backlog of refactors — but a durable principle
earns its memory place even when it also spawns a task.

## Generalization transform (apply to every candidate)

Strip the point-in-time scaffolding, keep the transferable rule:

- DROP: commit hashes / SHAs; "committed as ...", "fixed in ...", "decided on <date>";
  finished-task residue ("update file X", "rename Y now"); one-off absolute paths;
  specific project / repo / file names UNLESS the lesson is genuinely scoped to that
  project and useless elsewhere.
- KEEP, phrased as conditional guidance: "When <situation>, do <action>, because
  <outcome>." Prefer this shape over a narrated fact.

## Litmus gate (decide keep vs filter)

For each candidate ask, in order:
1. "Does this ask the toolchain to change its own code/config/structure?" -> ACTION ITEM.
2. "Facing a DIFFERENT but similar situation in the future, does this change what I'd do?"
   - YES, and it reads as transferable guidance -> KEEP (generalized) as memory.
   - It only records that something happened / was decided / was fixed -> FILTER OUT.
   - A true general lesson wrapped in specifics -> KEEP THE LESSON, DROP THE SPECIFICS.

## Examples

RAW: "npm install -g @anthropic-ai/claude-code is deprecated. Use
curl -fsSL https://claude.ai/install.sh | bash. Update multiplai-container Dockerfile."
KEEP: "Claude Code is no longer installed via npm; official method is
`curl -fsSL https://claude.ai/install.sh | bash`."
(Dropped "update the Dockerfile" — a one-time task, now done.)

RAW: "pluginConfigs key must be plugin@marketplace compound form; wrong key silently
falls back to the home-directory defaults with no error. Sideloaded plugins ignore
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

RAW: "Memory files covering multiple domains (career facts + career strategy) degrade
routing precision — split memory files by retrieval domain, not topic affinity."
ACTION ITEM only: "Delete the stale `.multiplai/memory/memory-catalog.json` orphan (the live
catalog is `.multiplai/data/catalogs/memory.json`)."
(Pure cleanup — no rule that outlives the deletion, so NO memory entry.)

RAW: "Mixed-domain memory files (career facts + career strategy) degrade routing precision —
split memory files by retrieval domain, not topic affinity."
BOTH — ACTION ITEM: "Split career-history vs career-strategy by retrieval domain." PLUS MEMORY
(design heuristic, guides the next file too): "Split memory files by retrieval domain, not
topic affinity — mixed-domain files degrade routing precision."

RAW: "Decision: scrub gho_ token from kit history + rotate before going public; remove
scalestack skill (employer content)."
BOTH — ACTION ITEMS: "Scrub gho_ token from kit history and rotate it"; "Remove scalestack
skill from the public kit". PLUS MEMORY (general principle, outlives these one-time tasks):
"Before making any repo public, scrub secrets from git history AND rotate them, and strip
employer-specific content."

## Output format

```
# Processed Learnings — {date}

**Sources:** {N} files, ~{M} entries
Generated by Dream. Review with `/multiplai-context:dream-remember` to apply.

---

## Updates for `{filename}`

### {N}. {short_title}
**Section:** {existing section or "New section"}
**Change:** add / update / replace
> Exact text to insert (generalized, concise, ideally "When X, do Y")

**Source:** {learnings_file}:{line-number(s)}

---

## Action Items ({N} items)

Changes the toolchain itself should make — NOT memory. Approved ones get written to PLANS/.

### A{N}. {short imperative title}
**What:** concrete change to make (file/script/config + the change), one or two lines.
**Why:** the problem it fixes.
**Source:** {learnings_file}:{line-number(s)}

---

## Filtered Out ({N} items)

- "{short description}": {reason} (diary/event-only / already applied / too specific /
  task residue / superseded)
```

Title markers (prefix the {short_title}, none in the normal case):
- **[RULE-PROPOSAL]** — a change to CLAUDE.md behavioral rules; requires individual approval.
- **[warning low confidence]** — an item you are including despite weak/unverified support.

## Routing — pick the target file by DOMAIN, not by header keyword

Each candidate file is shown with PURPOSE and OWNS DOMAINS. Route each entry to the file whose
domain actually owns the learning's SUBJECT, then pick a section within it. The headers only
choose the section — never the file.
- Match on subject domain: general/language-agnostic software & data-engineering patterns
  (data transformation & validation, migration/cutover correctness, defensive scripting, testing
  strategy, debugging methodology) → the dev-patterns file; GCP/Docker/Terraform/deploy/IAM/
  networking → the infra file; git → the git file; Python-language gotchas → the Python file;
  tooling/workflow preferences → the technical-preferences file.
- `ai-agent-patterns.md` is NOT a catch-all. Put something there ONLY if its subject is
  genuinely LLM inference, agent design, the Claude Agent SDK, RAG, or memory/retrieval-system
  design — not just because an agent happened to run the script. "A migration is run by an
  agent" does NOT make it an agent pattern — it's a data/dev pattern → the dev-patterns file.
- The dev-patterns vs infra cut: an abstract engineering principle that would hold on any
  platform (e.g. "data converters must hard-fail on zero transformations", "supplement parity
  hashes with absolute assertions") → dev-patterns. A platform-specific fact (a GCP/Docker/
  Terraform behavior, a specific service's quirk) → infra.
- If no file's domain fits, say so (propose a new file or filter) — do not force-fit into the
  nearest broadly-named file.

## Rules

- Group updates by target memory file. Do NOT print per-file learning counts, "seen Nx"
  repetition notes, or trust levels — they cost tokens and serve no reader.
- Each entry ends with a **Source:** line for provenance: the learnings filename and the
  line number(s) it was distilled from, so the origin is traceable on re-processing. The
  pending learnings are shown with `N: ` line-number prefixes — cite those exact numbers.
  Format `filename:line` or `filename:start-end`; if an entry merges several learnings, cite
  each separated by `; ` (e.g. `2026-06-15.md:42; 2026-06-16.md:10-12`). Cite only numbers
  you actually see — never invent a line number.
- Deduplicate: if the same lesson appears multiple times, merge into one entry. (Don't
  annotate the count.)
- Resolve contradictions: keep the most recent / most reliable version; note what was superseded.
- Most learnings are verified — do NOT label them. Filter out genuinely junk low-trust
  single-occurrence items. If you DO include a weakly-supported item, prefix its title with
  **[warning low confidence]** instead of dropping it.
- Filter out: diary/event-only entries, finished-task residue, already-applied facts,
  one-time fixes with no general pattern, entries with no clear target file.
- Route to Action Items any learning that calls for the toolchain to change its own
  code/config/file-structure (A{N} numbering, **Source:** line, same provenance rules). Do NOT
  mirror it as a memory entry UNLESS it also carries a general principle that outlives the
  change (one that would guide a different future situation) — then keep BOTH: the principle as
  memory, the concrete change as the action item. If the principle is just the action restated,
  action only.
- Omit the Action Items section entirely if there are none.
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

    # Each file's catalog domain (summary + intent_domains) drives routing; the
    # headers only pick the section WITHIN the chosen file.
    catalog = _load_memory_catalog(get_paths().catalogs_dir())

    blocks = []
    for name, content in memory_contents.items():
        meta = catalog.get(name, {})
        lines = [f"### {name}"]
        if meta.get("summary"):
            lines.append(f"PURPOSE: {meta['summary']}")
        if meta.get("intent_domains"):
            lines.append("OWNS DOMAINS: " + "; ".join(meta["intent_domains"]))
        lines.append(f"SECTIONS:\n{_extract_headers(content)}")
        blocks.append("\n".join(lines))
    memory_context = "\n\n".join(blocks)

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
    return await _critique_proposal(client, response.content)


# Second pass — bounded surgical critic. The drafting analyst reliably shifts aggregate
# behavior but still leaves point-in-time residue on individual high-trust KEEP entries
# (commit SHAs, "Decision (date):", "update X accordingly", one-off paths). This pass
# operates only on the already-drafted proposal (not the raw backlog), so it's cheap, and
# it enforces the strip the few-shot examples can't guarantee.
_CRITIC_SYSTEM = """\
You are a strict editor doing a SECOND PASS over an already-drafted memory proposal. The
analyst that drafted it generalizes most things well but still (a) leaves point-in-time
residue on some KEEP entries and (b) keeps whole past-event records because they embed a
useful fragment. Your job is to enforce both fixes — be decisive.

## 1. Strip residue (every '### N.' entry)

Surgically remove any residual:
- commit hashes / SHAs ("committed as abc1234", "fixed in def5678")
- dated-decision framing ("Decision (2026-06-15):", "as of <date>", "(decided <date>)")
- finished-task imperatives ("update file X", "remove Y now", "... accordingly")
- one-off absolute paths and over-scoped project / repo / file names, UNLESS the lesson is
  genuinely scoped to that project and useless elsewhere
Keep the transferable rule, phrased as guidance.

## 2. Demote past-event records (be bold)

An entry that is fundamentally a record of a PAST EVENT — a dated decision, a completed
checklist/migration/cutover, a "we did/decided/shipped X" status — is DIARY, even when it
embeds a reusable fragment. Do NOT keep the event in order to save the fragment. Instead:
- If a genuine general rule can be lifted out, REPLACE the entry's text with that rule alone
  (strip ALL event scaffolding: dates, specific repo/project names, the checklist itself,
  what was done) and keep the entry with its Source line. Example: "Decision: repos A/B/C go
  public; pre-public: scrub gho_ token, remove scalestack skill, secret scan" →
  "Before making any repo public: scrub secrets from git history AND rotate them, and strip
  employer-specific content."
- Otherwise MOVE the whole entry to 'Filtered Out' with a one-line reason.
When unsure whether something is a durable rule or a one-time event, treat it as an EVENT:
extract any rule, filter the rest. Memory is guidance that changes future action — not a log
of what happened.

DO keep durable reference facts — how a system is configured, stable identifiers (regions,
instance/secret names, ports), standing preferences. Those are not events; they inform future
work. The target is records of things that HAPPENED or were DECIDED at a point in time.

## 3. Reroute mis-filed action items

If a memory '### N.' entry is really a change-request to the TOOLCHAIN's own code / config /
file-structure ("split these files", "delete this orphan", "this script should also check X"),
the change belongs in '## Action Items' — MOVE it there (create the section if absent),
reformat as `### A{N}.` with **What:** / **Why:** / **Source:** lines. Leave a memory copy
behind ONLY if the entry also states a general principle that outlives the change (would guide
a different future situation); then keep the principle as the memory entry AND the concrete
change as the action item. If the principle is just the action restated, no memory copy. Do not
move general knowledge that merely mentions the system.

## 4. Fix catch-all mis-routing

`ai-agent-patterns.md` is not a catch-all. If an entry filed under it is really about general
software / data-engineering patterns (data transformation & validation, migration/cutover
correctness, defensive scripting, testing, debugging methodology), MOVE it to the dev-patterns
file. If it's about a specific platform (GCP/Docker/Terraform), git, or a Python-language gotcha,
move it to the infra / git / Python file respectively. An agent running a script does not make
the script an agent pattern. Only move on a clear subject mismatch; do not reshuffle borderline
entries.

NEVER alter the **Source:** provenance line — it cites `filename:line` for traceability and
must stay exact. Strip residue from the entry's generalized text only, never from its Source.

Do NOT: add new content, change section groupings (beyond the demotions/reroutes above),
re-judge clean entries, reorder, or touch anything already clean. PRESERVE the exact output
format and any **[RULE-PROPOSAL]** / **[warning low confidence]** markers, and the
'## Action Items' section if present. Renumber entries only if you moved one out.
Return the full cleaned proposal and nothing else.
"""


async def _critique_proposal(client, proposal: str) -> str:
    """Run the bounded surgical critic over a drafted proposal; return the cleaned version.

    Falls back to the original proposal if the critic call fails — a residue-bearing
    proposal is still useful, and report mode must not crash on the second pass.
    """
    messages = [{"role": "user", "content": f"## Drafted proposal:\n\n{proposal}"}]
    try:
        response = await client.query(system=_CRITIC_SYSTEM, messages=messages)
        cleaned = (response.content or "").strip()
        # Guard against a degenerate/empty critic response clobbering a good draft.
        if "## Updates for" in cleaned or "## Filtered Out" in cleaned:
            logger.info("Critic pass applied (%d -> %d bytes)",
                        len(proposal.encode("utf-8")), len(cleaned.encode("utf-8")))
            return cleaned
        logger.warning("Critic returned no recognizable proposal — keeping original draft")
        return proposal
    except Exception:
        logger.exception("Critic pass failed — keeping original draft")
        return proposal


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

    client = await create_client(component="dream")
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
        if l.startswith("## Updates for `") and "`" in l[16:]
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
    output_file = _proposal_output_path(dreams_dir, today)
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
        # Check for staged changes scoped to the *.md pathspec only — otherwise
        # anything the user had pre-staged elsewhere would make this look
        # "dirty" and fire a snapshot that sweeps those unrelated files in.
        diff = subprocess.run(
            ["git", "-C", str(memory_dir), "diff", "--cached", "--quiet", "--", "*.md"],
            timeout=10, capture_output=True,
        )
        if diff.returncode == 0:
            logger.info("Memory auto-commit skipped — no changes to record")
            return False

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Restrict the snapshot to the *.md pathspec: passing the pathspec
        # records just those paths and leaves any other staged files untouched.
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit",
             "-m", f"dream: consolidate {today}", "--", "*.md"],
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
        if line.startswith("## Updates for `") and "`" in line[16:]:
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


def _is_safe_memory_update(current: str, new: str) -> bool:
    """Guard against an applier response that would destroy a memory file.

    A consolidation rewrites the file in full, so a truncated response or a
    refusal preamble ("I'm sorry, I can't…") would silently overwrite good
    content with garbage. Consolidation only adds or lightly edits, so the
    result should never collapse to a fraction of the original. Reject an
    empty/whitespace result or one that lost more than 40% of the original
    length — the caller then keeps the existing file and the learnings so the
    run can be retried.
    """
    stripped = new.strip()
    if not stripped:
        return False
    # A memory file is prose+markdown; a bare apology/refusal is not a valid
    # rewrite. Cheap heuristic on the opening.
    head = stripped[:80].lower()
    if head.startswith(("i'm sorry", "i am sorry", "i cannot", "i can't", "sorry,")):
        return False
    if len(current.strip()) >= 200 and len(stripped) < 0.6 * len(current.strip()):
        return False
    return True


async def _apply_proposal_to_file(client, memory_file: Path, proposal_section: str) -> str | None:
    """Apply one file's slice of the proposal. Returns validated new content,
    or None if the call failed or the result looks unsafe to write."""
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
    except Exception:
        logger.exception("Failed to apply updates to %s", memory_file.name)
        return None

    if not _is_safe_memory_update(current_content, response.content):
        logger.error(
            "Rejected unsafe applier output for %s (%d chars -> %d); keeping original",
            memory_file.name, len(current_content), len(response.content.strip()),
        )
        return None
    return response.content


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
            client = await create_client(component="dream")
            logger.info("Dream (auto) using %s", type(client).__name__)

            # Stage 1 — generalize. IDENTICAL to report mode: same _PROPOSAL_SYSTEM,
            # same call. All the diary-vs-memory judgment happens here. The only
            # difference from report mode is that we apply the result instead of
            # waiting for /dream-remember approval.
            memory_contents = _read_memory_files(memory_dir)
            proposal = await _generate_proposal(
                client, all_learnings, memory_contents, source_files
            )

            # Audit trail: write the same proposal artifact report mode would,
            # without clobbering a prior same-day artifact.
            dreams_dir = paths.dreams_dir()
            dreams_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _proposal_output_path(dreams_dir, today).write_text(proposal)

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
            failed_count = 0
            for (filename, memory_file, _), updated_content in zip(targets, results):
                if updated_content:
                    memory_file.write_text(updated_content)
                    updated_count += 1
                    logger.info("Applied updates to %s", filename)
                else:
                    failed_count += 1

            # Only delete the raw learnings once every target that was supposed
            # to change actually did. If any apply failed (API outage, unsafe
            # output), keep the backlog so the next run can retry — deleting it
            # here would lose the source insights with nothing persisted.
            if failed_count == 0:
                for f in source_files:
                    f.unlink(missing_ok=True)
                    logger.info("Deleted processed learnings: %s", f.name)
            else:
                logger.warning(
                    "Kept %d learnings file(s): %d/%d targets failed to apply — "
                    "will retry next run",
                    len(source_files), failed_count, len(targets),
                )

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
