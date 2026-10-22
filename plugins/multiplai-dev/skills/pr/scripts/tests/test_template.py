from __future__ import annotations

from pathlib import Path

import pytest
from pr_pipeline.template import (
    STANDARD_TEMPLATE,
    PrConfig,
    discover,
    load,
    load_config,
    parse,
    set_tick,
)


def _write(path: Path, text: str = "## Summary\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- the standard template ----------------------------------------------------


def test_standard_template_parses_into_its_six_sections_in_order():
    sections = load(STANDARD_TEMPLATE)
    assert [s.title for s in sections[1:]] == [
        "Why",
        "Blocks",
        "Documentation",
        "Companion artifacts",
        "Ticket",
        "Size",
    ]
    assert all(s.level == 2 for s in sections[1:])
    # Each heading carries an HTML-comment hint, as GitHub templates usually do.
    assert all(any(b.kind == "comment" for b in s.blocks) for s in sections[1:])
    assert not sections[0].blocks or all(b.kind == "blank" for b in sections[0].blocks)


# --- discovery: one fixture repo per GitHub location -------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".github/pull_request_template.md",
        "pull_request_template.md",
        "docs/pull_request_template.md",
    ],
)
def test_discovery_finds_the_template_in_each_github_location(tmp_path, rel):
    path = _write(tmp_path / rel)
    found = discover(tmp_path)
    assert (
        found.path == path.resolve() and not found.standard and found.diagnosis is None
    )


@pytest.mark.parametrize("loc", [".github", "", "docs"])
def test_discovery_uses_a_template_directory_with_exactly_one_file(tmp_path, loc):
    base = tmp_path / loc if loc else tmp_path
    path = _write(base / "PULL_REQUEST_TEMPLATE" / "feature.md")
    assert discover(tmp_path).path == path.resolve()


def test_discovery_order_is_github_then_root_then_docs(tmp_path):
    _write(tmp_path / "docs" / "pull_request_template.md")
    root = _write(tmp_path / "pull_request_template.md")
    assert discover(tmp_path).path == root.resolve()
    gh = _write(tmp_path / ".github" / "pull_request_template.md")
    assert discover(tmp_path).path == gh.resolve()


def test_discovery_prefers_the_single_file_over_the_directory_in_one_location(tmp_path):
    single = _write(tmp_path / ".github" / "pull_request_template.md")
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "a.md")
    assert discover(tmp_path).path == single.resolve()


def test_discovery_matches_an_uppercase_name(tmp_path):
    path = _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md")
    assert discover(tmp_path).path == path.resolve()


def test_discovery_matches_a_lowercase_directory_name(tmp_path):
    path = _write(tmp_path / ".github" / "pull_request_template" / "only.md")
    assert discover(tmp_path).path == path.resolve()


def test_no_template_uses_the_standard_template(tmp_path):
    found = discover(tmp_path)
    assert (
        found.path == STANDARD_TEMPLATE and found.standard and found.diagnosis is None
    )


def test_several_templates_use_the_standard_template_and_name_them(tmp_path):
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "bug.md")
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "feature.md")
    found = discover(tmp_path)
    assert found.path == STANDARD_TEMPLATE and found.standard
    assert found.diagnosis == (
        "several templates, used the standard one; set pr.template in specs/config.yaml to pick one of: "
        ".github/PULL_REQUEST_TEMPLATE/bug.md, .github/PULL_REQUEST_TEMPLATE/feature.md"
    )


# --- discovery stays inside the repository -------------------------------------


