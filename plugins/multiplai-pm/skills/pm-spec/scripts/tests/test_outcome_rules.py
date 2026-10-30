"""Plan items 52-55: the split names an owner for each shared rule, the specs
prompt asks for stated outcomes, the consistency check may read the function a
statement about today's behaviour names, and each check writes advisory counts
to the progress file."""

from __future__ import annotations

from conftest import TWO, change_dir, run
from pm_spec_pipeline import counts


def _progress(repo) -> str:
    return (change_dir(repo) / ".pm-spec" / "progress.md").read_text()


def test_the_split_prompt_names_one_owner_for_each_shared_rule(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("components")[0]
    assert "One owner for each shared rule" in prompt
    assert "lists the owner in `depends_on`" in prompt


def test_the_specs_prompt_asks_for_stated_outcomes(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.prompts("spec:specs")[0]
    for rule in (
        "A refusal states its HTTP status, or the error the caller sees, and its message",
        "says how that baseline is\n  captured, and by whom",
        "A requirement that needs production data names who supplies it",
        "names the file and the function\n  that do it, with no line number and no commit",
        "Production row counts and ids do not go in specs.md",
        "Do not restate conventions the proposal already states",
    ):
        assert rule in prompt, rule


def test_the_consistency_check_may_read_the_function_a_statement_names(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("consistency")[0]
    assert (
        "read that function, and report a `contradiction` when\nthe code does something else"
        in prompt
    )


def test_count_text_counts_each_kind():
    lines = [
        "- **THEN** the request is rejected",
        "- **THEN** the request is rejected with 403",
        '- **THEN** the request is refused with "Not allowed"',
        "The SHALL line refuses nothing a test reads",
        "- **THEN** the feed is unchanged",
        "- **THEN** the feed is unchanged from the baseline captured by QA",
        "As today, the login maps to its company.",
        "As today, `resolve_company()` maps the login.",
        "Today's check lives in `auth_utils.py`.",
        "Pinned at commit 845ff594 and at deadbeef12.",
    ]
    text = "\n".join(lines)
    assert counts.count_text(text) == {
        "refusal_no_status": 1,
        "as_before_no_baseline": 1,
        "today_no_function": 1,
        "commit_pins": 2,
    }


def test_each_check_writes_one_advisory_counts_line(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    text = _progress(repo)
    assert text.count("## SPEC_COUNTS") == 1
    line = text.split("## SPEC_COUNTS", 1)[1].splitlines()[2]
    assert line.startswith("Advisory: refusals with no status: ")
    assert "commit pins: " in line
    assert "SPEC:READY" not in line
