"""write: one agent per section, no tools. Input is gated facts, path hops and doc claims only.

Agents refer to code by citation id (`[[c42]]`, `[[p2h3]]` for a path hop); `render.py` turns ids into
`path:line` links and puts in snippets taken from the commit, so a model never
types a code snippet. `write_gate` checks every paragraph. A failing paragraph
gets one rewrite with its failure list; if it still fails it is cut from the
body and listed in the appendix "Cut by the checks", with the reason.

In human mode the walkthrough is an explainer. Its first sections are
structured answers (`STRUCTURED`): a summary, use cases with examples, the
flow as boxes and arrows plus a walk through it, what the code creates with
example payloads, and its configuration. Each item is checked by a gate in
`gates.py`; a section with a failure is asked again once, and what still
fails is cut and listed. Code draws the flow's Mermaid from the gated boxes
and arrows. The Reference subsections are the prose sections from before.

A codebase run (`--scope codebase`) adds three tables (`CODEBASE_TABLES`): loops
and caps, exit codes and markers, and state and resume.
Their material leads with the survey stage's facts.

The glossary is not written by an agent: it is the gated terms from explore,
each checked again by `glossary_gate`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from multiplai_core.untrusted import fence, markdown_notice

from .. import sdk
from ..gates import (
    Corpus,
    cite_id,
    config_gate,
    core_part_gate,
    exit_gate,
    explainer_text_gate,
    flow_gate,
    glossary_gate,
    loop_gate,
    output_gate,
    resume_gate,
    usecase_gate,
    write_gate,
)
from ..models import (
    ConfigOutput,
    CoreOutput,
    ExitsOutput,
    Fact,
    FlowOutput,
    LoopsOutput,
    OutputsOutput,
    PathsResult,
    ResumeOutput,
    Section,
    SectionOutput,
    SummaryOutput,
    UseCasesOutput,
    WalkState,
    hop_id,
)
from ..prompts import EXPLAINER_RULES, WRITE_RULES, write_section, write_structured
from . import RunContext, bounded

log = logging.getLogger(__name__)

MATERIAL_MAX_CHARS = 60_000

# (key, title) of the human-mode H2s, in document order. `write_specs` asks for the structured ones and
# gotchas; Reference and How this was made hold the sections below, as H3s. The `core` section's title is
# `state.core["title"]` (`human_sections`); it is left out when the core stage chose nothing.
HUMAN_SECTIONS = [
    ("summary", "What it is"),
    ("usecases", "Use cases"),
    ("flow", "How it works"),
    ("core", ""),
    ("outputs", "What it creates"),
    ("config", "Configuration"),
    ("gotchas", "Gotchas"),
    ("reference", "Reference"),
    ("made", "How this was made"),
]
REFERENCE_SUBSECTIONS = [
    "Paths",
    "Module tour",
    "Who calls it and what it calls",
    "Data stores",
    "Vendor docs",
    "Glossary",
]
MADE_SUBSECTIONS = ["Commits read", "Coverage", "Cut by the checks"]
# A change's H2s (`--scope pr`). "What it does not touch" is written by code, from the diff and the paths.
CHANGE_SECTIONS = [
    ("summary", "What changed and why"),
    ("flow", "The path through the change"),
    ("affected", "Who is affected"),
    ("untouched", "What it does not touch"),
    ("reference", "Reference"),
    ("made", "How this was made"),
]
# A codebase run's H2s (`--scope codebase`): a module's, with the three tables of CODEBASE_TABLES.
CODEBASE_SECTIONS = [
    ("summary", "What it is"),
    ("usecases", "Use cases"),
    ("flow", "How it works"),
    ("core", ""),
    ("loops", "Loops and caps"),
    ("exits", "Exit codes and markers"),
    ("resume", "State and resume"),
    ("outputs", "What it creates"),
    ("config", "Configuration"),
    ("gotchas", "Gotchas"),
    ("reference", "Reference"),
    ("made", "How this was made"),
]
# Each table's rows: (field naming a row, the gate, the text fields kept, the field a gate may drop alone).
CODEBASE_TABLES = {
    "loops": ("what", loop_gate, ("what", "cap", "ends_well", "ends_badly"), "cap"),
    "exits": ("signal", exit_gate, ("signal", "meaning", "action"), ""),
    "resume": ("stop", resume_gate, ("stop", "on_disk", "rerun"), ""),
}
STRUCTURED = {
    "summary": SummaryOutput,
    "usecases": UseCasesOutput,
    "flow": FlowOutput,
    "outputs": OutputsOutput,
    "config": ConfigOutput,
    "core": CoreOutput,
    "loops": LoopsOutput,
    "exits": ExitsOutput,
    "resume": ResumeOutput,
}
MIN_USE_CASES = 2
AGENT_SECTIONS = [
    ("files", "Files"),
    ("architecture", "Architecture"),
    ("patterns", "Patterns & Conventions"),
    ("interfaces", "Key Interfaces"),
    ("gotchas", "Gotchas"),
    ("coverage", "Coverage"),
]


def human_sections(options: dict, core: dict | None = None) -> list[tuple[str, str]]:
    """The H2s of a human-mode walkthrough: a module's, a codebase's, or a change's (`--scope pr`). *core*
    is `state.core`: its title names the core section, which is left out when there is none."""
    sections = {"pr": CHANGE_SECTIONS, "codebase": CODEBASE_SECTIONS}.get(
        options.get("scope"), HUMAN_SECTIONS
    )
    title = (core or {}).get("title", "")
    return [
        (k, title if k == "core" else t) for k, t in sections if k != "core" or title
    ]


def required_titles(
    mode: str, scope: str = "module", core_title: str = ""
) -> list[str]:
    if mode == "agent":
        return [t for _, t in AGENT_SECTIONS]
    return [t for _, t in human_sections({"scope": scope}, {"title": core_title})]


@dataclass
class Spec:
    key: str
    title: str
    brief: str
    material: str
    kind: str = "prose"  # "prose", or a key of STRUCTURED
    rules: str = WRITE_RULES  # the prose rule block; the structured prompts always use EXPLAINER_RULES
    ask: str = ""  # the prompts.STRUCTURED_ASKS key, when it is not `kind` (a change's sections)


def _fact_line(f: Fact) -> str:
    c = f.citations[0]
    more = "".join(f" [{x.id}]" for x in f.citations[1:])
    return f"- [{c.id}]{more} {f.claim}  ({c.path}:{c.line_start})"


def _material(lines: list[str]) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) > MATERIAL_MAX_CHARS:
            out.append(f"- ({len(lines) - len(out)} more items left out for length)")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) or "- (nothing passed the checks for this section)"


def _facts(state: WalkState, prefixes: tuple[str, ...]) -> list[Fact]:
    return [f for f in state.facts if f.source.startswith(prefixes)]


def hop_prompt_block(result: PathsResult, repo_key: str) -> str:
    """The "Path hops" material: every hop id with its called symbol, `path:start-end` and quote.

    The hop lines quote the repository and carry the tracing agent's notes, so they go to the writer inside
    one `<untrusted-content>` fence (docs/untrusted-content.md), after the notice that says it is data.
    """
    if not result.paths:
        return "Path hops: none."
    hops = []
    for path in result.paths:
        for k, hop in enumerate(path.hops, start=1):
            note = f" — {hop.note}" if hop.note else ""
            hops.append(
                f"- [{hop_id(path.id, k)}] `{hop.symbol_called}` {repo_key}/{hop.path}:{hop.line_start}-"
                f"{hop.line_end} {json.dumps(' '.join(hop.quote.split())[:200], ensure_ascii=False)}{note}"
            )
    return "\n".join(
        [
            "Path hops (checked by a program; cite a hop as [[<id>]], e.g. "
            f"[[{hop_id(result.paths[0].id, 1)}]]):",
            markdown_notice(
                "code quoted from the repository and the tracing agent's notes on it",
                "Repository text",
            ),
            *fence("\n".join(hops), f"{repo_key} path hops"),
        ]
    )


def write_specs(state: WalkState) -> list[Spec]:
    if state.options.get("mode") == "agent":
        all_facts = [_fact_line(f) for f in state.facts if "/file" not in f.source]
        by_id = {f.id: f for f in state.facts}
        return [
            Spec(
                "architecture",
                "Architecture",
                "How the files connect: data flow, import chains, which file owns "
                "which responsibility. Concrete and terse; bullet points.",
                _material(all_facts),
            ),
            Spec(
                "patterns",
                "Patterns & Conventions",
                "Naming, error handling, testing and other conventions the "
                "facts show. Bullet points.",
                _material(all_facts),
            ),
            Spec(
                "interfaces",
                "Key Interfaces",
                "The functions and classes other code calls: what they accept and "
                "return. Bullet points.",
                _material(
                    [_fact_line(by_id[i]) for i in state.interfaces if i in by_id]
                ),
            ),
            Spec(
                "gotchas",
                "Gotchas",
                "What would trip an agent changing this code. Bullet points.",
                _material([_fact_line(by_id[i]) for i in state.gotchas if i in by_id]),
            ),
        ]
    if state.target.scope == "change":
        return _change_specs(state) + _reference_specs(state)
    if state.options.get("scope") == "codebase":
        return _codebase_specs(state) + _reference_specs(state)
    return _explainer_specs(state) + _reference_specs(state)


def change_material(state: WalkState) -> list[str]:
    """The PR's title and description and the commit subjects, inside one untrusted-content fence."""
    ch = state.target.change
    text = []
    if ch.kind == "pr":
        text.append(f"PR #{ch.pr}: {ch.title}")
        if ch.body:
            text += ["", ch.body[:8000]]
    text += ["", "Commits, oldest first:"] + [
        f"- {sha[:8]} {subject}" for sha, subject in ch.commits[:200]
    ]
    return [
        markdown_notice(
            "the PR description and commit subjects, written by the change's author",
            "Change description",
        ),
        *fence("\n".join(text).strip(), f"{state.target.package} description"),
    ]


