"""Pipeline models.

Paths. Every path an agent sees or returns is *workspace-relative*: the repo's
key, a slash, then the path inside that repo (`DolceEngine/DolceChannex/urls.py`).
The run's snapshot directory holds one sub-directory per repo key, so the same
string is a path the agents' `Read` tool can open and a path `gates.py` can
split back into (repo, path) to re-read with `git show <sha>:<path>`.

Ids. A gated citation gets the id `c<N>` and a gated fact `f<N>`, assigned by
code in the order they pass; hop k of path `p<n>` is `p<n>h<k>`. The write
agents refer to code only by those ids.
"""

from __future__ import annotations

from typing import Literal

# The explore answers and units live in code_facts, shared with review and buildme.
from code_facts.models import (  # noqa: F401
    Citation,
    Dropped,
    ExploreOutput,
    Fact,
    FilePurpose,
    Term,
    Unit,
)
from code_facts.paths import PathsResult
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Stage names in run order. `WalkState.stage` holds the last one completed.
STAGES: tuple[str, ...] = (
    "target",
    "repos",
    "symbols",
    "references",
    "boundary",
    "partition",
    "explore",
    "survey",
    "docs",
    "trace",
    "write",
    "render",
    "done",
)

Method = Literal["ast", "ast-grep", "lsp", "sql-identifier"]


class _Model(BaseModel):
    # Models answer in JSON; an unknown key is ignored rather than failing the parse.
    model_config = ConfigDict(extra="ignore")


class GateResult(BaseModel):
    passed: bool
    reason: str = ""


# --- repos and target ----------------------------------------------------------


class RepoInfo(BaseModel):
    key: str  # unique short name; the first path component agents see
    path: str  # absolute path of the repo root
    head_sha: str
    remote_url: str | None = None


class ChangeInfo(BaseModel):
    """What a change target is (`--scope pr`): the PR or range, both commits and what changed between them."""

    kind: Literal["pr", "range"]
    ref: str  # "417" for a PR, "<base>..<head>" as given for a range
    pr: int | None = None
    title: str = ""  # the PR's, as gh returned it: untrusted text
    body: str = ""  # the PR's, as gh returned it: untrusted text
    base_ref: str = ""
    head_ref: str = ""
    base_sha: str  # the merge-base: the commit the change is read against
    base_key: str  # the repo key under which the base commit is read (`<key>@base`)
    commits: list[list[str]] = Field(
        default_factory=list
    )  # [sha, subject], oldest first
    files: list[dict] = Field(default_factory=list)  # git_targets.ChangedFile.to_dict()

    def added(self) -> dict[str, set[int]]:
        """Head lines each file adds, keyed by its repo-relative path at head."""
        return {
            f["path"]: {n for a, b in f["added"] for n in range(a, b + 1)}
            for f in self.files
            if f["status"] != "D"
        }


class TargetInfo(BaseModel):
    scope: Literal["module", "change"] = "module"
    change: ChangeInfo | None = None
    repo_key: str
    repo_path: str
    subpath: str  # repo-relative directory of the module ("" = whole repo)
    head_sha: str
    name: str  # base name for output files
    package: str  # dotted Python package of the subpath ("DolceChannex")
    files: list[str] = Field(default_factory=list)  # repo-relative, the files explained
    file_lines: dict[str, int] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)  # repo-relative path -> why
    migrations: int = 0
    latest_migration: str = ""
    languages: dict[str, int] = Field(default_factory=dict)  # extension -> file count

    def ws(self, path: str) -> str:
        """Workspace-relative form of a repo-relative path in the target repo."""
        return f"{self.repo_key}/{path}"


# --- symbols and references -----------------------------------------------------


class SymbolEntry(_Model):
    """Something the target defines (`direction="defines"`) or reaches out to."""

    kind: str
    name: str
    path: str  # workspace-relative
    line: int = Field(ge=1)
    quote: str
    direction: Literal["defines", "calls"] = "defines"
    detail: dict[str, str] = Field(default_factory=dict)


class Reference(_Model):
    """A place outside the target that refers to it."""

    repo: str
    kind: str
    symbol: str
    path: str  # workspace-relative
    line: int = Field(ge=1)
    quote: str
    method: Method


# --- agents' answers ---------------------------------------------------------------


class DocClaim(_Model):
    claim: str
    url: str
    doc_quote: str


class EndpointMapping(_Model):
    api_path: str
    url: str
    doc_quote: str = ""


class Disagreement(_Model):
    summary: str
    url: str
    doc_quote: str
    citation: Citation


class DocsOutput(_Model):
    claims: list[DocClaim] = Field(default_factory=list)
    mappings: list[EndpointMapping] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)


class Paragraph(_Model):
    text: str


class SectionOutput(_Model):
    paragraphs: list[Paragraph] = Field(default_factory=list)
    glossary: list[Term] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


# The human-mode explainer's structured sections. Every text field may cite `[[cN]]`; a cite field
# holds one id. Code checks each item (gates.py) and draws the flow's Mermaid from `nodes` and `edges`.


def _lower(value):
    return value.strip().lower() if isinstance(value, str) else value


class SummaryOutput(_Model):
    tagline: str = ""  # one sentence: what the code is for
    paragraphs: list[Paragraph] = Field(default_factory=list)


