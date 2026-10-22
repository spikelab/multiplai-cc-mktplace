"""The human-mode explainer: its eight sections, and the gates on its use cases, flow, outputs and settings.

Every planted failure comes from `FakeAgents(plant=...)`; no model is called.
"""

from __future__ import annotations

import json
import re

import fixture_repo
from fakes import FakeAgents
from test_pipeline import run_args
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.gates import (
    Corpus,
    example_words,
    explainer_text_gate,
    payload_gate,
)
from walkthrough_pipeline.models import Citation, Payload
from walkthrough_pipeline.render import markdown_headings, section_texts
from walkthrough_pipeline.state import STATE_FILE

H2 = ["What it is", "Use cases", "How it works", "What it creates", "Configuration", "Gotchas", "Reference",
      "How this was made"]
REFERENCE = ["Paths", "Module tour", "Who calls it and what it calls", "Data stores", "Vendor docs", "Glossary"]


def _run(ws, tmp_path, monkeypatch, **fake):
    agents = FakeAgents(**fake)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path)) == 0
    md = (tmp_path / "out" / "bookings-walkthrough.md").read_text()
    return md, next((tmp_path / "runs").iterdir()), agents


def _calls(agents: FakeAgents, schema: str) -> list[dict]:
    return [c for c in agents.calls if c["schema"] == schema]


def _cut(md: str) -> str:
    return section_texts(md, "Cut by the checks")[0]


def test_a_full_run_writes_the_eight_sections_and_reference_holds_the_six(done_run):
    md_path, _, _ = done_run
    md = md_path.read_text()
    assert markdown_headings(md) == H2
    reference = section_texts(md, "Reference")[0]
    assert markdown_headings(reference, 3)[:6] == REFERENCE
    made = section_texts(md, "How this was made")[0]
    assert markdown_headings(made, 3) == ["Commits read", "Coverage", "Cut by the checks"]
    assert md.count("## What it is") == 1 and md.count("Every citation in this file was checked") == 1


def test_the_top_has_the_tagline_and_one_run_line(done_run):
    md_path, _, _ = done_run
    top = md_path.read_text().split("\n## ", 1)[0]
    assert top.splitlines()[0] == "# bookings — codebase walkthrough"
    assert "Bookings takes reservations from Acme." in top
    assert len(re.findall(r"^> Generated ", top, re.MULTILINE)) == 1
    assert "## Commits read" not in top and "Module directory" not in top


def test_a_use_case_citing_an_unknown_flag_is_cut_and_counted(ws, tmp_path, monkeypatch, fake_fetch):
    md, _, agents = _run(ws, tmp_path, monkeypatch, plant={"usecase_flag"})
    usecases = section_texts(md, "Use cases")[0]
    assert markdown_headings(usecases, 3) == ["Poll for new bookings", "Receive a webhook"]
    assert "--no-such-flag" not in usecases
    assert "`--no-such-flag` in the example is in no cited quote and no snapshot" in _cut(md)
    assert len(_calls(agents, "UseCasesOutput")) == 2                 # one re-ask, which plants it again
    cut_row = re.search(r"\| Paragraphs and items cut by the write checks \| (\d+) \|", md)
    assert int(cut_row.group(1)) >= 1


def test_a_use_case_example_is_in_a_fenced_block(done_run):
    md_path, _, _ = done_run
    usecases = section_texts(md_path.read_text(), "Use cases")[0]
    assert "```bash\nbookings poll\n```" in usecases
    assert '```python\nReservation(guest="Ada")\n```' in usecases


def test_a_flow_box_with_an_unknown_cite_is_dropped(ws, tmp_path, monkeypatch, fake_fetch):
    md, _, _ = _run(ws, tmp_path, monkeypatch, plant={"flow_node"})
    flow = section_texts(md, "How it works")[0]
    diagram = re.search(r"```mermaid\n(.*?)```", flow, re.DOTALL).group(1)
    assert diagram.startswith("flowchart TD") and "Acme sends a booking" in diagram
    assert "A box with no source" not in diagram
    assert "[[c999]] is not a gated citation" in _cut(md)


def test_a_flow_that_is_not_connected_fails_its_gate_after_one_reask(ws, tmp_path, monkeypatch, fake_fetch):
    md, _, agents = _run(ws, tmp_path, monkeypatch, plant={"flow_disconnected"})
    assert len(_calls(agents, "FlowOutput")) == 2
    assert "failed these checks" in _calls(agents, "FlowOutput")[1]["prompt"]
    assert "the diagram is not connected" in _calls(agents, "FlowOutput")[1]["prompt"]
    flow = section_texts(md, "How it works")[0]
    assert "```mermaid" not in flow
    assert markdown_headings(flow, 3) == ["1. Receive", "2. Store"]          # the steps are kept
    assert "the diagram is not connected" in _cut(md)


def test_a_connected_flow_is_asked_once(done_run):
    _, _, agents = done_run
    assert len(_calls(agents, "FlowOutput")) == 1


def test_a_json_example_with_a_key_absent_from_the_corpus_loses_its_example(ws, tmp_path, monkeypatch, fake_fetch):
    md, _, _ = _run(ws, tmp_path, monkeypatch, plant={"output_key"})
    outputs = section_texts(md, "What it creates")[0]
    assert markdown_headings(outputs, 3) == ["Reservation row", "Acknowledgement", "Poll log"]
    row = section_texts(md, "Reservation row")[0]
    assert "```json" not in row and "zz_not_in_the_code" not in row
    assert "key `zz_not_in_the_code` is in no cited quote and no snapshot" in _cut(md)