def _changed_files_block(state: WalkState) -> list[str]:
    ch = state.target.change
    out = ["Files the change touches (status, head lines added, base lines removed):"]
    for f in ch.files:
        added = sum(b - a + 1 for a, b in f["added"])
        removed = sum(b - a + 1 for a, b in f["removed"])
        moved = (
            f" (from {f['old_path']})" if f["old_path"] not in ("", f["path"]) else ""
        )
        out.append(f"- {f['status']} {f['path']}{moved}: +{added} -{removed}")
    return out


def _change_specs(state: WalkState) -> list[Spec]:
    name = state.target.package
    unit = [f for f in _facts(state, ("unit:",)) if "/file" not in f.source]
    main = main_first(state)
    on_main = (
        {f"{state.target.repo_key}/{h.path}" for h in main[0].hops} if main else set()
    )
    flow_facts = [f for f in unit if any(c.path in on_main for c in f.citations)] + unit
    callers = [
        f"- `{r.symbol}` is called in {r.repo}: {r.path}:{r.line}"
        for r in state.references
        if r.kind == "caller"
    ]
    by_id = {f.id: f for f in state.facts}
    return [
        Spec(
            "summary",
            "What changed and why",
            f"What `{name}` changes, and why.",
            "\n".join(change_material(state))
            + "\n\n"
            + _material(_changed_files_block(state) + _lines(unit[:150])),
            kind="summary",
            ask="change_summary",
        ),
        Spec(
            "flow",
            "The path through the change",
            f"How a run goes through the code `{name}` changes.",
            _material([_hops(state)] + _lines(flow_facts)),
            kind="flow",
            ask="change_flow",
        ),
        Spec(
            "affected",
            "Who is affected",
            "Say who and what this change affects: the code that calls what it changed, the entry points "
            "whose paths go through it, and the people or systems behind them. Short paragraphs or a short "
            "bullet list. Name only callers and entry points in the material.",
            _material(
                _entries_block(state)
                + callers
                + _lines(_facts(state, ("boundary:inbound",)))
                + [_fact_line(by_id[i]) for i in state.interfaces if i in by_id]
            ),
            rules=EXPLAINER_RULES,
        ),
    ]


