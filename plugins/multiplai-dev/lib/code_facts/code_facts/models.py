"""The facts an explore agent returns, and the units of code it is given.

Paths. A path is whatever form the caller's `resolve(path, side)` function
understands: codebase-walkthrough uses `<repo key>/<path in repo>`, a single-repo
caller can use the repo-relative path. Agents return paths in the same form
they were given.

Ids. A gated citation gets the id `c<N>` and a gated fact `f<N>`, assigned by
code in the order they pass.
"""

from __future__ import annotations

from typing import Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

Side = Literal["base", "head"]


class _Model(BaseModel):
    # Models answer in JSON; an unknown key is ignored rather than failing the parse.
    model_config = ConfigDict(extra="ignore")


# --- agents' answers -------------------------------------------------------------


class Citation(_Model):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    id: str = ""
    # Which commit the lines are at: "head" (after a change, or the only commit)
    # or "base" (before a change, for removed lines).
    side: Side = "head"

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
    # Rules new code must follow to fit in (naming, error handling, test style).
    # Asked for only when the prompt's conventions section is on.
    conventions: list[Fact] = Field(default_factory=list)


# --- what the agents are given ------------------------------------------------------


class Span(BaseModel):
    """Lines `start..end` (1-based, inclusive) of one file at one side of a change."""
    path: str
    start: int = Field(default=1, ge=1)
    end: int = Field(ge=0)          # 0 only for an empty whole file
    side: Side = "head"
    whole_file: bool = False

    @property
    def lines(self) -> int:
        return max(0, self.end - self.start + 1)

    @classmethod
    def whole(cls, path: str, lines: int, side: Side = "head") -> "Span":
        return cls(path=path, start=1, end=lines, side=side, whole_file=True)


class Scope(BaseModel):
    """The line ranges to explain: whole files for a module, hunks for a diff."""
    spans: list[Span] = Field(default_factory=list)

    @classmethod
    def whole_files(cls, files: Mapping[str, int] | Iterable[tuple[str, int]]) -> "Scope":
        items = files.items() if isinstance(files, Mapping) else files
        return cls(spans=[Span.whole(p, n) for p, n in items])


class Unit(BaseModel):
    id: str
    files: list[str]         # the distinct paths of its spans, in order
    lines: int
    oversize: bool = False
    spans: list[Span] = Field(default_factory=list)


class Dropped(BaseModel):
    stage: str
    what: str
    reason: str
