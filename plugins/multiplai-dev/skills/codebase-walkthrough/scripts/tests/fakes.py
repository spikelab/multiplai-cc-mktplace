"""Fake agents: answer each stage's prompt from the snapshot the agent would read.

No model is called anywhere in the tests. Each answer is built by reading the
files the prompt names, so the citations it returns are real and the gates
pass them; the options plant the failures the gates must catch.

Path tracing goes through `code_facts.map_paths`, which asks with its own
schemas (`TraceAnswer`, `FallbackAnswer`) and the snapshot root as `cwd`. The
fake answers a trace with hop 1 at the entry's handler (or its declaration)
and then the hops `trace_plan` lists for that entry name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from walkthrough_pipeline import budget
from walkthrough_pipeline.models import (
    ConfigOutput,
    CoreChoice,
    CoreOutput,
    DocsOutput,
    ExitsOutput,
    ExploreOutput,
    FlowOutput,
    LoopsOutput,
    OutputsOutput,
    PageOutput,
    ResumeOutput,
    SectionOutput,
    SummaryOutput,
    UseCasesOutput,
)

# The explainer's structured schemas, the codebase tables included.
STRUCTURED = (
    SummaryOutput,
    UseCasesOutput,
    FlowOutput,
    OutputsOutput,
    ConfigOutput,
    CoreOutput,
    LoopsOutput,
    ExitsOutput,
    ResumeOutput,
)
# What the survey answer cites in each file, per question (`--scope codebase`).
SURVEY_NEEDLES = {
    "loops": ("MAX_", "range(", "sys.exit(", 'print("'),
    "lifecycle": ("write_text(", "json.loads(", ".open(", "STATE =", "| `--run-dir"),
}
SURVEY_MAX_FACTS = 12


def _line(cwd: str, path: str, n: int) -> str:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return lines[n - 1].strip()


def _text(cwd: str, path: str) -> str:
    return (Path(cwd) / path).read_text(encoding="utf-8")


def _first_code_line(cwd: str, path: str) -> int:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return next((i for i, l in enumerate(lines, start=1) if l.strip()), 1)


def _find(cwd: str, path: str, needle: str) -> int:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return next(i for i, l in enumerate(lines, start=1) if needle in l)


def _def_line(cwd: str, path: str, symbol: str) -> int:
    return _find(cwd, path, f"def {symbol}(")


class FakeAgents:
    def __init__(
        self,
        *,
        cost: float = 0.0,
        invent_path: bool = False,
        fail_on: str | None = None,
        trace_plan: dict[str, list] | None = None,
        span_declaration: bool = False,
        stop_trace_after: int | None = None,
        plant: set[str] | None = None,
        stop_write_after: int | None = None,
        stop_survey_after: int | None = None,
        stop_core_after: int | None = None,
        stop_page_after: int | None = None,
    ):
        self.calls: list[dict] = []
        self.cost = cost
        self.invent_path = invent_path
        self.fail_on = fail_on
        # entry name -> hops after hop 1: (path, symbol) cites `def symbol(`;
        # (path, start, end, called, quote) cites exactly that range.
        self.trace_plan = trace_plan or {}
        # Hop 1 runs from the entry's declaration line down to its handler's `def`.
        self.span_declaration = span_declaration
        # Raise BudgetExceededError on trace call number stop_trace_after + 1.
        self.stop_trace_after = stop_trace_after
        self.trace_calls = 0
        # Failures planted in the explainer's structured answers: "usecase_flag" (a use case with a flag
        # the code does not have), "flow_node" (a box citing an unknown id), "flow_disconnected" (two boxes,
        # no arrow), "output_key" (a JSON example with a key the code does not have), "flow_fan_number" (a
        # fan-out whose number is in no quote and no snapshot), "flow_step_order" (the steps swapped).
        self.plant = plant or set()
        # Raise BudgetExceededError on write call number stop_write_after + 1.
        self.stop_write_after = stop_write_after
        self.write_calls = 0
        # Raise BudgetExceededError on survey call number stop_survey_after + 1. The codebase tables'
        # plants: "exit_number" (a signal whose number is in no quote) and "loop_cap" (a cap whose number
        # is in no quote).
        self.stop_survey_after = stop_survey_after
        self.survey_calls = 0
        # Raise BudgetExceededError on core call number stop_core_after + 1 (the choice, its re-ask and
        # the deep read count). The core plants: "core_title_cite" (a title holding a citation) and
        # "core_outside_file" (a file outside the target), both in every choice answer.
        self.stop_core_after = stop_core_after
        self.core_calls = 0
        # Raise BudgetExceededError on page call number stop_page_after + 1. The page plants, each in the
        # first answer only, or in every answer with ":always" after it: "page_script" (a <script>),
        # "page_unknown_id" (a citation id that is not in the material), "page_colour" (a colour literal
        # in the CSS), "page_heading_cite" (a citation inside an <h3>), "page_no_flow" (no {{flow}}) and
        # "page_long" (an open body over the word limit).
        self.stop_page_after = stop_page_after
        self.page_calls = 0

    async def __call__(self, prompt: str, schema, **kw):
        from walkthrough_pipeline import sdk

        sdk.require_trusted_repo()
        budget.check(stage=kw.get("budget_label", ""))
        self.calls.append({"schema": schema.__name__, "prompt": prompt, **kw})
        budget.record(
            SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=self.cost),
            label=kw.get("budget_label", ""),
        )
        if self.fail_on and self.fail_on in prompt:
            raise sdk.AgentCallError("planted failure")
        cwd = kw.get("cwd") or ""
        if schema is CoreChoice or (
            schema is ExploreOutput and "Core part: [core:read]" in prompt
        ):
            self.core_calls += 1
            if (
                self.stop_core_after is not None
                and self.core_calls > self.stop_core_after
            ):
                raise budget.BudgetExceededError(
                    "circuit breaker: planted stop during core"
                )
            if schema is CoreChoice:
                return CoreChoice.model_validate(self.core_choice(prompt))
            return self.core_read(prompt, cwd)
        if schema is ExploreOutput and "Survey question: [survey:" in prompt:
            self.survey_calls += 1
            if (
                self.stop_survey_after is not None
                and self.survey_calls > self.stop_survey_after
            ):
                raise budget.BudgetExceededError(
                    "circuit breaker: planted stop during survey"
                )
            return self.survey(prompt, cwd)
        if schema is ExploreOutput:
            return self.explore(prompt, cwd)
        if schema is DocsOutput:
            return self.docs(prompt, cwd)
        if schema.__name__ == "TraceAnswer":
            self.trace_calls += 1
            if (
                self.stop_trace_after is not None
                and self.trace_calls > self.stop_trace_after
            ):
                raise budget.BudgetExceededError(
                    "circuit breaker: planted stop during trace"
                )
            return schema.model_validate(self.trace(prompt, cwd))
        if schema.__name__ == "FallbackAnswer":
            return schema.model_validate(self.fallback(prompt, cwd))
        if schema is SectionOutput or schema in STRUCTURED:
            self.write_calls += 1
            if (
                self.stop_write_after is not None
                and self.write_calls > self.stop_write_after
            ):
                raise budget.BudgetExceededError(
                    "circuit breaker: planted stop during write"
                )
        if schema is PageOutput:
            self.page_calls += 1
            if (
                self.stop_page_after is not None
                and self.page_calls > self.stop_page_after
            ):
                raise budget.BudgetExceededError(
                    "circuit breaker: planted stop during page"
                )
            return PageOutput.model_validate(self.page(prompt))
        if schema is SectionOutput:
            return self.write(prompt)
        if schema in STRUCTURED:
            return schema.model_validate(self.explain(schema, prompt))
        if schema.__name__ == "TraceOutput":
            # The trace stage from before map_paths: an empty answer, so suites that do not test
            # tracing still run against it. Unreachable once that stage and its model are gone.
            return schema.model_validate({"hops": []})
        raise AssertionError(schema)

    def explore(self, prompt: str, cwd: str) -> ExploreOutput:
        facts, files, terms, gotchas = [], [], [], []
        if "Files in your part of the module:" in prompt:
            for path in re.findall(r"^- (\S+) \(\d+ lines\)$", prompt, re.MULTILINE):
                n = _first_code_line(cwd, path)
                cit = {
                    "path": path,
                    "line_start": n,
                    "line_end": n,
                    "quote": _line(cwd, path, n),
                }
                facts.append({"claim": f"{path} starts here", "citations": [cit]})
                files.append(
                    {"path": path, "purpose": "part of bookings", "citation": cit}
                )
                # The bookings fixture's models.py (a real tree, as in the free check, has others).
                if path.endswith("models.py") and "ospite_count" in _text(cwd, path):
                    k = _find(cwd, path, "ospite_count")
                    terms.append(
                        {
                            "term": "ospite",
                            "meaning": "guest (Italian)",
                            "citation": {
                                "path": path,
                                "line_start": k,
                                "line_end": k,
                                "quote": "ospite_count",
                            },
                        }
                    )
                    gotchas.append(
                        {
                            "claim": "guest is free text",
                            "citations": [
                                {
                                    "path": path,
                                    "line_start": k - 1,
                                    "line_end": k - 1,
                                    "quote": "guest = models.CharField",
                                }
                            ],
                        }
                    )
            facts.append(
                {
                    "claim": "an invented claim",
                    "citations": [
                        {
                            "path": "engine/bookings/nowhere.py",
                            "line_start": 1,
                            "line_end": 1,
                            "quote": "nothing",
                        }
                    ],
                }
            )
        elif "Line ranges in your part of the code:" in prompt:
            # A change's ranges: one fact citing the first non-blank line of each range.
            for path, a, b in re.findall(
                r"^- (\S+?), lines (\d+)-(\d+)", prompt, re.MULTILINE
            ):
                lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
                n = next(
                    (i for i in range(int(a), int(b) + 1) if lines[i - 1].strip()),
                    int(a),
                )
                cit = {
                    "path": path,
                    "line_start": n,
                    "line_end": n,
                    "quote": lines[n - 1].strip(),
                }
                facts.append({"claim": f"{path} changes here", "citations": [cit]})
                if "@base/" not in path:
                    files.append(
                        {"path": path, "purpose": "changed by the PR", "citation": cit}
                    )
        else:
            for path, line in re.findall(r"^\[(\S+?):(\d+)\] ", prompt, re.MULTILINE):
                n = int(line)
                facts.append(
                    {
                        "claim": f"{path} uses bookings",
                        "citations": [
                            {
                                "path": path,
                                "line_start": n,
                                "line_end": n,
                                "quote": _line(cwd, path, n),
                            }
                        ],
                    }
                )
        return ExploreOutput.model_validate(
            {"facts": facts, "files": files, "terms": terms, "gotchas": gotchas}
        )

    def survey(self, prompt: str, cwd: str) -> ExploreOutput:
        """One fact per line holding one of the question's needles, in the listed files and
        libraries (and, for lifecycle, the docs); for the loops question the first is the cap, and one gotcha cites it."""
        question = re.search(r"Survey question: \[survey:(\w+)\]", prompt).group(1)
        listed = re.findall(r"^- (\S+) \(\d+ lines\)$", prompt, re.MULTILINE)
        # The lifecycle answer also cites the docs' line on where state is kept.
        docs = re.findall(r"^- (\S+\.md)$", prompt, re.MULTILINE)
        libs = [
            f.relative_to(cwd).as_posix()
            for d in re.findall(r"^- (\S+)/$", prompt, re.MULTILINE)
            for f in sorted((Path(cwd) / d).rglob("*.py"))
        ]
        facts = []
        for path in listed + libs + (docs if question == "lifecycle" else []):
            lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
            for n, line in enumerate(lines, start=1):
                if any(needle in line for needle in SURVEY_NEEDLES[question]):
                    cit = {
                        "path": path,
                        "line_start": n,
                        "line_end": n,
                        "quote": line.strip(),
                    }
                    facts.append(
                        {"claim": f"{question}: {line.strip()}", "citations": [cit]}
                    )
        facts = facts[:SURVEY_MAX_FACTS]
        gotchas = facts[:1] if question == "loops" else []
        return ExploreOutput.model_validate({"facts": facts, "gotchas": gotchas})

    def core_choice(self, prompt: str) -> dict:
        """The first `.py` file listed that is not an `__init__.py`, citing the first fact id of the material."""
        files = [
            f
            for f in re.findall(r"^- (\S+) \(\d+ lines\)$", prompt, re.MULTILINE)
            if f.endswith(".py") and not f.endswith("__init__.py")
        ]
        ids = re.findall(r"^- \[([a-z0-9]+)\]", prompt, re.MULTILINE) or ["c1"]
        title = "How it stores bookings"
        if "core_title_cite" in self.plant:
            title += f" [[{ids[0]}]]"
        chosen = files[:1] + (
            ["elsewhere/not_the_target.py"] if "core_outside_file" in self.plant else []
        )
        return {
            "title": title,
            "job": f"It keeps each booking it is sent. [[{ids[0]}]]",
            "files": chosen,
            "why": "That file holds the code that saves them.",
            "cites": [ids[0]],
        }

    def core_read(self, prompt: str, cwd: str) -> ExploreOutput:
        """One fact per `def ` or `class ` line of each listed file, and one gotcha on the first."""
        facts = []
        for path in re.findall(r"^- (\S+) \(\d+ lines\)$", prompt, re.MULTILINE):
            lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
            for n, line in enumerate(lines, start=1):
                if line.lstrip().startswith(("def ", "class ")):
                    cit = {
                        "path": path,
                        "line_start": n,
                        "line_end": n,
                        "quote": line.strip(),
                    }
                    facts.append({"claim": f"core: {line.strip()}", "citations": [cit]})
        return ExploreOutput.model_validate({"facts": facts, "gotchas": facts[:1]})

    def docs(self, prompt: str, cwd: str) -> DocsOutput:
        pages = re.findall(r"^- (https://\S+) -> file `(\S+)`", prompt, re.MULTILINE)
        paths = (
            re.search(r"The code calls these API paths: (.*)\.$", prompt, re.MULTILINE)
            .group(1)
            .split(", ")
        )
        claims, mappings = [], []
        for url, name in pages:
            text = (Path(cwd) / name).read_text(encoding="utf-8").split("\n")
            quote = next(l for l in text[1:] if l.strip() and not l.startswith("#"))
            claims.append(
                {
                    "claim": f"the page {url} documents it",
                    "url": url,
                    "doc_quote": quote,
                }
            )
            for p in paths:
                if p.strip("/").split("/")[0] in url:
                    mappings.append({"api_path": p, "url": url, "doc_quote": quote})
        claims.append(
            {
                "claim": "made up",
                "url": pages[0][0] if pages else "https://x.test",
                "doc_quote": "this sentence is not on the page",
            }
        )
        return DocsOutput.model_validate({"claims": claims, "mappings": mappings})

    def trace(self, prompt: str, cwd: str) -> dict:
        """A `TraceAnswer`: hop 1 at the entry, then the hops `trace_plan` gives for its name."""
        name = re.search(r"^- name: (.+)$", prompt, re.MULTILINE).group(1).strip()
        decl_path, decl_line = re.search(
            r"^- declared at: (\S+):(\d+)$", prompt, re.MULTILINE
        ).groups()
        handler = re.search(r"^- handler: (\S+) at (\S+):(\d+)$", prompt, re.MULTILINE)
        if handler:
            path, line = handler.group(2), int(handler.group(3))
            start = (
                int(decl_line) if self.span_declaration and decl_path == path else line
            )
        else:
            path, line = decl_path, int(decl_line)
            start = line
        hops = [
            {
                "symbol_called": name,
                "path": path,
                "line_start": min(start, line),
                "line_end": line,
                "quote": _line(cwd, path, line),
                "note": "the entry",
            }
        ]
        for step in self.trace_plan.get(name, []):
            if len(step) == 2:
                p, symbol = step
                n = _def_line(cwd, p, symbol)
                hops.append(
                    {
                        "symbol_called": symbol,
                        "path": p,
                        "line_start": n,
                        "line_end": n,
                        "quote": _line(cwd, p, n),
                        "note": f"calls {symbol}",
                    }
                )
            else:
                p, a, b, called, quote = step
                hops.append(
                    {
                        "symbol_called": called,
                        "path": p,
                        "line_start": a,
                        "line_end": b,
                        "quote": quote,
                        "note": f"runs {called}",
                    }
                )
        return {"hops": hops}

    def fallback(self, prompt: str, cwd: str) -> dict:
        """A `FallbackAnswer`: the static scan missed nothing."""
        return {"entries": []}

    def write(self, prompt: str) -> SectionOutput:
        ids = re.findall(r"^- \[([a-z0-9]+)\]", prompt, re.MULTILINE)
        paragraphs = [
            {
                "text": f"This part is explained by [[{ids[0]}]] and `Reservation`."
                if ids
                else "Nothing passed the checks here."
            }
        ]
        if self.invent_path:
            paragraphs.append(
                {"text": "See engine/bookings/nowhere.py:99 for the retry."}
            )
        questions = (
            ["Who owns `poll_task`?"]
            if "questions" in prompt and "Gotchas" in prompt
            else []
        )
        return SectionOutput.model_validate(
            {"paragraphs": paragraphs, "questions": questions}
        )

    def _planted(self, name: str, prompt: str) -> bool:
        """*name* is planted in this page answer: the first, or every one with ":always"."""
        if f"{name}:always" in self.plant:
            return True
        return name in self.plant and "Your previous answer failed" not in prompt

    def page(self, prompt: str) -> dict:
        """The page writer's answer, built from its material: the summary's paragraphs and the flow's
        steps, one cited sentence for every other open section, a snippet in What it creates and a
        fold-out in Configuration."""
        keys = re.findall(r'^- "([a-z]+)" \(', prompt, re.MULTILINE)
        body = prompt.split('<untrusted-content source="page material">', 1)[1]
        body = body.split("</untrusted-content>", 1)[0].strip()
        material = json.loads(body.strip("`").removeprefix("text").strip())
        tokens = re.search(r"from this list: (--[\w-]+)", prompt)
        token = tokens.group(1) if tokens else "--fg"
        ids = [f["id"] for f in material["facts"]] or ["c1"]
        secs = material["sections"]
        out = {}
        for key in keys:
            if key == "summary":
                paras = secs["summary"]["paragraphs"]
                html = "".join(f"<p>{p}</p>" for p in paras) + "<p>{{flow}}</p>"
                if self._planted("page_script", prompt):
                    html += "<script>alert(1)</script>"
                if self._planted("page_no_flow", prompt):
                    html = html.replace("<p>{{flow}}</p>", "")
                if self._planted("page_long", prompt):
                    html += "<p>" + " ".join(["word"] * 1300) + "</p>"
            elif key == "flow":
                html = "".join(
                    f"<h3>{s['title']}</h3><p>{s['summary']}</p>"
                    for s in secs["flow"]["steps"]
                )
                if self._planted("page_heading_cite", prompt):
                    html = f"<h3>Where it starts [[{ids[0]}]]</h3>" + html
            elif key == "core":
                html = f'<p class="lead">{material["core"]["job"]}</p>'
                if (secs.get("core") or {}).get("flow"):
                    html += "<p>{{flow:core}}</p>"
            elif key == "outputs":
                html = (
                    f"<p>It saves each booking it is sent. [[{ids[0]}]]</p>"
                    f"<p>[[snippet:{ids[0]}]]</p>"
                )
            elif key == "config":
                html = (
                    f"<p>Its settings come from the environment. [[{ids[1 % len(ids)]}]]</p>"
                    "<details><summary>Every setting</summary><p>"
                    + " ".join(["hidden"] * 50)
                    + "</p></details>"
                )
                if self._planted("page_unknown_id", prompt):
                    html += "<p>It has one more. [[c9999]]</p>"
            else:
                html = f"<p>The {key} section says one thing. [[{ids[0]}]]</p>"
            out[key] = html
        css = f".lead {{ color: var({token}); }}"
        if self._planted("page_colour", prompt):
            css += "\n.lead { background: #ff0000; }"
        return {
            "css": css,
            "sections": [{"key": k, "html": h} for k, h in out.items()],
        }

    def explain(self, schema, prompt: str) -> dict:
        """An answer for one of the explainer's structured sections, citing the first ids of its material."""
        ids = re.findall(r"^- \[([a-z0-9]+)\]", prompt, re.MULTILINE) or ["c1"]
        a, b, c = (ids + ids + ids)[:3]

        def id_with(needle: str) -> str:
            """The first material id whose line holds *needle*, else the first id."""
            m = re.search(
                rf"^- \[([a-z0-9]+)\][^\n]*{re.escape(needle)}", prompt, re.MULTILINE
            )
            return m.group(1) if m else a

        if schema is CoreOutput:
            return {
                "summary": f"It saves each booking as one row. [[{a}]]",
                "paragraphs": [
                    {"text": f"The view writes the row and returns. [[{b}]]"}
                ],
                "parts": [
                    {
                        "name": "The model",
                        "looks_at": "The booking it is given.",
                        "given": "The guest and the count.",
                        "produces": "One saved row.",
                        "checked_by": f"The database schema. [[{a}]]",
                        "cite": a,
                    }
                ],
            }
        if schema is LoopsOutput:
            rows = [
                {
                    "what": "The build attempts",
                    "cap": "3 (`MAX_TRIES`)",
                    "ends_well": "Every attempt is recorded.",
                    "ends_badly": "An error stops it.",
                    "cite": id_with("MAX_TRIES ="),
                }
            ]
            if "loop_cap" in self.plant:
                rows[0]["cap"] = "7 (`MAX_TRIES`)"
            return {"rows": rows}
        if schema is ExitsOutput:
            rows = [
                {
                    "signal": "exit 2",
                    "meaning": "The check found a problem.",
                    "action": "Read the output and fix it.",
                    "cite": id_with("sys.exit(2)"),
                },
                {
                    "signal": "DONE:",
                    "meaning": "The build finished.",
                    "action": "Nothing.",
                    "cite": id_with('print("DONE'),
                },
            ]
            if "exit_number" in self.plant:
                rows.append(
                    {
                        "signal": "exit 9",
                        "meaning": "Something else.",
                        "action": "Nothing.",
                        "cite": id_with("sys.exit(2)"),
                    }
                )
            return {"rows": rows}
        if schema is ResumeOutput:
            return {
                "rows": [
                    {
                        "stop": "A crash mid build",
                        "on_disk": "The attempts done so far.",
                        "rerun": "It skips the attempts already recorded.",
                        "cite": id_with("write_text("),
                    }
                ]
            }
        if schema is SummaryOutput:
            return {
                "tagline": f"Bookings takes reservations from Acme. [[{a}]]",
                "paragraphs": [
                    {"text": f"It keeps each `Reservation` in the database. [[{a}]]"},
                    {"text": f"Other code calls it over HTTP and on a timer. [[{b}]]"},
                ],
            }
        if schema is UseCasesOutput:
            cases = [
                {
                    "title": "Poll for new bookings",
                    "who": "The scheduler",
                    "when": "Every minute",
                    "example": {"kind": "command", "text": "bookings poll"},
                    "what_happens": f"It asks Acme for new bookings. [[{a}]]",
                    "result": "New reservations.",
                    "cites": [a],
                },
                {
                    "title": "Receive a webhook",
                    "who": "Acme",
                    "when": "When a booking changes",
                    "example": {"kind": "call", "text": 'Reservation(guest="Ada")'},
                    "what_happens": f"It stores the booking. [[{b}]]",
                    "result": "One reservation row.",
                    "cites": [f"[[{b}]]"],
                },
            ]
            if "usecase_flag" in self.plant:
                cases.append(
                    {
                        "title": "Poll in a hurry",
                        "who": "An engineer",
                        "when": "Debugging",
                        "example": {
                            "kind": "command",
                            "text": "bookings poll --no-such-flag",
                        },
                        "what_happens": f"It polls. [[{a}]]",
                        "result": "",
                        "cites": [a],
                    }
                )
            return {"cases": cases}
        if schema is FlowOutput:
            nodes = [
                {
                    "id": "n1",
                    "label": "Acme sends a booking",
                    "cite": a,
                    "kind": "Input",
                    "does": "A webhook call",
                },
                {
                    "id": "n2",
                    "label": "The view stores it",
                    "cite": f"[[{b}]]",
                    "kind": "code",
                    "does": "Saves one row",
                    "reads": ["the booking"],
                    "writes": ["a reservation row"],
                },
                {
                    "id": "n3",
                    "label": "The task acknowledges it",
                    "cite": c,
                    "kind": "agent",
                    "does": "Tells Acme it arrived",
                    "fan_out": "987654 workers"
                    if "flow_fan_number" in self.plant
                    else "",
                },
            ]
            edges = [
                {"from": "n1", "to": "n2", "label": "webhook"},
                {"from": "n2", "to": "n3"},
            ]
            if "flow_node" in self.plant:
                nodes.append(
                    {"id": "n4", "label": "A box with no source", "cite": "c999"}
                )
                edges.append({"from": "n3", "to": "n4"})
            if "flow_disconnected" in self.plant:
                edges = []
            steps = [
                {
                    "node": "n2",
                    "title": "Receive",
                    "summary": f"A booking arrives. [[{a}]]",
                    "details_paragraphs": [f"The handler reads the body. [[{b}]]"],
                    "snippet_cites": [a],
                },
                {
                    "node": "n3",
                    "title": "Store",
                    "summary": f"It is saved. [[{c}]]",
                    "details_paragraphs": [],
                    "snippet_cites": [],
                },
            ]
            if "flow_step_order" in self.plant:
                steps.reverse()
            return {"nodes": nodes, "edges": edges, "steps": steps}
        if schema is OutputsOutput:
            key = "zz_not_in_the_code" if "output_key" in self.plant else "ospite_count"
            return {
                "items": [
                    {
                        "name": "Reservation row",
                        "what": f"One row per booking. [[{a}]]",
                        "written_by_cite": a,
                        "example": {
                            "format": "json",
                            "text": '{"guest": "Ada", "' + key + '": 2}',
                        },
                    },
                    {
                        "name": "Acknowledgement",
                        "what": f"What it sends back to Acme. [[{b}]]",
                        "written_by_cite": b,
                        "example": {"format": "text", "text": "ok"},
                    },
                    {
                        "name": "Poll log",
                        "what": f"A line per poll. [[{c}]]",
                        "written_by_cite": c,
                    },
                ]
            }
        if "| `--run-dir" in prompt:
            # The plugin fixture: a row citing its docs' flag table, whose `|` must survive the cell.
            flag = id_with("| `--run-dir")
            return {
                "rows": [
                    {
                        "name": "--run-dir",
                        "effect": f"Where the build keeps its state. [[{flag}]]",
                        "default": "",
                        "cite": flag,
                    }
                ]
            }
        return {
            "rows": [
                {
                    "name": "ACME_ENV",
                    "effect": f"Picks the staging or production key. [[{a}]]",
                    "default": "production",
                    "cite": a,
                }
            ]
        }
