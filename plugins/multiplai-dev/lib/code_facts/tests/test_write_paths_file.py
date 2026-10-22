"""write_paths_file: byte-stable serialisation, validation before writing, atomic replace."""

from __future__ import annotations

import json
import os

import pytest
from conftest import paths_name


def _result(doc):
    return paths_name("PathsResult").model_validate(doc)


def _expected_text(result) -> str:
    return json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def test_writes_byte_stable_json_and_returns_the_path(tmp_path, full_paths_doc):
    write = paths_name("write_paths_file")
    result = _result(full_paths_doc)
    target = tmp_path / "paths.json"
    assert write(result, target) == target
    data = target.read_bytes()
    assert data == _expected_text(result).encode("utf-8")
    assert data.endswith(b"}\n") and not data.endswith(b"\n\n")
    assert "café".encode() in data and b"\\u00e9" not in data


def test_keys_are_written_in_declaration_order(tmp_path, full_paths_doc):
    target = tmp_path / "paths.json"
    paths_name("write_paths_file")(_result(full_paths_doc), target)
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert list(doc) == ["schema_version", "commit", "scoped", "cap", "fallback", "paths", "dropped",
                         "diagnostics", "test_listing_skips"]
    assert list(doc["paths"][0]) == ["id", "entry", "hops", "stops_at", "break_reason",
                                     "changed_lines_covered", "tests"]
    assert list(doc["paths"][0]["entry"]) == ["kind", "name", "location", "handler", "invocation",
                                              "invoked_by", "also_invoked_by"]
    assert list(doc["paths"][0]["hops"][0]) == ["symbol_called", "path", "line_start", "line_end", "quote",
                                                "note", "side"]
    assert list(doc["dropped"][0]) == ["stage", "reason", "detail", "entry"]
    assert list(doc["paths"][0]["tests"]["mocks_hop_2"][0])[-2:] == ["patch_file", "patch_line"]


def test_round_trip_through_the_model_is_byte_identical(tmp_path, full_paths_doc):
    write = paths_name("write_paths_file")
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    write(_result(full_paths_doc), first)
    reread = paths_name("PathsResult").model_validate_json(first.read_bytes())
    write(reread, second)
    assert second.read_bytes() == first.read_bytes()


def test_invalid_result_names_the_pointer_and_writes_nothing(tmp_path, full_paths_doc):
    result_cls = paths_name("PathsResult")
    valid = _result(full_paths_doc)
    fields = {k: getattr(valid, k) for k in result_cls.model_fields}
    bad = result_cls.model_construct(**{**fields, "schema_version": "bad"})
    target = tmp_path / "out" / "paths.json"
    target.parent.mkdir()
    with pytest.raises(ValueError, match="/schema_version"):
        paths_name("write_paths_file")(bad, target)
    assert list(target.parent.iterdir()) == []


def test_pointer_into_an_array_uses_the_index(tmp_path, full_paths_doc):
    valid = _result(full_paths_doc)
    bad_path = valid.paths[0].model_copy(update={"id": "p0"})
    bad = valid.model_copy(update={"paths": [bad_path, valid.paths[1]]})
    with pytest.raises(ValueError, match="/paths/0/id"):
        paths_name("write_paths_file")(bad, tmp_path / "paths.json")
    assert list(tmp_path.iterdir()) == []


def test_existing_file_is_preserved_when_validation_fails(tmp_path, full_paths_doc):
    write = paths_name("write_paths_file")
    target = tmp_path / "paths.json"
    write(_result(full_paths_doc), target)
    before = target.read_bytes()
    valid = _result(full_paths_doc)
    bad = valid.model_copy(update={"cap": "ten"})
    with pytest.raises(ValueError, match="/cap"):
        write(bad, target)
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["paths.json"]


@pytest.mark.parametrize("step", ["fsync", "replace"])
@pytest.mark.parametrize("existing", [True, False])
def test_interrupted_write_leaves_the_target_untouched_and_no_temp_file(tmp_path, full_paths_doc, monkeypatch,
                                                                        step, existing):
    write = paths_name("write_paths_file")
    target = tmp_path / "paths.json"
    if existing:
        target.write_bytes(b'{"old": true}\n')

    def boom(*args, **kwargs):
        raise OSError(f"{step} interrupted")

    monkeypatch.setattr(os, step, boom)
    with pytest.raises(OSError, match="interrupted"):
        write(_result(full_paths_doc), target)
    monkeypatch.undo()
    if existing:
        assert target.read_bytes() == b'{"old": true}\n'
        assert [p.name for p in tmp_path.iterdir()] == ["paths.json"]
    else:
        assert list(tmp_path.iterdir()) == []
