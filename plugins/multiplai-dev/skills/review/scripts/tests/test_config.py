"""`review.yaml`'s `dimensions` key and the default concurrency."""

from __future__ import annotations

import pytest
from review_pipeline.config import (
    DEFAULT_CONCURRENCY,
    DIMENSIONS,
    ConfigError,
    ReviewConfig,
    load_config,
)
from test_reported_finders import make_repo, model, review, saved  # noqa: F401


def config(tmp_path, text: str) -> ReviewConfig:
    (tmp_path / "review.yaml").write_text(text)
    return load_config(tmp_path)


def test_eight_calls_run_at_once_by_default(tmp_path):
    assert DEFAULT_CONCURRENCY == 8
    assert load_config(tmp_path).concurrency == 8
    assert config(tmp_path, "concurrency: 3\n").concurrency == 3


def test_without_the_key_every_dimension_and_trim_run(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.dimensions == DIMENSIONS and cfg.trim is True


def test_a_list_names_the_dimensions_that_run_in_their_usual_order(tmp_path):
    cfg = config(tmp_path, "dimensions: [tests, diff-bugs, trim]\n")
    assert cfg.dimensions == ("diff-bugs", "tests") and cfg.trim is True


def test_a_list_without_trim_turns_the_trim_finder_off(tmp_path):
    cfg = config(tmp_path, "dimensions: [diff-bugs, callers]\n")
    assert cfg.dimensions == ("diff-bugs", "callers") and cfg.trim is False


def test_trim_false_drops_trim_from_a_list_that_names_it(tmp_path):
    cfg = config(tmp_path, "dimensions: [diff-bugs, trim]\ntrim: false\n")
    assert cfg.dimensions == ("diff-bugs",) and cfg.trim is False


@pytest.mark.parametrize(
    "value", ["[diff-bugs, typos]", "[]", "diff-bugs", "[1]"], ids=str
)
def test_an_unknown_or_malformed_list_is_an_error_naming_the_valid_dimensions(
    tmp_path, value
):
    with pytest.raises(ConfigError) as exc:
        config(tmp_path, f"dimensions: {value}\n")
    assert "diff-bugs, callers, history, conventions, tests, trim" in str(exc.value)
    if value == "[diff-bugs, typos]":
        assert "unknown dimension 'typos'" in str(exc.value)


def test_the_review_exits_2_on_an_unknown_dimension_and_starts_nothing(
    tmp_path,
    model,  # noqa: F811
    capsys,
):
    repo, base, head = make_repo(tmp_path, 1)
    fake = model()
    assert review(repo, base, head, tmp_path / "out", yaml="dimensions: [typos]\n") == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR: ") and "unknown dimension 'typos'" in err
    assert fake.report_calls == [] and fake.other_calls == []


def test_a_change_review_runs_and_stores_the_listed_dimensions(
    tmp_path,
    model,  # noqa: F811
):
    repo, base, head = make_repo(tmp_path, 2)
    fake = model()
    assert (
        review(
            repo,
            base,
            head,
            tmp_path / "out",
            yaml="dimensions: [diff-bugs, callers]\n",
        )
        == 0
    )
    _, state = saved(tmp_path / "out")
    assert state.dimensions == ["diff-bugs", "callers"]
    assert sorted(c["key"] for c in fake.report_calls) == [
        "callers@1",
        "callers@2",
        "diff-bugs@1",
        "diff-bugs@2",
    ]
