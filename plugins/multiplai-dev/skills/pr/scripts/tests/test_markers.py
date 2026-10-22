from __future__ import annotations

import pytest
from pr_pipeline import markers
from pr_pipeline.markers import MarkerError, Tail


def _body(*sections: tuple[str, str, str], written: list[str] | None = None, human: str = "") -> str:
    keys = written if written is not None else [k for k, _, _ in sections]
    parts = [f"{markers.MANAGED}\n{markers.written_line(keys)}"]
    if human:
        parts.append(human)
    parts += [f"{heading}\n{markers.wrap(key, content)}" for key, heading, content in sections]
    return "\n\n".join(parts) + "\n"


def test_spans_find_each_pair():
    body = _body(("why", "## Why", "old why"), ("size", "## Size", "1 file"))
    found = markers.spans(body)
    assert set(found) == {"written", "why", "size"}
    a, b = found["why"]
    assert body[a:b] == "\nold why\n"


def test_nested_markers_are_refused():
    body = f"{markers.begin('a')}\n{markers.begin('b')}\nx\n{markers.end('b')}\n{markers.end('a')}"
    with pytest.raises(MarkerError, match="nested"):
        markers.spans(body)


@pytest.mark.parametrize("body,match", [
    (f"{markers.begin('a')}\nx\n", "never closed"),
    (f"x\n{markers.end('a')}", "no matching begin"),
    (f"{markers.begin('a')}\nx\n{markers.end('b')}", "no matching begin"),
    (f"{markers.wrap('a', 'x')}\n{markers.wrap('a', 'y')}", "appears twice"),
])
def test_unbalanced_or_repeated_markers_are_refused(body, match):
    with pytest.raises(MarkerError, match=match):
        markers.spans(body)


def test_merge_replaces_only_bytes_inside_markers_and_keeps_a_human_edit_outside():
    human = "A note a reviewer typed by hand.\n\n- [x] I checked this myself"
    current = _body(("why", "## Why", "old why"), ("size", "## Size", "1 file"), human=human)
    current = current.replace("## Why\n", "## Why\nHuman line under the heading.\n")
    m = markers.merge(current, {"why": "new why", "size": "1 file"}, {"why"}, [Tail("size", "## Size", "1 file")])
    assert m.changed == ["why"]
    assert "new why" in m.body and "old why" not in m.body
    assert human in m.body and "Human line under the heading." in m.body
    # Every byte outside the why pair is unchanged.
    a, b = markers.spans(current)["why"]
    a2, b2 = markers.spans(m.body)["why"]
    assert current[:a] == m.body[:a2] and current[b:] == m.body[b2:]


def test_a_deleted_marker_pair_is_not_restored():
    current = _body(("why", "## Why", "w"), ("size", "## Size", "s"))
    current = current.replace(f"## Size\n{markers.wrap('size', 's')}\n", "")
    m = markers.merge(current, {"why": "w", "size": "s2"}, {"why"}, [Tail("size", "## Size", "s2")])
    assert m.not_restored == ["size"]
    assert "pr:begin size" not in m.body and m.body == current


def test_a_deleted_template_section_is_not_restored_even_without_the_written_list():
    current = f"{markers.MANAGED}\n\n## Why\n{markers.wrap('why', 'w')}\n"
    m = markers.merge(current, {"why": "w", "notes": "n"}, {"why", "notes"}, [])
    assert m.not_restored == ["notes"] and m.body == current


def test_a_new_extra_section_is_appended_and_recorded_as_written():
    current = _body(("why", "## Why", "w"))
    extra = Tail("extra:walkthroughs", "## Stale walkthroughs", "- a.md")
    m = markers.merge(current, {"why": "w", extra.key: extra.content}, {"why"}, [extra])
    assert m.changed == ["extra:walkthroughs"]
    assert m.body.rstrip().endswith(f"## Stale walkthroughs\n{markers.wrap(extra.key, '- a.md')}")
    assert markers.read_written(m.body, markers.spans(m.body)) == {"why", "extra:walkthroughs"}
    # A human then deletes it: the next edit does not bring it back.
    gone = m.body.replace(f"\n\n## Stale walkthroughs\n{markers.wrap(extra.key, '- a.md')}", "")
    again = markers.merge(gone, {"why": "w", extra.key: "- b.md"}, {"why"}, [extra])
    assert again.not_restored == ["extra:walkthroughs"] and again.body == gone


def test_owned_text_excludes_the_written_list_and_human_text():
    body = _body(("why", "## Why", "mine"), human="theirs")
    assert markers.owned_text(body).strip() == "mine"
