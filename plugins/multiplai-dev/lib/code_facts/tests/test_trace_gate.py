"""`gate_hops`: the pure, model-free check of a trace answer, hop by hop, in the fixed rule order."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from trace_fixtures import (
    CHAIN_LEN,
    CLI,
    PR_COMMANDS,
    PR_GITHUB,
    PR_MAIN,
    PR_SKILL,
    chain_hops,
    chain_source,
    cli_source,
    commit_snapshot,
    lib,
    pr_edit_hops,
    pr_snapshot,
)

DOC = "docs/run.md"
DOC_TEXT = "# Run\n\nRun python -m pkg opal --remain now.\n\nThen main takes over.\n"
CHAIN = "pkg/chain.py"
NESTED = "repo_b/pkg/mod.py"
EXTRA = """

def cmd_note(args):
    # load_config would be called here
    return 0


def cmd_text(args):
    label = "load_config"
    return label


def cmd_real(args):
    return load_config()


def load_config():
    return {}


def ping(n):
    return pong(n)


def pong(n):
    return ping(n - 1)
"""


@pytest.fixture
def snap(tmp_path):
    (tmp_path / "outside.py").write_text("def outside():\n    return 0\n")
    return commit_snapshot(
        tmp_path / "repo",
        {
            "pkg/__init__.py": "",
            CLI: cli_source(["opal"], prelude=EXTRA),
            "pkg/other.py": "def elsewhere(args):\n    return 0\n",
            DOC: DOC_TEXT,
            CHAIN: chain_source(),
            NESTED: "def run():\n    return helper()\n\n\ndef helper():\n    return 0\n",
        },
    )


def entry(
    kind="argparse_subcommand", name="opal", path=CLI, line=None, handler=None, edges=()
):
    Entry, Location, Handler = lib("Entry"), lib("Location"), lib("Handler")
    return Entry(
        kind=kind,
        name=name,
        location=Location(path=path, line=line),
        handler=Handler(**handler) if handler else None,
        invocation=None,
        invoked_by=f"a person or a script running {name}",
        known_edges=list(edges),
        also_invoked_by=[],
    )


def opal_entry(snap):
    return entry(
        line=snap.line(CLI, "add_parser('opal')"),
        handler={
            "path": CLI,
            "line": snap.line(CLI, "def cmd_opal("),
            "symbol": "cmd_opal",
        },
    )


def seed(snap, path, symbol):
    line = snap.line(path, f"def {symbol}(")
    return entry(
        kind="seed",
        name=f"{path}:{symbol}",
        path=path,
        line=line,
        handler={"path": path, "line": line, "symbol": symbol},
    )


def gate(snap, hops, ent, *, edges=(), places=()):
    TraceHop = lib("TraceHop")
    return lib("gate_hops")(
        [TraceHop.model_validate(h) for h in hops],
        root=snap.root,
        resolve=snap.resolve,
        entry=ent,
        known_edges=list(edges),
        places=list(places),
    )


def outcome(result):
    return result.broken_at, result.break_reason, len(result.accepted)


# --- accepted traces -------------------------------------------------------------------------


def test_valid_chain_is_accepted_whole_as_head_side_path_hops(snap):
    hops = chain_hops(snap, "opal")
    result = gate(snap, hops, opal_entry(snap))
    assert outcome(result) == (None, None, 4)
    assert result.accepted == [lib("PathHop")(**h) for h in hops]
    assert all(h.side == "head" for h in result.accepted)


def test_gate_is_deterministic(snap):
    hops = chain_hops(snap, "opal")[:2] + [snap.hop(CLI, "step3_opal", quote="wrong")]
    assert gate(snap, hops, opal_entry(snap)) == gate(snap, hops, opal_entry(snap))


def test_empty_hop_list_breaks_at_hop_1_as_empty_trace(snap):
    assert outcome(gate(snap, [], opal_entry(snap)))[:2] == (1, "empty_trace")


def test_nested_repository_path_is_accepted(snap):
    hops = [snap.hop(NESTED, "run"), snap.hop(NESTED, "helper")]
    assert outcome(gate(snap, hops, seed(snap, NESTED, "run"))) == (None, None, 2)


# --- hop 1 anchoring ---------------------------------------------------------------------------


def test_hop_1_at_the_add_parser_line_passes(snap):
    ent = opal_entry(snap)
    first = snap.hop(CLI, "main", line=ent.location.line)
    second = snap.hop(CLI, "cmd_opal")
    assert outcome(gate(snap, [first, second], ent)) == (None, None, 2)


def test_hop_1_range_covering_the_entry_line_passes(snap):
    ent = opal_entry(snap)
    line = ent.location.line
    first = snap.hop(
        CLI, "main", line=line - 1, end=line + 1, quote="sub.add_parser('opal')"
    )
    assert outcome(gate(snap, [first], ent)) == (None, None, 1)


def test_hop_1_in_another_file_is_entry_mismatch(snap):
    result = gate(snap, [snap.hop("pkg/other.py", "elsewhere")], opal_entry(snap))
    assert outcome(result) == (1, "entry_mismatch", 0)


def test_hop_1_in_the_handler_file_at_another_line_is_entry_mismatch(snap):
    result = gate(snap, [snap.hop(CLI, "step2_opal")], opal_entry(snap))
    assert outcome(result) == (1, "entry_mismatch", 0)


def test_entry_mismatch_applies_only_to_hop_1(snap):
    # hop 2 lies far from the entry and handler lines and is still accepted.
    assert outcome(gate(snap, chain_hops(snap, "opal", 2), opal_entry(snap))) == (
        None,
        None,
        2,
    )


# --- citation rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside.py", "pkg/../../outside.py", "pkg\\cli.py"]
)
def test_paths_leaving_the_root_are_path_outside_root(snap, path):
    bad = {
        **snap.hop(CLI, "step1_opal"),
        "path": path,
        "line_start": 1,
        "line_end": 1,
        "quote": "def outside():",
    }
    result = gate(snap, [chain_hops(snap, "opal")[0], bad], opal_entry(snap))
    assert outcome(result) == (2, "path_outside_root", 1)


def test_missing_file(snap):
    bad = {**snap.hop(CLI, "step1_opal"), "path": "pkg/gone.py"}
    assert outcome(
        gate(snap, [chain_hops(snap, "opal")[0], bad], opal_entry(snap))
    ) == (2, "missing_file", 1)


def test_line_past_the_end_of_file_is_line_out_of_range(snap):
    bad = snap.hop("pkg/other.py", "elsewhere", line=500, quote="def elsewhere(args):")
    result = gate(snap, [chain_hops(snap, "opal")[0], bad], opal_entry(snap))
    assert outcome(result) == (2, "line_out_of_range", 1)


def test_quote_not_at_the_cited_lines_is_text_mismatch(snap):
    bad = snap.hop(CLI, "step1_opal", quote="def step1_opal(argv, extra):")
    result = gate(snap, [chain_hops(snap, "opal")[0], bad], opal_entry(snap))
    assert outcome(result) == (2, "text_mismatch", 1)


def test_base_side_citation_breaks_even_when_the_text_matches_at_head(snap):
    bad = snap.hop(CLI, "step1_opal", side="base")
    result = gate(snap, [chain_hops(snap, "opal")[0], bad], opal_entry(snap))
    assert outcome(result) == (2, "base_side_citation", 1)


def test_side_defaults_to_head(snap):
    hops = chain_hops(snap, "opal", 2)
    for h in hops:
        del h["side"]
    assert outcome(gate(snap, hops, opal_entry(snap))) == (None, None, 2)


# --- connectivity -------------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["cmd_note", "cmd_text"])
def test_name_only_in_a_comment_or_string_is_unconnected(snap, caller):
    hops = [snap.hop(CLI, caller), snap.hop(CLI, "load_config")]
    assert outcome(gate(snap, hops, seed(snap, CLI, caller))) == (
        2,
        "unconnected_hop",
        1,
    )


def test_name_called_anywhere_in_the_enclosing_function_connects(snap):
    # hop 1 cites only the `def` line; the call sits on the next line of the same function.
    hops = [snap.hop(CLI, "cmd_real"), snap.hop(CLI, "load_config")]
    assert outcome(gate(snap, hops, seed(snap, CLI, "cmd_real"))) == (None, None, 2)


def test_dotted_symbol_matches_on_its_last_component(snap):
    hops = [
        snap.hop(CLI, "cmd_real"),
        snap.hop(CLI, "load_config", called="pkg.cli.load_config"),
    ]
    assert outcome(gate(snap, hops, seed(snap, CLI, "cmd_real"))) == (None, None, 2)


def edge(from_line, to_line, path=CLI):
    Location = lib("Location")
    return lib("KnownEdge")(
        from_=Location(path=path, line=from_line), to=Location(path=path, line=to_line)
    )


def test_skipped_call_is_unconnected_unless_an_edge_or_place_covers_it(snap):
    ent = opal_entry(snap)
    hops = [
        snap.hop(CLI, "cmd_opal"),
        snap.hop(CLI, "step2_opal"),
    ]  # cmd_opal calls step1, not step2
    to = snap.line(CLI, "def step2_opal(")
    inside = snap.line(CLI, "return step1_opal(args)")
    outside = snap.line(CLI, "return args.func(args)")
    Location = lib("Location")
    assert outcome(gate(snap, hops, ent))[:2] == (2, "unconnected_hop")
    assert outcome(gate(snap, hops, ent, edges=[edge(inside, to)])) == (None, None, 2)
    assert outcome(gate(snap, hops, ent, edges=[edge(outside, to)]))[:2] == (
        2,
        "unconnected_hop",
    )
    assert outcome(gate(snap, hops, ent, places=[Location(path=CLI, line=to)])) == (
        None,
        None,
        2,
    )
    assert outcome(gate(snap, hops, ent, places=[Location(path=CLI, line=inside)]))[
        :2
    ] == (2, "unconnected_hop")


def test_place_does_not_excuse_a_bad_citation(snap):
    ent = opal_entry(snap)
    to = snap.line(CLI, "def step2_opal(")
    hops = [
        snap.hop(CLI, "cmd_opal"),
        snap.hop(CLI, "step2_opal", quote="nothing like this"),
    ]
    result = gate(snap, hops, ent, places=[lib("Location")(path=CLI, line=to)])
    assert outcome(result)[:2] == (2, "text_mismatch")


@pytest.mark.parametrize(
    "needle,expected",
    [("--remain", (2, "unconnected_hop", 1)), ("Then main", (None, None, 2))],
)
def test_non_python_hop_connects_by_whole_word_in_its_quote(snap, needle, expected):
    line = snap.line(DOC, needle)
    ent = entry(kind="skill_command", name="run: pkg opal", path=DOC, line=line)
    first = snap.hop(DOC, "pkg", line=line, called="pkg")
    second = snap.hop(CLI, "main", called="pkg.cli.main")
    assert outcome(gate(snap, [first, second], ent)) == expected


# --- argparse registrations and command lines -------------------------------------------------


@pytest.fixture
def pr(tmp_path):
    return pr_snapshot(tmp_path / "pr")


def discovered(snap, name):
    return next(e for e in lib("discover_entries")(snap.root).entries if e.name == name)


def test_pr_295_edit_trace_from_its_add_parser_line_keeps_every_hop(pr):
    # `build_parser` never names `cmd_edit`, and the entry's known edge starts at `commands.run`'s dispatch.
    ent = discovered(pr, "edit")
    hops = pr_edit_hops(pr)
    assert outcome(gate(pr, hops, ent, edges=ent.known_edges)) == (
        None,
        None,
        len(hops),
    )


def test_add_parser_line_connects_only_to_its_own_entrys_handler(pr):
    ent = discovered(pr, "edit")
    first = pr_edit_hops(pr)[0]
    wrong = pr.hop(PR_COMMANDS, "cmd_open")
    assert outcome(gate(pr, [first, wrong], ent, edges=ent.known_edges)) == (
        2,
        "unconnected_hop",
        1,
    )


def model_entry(snap, path, needle, name):
    """An entry as the model fallback adds one: no handler and no known edges."""
    return entry(kind="model", name=name, path=path, line=snap.line(path, needle))


SKILL_FIRST_LINE = "python -m pr_pipeline open"


@pytest.mark.parametrize("lines", [1, 2], ids=["continued-line", "whole-command"])
def test_skill_md_hop_1_connects_to_the_module_its_command_runs(pr, lines):
    ent = model_entry(pr, PR_SKILL, SKILL_FIRST_LINE, "pr: pr_pipeline open")
    line = ent.location.line
    quote = "\n".join(pr.files[PR_SKILL].splitlines()[line - 1 : line - 1 + lines])
    first = pr.hop(
        PR_SKILL, "pr_pipeline", line=line, end=line + lines - 1, quote=quote
    )
    second = pr.hop(PR_MAIN, "main")
    assert outcome(gate(pr, [first, second], ent)) == (None, None, 2)


def test_skill_md_hop_1_does_not_connect_to_a_file_its_command_does_not_run(pr):
    ent = model_entry(pr, PR_SKILL, SKILL_FIRST_LINE, "pr: pr_pipeline open")
    first = pr.hop(PR_SKILL, "pr_pipeline", line=ent.location.line)
    assert outcome(gate(pr, [first, pr.hop(PR_GITHUB, "run_write")], ent)) == (
        2,
        "unconnected_hop",
        1,
    )


def test_ci_step_hop_1_connects_to_the_script_its_run_line_names(tmp_path):
    ci = ".github/workflows/ci.yml"
    snap = commit_snapshot(
        tmp_path / "ci",
        {
            ci: "jobs:\n  check:\n    steps:\n      - run: python scripts/check.py --all\n",
            "scripts/check.py": "def main():\n    return 0\n",
        },
    )
    ent = model_entry(snap, ci, "run: python", "CI job check")
    first = snap.hop(ci, "check", line=ent.location.line)
    assert outcome(gate(snap, [first, snap.hop("scripts/check.py", "main")], ent)) == (
        None,
        None,
        2,
    )


# --- order and stopping ------------------------------------------------------------------------


def test_repeated_hop_after_a_cycle(snap):
    hops = [snap.hop(CLI, "ping"), snap.hop(CLI, "pong"), snap.hop(CLI, "ping")]
    assert outcome(gate(snap, hops, seed(snap, CLI, "ping"))) == (3, "repeated_hop", 2)


def test_first_failing_rule_wins_on_one_hop(snap):
    ent = opal_entry(snap)
    # step2 is unconnected from cmd_opal; at an out-of-range line it reports the range first.
    unconnected = snap.hop(CLI, "step2_opal")
    out_of_range = {**unconnected, "line_start": 9000, "line_end": 9000}
    assert outcome(gate(snap, [snap.hop(CLI, "cmd_opal"), unconnected], ent))[:2] == (
        2,
        "unconnected_hop",
    )
    assert outcome(gate(snap, [snap.hop(CLI, "cmd_opal"), out_of_range], ent))[:2] == (
        2,
        "line_out_of_range",
    )
    # Hop 1 in the wrong file with a wrong quote: the quote rule comes before anchoring.
    wrong = snap.hop("pkg/other.py", "elsewhere", quote="not in the file")
    assert outcome(gate(snap, [wrong], ent))[:2] == (1, "text_mismatch")
    # Outside the root and missing: the root rule comes first.
    gone = {**unconnected, "path": "../nope.py"}
    assert outcome(gate(snap, [snap.hop(CLI, "cmd_opal"), gone], ent))[:2] == (
        2,
        "path_outside_root",
    )


def test_later_broken_hops_are_ignored_after_the_first(snap):
    hops = [snap.hop(CHAIN, f"f{k}") for k in range(1, 8)]
    hops[2] = {**hops[2], "quote": "broken"}
    hops[5] = {**hops[5], "path": "pkg/gone.py"}
    result = gate(snap, hops, seed(snap, CHAIN, "f1"))
    assert outcome(result) == (3, "text_mismatch", 2)
    assert [h.symbol_called for h in result.accepted] == ["f1", "f2"]


# --- hop limit ---------------------------------------------------------------------------------


def chain(snap, count):
    return [snap.hop(CHAIN, f"f{k}") for k in range(1, count + 1)]


def test_exactly_twenty_hops_is_not_cut(snap):
    assert outcome(gate(snap, chain(snap, 20), seed(snap, CHAIN, "f1"))) == (
        None,
        None,
        20,
    )


@pytest.mark.parametrize("count", [21, CHAIN_LEN])
def test_more_than_twenty_hops_keeps_the_first_twenty_as_hop_limit(snap, count):
    result = gate(snap, chain(snap, count), seed(snap, CHAIN, "f1"))
    assert result.break_reason == "hop_limit"
    assert [h.symbol_called for h in result.accepted] == [f"f{k}" for k in range(1, 21)]


def test_hops_past_twenty_are_never_checked(snap):
    hops = chain(snap, CHAIN_LEN)
    for h in hops[20:]:
        h["path"] = "pkg/gone.py"
    result = gate(snap, hops, seed(snap, CHAIN, "f1"))
    assert (result.break_reason, len(result.accepted)) == ("hop_limit", 20)


def test_a_break_within_the_first_twenty_wins_over_the_hop_limit(snap):
    hops = chain(snap, CHAIN_LEN)
    hops[6] = {**hops[6], "quote": "broken"}
    assert outcome(gate(snap, hops, seed(snap, CHAIN, "f1"))) == (7, "text_mismatch", 6)


# --- the answer models ------------------------------------------------------------------------


def test_trace_hop_has_the_path_hop_fields_and_a_two_valued_side():
    TraceHop = lib("TraceHop")
    assert list(TraceHop.model_fields) == list(lib("PathHop").model_fields)
    base = {
        "symbol_called": "f",
        "path": "a.py",
        "line_start": 1,
        "line_end": 1,
        "quote": "x",
    }
    assert TraceHop.model_validate(base).side == "head"
    assert TraceHop.model_validate({**base, "side": "base"}).side == "base"
    with pytest.raises(ValidationError):
        TraceHop.model_validate({**base, "side": "left"})


@pytest.mark.parametrize(
    "answer",
    [
        {"hops": [], "alternatives": [[]]},
        {
            "hops": [
                {
                    "symbol_called": "f",
                    "path": "a.py",
                    "line_start": 1,
                    "line_end": 1,
                    "quote": "x",
                    "hops": [],
                }
            ]
        },
    ],
)
def test_branching_answers_fail_validation(answer):
    with pytest.raises(ValidationError):
        lib("TraceAnswer").model_validate(answer)


def test_break_codes_are_the_fixed_set():
    assert lib("BREAK_CODES") == (
        "malformed_answer",
        "empty_trace",
        "path_outside_root",
        "missing_file",
        "line_out_of_range",
        "text_mismatch",
        "base_side_citation",
        "entry_mismatch",
        "unconnected_hop",
        "repeated_hop",
        "hop_limit",
    )
