"""Every prompt that carries text a person outside this session wrote (a PRD,
a ticket, replies, or documents written from them) carries the untrusted-content
notice."""

from __future__ import annotations

import importlib
import pkgutil

import pm_spec_pipeline.prompts as prompts_pkg
from conftest import change_dir, run, write
from pm_spec_pipeline.prompts import UNTRUSTED_NOTICE
from test_prototype import NOTES_FINDINGS, agent, ui_proposal


def templates():
    for info in pkgutil.iter_modules(prompts_pkg.__path__):
        module = importlib.import_module(f"{prompts_pkg.__name__}.{info.name}")
        for name, value in vars(module).items():
            if name.endswith("_PROMPT") and isinstance(value, str):
                yield f"{info.name}.{name}", value


def test_every_prompt_template_has_the_notice():
    found = dict(templates())
    assert len(found) >= 10, sorted(found)
    assert [name for name, text in found.items() if "{notice}" not in text] == []


def test_every_call_that_carries_fenced_text_carries_the_notice(repo, model, tmp_path):
    """A full run with a PRD, a ticket, a prototype and a consistency rewrite."""
    from conftest import PRD
    from test_consistency import finding, recheck
    ticket = write(tmp_path / "t.md", "Throttle the API.\n")
    prd = write(tmp_path / "prd.md", PRD)
    model["spec:proposal"] = lambda prompt, label, kw: ui_proposal() + (
        '\n## Sources\n\n- `rate-limit`: PRD "Limit requests per client"\n'
        '- `retry-policy`: PRD "Retry guidance"\n')
    model["prototype"] = agent(NOTES_FINDINGS)
    model["prototype:rewrite:proposal"] = model.handlers["spec:proposal"]
    model["consistency"] = [{"findings": [finding()]}, recheck()]
    assert run(repo, "new", "throttle", "--auto", "--prd", str(prd), "--ticket-file", str(ticket),
               "--ticket-ref", "EPIC-9") == 0
    labels = {label.split(":")[0] for label in model.labels()}
    assert {"interview", "epic_split", "spec", "prototype", "consistency"} <= labels
    for label, prompt, _ in model.calls:
        if label.endswith(":reformat"):
            continue
        assert UNTRUSTED_NOTICE in prompt, label
    assert (change_dir(repo) / "tickets.json").is_file()
