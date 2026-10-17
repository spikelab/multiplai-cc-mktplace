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

# The explore answers and units live in code_facts, shared with review and buildme.
from code_facts.models import Citation, Dropped, ExploreOutput, Fact, FilePurpose, Term, Unit  # noqa: F401
from code_facts.paths import PathsResult
from pydantic import BaseModel, ConfigDict, Field

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


# --- stage records in the state ------------------------------------------------------


class Section(BaseModel):
    key: str
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    cut: list[dict] = Field(default_factory=list)   # [{text, reasons}]
    glossary: list[Term] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


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
    paths: PathsResult | None = None                                # the trace stage's map_paths result
    paths_error: str | None = None                                  # "<ExceptionType>: <message>" when it failed
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
        return out

