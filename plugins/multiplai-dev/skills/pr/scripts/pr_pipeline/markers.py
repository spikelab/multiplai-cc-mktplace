"""Ownership markers: which bytes of a PR body this skill wrote.

The skill wraps each section's content (never its heading) in
`<!-- pr:begin <key> -->` … `<!-- pr:end <key> -->`. `<key>` is the heading's
slug, `ticket`, `size` or `extra:<slug>`. The body's first line is
`<!-- pr:managed v1 -->`. The second is a marker pair of its own, `written`,
whose content is one HTML comment listing every key the skill has written to
this body: that is how `edit` tells a section a human deleted (in the list,
missing from the body: not restored) from one it has never written (appended).
All of it is HTML comments, so GitHub renders none of it.

`edit` replaces only the bytes between a marker pair it would write now,
appends sections it has never written, and leaves every other byte alone. A
body whose markers nest, do not pair up, or repeat a key is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MANAGED = "<!-- pr:managed v1 -->"
# Ends the comment `edit` posts on a body it did not manage before, so a tool
# reading the PR's comments (buildme-findings) can tell it is not a review.
NOTICE = "<!-- multiplai-pr:notice v=1 -->"
WRITTEN = "written"
_TOKEN_RE = re.compile(r"<!-- pr:(begin|end) ([A-Za-z0-9:_.-]+) -->")
_ANY_PR_TOKEN_RE = re.compile(r"<!--\s*pr:")


class MarkerError(Exception):
    """Markers that nest, do not pair up or repeat a key. Exit code 2."""


def begin(key: str) -> str:
    return f"<!-- pr:begin {key} -->"


def end(key: str) -> str:
    return f"<!-- pr:end {key} -->"


def wrap(key: str, content: str) -> str:
    return f"{begin(key)}\n{content}\n{end(key)}"


def written_line(keys: list[str]) -> str:
    return f"{begin(WRITTEN)}<!-- {' '.join(keys)} -->{end(WRITTEN)}"


def has_pr_token(text: str) -> bool:
    """True when *text* holds anything that looks like this skill's markers."""
    return bool(_ANY_PR_TOKEN_RE.search(text))


def is_managed(body: str) -> bool:
    return MANAGED in body


def spans(body: str) -> dict[str, tuple[int, int]]:
    """key → (start, end) of the content between its begin and end markers.

    Raises MarkerError when a begin opens inside another pair, an end has no
    open begin of its key, a pair is left open, or a key appears twice."""
    out: dict[str, tuple[int, int]] = {}
    open_key: str | None = None
    open_at = 0
    for m in _TOKEN_RE.finditer(body):
        kind, key = m.group(1), m.group(2)
        if kind == "begin":
            if open_key is not None:
                raise MarkerError(
                    f"marker `{key}` begins inside `{open_key}`: nested markers are not allowed"
                )
            if key in out:
                raise MarkerError(f"marker `{key}` appears twice")
            open_key, open_at = key, m.end()
        else:
            if open_key != key:
                raise MarkerError(f"marker end `{key}` has no matching begin")
            out[key] = (open_at, m.start())
            open_key = None
    if open_key is not None:
        raise MarkerError(f"marker `{open_key}` is never closed")
    return out


def read_written(body: str, found: dict[str, tuple[int, int]]) -> set[str] | None:
    """The keys the `written` pair lists, or None when the pair is gone."""
    if WRITTEN not in found:
        return None
    start, stop = found[WRITTEN]
    m = re.search(r"<!--(.*?)-->", body[start:stop], re.DOTALL)
    return set(m.group(1).split()) if m else set()


def owned_text(body: str) -> str:
    """Everything between marker pairs, joined (the `written` list excluded)."""
    return "\n".join(body[a:b] for key, (a, b) in spans(body).items() if key != WRITTEN)


@dataclass
class Tail:
    """A section appended after the template's sections: ticket, size, or an extra."""

    key: str
    heading: str
    content: str


@dataclass
class Merge:
    body: str
    changed: list[str] = field(
        default_factory=list
    )  # keys replaced with different content, or appended
    not_restored: list[str] = field(
        default_factory=list
    )  # in the written list, missing from the body
    not_added: list[str] = field(
        default_factory=list
    )  # template keys never written and missing


def append_tail(body: str, tails: list[Tail]) -> str:
    out = body.rstrip("\n")
    for t in tails:
        out += f"\n\n{t.heading}\n{wrap(t.key, t.content)}"
    return out + "\n"


def merge(
    current: str, owned: dict[str, str], template_keys: set[str], tails: list[Tail]
) -> Merge:
    """Splice *owned* (key → fresh content) into a managed *current* body.

    A key with a marker pair in *current* gets its content replaced. A tail
    key never written is appended. A key in the `written` list but missing
    from the body was deleted by a human and stays deleted; so does a
    template key when the `written` pair itself is gone, since nothing then
    says it was never written."""
    found = spans(current)
    written = read_written(current, found)
    tail_by_key = {t.key: t for t in tails}
    result = Merge(body=current)
    replacements: list[tuple[int, int, str]] = []
    appended: list[Tail] = []
    for key, content in owned.items():
        if key in found:
            a, b = found[key]
            new = f"\n{content}\n"
            if current[a:b] != new:
                replacements.append((a, b, new))
                result.changed.append(key)
        elif (written is not None and key in written) or (
            written is None and key in template_keys
        ):
            result.not_restored.append(key)
        elif key in tail_by_key:
            appended.append(tail_by_key[key])
            result.changed.append(key)
        else:
            result.not_added.append(key)
    if appended and written is not None:
        a, b = found[WRITTEN]
        keys = sorted(written | {t.key for t in appended})
        replacements.append((a, b, f"<!-- {' '.join(keys)} -->"))
    body = current
    for a, b, new in sorted(replacements, reverse=True):
        body = body[:a] + new + body[b:]
    if appended:
        body = append_tail(body, appended)
    result.body = body
    return result
