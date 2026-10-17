"""The committed paths.v1 schema is the one generated from the models, so the two cannot drift."""

from __future__ import annotations

import importlib.resources

from code_facts.paths import paths_schema_text


def test_the_committed_schema_is_generated_from_the_models():
    committed = importlib.resources.files("code_facts").joinpath("schema", "paths.v1.schema.json")
    assert committed.read_text(encoding="utf-8") == paths_schema_text(), \
        "regenerate code_facts/schema/paths.v1.schema.json from code_facts.paths.paths_schema_text()"
