"""The strict paths.v1 output models: field order, the hop's defaults and constraints, invariants."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from conftest import paths_name, unscoped_variant

NEW_MODELS = ["Location", "Handler", "Invoker", "PathEntry", "PathHop", "E2ETest", "MockedE2ETest",
              "E2EListing", "TracedPath", "PathDrop", "PathDiagnostic", "ListingSkip", "FallbackStatus",
              "PathsResult", "Seed"]

# Declaration order is serialisation order, so these pin the bytes of paths.json.
FIELD_ORDER = {
    "PathsResult": ["schema_version", "commit", "scoped", "cap", "fallback", "paths", "dropped",
                    "diagnostics", "test_listing_skips"],
    "TracedPath": ["id", "entry", "hops", "stops_at", "break_reason", "changed_lines_covered", "tests"],
    "PathEntry": ["kind", "name", "location", "handler", "invocation", "invoked_by", "also_invoked_by"],
    "Invoker": ["kind", "name", "location"],
    "PathHop": ["symbol_called", "path", "line_start", "line_end", "quote", "note", "side"],
    "Location": ["path", "line"],
    "Handler": ["path", "line", "symbol"],
    "E2ETest": ["node_id", "file", "line", "match", "invocation_line"],
    "MockedE2ETest": ["node_id", "file", "line", "match", "invocation_line", "patch_file", "patch_line"],
    "E2EListing": ["e2e", "mocks_hop_2"],
    "PathDrop": ["stage", "reason", "detail", "entry"],
    "PathDiagnostic": ["path", "line", "reason", "detail"],
    "ListingSkip": ["file", "reason"],
    "FallbackStatus": ["status", "reason"],
    "Seed": ["path", "line", "symbol", "name"],
}


def test_every_new_model_is_strict_and_derives_from_model():
    from code_facts.models import _Model
    strict = paths_name("_Strict")
    assert issubclass(strict, _Model)
    assert strict.model_config.get("extra") == "forbid"
    assert strict.model_config.get("frozen") is True
    for name in NEW_MODELS:
        model = paths_name(name)
        assert issubclass(model, _Model), name
        assert issubclass(model, strict), name


@pytest.mark.parametrize("model_name", sorted(FIELD_ORDER))
def test_field_order_is_normative(model_name):
    assert list(paths_name(model_name).model_fields) == FIELD_ORDER[model_name]


def test_path_hop_defaults_and_constraints():
    hop = paths_name("PathHop")
    built = hop(symbol_called="run", path="pkg/core.py", line_start=3, line_end=4, quote="def run():")
    dumped = built.model_dump(mode="json")
    assert list(dumped) == FIELD_ORDER["PathHop"]
    assert dumped["note"] == "" and dumped["side"] == "head"
    with pytest.raises(ValidationError):
        hop(symbol_called="run", path="pkg/core.py", line_start=0, line_end=4, quote="x")
    with pytest.raises(ValidationError):
        hop(symbol_called="run", path="pkg/core.py", line_start=3, line_end=4, quote="x", side="base")


def test_full_document_parses_and_round_trips(full_paths_doc):
    result = paths_name("PathsResult").model_validate(full_paths_doc)
    assert result.model_dump(mode="json") == full_paths_doc


def test_unscoped_document_with_null_cap_parses(full_paths_doc):
    doc = unscoped_variant(full_paths_doc)
    result = paths_name("PathsResult").model_validate(doc)
    assert result.cap is None and result.scoped is False
    assert result.fallback.reason is None


def test_models_are_frozen(full_paths_doc):
    result = paths_name("PathsResult").model_validate(full_paths_doc)
    with pytest.raises(ValidationError):
        result.commit = "other"
    with pytest.raises(ValidationError):
        result.paths[0].entry.location.line = 2


@pytest.mark.parametrize("where", ["top", "path", "entry", "hop", "drop", "location", "e2e"])
def test_unknown_field_is_rejected_at_every_level(full_paths_doc, where):
    doc = copy.deepcopy(full_paths_doc)
    target = {
        "top": doc, "path": doc["paths"][0], "entry": doc["paths"][0]["entry"],
        "hop": doc["paths"][0]["hops"][0], "drop": doc["dropped"][0],
        "location": doc["paths"][0]["entry"]["location"], "e2e": doc["paths"][0]["tests"]["e2e"][0],
    }[where]
    target["foo"] = 1
    with pytest.raises(ValidationError):
        paths_name("PathsResult").model_validate(doc)


@pytest.mark.parametrize("field,value", [
    (("schema_version",), "paths.v2"),
    (("paths", 0, "entry", "kind"), "route"),
    (("paths", 0, "entry", "kind"), "argparse_subcommand"),   # discovery kinds never reach paths.json
    (("dropped", 0, "stage"), "explore"),
    (("diagnostics", 0, "reason"), "parse_errors"),
    (("test_listing_skips", 0, "reason"), "io_error"),
    (("fallback", "status"), "done"),
    (("paths", 0, "tests", "e2e", 0, "match"), "magic"),
])
def test_closed_enums_reject_unknown_values(full_paths_doc, field, value):
    doc = copy.deepcopy(full_paths_doc)
    node = doc
    for key in field[:-1]:
        node = node[key]
    node[field[-1]] = value
    with pytest.raises(ValidationError):
        paths_name("PathsResult").model_validate(doc)


@pytest.mark.parametrize("stops_at,break_reason", [(2, None), (None, "text_mismatch")])
def test_stop_and_reason_must_be_set_together(full_paths_doc, stops_at, break_reason):
    path = copy.deepcopy(full_paths_doc["paths"][0])
    path["stops_at"], path["break_reason"] = stops_at, break_reason
    with pytest.raises(ValidationError):
        paths_name("TracedPath").model_validate(path)


@pytest.mark.parametrize("stops_at", [0, 3])
def test_stops_at_must_index_a_retained_hop(full_paths_doc, stops_at):
    path = copy.deepcopy(full_paths_doc["paths"][0])   # two hops
    path["stops_at"] = stops_at
    with pytest.raises(ValidationError):
        paths_name("TracedPath").model_validate(path)


def test_cut_path_at_first_hop_is_valid(full_paths_doc):
    path = copy.deepcopy(full_paths_doc["paths"][0])
    path["hops"] = path["hops"][:1]
    path["stops_at"] = 1
    assert paths_name("TracedPath").model_validate(path).stops_at == 1


def test_same_test_id_in_both_listings_is_rejected(full_paths_doc):
    tests = copy.deepcopy(full_paths_doc["paths"][0]["tests"])
    tests["mocks_hop_2"][0]["node_id"] = tests["e2e"][0]["node_id"]
    with pytest.raises(ValidationError):
        paths_name("E2EListing").model_validate(tests)


def test_seed_defaults_and_strictness():
    seed_model = paths_name("Seed")
    seed = seed_model(path="lib/api.py", symbol="handle_request")
    assert seed.model_dump() == {"path": "lib/api.py", "line": None, "symbol": "handle_request", "name": None}
    with pytest.raises(ValidationError):
        seed_model(path="lib/api.py", line=3, kind="seed")
