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

import re
from pathlib import Path
from types import SimpleNamespace

from walkthrough_pipeline import budget
from walkthrough_pipeline.models import DocsOutput, ExploreOutput, SectionOutput


def _line(cwd: str, path: str, n: int) -> str:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return lines[n - 1].strip()


def _first_code_line(cwd: str, path: str) -> int:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return next((i for i, l in enumerate(lines, start=1) if l.strip()), 1)


def _find(cwd: str, path: str, needle: str) -> int:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return next(i for i, l in enumerate(lines, start=1) if needle in l)


def _def_line(cwd: str, path: str, symbol: str) -> int:
    return _find(cwd, path, f"def {symbol}(")


class FakeAgents:
    def __init__(self, *, cost: float = 0.0, invent_path: bool = False, fail_on: str | None = None,
                 trace_plan: dict[str, list] | None = None, span_declaration: bool = False,
                 stop_trace_after: int | None = None):
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

    async def __call__(self, prompt: str, schema, **kw):
        from walkthrough_pipeline import sdk
        sdk.require_trusted_repo()
        budget.check(stage=kw.get("budget_label", ""))
        self.calls.append({"schema": schema.__name__, "prompt": prompt, **kw})
        budget.record(SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=self.cost),
                      label=kw.get("budget_label", ""))
        if self.fail_on and self.fail_on in prompt:
            raise sdk.AgentCallError("planted failure")
        cwd = kw.get("cwd") or ""
        if schema is ExploreOutput:
            return self.explore(prompt, cwd)
        if schema is DocsOutput:
            return self.docs(prompt, cwd)
        if schema.__name__ == "TraceAnswer":
            self.trace_calls += 1
            if self.stop_trace_after is not None and self.trace_calls > self.stop_trace_after:
                raise budget.BudgetExceededError("circuit breaker: planted stop during trace")
            return schema.model_validate(self.trace(prompt, cwd))
        if schema.__name__ == "FallbackAnswer":
            return schema.model_validate(self.fallback(prompt, cwd))
        if schema is SectionOutput:
            return self.write(prompt)
        if schema.__name__ == "TraceOutput":
            # The trace stage from before map_paths: an empty answer, so suites that do not test
            # tracing still run against it. Unreachable once that stage and its model are gone.
            return schema.model_validate({"hops": []})
        raise AssertionError(schema)

    def explore(self, prompt: str, cwd: str) -> ExploreOutput:
        facts, files, terms, gotchas = [], [], [], []
        if "Files in your part of the module:" in prompt:
            for path in re.findall(r"^- (\S+) \(\d+ lines\)$", prompt, re.M):
                n = _first_code_line(cwd, path)
                cit = {"path": path, "line_start": n, "line_end": n, "quote": _line(cwd, path, n)}
                facts.append({"claim": f"{path} starts here", "citations": [cit]})
                files.append({"path": path, "purpose": "part of bookings", "citation": cit})
                if path.endswith("models.py"):
                    k = _find(cwd, path, "ospite_count")
                    terms.append({"term": "ospite", "meaning": "guest (Italian)",
                                  "citation": {"path": path, "line_start": k, "line_end": k,
                                               "quote": "ospite_count"}})
                    gotchas.append({"claim": "guest is free text", "citations": [
                        {"path": path, "line_start": k - 1, "line_end": k - 1, "quote": "guest = models.CharField"}]})
            facts.append({"claim": "an invented claim", "citations": [
                {"path": "engine/bookings/nowhere.py", "line_start": 1, "line_end": 1, "quote": "nothing"}]})
        else:
            for path, line in re.findall(r"^\[(\S+?):(\d+)\] ", prompt, re.M):
                n = int(line)
                facts.append({"claim": f"{path} uses bookings",
                              "citations": [{"path": path, "line_start": n, "line_end": n,
                                             "quote": _line(cwd, path, n)}]})
        return ExploreOutput.model_validate({"facts": facts, "files": files, "terms": terms, "gotchas": gotchas})

    def docs(self, prompt: str, cwd: str) -> DocsOutput:
        pages = re.findall(r"^- (https://\S+) -> file `(\S+)`", prompt, re.M)
        paths = re.search(r"The code calls these API paths: (.*)\.$", prompt, re.M).group(1).split(", ")
        claims, mappings = [], []
        for url, name in pages:
            text = (Path(cwd) / name).read_text(encoding="utf-8").split("\n")
            quote = next(l for l in text[1:] if l.strip() and not l.startswith("#"))
            claims.append({"claim": f"the page {url} documents it", "url": url, "doc_quote": quote})
            for p in paths:
                if p.strip("/").split("/")[0] in url:
                    mappings.append({"api_path": p, "url": url, "doc_quote": quote})
        claims.append({"claim": "made up", "url": pages[0][0] if pages else "https://x.test", "doc_quote":
                       "this sentence is not on the page"})
        return DocsOutput.model_validate({"claims": claims, "mappings": mappings})

    def trace(self, prompt: str, cwd: str) -> dict:
        """A `TraceAnswer`: hop 1 at the entry, then the hops `trace_plan` gives for its name."""
        name = re.search(r"^- name: (.+)$", prompt, re.M).group(1).strip()
        decl_path, decl_line = re.search(r"^- declared at: (\S+):(\d+)$", prompt, re.M).groups()
        handler = re.search(r"^- handler: (\S+) at (\S+):(\d+)$", prompt, re.M)
        if handler:
            path, line = handler.group(2), int(handler.group(3))
            start = int(decl_line) if self.span_declaration and decl_path == path else line
        else:
            path, line = decl_path, int(decl_line)
            start = line
        hops = [{"symbol_called": name, "path": path, "line_start": min(start, line), "line_end": line,
                 "quote": _line(cwd, path, line), "note": "the entry"}]
        for step in self.trace_plan.get(name, []):
            if len(step) == 2:
                p, symbol = step
                n = _def_line(cwd, p, symbol)
                hops.append({"symbol_called": symbol, "path": p, "line_start": n, "line_end": n,
                             "quote": _line(cwd, p, n), "note": f"calls {symbol}"})
            else:
                p, a, b, called, quote = step
                hops.append({"symbol_called": called, "path": p, "line_start": a, "line_end": b,
                             "quote": quote, "note": f"runs {called}"})
        return {"hops": hops}

    def fallback(self, prompt: str, cwd: str) -> dict:
        """A `FallbackAnswer`: the static scan missed nothing."""
        return {"entries": []}

    def write(self, prompt: str) -> SectionOutput:
        ids = re.findall(r"^- \[([a-z0-9]+)\]", prompt, re.M)
        paragraphs = [{"text": f"This part is explained by [[{ids[0]}]] and `Reservation`." if ids
                       else "Nothing passed the checks here."}]
        if self.invent_path:
            paragraphs.append({"text": "See engine/bookings/nowhere.py:99 for the retry."})
        questions = ["Who owns `poll_task`?"] if "questions" in prompt and "Gotchas" in prompt else []
        return SectionOutput.model_validate({"paragraphs": paragraphs, "questions": questions})