DOC_NAMES = ("SKILL.md", "README.md")
CLI_QUOTE = re.compile(
    r"add_argument|add_parser|add_subparsers|@click\.|@app\.command|typer\.|argparse|sys\.argv"
)
WRITES = re.compile(
    r"\b(writ|sav|creat|checkpoint|state|logs?\b|output|emit|record|append|commit|report)",
    re.IGNORECASE,
)
CONFIG = re.compile(
    r"\b(config|setting|flag|env\b|environment|option|default|yaml|toml)|--[a-z]",
    re.IGNORECASE,
)


def _lines(facts: list[Fact]) -> list[str]:
    out, seen = [], set()
    for f in facts:
        if f.id not in seen:
            seen.add(f.id)
            out.append(_fact_line(f))
    return out


def _doc_facts(state: WalkState) -> list[Fact]:
    return [
        f
        for f in state.facts
        if any(c.path.rsplit("/", 1)[-1] in DOC_NAMES for c in f.citations)
    ]


def _cli_facts(state: WalkState) -> list[Fact]:
    return [
        f for f in state.facts if any(CLI_QUOTE.search(c.quote) for c in f.citations)
    ]


def main_first(state: WalkState) -> list:
    """The traced paths, the module's main entry point first: most hops, then most facts citing the
    files its hops pass through; map_paths order breaks a tie."""
    paths = list(state.paths.paths) if state.paths else []
    cited: dict[str, int] = {}
    for f in state.facts:
        for c in f.citations:
            cited[c.path] = cited.get(c.path, 0) + 1
    key = state.target.repo_key

    def weight(p) -> int:
        return sum(cited.get(f"{key}/{path}", 0) for path in {h.path for h in p.hops})

    order = {p.id: i for i, p in enumerate(paths)}
    return sorted(paths, key=lambda p: (-len(p.hops), -weight(p), order[p.id]))


