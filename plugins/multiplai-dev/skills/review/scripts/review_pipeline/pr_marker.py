"""The hidden marker `post` puts on every finding it writes to a PR.

Format (version 1), on the last line of a comment, after the visible text, so
GitHub renders nothing for it:

    <!-- multiplai-review:finding id=<finding_id> round=<round_id> v=1 -->

- `id` is the finding's v1 id: the first 10 hex characters of a sha1
  (`models.finding_id`).
- `round` names the review round by its head: the head sha cut to 12 hex
  characters, with a `-2`, `-3`, ... suffix for a later round on the same
  head (`rounds.round_name`). Before multiplai-dev 0.45 this was also the
  round's directory name under `<out>/<slug>/rounds/`; rounds are now kept as
  `<NN>-<sha12>/`, and the marker keeps its v1 form so posted markers parse.
- `v` is `MARKER_VERSION`.

A tool that finds review findings on a PR (buildme-findings) fetches the PR's
review comments and review bodies and matches every body against this regex,
copied verbatim (it does not import this package):

    <!--\\s*multiplai-review:finding\\s+id=(?P<id>[0-9a-f]+)\\s+round=(?P<round>[0-9a-f]+(?:-[0-9]+)?)\\s+v=(?P<v>[0-9]+)\\s*-->

A change to the format must change `MARKER_VERSION`, the test that pins the
literal string, and the "Marker" section of the review SKILL.md.
"""

from __future__ import annotations

import re

MARKER_VERSION = 1

MARKER_RE = re.compile(
    r"<!--\s*multiplai-review:finding\s+id=(?P<id>[0-9a-f]+)\s+round=(?P<round>[0-9a-f]+(?:-[0-9]+)?)"
    r"\s+v=(?P<v>[0-9]+)\s*-->"
)


def format_marker(finding_id: str, round_id: str) -> str:
    """The marker for *finding_id* posted in round *round_id*."""
    return f"<!-- multiplai-review:finding id={finding_id} round={round_id} v={MARKER_VERSION} -->"


def parse_markers(body: str) -> list[dict]:
    """Every marker in *body*, in order: `{"id": str, "round": str, "v": int}`."""
    return [{"id": m["id"], "round": m["round"], "v": int(m["v"])} for m in MARKER_RE.finditer(body or "")]
