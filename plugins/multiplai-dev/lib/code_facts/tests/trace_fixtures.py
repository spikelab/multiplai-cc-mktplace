"""Snapshots, hop builders and a recording CallFn for the trace and gate tests.

Every snapshot is committed to a temp git repository, so citations re-read
through `git show` exactly as in production. No test calls a model.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import _git, line_of, make_tree

# Distinctive subcommand names: a prompt naming one of them names that entry and no other.
NAMES = ["opal", "ruby", "jade", "onyx", "topaz", "amber"]
DEPTH = 4  # cmd_<n> -> step1_<n> -> step2_<n> -> step3_<n>


def lib(name: str) -> Any:
    """A code_facts name from whichever module defines it, looked up at call time."""
    for mod in (
        "code_facts",
        "code_facts.trace_gate",
        "code_facts.paths",
        "code_facts.prompts",
        "code_facts.entries",
        "code_facts.pyindex",
        "code_facts.commands",
        "code_facts.fallback",
        "code_facts.tracing",
        "code_facts.scope",
    ):
        try:
            module = importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AssertionError(f"code_facts does not define {name!r}")


# --- snapshot sources --------------------------------------------------------------------


def cli_source(
    names: list[str], *, prelude: str = "", dispatch: str = "set_defaults"
) -> str:
    """An argparse CLI: one subcommand per name, each `cmd_<n>` calling a chain of `step<k>_<n>`."""
    out = ["import argparse", "", prelude]
    for n in names:
        out += ["", f"def cmd_{n}(args):", f"    return step1_{n}(args)", ""]
        for k in range(1, DEPTH):
            body = (
                f"    return step{k + 1}_{n}(args)"
                if k < DEPTH - 1
                else f"    print('done {n}')\n    return 0"
            )
            out += ["", f"def step{k}_{n}(args):", body, ""]
    out += [
        "",
        "def main(argv=None):",
        "    parser = argparse.ArgumentParser(prog='tool')",
        "    sub = parser.add_subparsers(dest='command', required=True)",
    ]
    for n in names:
        out.append(f"    p_{n} = sub.add_parser({n!r})")
        if dispatch == "set_defaults":
            out.append(f"    p_{n}.set_defaults(func=cmd_{n})")
    out.append("    args = parser.parse_args(argv)")
    if dispatch == "set_defaults":
        out.append("    return args.func(args)")
    else:
        for n in names:
            out += [f"    if args.command == {n!r}:", f"        return cmd_{n}(args)"]
        out.append("    return 1")
    return "\n".join(out) + "\n"


CHAIN_LEN = 25


def chain_source(length: int = CHAIN_LEN) -> str:
    """`f1` calls `f2` … calls `f<length>`: a connected chain longer than the hop limit."""
    out = []
    for k in range(1, length + 1):
        body = f"    return f{k + 1}()" if k < length else "    return 0"
        out += [f"def f{k}():", body, "", ""]
    return "\n".join(out)


@dataclass
class Snapshot:
    root: Path
    head: str
    files: dict[str, str]

    def line(self, path: str, needle: str, nth: int = 1) -> int:
        return line_of(self.files[path], needle, nth)

    def resolve(self, path: str, side: str):
        return str(self.root), self.head, path

    def hop(
        self,
        path: str,
        symbol: str,
        *,
        quote: str | None = None,
        side: str = "head",
        called: str | None = None,
        line: int | None = None,
        end: int | None = None,
    ) -> dict:
        """A hop citing the `def <symbol>(` line of `path` (or `line`), quoting it."""
        if line is None:
            line = self.line(path, f"def {symbol}(")
        text = (
            self.files[path].splitlines()[line - 1].strip()
            if line <= len(self.files[path].splitlines())
            else ""
        )
        return {
            "symbol_called": called if called is not None else symbol,
            "path": path,
            "line_start": line,
            "line_end": end if end is not None else line,
            "quote": quote if quote is not None else text,
            "note": "",
            "side": side,
        }


def commit_snapshot(root: Path, files: dict[str, str]) -> Snapshot:
    make_tree(root, files)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "snapshot")
    return Snapshot(root=root, head=_git(root, "rev-parse", "HEAD"), files=dict(files))


def cli_snapshot(root: Path, names: list[str] = NAMES, **kw: Any) -> Snapshot:
    files = {
        "pkg/__init__.py": "",
        "pkg/cli.py": cli_source(names, **kw),
        "pkg/other.py": "def elsewhere(args):\n    return 0\n",
    }
    return commit_snapshot(root, files)


CLI = "pkg/cli.py"


def chain_hops(snap: Snapshot, name: str, count: int = DEPTH) -> list[dict]:
    """The first `count` hops of entry `name`: `cmd_<name>`, then `step1_<name>` …"""
    symbols = [f"cmd_{name}"] + [f"step{k}_{name}" for k in range(1, DEPTH)]
    return [snap.hop(CLI, s) for s in symbols[:count]]


# --- the recording CallFn -----------------------------------------------------------------


@dataclass
class Call:
    stage: str
    entry: str | None
    prompt: str
    schema: Any
    kwargs: dict


Answer = Any  # a TraceAnswer, a dict, a JSON string, or an exception to raise
Responder = Callable[[str | None], Answer]


@dataclass
class FakeCall:
    """A CallFn shaped like `agent_call_structured(prompt, schema, **kwargs)`.

    It routes by the `budget_label` (`paths:trace:` / `paths:reask:` prefixes, `paths:fallback`),
    names the entry a prompt is about from the subcommand name in it, records
    every call and the most calls ever in flight, and returns (or raises) what
    the responder gives for that entry.
    """

    trace: Responder
    reask: Responder | None = None
    fallback: Responder | None = None
    delay: Callable[[], float] | None = None
    names: list[str] = field(default_factory=lambda: list(NAMES))
    calls: list[Call] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    def _entry(self, prompt: str) -> str | None:
        found = [n for n in self.names if n in prompt]
        return found[0] if len(found) == 1 else None

    async def __call__(self, prompt: str, schema: Any, **kwargs: Any) -> Any:
        label = kwargs.get("budget_label", "")
        stage = (
            "trace"
            if label.startswith("paths:trace:")
            else "reask"
            if label.startswith("paths:reask:")
            else label
        )
        entry = self._entry(prompt)
        self.calls.append(Call(stage, entry, prompt, schema, dict(kwargs)))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay() if self.delay else 0)
            if stage == "trace":
                out = self.trace(entry)
            elif stage == "reask" and self.reask is not None:
                out = self.reask(entry)
            elif stage == "paths:fallback" and self.fallback is not None:
                out = self.fallback(entry)
            else:
                raise AssertionError(f"unexpected call with budget_label {label!r}")
            if isinstance(out, BaseException):
                raise out
            return out
        finally:
            self.in_flight -= 1

    def of(self, entry: str, stage: str | None = None) -> list[Call]:
        return [
            c
            for c in self.calls
            if c.entry == entry and (stage is None or c.stage == stage)
        ]


def as_json(hops: list[dict]) -> str:
    return json.dumps({"hops": hops})


def run_map(snap: Snapshot, call: FakeCall, **kw: Any):
    kw.setdefault("fallback", False)
    return asyncio.run(
        lib("map_paths")(
            root=snap.root, commit=snap.head, resolve=snap.resolve, call=call, **kw
        )
    )
