"""Pipeline models.

Paths. Every path an agent sees or returns is *workspace-relative*: the repo's
key, a slash, then the path inside that repo (`DolceEngine/DolceChannex/urls.py`).
The run's snapshot directory holds one sub-directory per repo key, so the same
string is a path the agents' `Read` tool can open and a path `gates.py` can
split back into (repo, path) to re-read with `git show <sha>:<path>`.

Ids. A gated citation gets the id `c<N>` and a gated fact `f<N>`, assigned by
code in the order they pass. The write agents refer to code only by those ids.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Stage names in run order. `WalkState.stage` holds the last one completed.
STAGES: tuple[str, ...] = (
    "target", "repos", "symbols", "references", "boundary", "partition",
    "explore", "docs", "trace", "write", "render", "done",
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
    key: str          # unique short name; the first path component agents see
    path: str         # absolute path of the repo root
    head_sha: str
    remote_url: str | None = None


class TargetInfo(BaseModel):
    repo_key: str
    repo_path: str
    subpath: str            # repo-relative directory of the module ("" = whole repo)
    head_sha: str
    name: str               # base name for output files
    package: str            # dotted Python package of the subpath ("DolceChannex")
    files: list[str] = Field(default_factory=list)       # repo-relative, the files explained
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
    path: str      # workspace-relative
    line: int = Field(ge=1)
    quote: str
    direction: Literal["defines", "calls"] = "defines"
    detail: dict[str, str] = Field(default_factory=dict)


class Reference(_Model):
    """A place outside the target that refers to it."""
    repo: str
    kind: str
    symbol: str
    path: str      # workspace-relative
    line: int = Field(ge=1)
    quote: str
    method: Method


# --- agents' answers ---------------------------------------------------------------


class Citation(_Model):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    id: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "Citation":
        if self.line_end < self.line_start:
            raise ValueError(f"line_end {self.line_end} is before line_start {self.line_start}")
        return self


class Fact(_Model):
    claim: str
    citations: list[Citation] = Field(default_factory=list)
    id: str = ""
    source: str = ""   # which agent produced it: "unit:3", "boundary:inbound:DolceFront:0"


class FilePurpose(_Model):
    path: str
    purpose: str
    citation: Citation | None = None


class Term(_Model):
    term: str
    meaning: str
    citation: Citation | None = None


class ExploreOutput(_Model):
    facts: list[Fact] = Field(default_factory=list)
    files: list[FilePurpose] = Field(default_factory=list)
    interfaces: list[Fact] = Field(default_factory=list)
    gotchas: list[Fact] = Field(default_factory=list)
    terms: list[Term] = Field(default_factory=list)


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


class Hop(_Model):
    symbol_called: str
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    note: str = ""


class TraceOutput(_Model):
    title: str = ""
    hops: list[Hop] = Field(default_factory=list)


class Paragraph(_Model):
    text: str


class SectionOutput(_Model):
    paragraphs: list[Paragraph] = Field(default_factory=list)
    glossary: list[Term] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


# --- stage records in the state ------------------------------------------------------


class Unit(BaseModel):
    id: str
    files: list[str]         # workspace-relative
    lines: int
    oversize: bool = False


class Scenario(BaseModel):
    seed: dict                 # the boundary entry it starts from (SymbolEntry or Reference dump)
    title: str = ""
    hops: list[Hop] = Field(default_factory=list)
    stops_at: int | None = None   # 1-based hop index the trace stops at, when it broke
    break_reason: str = ""


class Section(BaseModel):
    key: str
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    cut: list[dict] = Field(default_factory=list)   # [{text, reasons}]
    glossary: list[Term] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


class Dropped(BaseModel):
    stage: str
    what: str
    reason: str


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
    explore_answers: dict[str, dict] = Field(default_factory=dict)  # task key -> ExploreOutput dump | {"error": ...}
    facts: list[Fact] = Field(default_factory=list)                 # gated, with ids
    file_purposes: dict[str, str] = Field(default_factory=dict)     # ws path -> purpose (gated)
    interfaces: list[str] = Field(default_factory=list)             # fact ids
    gotchas: list[str] = Field(default_factory=list)                # fact ids
    terms: list[Term] = Field(default_factory=list)
    uncovered_files: list[str] = Field(default_factory=list)
    uncovered_entries: list[str] = Field(default_factory=list)
    docs: dict = Field(default_factory=dict)
    docs_answers: dict[str, dict] = Field(default_factory=dict)
    scenarios: list[Scenario] = Field(default_factory=list)
    trace_answers: dict[str, dict] = Field(default_factory=dict)
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
        out: dict[str, Citation] = {}
        for fact in self.facts:
            for c in fact.citations:
                if c.id:
                    out[c.id] = c
        for sc in self.scenarios:
            for i, hop in enumerate(sc.hops):
                cid = hop_citation_id(self.scenarios.index(sc), i)
                out[cid] = Citation(path=hop.path, line_start=hop.line_start, line_end=hop.line_end,
                                    quote=hop.quote, id=cid)
        return out


def hop_citation_id(scenario: int, hop: int) -> str:
    return f"s{scenario + 1}h{hop + 1}"
