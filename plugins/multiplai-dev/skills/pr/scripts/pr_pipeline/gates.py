"""Gates on the assembled body. No gate calls a model.

- every template heading line appears as many times as in the template, in
  template order, counting only lines `parse` reads as headings (a heading
  line inside an HTML comment or a fenced code block is not one);
- every template checkbox line appears, unticked unless its id was ticked
  with a quote found verbatim in a source file;
- every section the model filled is non-empty (or holds the literal
  `_Not applicable to this change._`) and holds no marker of this skill;
- the body is at most `MAX_WORDS` words, HTML comments removed. On `edit`
  only the text inside this skill's markers is counted: a human's own text is
  never counted or cut.

GitHub's own limit needs no check of its own: its docs state none, and the
API rejects bodies over 65,536 characters, which 2,500 words of normal prose
do not reach.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import markers
from .template import Section, parse, set_tick

# Spike, 2026-10-12: a PR body longer than 1,500 to 2,500 words does not get
# read. The gate stops at the upper bound; the model is told to aim for the
# lower one in the sections it writes.
MAX_WORDS = 2500
MODEL_TARGET_WORDS = 1500

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def word_count(text: str) -> int:
    return len(strip_comments(text).split())


@dataclass
class GateResult:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def message(self) -> str:
        return "; ".join(self.errors)


def check_headings(sections: list[Section], body: str) -> list[str]:
    """Each template heading line appears in the body as many times as in the template, and
    the n-th copy in the body stands where the n-th copy in the template does, in order.
    Only lines `parse` reads as headings count, so a heading line inside a comment or a
    fence counts in neither the template nor the body."""
    lines = [s.heading for s in parse(body) if s.level != 0]
    headings = [s.heading for s in sections if s.level != 0]
    errors: list[str] = []
    found: dict[str, list[int]] = {}
    for h in dict.fromkeys(headings):
        found[h] = [i for i, line in enumerate(lines) if line == h]
        want = headings.count(h)
        if len(found[h]) != want:
            expected = "once" if want == 1 else str(want)
            errors.append(
                f"template heading {h!r} appears {len(found[h])} times, expected {expected}"
            )
    last = -1
    used: dict[str, int] = {}
    for h in headings:
        if len(found[h]) != headings.count(h):
            continue
        at = found[h][used.get(h, 0)]
        used[h] = used.get(h, 0) + 1
        if at < last:
            errors.append(f"template heading {h!r} is out of template order")
        last = max(last, at)
    return errors


def check_checkboxes(sections: list[Section], body: str, ticks: set[str]) -> list[str]:
    lines = set(body.split("\n"))
    errors = []
    for s in sections:
        for b in s.checkboxes:
            want = set_tick(b.text, b.id in ticks)
            if want not in lines:
                errors.append(f"template checkbox line {b.text.strip()!r} is missing")
    return errors


def check_filled(model_text: dict[str, str], model_keys: list[str]) -> list[str]:
    errors = []
    for key in model_keys:
        text = (model_text.get(key) or "").strip()
        if not text:
            errors.append(
                f"section `{key}` is empty; write it, or answer `_Not applicable to this change._`"
            )
        elif markers.has_pr_token(text):
            errors.append(
                f"section `{key}` contains a `<!-- pr:` marker, which only code may write"
            )
    return errors


def check_words(
    counted: dict[str, str], limit: int = MAX_WORDS, what: str = "the body"
) -> list[str]:
    """*counted* maps a label to its text; fails when their words add up past *limit*."""
    counts = {label: word_count(text) for label, text in counted.items()}
    total = sum(counts.values())
    if total <= limit:
        return []
    per = ", ".join(
        f"{label}: {n}" for label, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return [
        f"{what} is {total} words, over the {limit}-word limit; words per section: {per}"
    ]


def open_word_counts(body: str, owned: dict[str, str]) -> dict[str, str]:
    """For `open`: each owned section's content, plus everything else in the body (headings,
    checkbox lines, preamble) under `template text`. Their sum is the body's word count."""
    rest = body
    for key in owned:
        a, b = markers.begin(key), markers.end(key)
        rest = re.sub(re.escape(a) + r".*?" + re.escape(b), "", rest, flags=re.DOTALL)
    return {**owned, "template text": rest}


def edit_word_counts(body: str) -> dict[str, str]:
    """For `edit`: the content of each marker pair in the final body; nothing outside markers."""
    return {
        key: body[a:b]
        for key, (a, b) in markers.spans(body).items()
        if key != markers.WRITTEN
    }


def run_open_gates(
    sections: list[Section],
    body: str,
    owned: dict[str, str],
    ticks: set[str],
    model_text: dict[str, str],
    model_keys: list[str],
) -> GateResult:
    errors = check_headings(sections, body)
    errors += check_checkboxes(sections, body, ticks)
    errors += check_filled(model_text, model_keys)
    errors += check_words(open_word_counts(body, owned))
    return GateResult(errors)