def _repo_and_outside(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = _write(
        tmp_path / "outside" / "pull_request_template.md", "## Secret\nOUTSIDE-TEXT\n"
    )
    return repo, outside


def test_a_template_symlinked_outside_the_repo_is_ignored_and_named(tmp_path):
    repo, outside = _repo_and_outside(tmp_path)
    (repo / ".github").mkdir()
    (repo / ".github" / "pull_request_template.md").symlink_to(outside)
    found = discover(repo)
    assert found.path == STANDARD_TEMPLATE and found.standard
    assert (
        found.diagnosis
        == "`.github/pull_request_template.md` resolves outside the repository; ignored"
    )


def test_a_symlinked_github_directory_outside_the_repo_is_ignored_and_named(tmp_path):
    repo, outside = _repo_and_outside(tmp_path)
    (repo / ".github").symlink_to(outside.parent, target_is_directory=True)
    found = discover(repo)
    assert found.path == STANDARD_TEMPLATE and found.standard
    assert found.diagnosis == "`.github` resolves outside the repository; ignored"


def test_a_template_directory_entry_symlinked_outside_is_ignored(tmp_path):
    repo, outside = _repo_and_outside(tmp_path)
    inside = _write(repo / ".github" / "PULL_REQUEST_TEMPLATE" / "feature.md")
    (repo / ".github" / "PULL_REQUEST_TEMPLATE" / "leak.md").symlink_to(outside)
    found = discover(repo)
    assert found.path == inside.resolve() and not found.standard
    assert (
        found.diagnosis
        == "`.github/PULL_REQUEST_TEMPLATE/leak.md` resolves outside the repository; ignored"
    )


def test_discovery_goes_on_to_the_next_location_after_an_outside_symlink(tmp_path):
    repo, outside = _repo_and_outside(tmp_path)
    (repo / ".github").mkdir()
    (repo / ".github" / "pull_request_template.md").symlink_to(outside)
    docs = _write(repo / "docs" / "pull_request_template.md")
    found = discover(repo)
    assert (
        found.path == docs.resolve()
        and "resolves outside the repository" in found.diagnosis
    )


def test_a_template_symlinked_inside_the_repo_is_still_used(tmp_path):
    repo = tmp_path / "repo"
    real = _write(repo / "templates" / "pr.md")
    (repo / ".github").mkdir()
    link = repo / ".github" / "pull_request_template.md"
    link.symlink_to(real)
    found = discover(repo)
    assert found.path == link and not found.standard and found.diagnosis is None


def test_config_template_takes_precedence_over_discovery(tmp_path):
    _write(tmp_path / ".github" / "pull_request_template.md")
    chosen = _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "feature.md")
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "bug.md")
    _write(
        tmp_path / "specs" / "config.yaml",
        "pr:\n  template: .github/PULL_REQUEST_TEMPLATE/feature.md\n  draft: false\n",
    )
    found = discover(tmp_path)
    assert found.path == chosen.resolve() and not found.standard
    assert load_config(tmp_path).draft is False


def test_config_template_that_is_missing_or_outside_the_repo_warns_and_discovers(
    tmp_path,
):
    # buildme's config.py accepts any string here and runs the whole build before PUBLISH, so
    # the skill treats an unusable value as it treats a malformed one: say so, then discover.
    found_file = _write(tmp_path / ".github" / "pull_request_template.md")
    _write(tmp_path / "specs" / "config.yaml", "pr:\n  template: nope.md\n")
    found = discover(tmp_path)
    assert found.path == found_file.resolve() and not found.standard
    assert "pr.template names a file that does not exist: nope.md" in found.diagnosis
    found_file.unlink()
    _write(tmp_path / "specs" / "config.yaml", "pr:\n  template: ../elsewhere.md\n")
    found = discover(tmp_path)
    assert found.standard
    assert (
        "pr.template points outside the repository: ../elsewhere.md" in found.diagnosis
    )


def test_an_unusable_config_template_keeps_the_several_templates_diagnosis(tmp_path):
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "bug.md")
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "feature.md")
    _write(tmp_path / "specs" / "config.yaml", "pr:\n  template: nope.md\n")
    found = discover(tmp_path)
    assert found.standard and "does not exist: nope.md" in found.diagnosis
    assert "several templates" in found.diagnosis


def test_discover_reads_the_config_it_is_given(tmp_path):
    chosen = _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "feature.md")
    _write(tmp_path / ".github" / "PULL_REQUEST_TEMPLATE" / "bug.md")
    found = discover(
        tmp_path, PrConfig(template=".github/PULL_REQUEST_TEMPLATE/feature.md")
    )
    assert found.path == chosen.resolve() and found.diagnosis is None