def _entries_block(state: WalkState) -> list[str]:
    out = []
    for p in main_first(state):
        e = p.entry
        out.append(
            f"- entry point `{e.name}` ({e.kind}, path {p.id}), invoked by {e.invoked_by}; {len(p.hops)} "
            f"hop(s), cited as [[{hop_id(p.id, 1)}]] onwards"
        )
    return out


def _hops(state: WalkState) -> str:
    if not state.paths:
        return "Path hops: none."
    return hop_prompt_block(
        state.paths.model_copy(update={"paths": main_first(state)}),
        state.target.repo_key,
    )


def _explainer_specs(state: WalkState) -> list[Spec]:
    name = state.target.package
    unit = [f for f in _facts(state, ("unit:",)) if "/file" not in f.source]
    docs, cli = _doc_facts(state), _cli_facts(state)
    purposes = [f"- {p}: {s}" for p, s in sorted(state.file_purposes.items())]
    bnd = {k: len(v) for k, v in _boundary_counts(state).items()}
    main = main_first(state)
    on_main = (
        {f"{state.target.repo_key}/{h.path}" for h in main[0].hops} if main else set()
    )
    flow_facts = (
        docs + [f for f in unit if any(c.path in on_main for c in f.citations)] + unit
    )
    writes = [f for f in state.facts if WRITES.search(f.claim)] + _facts(
        state, ("boundary:data", "boundary:config")
    )
    config = (
        _facts(state, ("boundary:config",))
        + cli
        + [
            f
            for f in state.facts
            if CONFIG.search(f.claim)
            or any(CONFIG.search(c.quote) for c in f.citations)
        ]
    )
    by_id = {f.id: f for f in state.facts}
    return [
        Spec(
            "summary",
            "What it is",
            f"What `{name}` is for, in plain words. Reference counts found by code: {bnd}.",
            _material(_lines(docs) + purposes + _lines(unit[:120])),
            kind="summary",
        ),
        Spec(
            "usecases",
            "Use cases",
            f"How people use `{name}`: the commands, calls and modes, each with an example.",
            _material(_entries_block(state) + _lines(docs + cli) + [_hops(state)]),
            kind="usecases",
        ),
        Spec(
            "flow",
            "How it works",
            f"How `{name}` works, along its main path.",
            _material([_hops(state)] + _lines(flow_facts)),
            kind="flow",
        ),
        *_core_spec(state),
        Spec(
            "outputs",
            "What it creates",
            f"What `{name}` writes: files, state, logs and other outputs.",
            _material(_lines(writes)),
            kind="outputs",
        ),
        Spec(
            "config",
            "Configuration",
            f"What changes how `{name}` behaves.",
            _material(_lines(config)),
            kind="config",
        ),
        Spec(
            "gotchas",
            "Gotchas",
            "List the gotchas a newcomer would trip on, as short paragraphs or a short "
            "bullet list. Then in `questions` give 5-10 questions to ask the module's owner that the facts raise "
            "but do not answer.",
            _material([_fact_line(by_id[i]) for i in state.gotchas if i in by_id]),
            rules=EXPLAINER_RULES,
        ),
    ]


def _core_spec(state: WalkState) -> list[Spec]:
    """The core section's spec when the core stage chose a part: the deep read's facts first, then the facts
    citing the chosen files, then the job and why."""
    core = state.core
    if not core.get("title"):
        return []
    files = set(core.get("files") or [])
    read = _facts(state, ("core:read/",))
    citing = [f for f in state.facts if any(c.path in files for c in f.citations)]
    return [
        Spec(
            "core",
            core["title"],
            f"Explain the part of `{state.target.package}` that does its main job, deeper than the stages "
            f"around it. The job: {core['job']} Why these files: {core['why']}",
            _material(_lines(read + citing)),
            kind="core",
        )
    ]


LOOPS = re.compile(
    r"retr|attempt|iteration|round|max_|MAX_|loop|limit|circuit", re.IGNORECASE
)
EXITS = re.compile(r"\bexit|sys\.exit|return code|marker|\b[A-Z_]{3,}:")
RESUME = re.compile(r"resum|checkpoint|\bstate\b|re-?run|restart", re.IGNORECASE)


def _matching(state: WalkState, pattern: re.Pattern) -> list[Fact]:
    return [
        f
        for f in state.facts
        if pattern.search(f.claim) or any(pattern.search(c.quote) for c in f.citations)
    ]