class Example(_Model):
    kind: Literal["command", "call", "file"] = "command"
    text: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        return _lower(v)


class UseCase(_Model):
    title: str
    who: str = ""
    when: str = ""
    example: Example
    what_happens: str = ""
    result: str = ""
    cites: list[str] = Field(default_factory=list)


class UseCasesOutput(_Model):
    cases: list[UseCase] = Field(default_factory=list)


class FlowNode(_Model):
    id: str
    label: str
    cite: str = ""


class FlowEdge(_Model):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    source: str = Field(alias="from")
    to: str
    label: str = ""


class FlowStep(_Model):
    title: str
    summary: str
    details_paragraphs: list[str] = Field(default_factory=list)
    snippet_cites: list[str] = Field(default_factory=list)


class FlowOutput(_Model):
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)
    steps: list[FlowStep] = Field(default_factory=list)


class Payload(_Model):
    format: Literal["json", "yaml", "markdown", "text"] = "text"
    text: str = ""

    @field_validator("format", mode="before")
    @classmethod
    def _format(cls, v):
        return _lower(v)


class OutputItem(_Model):
    name: str
    what: str
    written_by_cite: str = ""
    example: Payload | None = None


class OutputsOutput(_Model):
    items: list[OutputItem] = Field(default_factory=list)


class ConfigRow(_Model):
    name: str
    effect: str
    default: str = ""
    cite: str = ""


class ConfigOutput(_Model):
    rows: list[ConfigRow] = Field(default_factory=list)


# `--scope codebase` adds four tables. Each row cites one id; its gate is in gates.py.


class CommandRow(_Model):
    situation: str
    command: str = ""
    why: str = ""
    cite: str = ""


class CommandsOutput(_Model):
    rows: list[CommandRow] = Field(default_factory=list)


class LoopRow(_Model):
    what: str
    cap: str = ""
    ends_well: str = ""
    ends_badly: str = ""
    cite: str = ""


class LoopsOutput(_Model):
    rows: list[LoopRow] = Field(default_factory=list)


class ExitRow(_Model):
    signal: str
    meaning: str = ""
    action: str = ""
    cite: str = ""


class ExitsOutput(_Model):
    rows: list[ExitRow] = Field(default_factory=list)


class ResumeRow(_Model):
    stop: str
    on_disk: str = ""
    rerun: str = ""
    cite: str = ""


class ResumeOutput(_Model):
    rows: list[ResumeRow] = Field(default_factory=list)


# --- stage records in the state ------------------------------------------------------


class Section(BaseModel):
    key: str
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    cut: list[dict] = Field(default_factory=list)  # [{text, reasons}]
    glossary: list[Term] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    data: dict = Field(
        default_factory=dict
    )  # a structured section's gated answer (human mode)


class WalkState(BaseModel):
    target: TargetInfo
    options: dict = Field(default_factory=dict)
    stage: str = "target"
    repos: dict[str, RepoInfo] = Field(default_factory=dict)
    searched_repos: list[str] = Field(default_factory=list)
    symbols: list[SymbolEntry] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    boundary_path: str = ""
    units: list[Unit] = Field(default_factory=list)
    explore_answers: dict[str, dict] = Field(
        default_factory=dict
    )  # task key -> ExploreOutput dump | {"error": ...}
    facts: list[Fact] = Field(default_factory=list)  # gated, with ids
    file_purposes: dict[str, str] = Field(
        default_factory=dict
    )  # ws path -> purpose (gated)
    interfaces: list[str] = Field(default_factory=list)  # fact ids
    gotchas: list[str] = Field(default_factory=list)  # fact ids
    terms: list[Term] = Field(default_factory=list)
    uncovered_files: list[str] = Field(default_factory=list)
    uncovered_entries: list[str] = Field(default_factory=list)
    docs: dict = Field(default_factory=dict)
    docs_answers: dict[str, dict] = Field(default_factory=dict)
    paths: PathsResult | None = None  # the trace stage's map_paths result
    paths_error: str | None = None  # "<ExceptionType>: <message>" when it failed
    sections: dict[str, Section] = Field(default_factory=dict)
    write_answers: dict[str, dict] = Field(default_factory=dict)
    dropped: list[Dropped] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    lsp: dict = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)

    def past(self, stage: str) -> bool:
        return STAGES.index(self.stage) >= STAGES.index(stage)

    def citation_index(self) -> dict[str, Citation]:
        """Every citation id the writers may use: `cN` from the facts, `p{n}h{k}` for path n's hop k."""
        out: dict[str, Citation] = {}
        for fact in self.facts:
            for c in fact.citations:
                if c.id:
                    out[c.id] = c
        for path in self.paths.paths if self.paths else []:
            for k, hop in enumerate(path.hops, start=1):
                hid = hop_id(path.id, k)
                out[hid] = Citation(
                    path=self.target.ws(hop.path),
                    line_start=hop.line_start,
                    line_end=hop.line_end,
                    quote=hop.quote,
                    id=hid,
                )
        return out


def hop_id(path_id: str, k: int) -> str:
    """The id of path `path_id`'s hop k (1-based): `p2` hop 3 is `p2h3`."""
    return f"{path_id}h{k}"