def test_config_template_auto_discovers_like_an_absent_value(tmp_path):
    # buildme setup writes `pr.template: auto` as its default; it means "find it", not a file.
    found_file = _write(tmp_path / ".github" / "pull_request_template.md")
    _write(tmp_path / "specs" / "config.yaml", "pr:\n  template: auto\n")
    assert load_config(tmp_path).template is None
    assert discover(tmp_path).path == found_file


def test_config_defaults_when_there_is_no_pr_block(tmp_path):
    _write(tmp_path / "specs" / "config.yaml", "git:\n  pr: draft\n")
    cfg = load_config(tmp_path)
    assert cfg.template is None and cfg.draft is True


@pytest.mark.parametrize(
    "text",
    [
        "pr: [a, b]\n",  # `pr:` not a mapping
        "pr:\n  template: 3\n",  # `pr.template` not a string
        "pr:\n  draft: maybe\n",  # `pr.draft` not a boolean
        "pr:\n  template: [\n",  # not valid YAML
        "- just a list\n",  # top level not a mapping
    ],
)
def test_a_malformed_pr_block_warns_and_uses_the_defaults_as_buildme_does(
    tmp_path, caplog, text
):
    # buildme's config.py warns and falls back on these same values, then runs this skill
    # with that config: refusing here would fail every PUBLISH buildme itself ran.
    found_file = _write(tmp_path / ".github" / "pull_request_template.md")
    _write(tmp_path / "specs" / "config.yaml", text)
    with caplog.at_level("WARNING", logger="pr_pipeline"):
        cfg = load_config(tmp_path)
        assert discover(tmp_path).path == found_file
    assert cfg.template is None and cfg.draft is True
    assert "specs/config.yaml" in caplog.text


# --- parsing --------------------------------------------------------------------


def test_a_template_with_no_headings_is_one_preamble_section():
    sections = parse("Describe your change.\n\n- [ ] I ran the tests\n")
    assert len(sections) == 1 and sections[0].level == 0
    kinds = [b.kind for b in sections[0].blocks]
    assert kinds == ["text", "blank", "checkbox"]


def test_nested_headings_belong_to_the_section_above_them():
    text = "# Title\n## Changes\n### Added\nnew\n### Removed\nold\n## Notes\nn\n"
    sections = parse(text)
    titles = [s.title for s in sections]
    assert titles == ["", "Title", "Changes", "Added", "Removed", "Notes"]
    changes = sections[2]
    # A section runs to the next heading of the same or higher level.
    assert [s.title for s in sections[3 : changes.subtree_end]] == ["Added", "Removed"]
    assert sections[1].subtree_end == len(sections)
    assert sections[3].subtree_end == 4


def test_headings_checkboxes_and_comments_are_kept_byte_for_byte():
    text = (
        "<!-- Thanks for the PR -->\n"
        "##  Summary  \n"
        "<!--\n  multi-line\n  hint\n-->\n"
        "  - [ ] Tests pass\n"
        "* [x] Docs updated\n"
    )
    sections = parse(text)
    assert sections[0].blocks[0].text == "<!-- Thanks for the PR -->"
    s = sections[1]
    assert s.heading == "##  Summary  " and s.title == "Summary"
    assert (
        s.blocks[0].kind == "comment"
        and s.blocks[0].text == "<!--\n  multi-line\n  hint\n-->"
    )
    assert [b.text for b in s.checkboxes] == [
        "  - [ ] Tests pass",
        "* [x] Docs updated",
    ]
    assert [b.id for b in s.checkboxes] == ["cb1", "cb2"]


def test_a_heading_inside_a_comment_or_a_code_fence_is_not_a_heading():
    text = "## Real\n<!--\n## not a heading\n-->\n```\n# also not\n```\n"
    sections = parse(text)
    assert [s.title for s in sections[1:]] == ["Real"]


def test_duplicate_titles_get_distinct_keys():
    sections = parse("## Notes\n### Notes\n")
    assert [s.key for s in sections[1:]] == ["notes", "notes-2"]


def test_set_tick_changes_only_the_box():
    assert set_tick("  - [ ] Tests pass", True) == "  - [x] Tests pass"
    assert set_tick("- [X] Done", False) == "- [ ] Done"
    assert set_tick("not a box", True) == "not a box"