def _codebase_specs(state: WalkState) -> list[Spec]:
    """A module's specs, with the three tables in H2 order. Each table's material leads with the survey
    answer that asked for it; the lifecycle answer also feeds What it creates."""
    name = state.target.package
    survey = {q: _facts(state, (f"survey:{q}/",)) for q in ("loops", "lifecycle")}
    tables = {
        "loops": Spec(
            "loops",
            "Loops and caps",
            f"Every loop, retry and round in `{name}`, with its cap.",
            _material(_lines(survey["loops"] + _matching(state, LOOPS))),
            kind="loops",
        ),
        "exits": Spec(
            "exits",
            "Exit codes and markers",
            f"The exit codes and printed markers of `{name}`, and what each means.",
            _material(_lines(survey["loops"] + _matching(state, EXITS))),
            kind="exits",
        ),
        "resume": Spec(
            "resume",
            "State and resume",
            f"What `{name}` keeps on disk, and what a re-run keeps, redoes or refuses after each kind of stop.",
            _material(_lines(survey["lifecycle"] + _matching(state, RESUME))),
            kind="resume",
        ),
    }
    specs = {s.key: s for s in _explainer_specs(state)}
    outputs = specs["outputs"]
    specs["outputs"] = Spec(
        outputs.key,
        outputs.title,
        outputs.brief,
        _material(
            _lines(
                survey["lifecycle"]
                + [f for f in state.facts if WRITES.search(f.claim)]
                + _facts(state, ("boundary:data", "boundary:config"))
            )
        ),
        kind="outputs",
    )
    specs.update(tables)
    return [specs[k] for k, _ in CODEBASE_SECTIONS if k in specs]


def _reference_specs(state: WalkState) -> list[Spec]:
    """The Reference subsections' prose: the paths, the module tour, the boundary, data and docs."""
    specs: list[Spec] = []
    for path in state.paths.paths if state.paths else []:
        one = state.paths.model_copy(update={"paths": [path]})
        specs.append(
            Spec(
                f"path:{path.id}",
                f"{path.id}: {path.entry.name}",
                "One or two paragraphs telling this path end to end, from its entry point, citing each "
                "hop by its id. The program lists the entry, the hops and the e2e tests after your text.",
                hop_prompt_block(one, state.target.repo_key),
            )
        )
    for u in state.units:
        facts = [
            _fact_line(f)
            for f in state.facts
            if f.source.startswith(f"unit:{u.id}/")
            or f.source.startswith(f"unit:{u.id}:reask/")
        ]
        files = ", ".join(p.split("/")[-1] for p in u.files[:12]) + (
            " …" if len(u.files) > 12 else ""
        )
        specs.append(
            Spec(
                f"tour:{u.id}",
                files,
                "Explain these files for a newcomer: what each does, how they "
                "work together, and the code worth seeing (use [[snippet:cN]] for one or two key "
                "places). Several short paragraphs.",
                _material(facts),
            )
        )
    for group, title, brief in (
        (
            "inbound",
            "Who calls it",
            "Explain, repository by repository, how other code uses the module.",
        ),
        (
            "outbound",
            "What it calls",
            "Explain what the module calls out to: other apps, external APIs.",
        ),
    ):
        facts = [_fact_line(f) for f in _facts(state, (f"boundary:{group}",))]
        specs.append(
            Spec(
                f"boundary:{group}",
                title,
                brief + " The program adds full tables after your text.",
                _material(facts),
            )
        )
    facts = [_fact_line(f) for f in _facts(state, ("boundary:data", "boundary:config"))]
    specs.append(
        Spec(
            "data",
            "Data stores and config",
            "Explain the tables and cache keys the module uses, and "
            "its configuration: which setting or env var switches what (staging vs production, keys, "
            "feature flags).",
            _material(facts),
        )
    )
    d = state.docs or {}
    if d.get("claims") or d.get("disagreements"):
        lines = [
            f"- {c['claim']} (docs: {c['url']}, quote: {c['doc_quote'][:160]!r})"
            for c in d.get("claims", [])
        ]
        lines += [
            f"- DISAGREEMENT [{_dis_cid(state, x)}] {x['summary']} (docs: {x['url']}, quote: "
            f"{x['doc_quote'][:160]!r})"
            for x in d.get("disagreements", [])
        ]
        specs.append(
            Spec(
                "docs",
                "Vendor docs",
                "Explain what the vendor docs say that matters for this code, "
                "and where the code and the docs disagree. Name doc pages by their URL. The program adds "
                "the endpoint tables after your text.",
                _material(lines),
            )
        )
    return specs


