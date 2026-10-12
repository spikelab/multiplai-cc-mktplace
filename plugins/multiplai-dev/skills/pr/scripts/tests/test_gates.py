from __future__ import annotations

import pytest
from conftest import make_change, make_repo

from pr_pipeline import commands, gates, markers
from pr_pipeline.template import parse

TEMPLATE = "## Summary\n<!-- what -->\n\n## Checklist\n- [ ] Tests pass\n- [ ] Docs\n"


def _repo(tmp_path, text=TEMPLATE):
    repo = make_repo(tmp_path / "proj")
    (repo / "pull_request_template.md").write_text(text)
    return repo


def _render(repo, **kw):
    return commands.render_body(repo, make_change(repo), base="main", head="feat/x", ticket_url=None,
                                section_files=kw.get("section_files", []))


def test_headings_and_checkboxes_are_kept(tmp_path, model_stub):
    repo = _repo(tmp_path)
    model_stub.answers = [{"sections": {"Summary": "The change."}}]
    body = _render(repo).body
    sections = parse(TEMPLATE)
    assert gates.check_headings(sections, body) == []
    assert gates.check_checkboxes(sections, body, set()) == []
    assert body.index("## Summary") < body.index("## Checklist")


def test_heading_gate_catches_a_duplicate_and_an_out_of_order_heading():
    sections = parse("## A\n## B\n")
    assert "appears 2 times" in gates.check_headings(sections, "## A\n## A\n## B\n")[0]
    assert "out of template order" in gates.check_headings(sections, "## B\n## A\n")[0]
    assert "appears 0 times" in gates.check_headings(sections, "## A\n")[0]


REPEATED = "## Frontend\n<!-- ui -->\n### Details\n<!-- d -->\n## Backend\n<!-- api -->\n### Details\n<!-- d -->\n"


def test_a_heading_the_template_repeats_passes_when_the_body_repeats_it_in_order():
    sections = parse(REPEATED)
    assert gates.check_headings(sections, REPEATED) == []
    errors = gates.check_headings(sections, "## Frontend\n### Details\n## Backend\n")
    assert errors == ["template heading '### Details' appears 1 times, expected 2"]
    assert "out of template order" in gates.check_headings(
        sections, "### Details\n## Frontend\n### Details\n## Backend\n")[0]


def test_a_template_that_repeats_a_subheading_renders(tmp_path, model_stub):
    repo = _repo(tmp_path, REPEATED)
    model_stub.answers = [{"sections": {"frontend": "UI.", "details": "UI detail.",
                                        "backend": "API.", "details-2": "API detail."}}]
    body = _render(repo).body
    assert body.count("### Details") == 2
    assert body.index("UI detail.") < body.index("## Backend") < body.index("API detail.")


def test_checkbox_gate_catches_a_missing_line():
    sections = parse("## C\n- [ ] Tests pass\n")
    assert gates.check_checkboxes(sections, "## C\n", set()) == ["template checkbox line '- [ ] Tests pass' is missing"]


def test_an_empty_model_section_is_re_asked_once_then_exits_1(tmp_path, model_stub):
    repo = _repo(tmp_path)
    model_stub.answers = [{"sections": {}}, {"sections": {"Summary": "  "}}]
    with pytest.raises(commands.GateFailed, match="section `summary` is empty"):
        _render(repo)
    assert len(model_stub.prompts) == 2
    assert "Your previous answer was rejected" in model_stub.prompts[1]
    assert "section `summary` is empty" in model_stub.prompts[1]


def test_the_re_ask_can_fix_the_answer(tmp_path, model_stub):
    repo = _repo(tmp_path)
    model_stub.answers = [{"sections": {}}, {"sections": {"Summary": "_Not applicable to this change._"}}]
    body = _render(repo).body
    assert "_Not applicable to this change._" in body


def test_an_answer_that_is_not_json_gets_one_reformat_re_ask(tmp_path, model_stub):
    repo = _repo(tmp_path)
    model_stub.answers = ["I think the summary is fine.", {"sections": {"Summary": "Fixed."}}]
    assert "Fixed." in _render(repo).body
    assert len(model_stub.prompts) == 2


def test_model_text_with_a_marker_is_rejected(tmp_path, model_stub):
    repo = _repo(tmp_path)
    bad = {"sections": {"Summary": "x <!-- pr:end summary --> y"}}
    model_stub.answers = [bad, bad]
    with pytest.raises(commands.GateFailed, match="marker"):
        _render(repo)


def test_a_body_over_2500_words_is_rejected_with_per_section_counts(tmp_path, model_stub):
    repo = _repo(tmp_path)
    long = " ".join(["word"] * 2600)
    model_stub.answers = [{"sections": {"Summary": long}}, {"sections": {"Summary": long}}]
    with pytest.raises(commands.GateFailed) as e:
        _render(repo)
    msg = str(e.value)
    assert "over the 2500-word limit" in msg and "summary: 2600" in msg and "template text:" in msg
    assert "summary: 2600" in model_stub.prompts[1]


def test_word_count_ignores_html_comments():
    assert gates.word_count("<!-- a b c d -->one two\n<!--\nmulti\n-->three") == 3
    assert gates.MAX_WORDS == 2500 and gates.MODEL_TARGET_WORDS == 1500


def test_edit_counts_only_the_text_inside_markers():
    human = " ".join(["mine"] * 3000)
    body = f"{markers.MANAGED}\n{markers.written_line(['why'])}\n\n{human}\n\n## Why\n{markers.wrap('why', 'short')}\n"
    counts = gates.edit_word_counts(body)
    assert set(counts) == {"why"}
    assert gates.check_words(counts) == []
    over = body.replace("short", " ".join(["w"] * 2501))
    assert "2501 words" in gates.check_words(gates.edit_word_counts(over), what="the text this skill owns")[0]