def test_a_json_example_whose_keys_are_in_the_code_is_kept(done_run):
    md_path, _, _ = done_run
    row = section_texts(md_path.read_text(), "Reservation row")[0]
    assert '```json\n{"guest": "Ada", "ospite_count": 2}\n```' in row


def test_the_configuration_table_lists_a_setting_the_code_reads(done_run):
    md_path, _, _ = done_run
    config = section_texts(md_path.read_text(), "Configuration")[0]
    assert re.search(r"^\| `ACME_ENV` \| Picks the staging or production key\. \[`engine/", config, re.MULTILINE)
    assert "| `production` |" in config


def test_commits_read_lists_only_repositories_with_references(fresh_ws, tmp_path, monkeypatch, fake_fetch):
    """A searched repository with no reference is counted, not listed; a name inside a quoted string is
    listed under "Mentions in strings", apart from the callers."""
    fixture_repo.make_repo(fresh_ws["ws"] / "lonely", {"README.md": "Nothing here names the module.\n"})
    (fresh_ws["engine"] / "reports" / "labels.py").write_text('LABEL = "bookings.models"\n')
    fixture_repo.commit(fresh_ws["engine"], "a dotted string")
    md, _, _ = _run(fresh_ws, tmp_path, monkeypatch)
    commits = section_texts(md, "Commits read")[0]
    assert re.findall(r"^\| (\w+) \| .* \| `[0-9a-f]{40}` \|$", commits, re.MULTILINE) == ["engine", "front", "warehouse"]
    assert "Searched 4 repositories under `" in commits and "; 3 had references." in commits
    who = section_texts(md, "Who calls it and what it calls")[0]
    calls, _, strings = who.partition("#### Mentions in strings (not calls)")
    assert "| engine | dotted-string | `bookings.models` |" in strings.split("#### What it calls")[0]
    assert "dotted-string" not in calls
    assert main(["check", str(tmp_path / "out" / "bookings-walkthrough.md")]) == 0


def test_the_explainer_prompts_carry_the_explainer_rules(done_run):
    _, _, agents = done_run
    for schema in ("SummaryOutput", "UseCasesOutput", "FlowOutput", "OutputsOutput", "ConfigOutput"):
        prompt = _calls(agents, schema)[0]["prompt"]
        assert "at the END of the sentence it supports" in prompt and "Never type code blocks" in prompt
    gotchas = next(c["prompt"] for c in _calls(agents, "SectionOutput") if '"Gotchas"' in c["prompt"])
    assert "at the END of the sentence it supports" in gotchas
    tour = next(c["prompt"] for c in _calls(agents, "SectionOutput") if "Explain these files" in c["prompt"])
    assert "at the END of the sentence" not in tour                   # Reference keeps WRITE_RULES


def test_the_state_keeps_each_structured_answer_as_it_passed(done_run):
    _, run_dir, _ = done_run
    sections = json.loads((run_dir / STATE_FILE).read_text())["sections"]
    assert [c["title"] for c in sections["usecases"]["data"]["cases"]] == ["Poll for new bookings",
                                                                          "Receive a webhook"]
    assert [n["label"] for n in sections["flow"]["data"]["nodes"]] == [
        "Acme sends a booking", "The view stores it", "The task acknowledges it"]


# --- the gates on their own ------------------------------------------------------------------------


class _Corpus(Corpus):
    def __init__(self, words: set[str]):
        self.words = words

    def contains(self, text: str, *, ignore_case: bool = False) -> bool:
        return text in self.words


def test_example_words_takes_flags_and_command_words_but_not_placeholders_or_files():
    words = example_words("command", "WALKTHROUGH_TRUST_REPO=1 python -m build_pipeline build <plan file> "
                                     "--auto --max-usd 25 plan.md 'a quoted value'")
    assert words == ["WALKTHROUGH_TRUST_REPO", "python", "-m", "build_pipeline", "build", "--auto", "--max-usd"]
    assert example_words("call", "run_build(spec, dry_run=True)") == ["run_build", "dry_run"]
    assert example_words("file", "max_rounds: 3\nfindings:\n  - allow: x\n") == ["max_rounds", "findings", "allow"]


def test_the_explainer_text_gate_bans_a_typed_path_with_a_line_even_when_it_is_gated():
    cit = {"c1": Citation(path="engine/a.py", line_start=3, line_end=3, quote="x = 1", id="c1")}
    corpus = _Corpus(set())
    assert explainer_text_gate("It sets x. [[c1]]", citations=cit, corpus=corpus, labels=set()) == []
    reasons = explainer_text_gate("See engine/a.py:3 for it.", citations=cit, corpus=corpus, labels=set())
    assert reasons == ["engine/a.py:3 is a path with a line number typed in the text"]
    assert explainer_text_gate("```\nx\n```", citations=cit, corpus=corpus, labels=set())


def test_payload_gate_checks_yaml_keys_and_drops_what_does_not_parse():
    corpus = _Corpus({"max_rounds", "findings"})
    assert payload_gate(Payload(format="yaml", text="max_rounds: 3\nfindings: {}\n"), "", corpus) == []
    assert payload_gate(Payload(format="yaml", text="nope: 1\n"), "", corpus) == [
        "key `nope` is in no cited quote and no snapshot"]
    assert payload_gate(Payload(format="yaml", text="nope: 1\n"), "the quote names nope", corpus) == []
    assert payload_gate(Payload(format="json", text="{not json"), "", corpus) == ["the json does not parse: "
                                                                                    "JSONDecodeError"]