def _boundary_counts(state: WalkState) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in state.references:
        out.setdefault(f"inbound {r.repo}", []).append(r)
    for e in state.symbols:
        if e.direction == "calls":
            out.setdefault(f"outbound {e.kind}", []).append(e)
    return out


def _dis_cid(state: WalkState, dis: dict) -> str:
    """Disagreement citations get ids `d1`, `d2`… in the citation index."""
    return f"d{(state.docs.get('disagreements') or []).index(dis) + 1}"


def citations_for_write(state: WalkState) -> dict:
    from ..models import Citation

    idx = state.citation_index()
    for i, dis in enumerate((state.docs or {}).get("disagreements") or []):
        c = Citation.model_validate(dis["citation"])
        idx[f"d{i + 1}"] = c.model_copy(update={"id": f"d{i + 1}"})
    return idx


def _gate_structured(
    kind: str, out, gate: dict, *, steps_follow_boxes: bool = False
) -> dict:
    """One structured answer through its gates: `data` (what passed, as stored in `Section.data`), `cut`
    ({text, reasons}) and `failures` (the reasons, for the one re-ask). *steps_follow_boxes* is
    `flow_gate`'s, for a module's or a codebase's flow."""
    cut: list[dict] = []
    if kind == "summary":
        tagline = out.tagline.strip()
        if tagline:
            r = explainer_text_gate(tagline, **gate)
            if r:
                cut.append({"text": tagline, "reasons": r})
                tagline = ""
        paragraphs = []
        for p in out.paragraphs[:2]:
            r = explainer_text_gate(p.text, **gate)
            (
                cut.append({"text": p.text, "reasons": r})
                if r
                else paragraphs.append(p.text)
            )
        data = {"tagline": tagline, "paragraphs": paragraphs}
        missing = [] if paragraphs else ["no paragraph passed the checks"]
    elif kind == "usecases":
        cases = []
        for c in out.cases:
            r = usecase_gate(c, **gate)
            if r:
                cut.append(
                    {"text": f"use case {c.title!r}: {c.example.text}", "reasons": r}
                )
                continue
            case = c.model_dump()
            case["cites"] = [cite_id(x) for x in c.cites if cite_id(x)]
            cases.append(case)
        data = {"cases": cases}
        missing = (
            []
            if len(cases) >= MIN_USE_CASES
            else [
                f"{len(cases)} use case(s) passed; at least {MIN_USE_CASES} are needed"
            ]
        )
    elif kind == "flow":
        g = flow_gate(out, **gate, steps_follow_boxes=steps_follow_boxes)
        cut = g["cut"]
        data = {"nodes": g["nodes"], "edges": g["edges"], "steps": g["steps"]}
        missing = g["graph"] + g["order"]
        if g["graph"]:
            cut.append({"text": "the flow diagram", "reasons": g["graph"]})
            data["nodes"], data["edges"] = [], []
    elif kind == "outputs":
        items = []
        for it in out.items:
            r, ex = output_gate(it, **gate)
            if r:
                cut.append({"text": f"output {it.name!r}: {it.what}", "reasons": r})
                continue
            example = (
                it.example.model_dump() if it.example is not None and not ex else None
            )
            if ex:
                cut.append(
                    {
                        "text": f"example of {it.name!r} (the item is kept without it)",
                        "reasons": ex,
                    }
                )
            items.append(
                {
                    "name": it.name.strip(),
                    "what": it.what,
                    "cite": cite_id(it.written_by_cite),
                    "example": example,
                }
            )
        data = {"items": items}
        missing = [] if items else ["no output passed the checks"]
    elif kind == "core":
        data, cut = _gate_core(out, gate)
        missing = (
            []
            if data["summary"] or data["paragraphs"] or data["parts"]
            else ["nothing in the section passed the checks"]
        )
    elif kind in CODEBASE_TABLES:
        first, check, fields, optional = CODEBASE_TABLES[kind]
        rows = []
        for row in out.rows:
            result = check(row, **gate)
            reasons, keep_optional = (
                result if isinstance(result, tuple) else (result, True)
            )
            if reasons:
                cut.append(
                    {
                        "text": f"row {getattr(row, first)!r} of {kind}",
                        "reasons": reasons,
                    }
                )
                continue
            kept = {f: getattr(row, f).strip() for f in fields}
            if optional and kept[optional] and not keep_optional:
                cut.append(
                    {
                        "text": f"{optional} of {getattr(row, first)!r}: {kept[optional]!r} (the row is "
                        "kept without it)",
                        "reasons": [
                            f"the {optional} is in no cited quote and no snapshot"
                        ],
                    }
                )
                kept[optional] = ""
            kept["cite"] = cite_id(row.cite)
            rows.append(kept)
        data = {"rows": rows}
        missing = [] if rows else [f"no row of {kind} passed the checks"]
    else:
        rows = []
        for row in out.rows:
            r, keep_default = config_gate(row, **gate)
            if r:
                cut.append(
                    {"text": f"setting {row.name!r}: {row.effect}", "reasons": r}
                )
                continue
            if row.default.strip() and not keep_default:
                cut.append(
                    {
                        "text": f"default of {row.name!r}: {row.default!r} (the row is kept without it)",
                        "reasons": ["the default is in no cited quote and no snapshot"],
                    }
                )
            rows.append(
                {
                    "name": row.name.strip().strip("`"),
                    "effect": row.effect,
                    "cite": cite_id(row.cite),
                    "default": row.default.strip().strip("`") if keep_default else "",
                }
            )
        data = {"rows": rows}
        missing = [] if rows else ["no setting passed the checks"]
    failures = missing + [f"{c['text'][:120]}: {r}" for c in cut for r in c["reasons"]]
    return {"data": data, "cut": cut, "failures": failures}


