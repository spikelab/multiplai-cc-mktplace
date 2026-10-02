"""Fake agents: answer each stage's prompt from the snapshot the agent would read.

No model is called anywhere in the tests. Each answer is built by reading the
files the prompt names, so the citations it returns are real and the gates
pass them; the options plant the failures the gates must catch.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from walkthrough_pipeline import budget
from walkthrough_pipeline.models import DocsOutput, ExploreOutput, SectionOutput, TraceOutput


def _line(cwd: str, path: str, n: int) -> str:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return lines[n - 1].strip()


def _first_code_line(cwd: str, path: str) -> int:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return next((i for i, l in enumerate(lines, start=1) if l.strip()), 1)


def _find(cwd: str, path: str, needle: str) -> int:
    lines = (Path(cwd) / path).read_text(encoding="utf-8").split("\n")
    return next(i for i, l in enumerate(lines, start=1) if needle in l)


class FakeAgents:
    def __init__(self, *, cost: float = 0.0, broken_trace: bool = False, invent_path: bool = False,
                 fail_on: str | None = None):
        self.calls: list[dict] = []
        self.cost = cost
        self.broken_trace = broken_trace
        self.invent_path = invent_path
        self.fail_on = fail_on

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
        if schema is TraceOutput:
            return self.trace(prompt, cwd)
        if schema is SectionOutput:
            return self.write(prompt)
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

    def trace(self, prompt: str, cwd: str) -> TraceOutput:
        m = re.search(r"entry point \(found by code\):\n(\S+?):(\d+) — ", prompt)
        path, line = m.group(1), int(m.group(2))
        hops = [{"symbol_called": "entry", "path": path, "line_start": line, "line_end": line,
                 "quote": _line(cwd, path, line), "note": "entry"}]
        if path.endswith("bookings/urls.py") and "hooks/acme/" in _line(cwd, path, line):
            views = "engine/bookings/views.py"
            a, b = _find(cwd, views, "def webhook"), _find(cwd, views, "return ack_booking")
            hops.append({"symbol_called": "views.webhook", "path": views, "line_start": a, "line_end": b,
                         "quote": "return ack_booking(booking_id)", "note": "the view"})
            client = "engine/bookings/client.py"
            a, b = _find(cwd, client, "def ack_booking"), _find(cwd, client, "httpx.post")
            name = "missing_function" if self.broken_trace else "ack_booking"
            hops.append({"symbol_called": name, "path": client, "line_start": a, "line_end": b,
                         "quote": "return httpx.post(", "note": "acknowledges the booking"})
        return TraceOutput.model_validate({"title": "A webhook arrives", "hops": hops})

    def write(self, prompt: str) -> SectionOutput:
        ids = re.findall(r"^- \[([a-z0-9]+)\]", prompt, re.M)
        paragraphs = [{"text": f"This part is explained by [[{ids[0]}]] and `Reservation`." if ids
                       else "Nothing passed the checks here."}]
        if self.invent_path:
            paragraphs.append({"text": "See engine/bookings/nowhere.py:99 for the retry."})
        questions = ["Who owns `poll_task`?"] if "questions" in prompt and "Gotchas" in prompt else []
        return SectionOutput.model_validate({"paragraphs": paragraphs, "questions": questions})
