"""Verifier prompt: one fresh agent per finding."""

from __future__ import annotations

import json

from ..models import Finding, TargetInfo
from . import CITATION_RULES, JSON_ONLY, description_block, settings_block, workspace_block

SCHEMA = """\
{"status": "confirmed" | "refuted" | "unverifiable",
 "reason": "what you read and why it settles the question",
 "citations": [{"path": "...", "line_start": 1, "line_end": 1, "quote": "exact text"}],
 "expected_behaviour": "one sentence: what correct behaviour would be",
 "topic": "code" | "tests" | "docs" | "config" | "infra" | "data" | "security" | "performance" | "process",
 "impact": "breaks-users" | "breaks-business" | "correctness-only" | "hygiene",
 "needs": [{"what": "one sentence naming the information you could not read",
            "cause": "no-access" | "unreachable",
            "command": "one read-only shell command that fetches it, or empty",
            "where": "where a person finds it when no single command does, or empty"}]}"""

IMPACT_RULES = """\
Unless you answer `refuted`, set `impact`: if this change is merged as it is, what goes wrong in
production, for whom, on a path that will really be taken? Rate the consequence, not how tidy the
code is, and not the severity the claim was given.
- `breaks-users`: after merge, people who use the product see a failure, a wrong result, lost data
  or exposed data, on a path they will take. Example: rate plans match a literal keyword, so a
  booking gets the wrong price.
- `breaks-business`: after merge, the company is harmed without a user necessarily seeing it: money
  is lost or miscounted, a deploy or scheduled job fails, access is granted wider than intended, a
  record that audits or billing rely on is wrong, or a document or runbook would lead an operator
  to a wrong action in production. Example: a role granted to the whole organisation instead of
  one project.
- `correctness-only`: the code is wrong, but no path after merge reaches a user or the business: an
  edge case no caller reaches, a trap for the next change. A missing or weak test is always
  `correctness-only`; when the code it fails to cover is itself wrong now, the finding is about
  that code (`topic` `code`), and you rate the code's failure.
- `hygiene`: nothing behaves wrongly: naming, dead code, a convention breached with no consequence,
  a stale comment or document that misleads nobody about production.
`impact` is required on every answer but `refuted`; an answer without it is rejected."""

NEEDS_RULES = """\
When your answer is `unverifiable` because something could not be read, fill `needs`: one item per
missing piece of information. `what` names it in one sentence. `cause` is `no-access` when it needs
access you do not have (a cloud project, a database, a private API) and `unreachable` when it is
public but you could not reach it on the web. `command` is ONE read-only shell command that a person
with normal access to this project would run to get it, on one line, with no pipes, redirects or
`;`. For example:
- `gh api repos/<owner>/<repo>/rules/branches/main`
- `gcloud run services describe <service> --region <region> --format json`
- `pip download tavily-python==0.8.4 --no-deps`
Fill in the real names you read in the repository. Leave `command` empty when you know of no such
command. `where` tells a person where to find it when no such command gets it: the console page and
its path (Console > Dataform > <repository> > Workflow execution logs), the API method, the
dashboard, the file outside this repository, or the team that owns it, with the real names filled
in. Fill `where` whenever `command` is empty; never leave both empty. Leave `needs` empty for any
other answer."""


def build(target: TargetInfo, finding: Finding) -> str:
    cited = json.dumps([c.model_dump() for c in finding.citations], indent=1)
    return "\n\n".join(p for p in [
        "You are checking one claim from a code review. You did not write it; assume nothing it says "
        "until you have read the code yourself.",
        workspace_block(target, web=True),
        description_block(target),
        settings_block(target),
        f"Claim ({finding.severity}) about `{finding.file}` lines {finding.line_start}-{finding.line_end}:\n"
        f"{finding.claim}",
        f"Failure scenario given:\n{finding.failure_scenario}",
        f"Lines cited for it:\n{cited}",
        "Read the cited lines and whatever else decides the question: callers, the values that reach "
        "this code, the settings it reads. Then answer:\n"
        "- `confirmed`: the failure scenario can happen. Cite the lines that make it happen.\n"
        "- `refuted`: the code shows it cannot happen. Cite the lines that prevent it.\n"
        "- `unverifiable`: it depends on something you could not read (production data, a vendor's "
        "configuration, runtime values, a dependency whose source or documentation you could not reach "
        "on the web) or the code does not settle it. Say what is missing. When part of the claim holds "
        "in this repository and part depends on something you could not read, the answer is "
        "`unverifiable`, not `confirmed` with a caveat.\n"
        "A `confirmed` answer without a citation that a program can find at the cited lines is "
        "recorded as `unverifiable`.",
        "Unless you answer `refuted`, set `expected_behaviour`: state what correct behaviour would be, in "
        "one sentence, without proposing a code change. For example: \"A null timeout is rejected, not "
        "skipped.\" Say what the code should do, not how to change it.",
        "Set `topic` to what the finding is about, whatever your answer: `code` (behaviour of the "
        "program), `tests`, `docs` (documentation, comments, decision logs), `config`, `infra` "
        "(deployment, CI, cloud resources), `data` (schemas, migrations, stored data), `security`, "
        "`performance`, or `process` (the order of steps a person must follow, such as merge before "
        "deploy). Pick the one a reader would file it under, not the file's type. Use one of these words "
        "exactly (`tests`, not `test`): `topic` is required on every answer but `refuted`, and an answer "
        "without one from this list is rejected.",
        IMPACT_RULES,
        NEEDS_RULES,
        CITATION_RULES,
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ] if p)