def _gate_core(out: CoreOutput, gate: dict) -> tuple[dict, list[dict]]:
    """The core section's parts that pass: its summary and paragraphs (`explainer_text_gate`), its parts
    (`core_part_gate`), and its flow (`flow_gate`, one step per box), which is dropped alone when its graph
    or its steps fail."""
    cut: list[dict] = []
    summary = out.summary.strip()
    if summary:
        r = explainer_text_gate(summary, **gate)
        if r:
            cut.append({"text": summary, "reasons": r})
            summary = ""
    paragraphs = []
    for p in out.paragraphs:
        r = explainer_text_gate(p.text, **gate)
        (cut.append({"text": p.text, "reasons": r}) if r else paragraphs.append(p.text))
    parts = []
    for part in out.parts:
        r = core_part_gate(part, **gate)
        if r:
            cut.append({"text": f"part {part.name!r}", "reasons": r})
            continue
        parts.append(
            {
                "name": part.name.strip(),
                "looks_at": part.looks_at.strip(),
                "given": part.given.strip(),
                "produces": part.produces.strip(),
                "checked_by": part.checked_by.strip(),
                "cite": cite_id(part.cite),
            }
        )
    flow = None
    if out.flow is not None and out.flow.nodes:
        g = flow_gate(out.flow, **gate, steps_follow_boxes=True)
        cut += g["cut"]
        if g["graph"] or g["order"]:
            cut.append(
                {"text": "the core flow diagram", "reasons": g["graph"] + g["order"]}
            )
        else:
            flow = {"nodes": g["nodes"], "edges": g["edges"], "steps": g["steps"]}
    data = {"summary": summary, "paragraphs": paragraphs, "parts": parts, "flow": flow}
    return data, cut


# The list each list-shaped section keeps, and the field that names an item, for merging a re-ask.
_LISTS = {
    "usecases": ("cases", "title"),
    "outputs": ("items", "name"),
    "config": ("rows", "name"),
    **{k: ("rows", first) for k, (first, *_) in CODEBASE_TABLES.items()},
}


def merge_reask(kind: str, first: dict, second: dict) -> dict:
    """The gated first answer and its re-ask, as one section.

    A list section keeps the first answer's items that passed, plus the re-ask's items that passed and
    are new by name; what the re-ask still fails is cut. The summary and the flow take the re-ask's
    answer unless it fails more checks than the first (a flow whose graph fails always loses)."""
    if kind in _LISTS:
        field, name = _LISTS[kind]
        kept = list(first["data"][field])
        names = {str(x[name]).strip().lower() for x in kept}
        kept += [
            x
            for x in second["data"][field]
            if str(x[name]).strip().lower() not in names
        ]
        data = {**first["data"], field: kept}
        return {"data": data, "cut": second["cut"], "failures": second["failures"]}

    def score(g: dict) -> tuple[int, int]:
        graph_fails = int(
            kind == "flow" and any(c["text"] == "the flow diagram" for c in g["cut"])
        )
        return graph_fails, len(g["failures"])

    return second if score(second) <= score(first) else first


async def run_write(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("write"):
        return state
    corpus = Corpus([ctx.snap_root, ctx.run_dir / "docs-cache"])
    citations = citations_for_write(state)
    labels = (
        set(ctx.repos)
        | {c.path for c in citations.values()}
        | {e.name for e in state.symbols}
    )
    gate = {"citations": citations, "corpus": corpus, "labels": labels}
    cfg = ctx.config
    specs = write_specs(state)
    name = state.target.package

    async def call(key: str, prompt: str, schema=SectionOutput):
        if key in state.write_answers:
            raw = state.write_answers[key]
            return None if "error" in raw else schema.model_validate(raw)
        try:
            out = await sdk.agent_call_structured(
                prompt,
                schema,
                allowed_tools=sdk.WRITE_TOOLS,
                model=cfg.write_model,
                effort=cfg.effort,
                max_turns=2,
                budget_label="write",
            )
            state.write_answers[key] = out.model_dump()
            return out
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("write %s failed", key, exc_info=True)
            state.write_answers[key] = {"error": str(e).splitlines()[0][:200]}
            state.errors.append(f"write {key}: {str(e).splitlines()[0][:200]}")
            return None

    async def structured(spec: Spec) -> Section:
        schema = STRUCTURED[spec.kind]
        sec = Section(key=spec.key, title=spec.title)
        out = await call(
            spec.key,
            write_structured(
                name,
                spec.ask or spec.kind,
                spec.title,
                spec.brief,
                spec.material,
                schema,
            ),
            schema,
        )
        if out is None:
            return sec
        # A change's flow follows its path, not one step per box.
        follow = spec.ask != "change_flow"
        gated = _gate_structured(spec.kind, out, gate, steps_follow_boxes=follow)
        if gated["failures"]:
            retry = await call(
                spec.key + ":rewrite",
                write_structured(
                    name,
                    spec.ask or spec.kind,
                    spec.title,
                    spec.brief,
                    spec.material,
                    schema,
                    failures=gated["failures"],
                    previous=json.dumps(
                        out.model_dump(by_alias=True), ensure_ascii=False
                    ),
                ),
                schema,
            )
            if retry is not None:
                gated = merge_reask(
                    spec.kind,
                    gated,
                    _gate_structured(spec.kind, retry, gate, steps_follow_boxes=follow),
                )
        sec.data, sec.cut = gated["data"], gated["cut"]
        if spec.kind == "summary":
            sec.paragraphs = list(gated["data"]["paragraphs"])
        return sec

    async def one(spec: Spec) -> Section:
        if spec.kind != "prose":
            return await structured(spec)
        check = explainer_text_gate if spec.rules is EXPLAINER_RULES else write_gate
        sec = Section(key=spec.key, title=spec.title)
        out = await call(
            spec.key,
            write_section(
                name,
                spec.title,
                spec.brief,
                spec.material,
                SectionOutput,
                rules=spec.rules,
            ),
        )
        if out is None:
            return sec
        texts = [p.text for p in out.paragraphs] + [
            f"QUESTION: {q}" for q in out.questions
        ]
        results = [(t, check(t, **gate)) for t in texts]
        failing = [(t, r) for t, r in results if r]
        if failing:
            retry = await call(
                spec.key + ":rewrite",
                write_section(
                    name,
                    spec.title,
                    spec.brief,
                    spec.material,
                    SectionOutput,
                    failures=[x for _, r in failing for x in r],
                    previous=[t for t, _ in failing],
                    rules=spec.rules,
                ),
            )
            if retry is not None:
                fixed = [p.text for p in retry.paragraphs] + [
                    f"QUESTION: {q}" for q in retry.questions
                ]
                good = [t for t in fixed if not check(t, **gate)]
                still = [(t, check(t, **gate)) for t in fixed]
                # The rewrite replaces the failing paragraphs; what still fails is cut.
                results = [(t, r) for t, r in results if not r] + [
                    (t, []) for t in good
                ]
                failing = [(t, r) for t, r in still if r]
        for t, r in results:
            if r:
                continue
            if t.startswith("QUESTION: "):
                sec.questions.append(t[len("QUESTION: ") :])
            else:
                sec.paragraphs.append(t)
        sec.cut = [{"text": t, "reasons": r} for t, r in failing]
        return sec

    sections = await bounded(specs, one, cfg.concurrency)
    state.sections = {s.key: s for s in sections}
    if state.options.get("mode") != "agent":
        gl = Section(key="glossary", title="Glossary")
        for t in sorted(state.terms, key=lambda t: t.term.lower()):
            g = glossary_gate(t.term, corpus)
            if g.passed:
                gl.glossary.append(t)
            else:
                gl.cut.append({"text": f"{t.term}: {t.meaning}", "reasons": [g.reason]})
        state.sections["glossary"] = gl
    ctx.counts = {
        "sections": len(state.sections),
        "paragraphs": sum(len(s.paragraphs) for s in state.sections.values()),
        "cut": sum(len(s.cut) for s in state.sections.values()),
    }
    state.stage = "write"
    return state
