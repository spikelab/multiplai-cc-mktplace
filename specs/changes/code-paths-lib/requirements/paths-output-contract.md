## ADDED Requirements

### Requirement: Public entry points exported from the library
`plugins/multiplai-dev/lib/code_facts/__init__.py` SHALL export `map_paths` and `write_paths_file`, plus the result models they use: the top-level result, path, entry, hop, test-listing and drop models. Every name exported before this change SHALL still be exported, with the same signature and behaviour.

#### Scenario: New names are importable
- **WHEN** a test runs `from code_facts import map_paths, write_paths_file`
- **THEN** the import succeeds and both names are callable

#### Scenario: Existing exports are unchanged
- **WHEN** the export list of `code_facts/__init__.py` after this change is compared with the list before it
- **THEN** every earlier name is still present
- **AND** the existing `code_facts` test suite passes without modification

### Requirement: `map_paths` returns a model and writes only cache files
`map_paths(...)` SHALL take its inputs as keyword arguments:
- snapshot root;
- commit, the label written to `paths.json`;
- caller-supplied `Resolve`, through which citations are re-read at the commit (as for `explore`);
- caller-supplied `CallFn`;
- optional changed lines;
- optional seeds;
- optional known edges (`places`);
- `cap`;
- concurrency bound;
- cache location.

It SHALL return the top-level result model. It SHALL NOT write `paths.json` or any other file outside the cache location.

#### Scenario: No output file is produced by mapping alone
- **WHEN** `map_paths` runs on a fixture repository in a temporary directory with a stub `CallFn`
- **THEN** it returns an instance of the top-level result model
- **AND** no file other than files under the cache location is created or modified in the temporary directory

#### Scenario: Missing required argument
- **WHEN** `map_paths` is called without a `CallFn`, or without a `Resolve`
- **THEN** it raises `TypeError` before any discovery or model call happens

### Requirement: Schema file exists and follows the review facts schema style
The file `schema/paths.v1.schema.json` SHALL be a JSON Schema document that follows the conventions of `plugins/multiplai-dev/skills/review/schema/facts.v1.schema.json`. That means the same `$schema` draft, a `$id`, a `title`, and `$defs` for reused shapes. Every object type SHALL set `additionalProperties: false`.

#### Scenario: Schema is itself valid
- **WHEN** a test loads `schema/paths.v1.schema.json` and checks it against its declared meta-schema with the JSON Schema validator
- **THEN** the check passes

#### Scenario: Draft matches the facts schema
- **WHEN** a test compares the `$schema` value of `paths.v1.schema.json` with that of `facts.v1.schema.json`
- **THEN** the two values are equal

#### Scenario: Unknown fields are rejected
- **WHEN** a valid `paths.json` document has an extra field `"foo": 1` added to the top level, to a path, to an entry, to a hop or to a drop record, and is then validated
- **THEN** validation fails in each case

### Requirement: Top-level document shape
A `paths.json` document SHALL be an object with exactly these required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | constant string `"paths.v1"` | Schema version |
| `commit` | string | The commit that all citations were re-read at |
| `scoped` | boolean | `true` if and only if changed lines were supplied |
| `cap` | integer or `null` | `null` when `scoped` is `false` |
| `paths` | array of path objects | The kept paths |
| `dropped` | array of drop records | Every dropped entry or path |

#### Scenario: Version constant enforced
- **WHEN** a document with `"schema_version": "paths.v2"` is validated
- **THEN** validation fails

#### Scenario: Missing required field
- **WHEN** a valid document has `dropped` removed and is then validated
- **THEN** validation fails

#### Scenario: Unscoped run
- **WHEN** `map_paths` runs without changed lines and its result is written
- **THEN** the document has `"scoped": false` and `"cap": null`

#### Scenario: Scoped run
- **WHEN** `map_paths` runs with changed lines and `cap=5` and its result is written
- **THEN** the document has `"scoped": true` and `"cap": 5`

### Requirement: Path object shape
Each element of `paths` SHALL have these fields:

| Field | Type | Rule |
|---|---|---|
| `id` | string | Matches `^p[1-9][0-9]*$` |
| `entry` | entry object | — |
| `hops` | array of hop objects | 1 to 20 items |
| `stops_at` | integer or `null` | — |
| `break_reason` | string or `null` | — |
| `changed_lines_covered` | array of locations | — |
| `tests` | test-listing object | — |

#### Scenario: Hop count bounds
- **WHEN** a path with 0 hops is validated, and a path with 21 hops is validated
- **THEN** both validations fail

#### Scenario: Id format
- **WHEN** a path with `"id": "p0"` or `"id": "path1"` is validated
- **THEN** validation fails

### Requirement: Entry object shape
An entry object SHALL have these fields:
- `kind`: one of `argparse`, `click`, `typer`, `script`, `main`, `skill_command`, `hook`, `seed`, `model`.
- `name`: a string.
- `location`: a location object giving the definition or dispatch line.
- `also_invoked_by`: an array of invoker records. Each record has its own `kind`, `name` and `location`. The array is sorted by `path`, then `line`, then `kind`. It is empty when no other invoker resolved to the same handler.

#### Scenario: Unknown kind rejected
- **WHEN** an entry with `"kind": "route"` is validated
- **THEN** validation fails

#### Scenario: Deduplicated invokers recorded
- **WHEN** a fixture has a `[project.scripts]` target and an argparse subcommand that resolve to the same handler, and the result is written
- **THEN** exactly one path has that handler as its entry
- **AND** its `also_invoked_by` has exactly one record, for the less specific invoker

#### Scenario: Known discovery case keeps the add_parser line
- **WHEN** discovery runs on a copy of `pr_pipeline`, which dispatches on `args.command`
- **THEN** the written `open`, `edit` and `render` entries have kind `argparse`
- **AND** each entry's `location.line` is the line of its `add_parser` call

### Requirement: Hop object shape mirrors the walkthrough Hop
A hop object SHALL be flat: the fields of `Hop` in `walkthrough_pipeline/models.py:122-128`, with the same names, types and order, followed by `side`, the constant `"head"` with that default. It SHALL NOT embed a `Citation`, so no citation `id` reaches the output. The library SHALL NOT import from `walkthrough_pipeline`.

#### Scenario: Field names match
- **WHEN** a test compares the hop model's fields with the fields of the walkthrough `Hop` model, read by parsing its source with `ast`
- **THEN** the hop model declares the walkthrough fields in the same order with the same annotations, followed by `side: Literal["head"]`

#### Scenario: No cross-import
- **WHEN** the source files under `code_facts/` are searched for `walkthrough_pipeline`
- **THEN** no import statement references it

#### Scenario: Default side
- **WHEN** a hop is built without an explicit side and serialised
- **THEN** the JSON has `"side": "head"`

### Requirement: Locations are relative to the snapshot root
Every `path` value in a location or citation SHALL be relative to the snapshot root passed to `map_paths`. It SHALL use `/` as the separator. It SHALL NOT start with `/`, SHALL NOT contain a `..` segment, and SHALL NOT contain a `.` segment. Line numbers SHALL be integers of 1 or more.

#### Scenario: Multi-repository snapshot
- **WHEN** `map_paths` runs on a snapshot root that contains two repositories, `repo_a/` and `repo_b/`, and an entry in `repo_b/cli.py` is found
- **THEN** that entry's `location.path` is `repo_b/cli.py`

#### Scenario: Absolute or escaping path rejected
- **WHEN** a document with a location path of `/abs/x.py`, `../x.py` or `a\\b.py` is validated
- **THEN** validation fails

#### Scenario: Line zero rejected
- **WHEN** a document has a location with `"line": 0`
- **THEN** validation fails

### Requirement: Cut paths carry stop and reason together
`stops_at` and `break_reason` SHALL be either both `null` or both non-null. When they are non-null:
- `stops_at` is the 1-based index of the last hop kept. It is at least 1 and no more than the length of `hops`.
- `break_reason` is a non-empty string.

A path that reached its last effect SHALL have both set to `null`.

#### Scenario: Complete path
- **WHEN** a fixture path passes the trace gate on every hop and is written
- **THEN** that path has `"stops_at": null` and `"break_reason": null`

#### Scenario: Cut path
- **WHEN** a stub `CallFn` returns a chain whose third hop fails the gate, both originally and on the one re-ask, and the result is written
- **THEN** that path has `"stops_at": 2`, a non-empty `break_reason`, and exactly 2 hops

#### Scenario: Mismatched pair rejected
- **WHEN** the result model is built with `stops_at=2` and `break_reason=None`
- **THEN** model validation raises an error

### Requirement: Ids are contiguous and follow array order
The ids in `paths` SHALL be `p1`, `p2`, ..., `pN` in array order, with no gaps or repeats. N is the number of kept paths. Ids SHALL be assigned after the deterministic ordering defined in `path-scope-and-cap`.

#### Scenario: Contiguous ids
- **WHEN** a fixture run keeps 4 paths
- **THEN** the ids, read in array order, are exactly `["p1", "p2", "p3", "p4"]`

#### Scenario: Ids follow ordering, not discovery order
- **WHEN** the same fixture is mapped twice, with entry discovery order reversed by the test harness the second time
- **THEN** both documents have the same `id` attached to each entry location

### Requirement: Changed-line coverage field
`changed_lines_covered` SHALL list the changed lines that the path's hops cover. The list is sorted by `path`, then `line`, with no duplicates. When `scoped` is `false`, it SHALL be an empty array on every path.

#### Scenario: Coverage listed
- **WHEN** changed lines `{"src/a.py": [10, 11]}` are supplied and a kept path has a hop citing `src/a.py:10-11`
- **THEN** that path's `changed_lines_covered` is `[{"path":"src/a.py","line":10},{"path":"src/a.py","line":11}]`

#### Scenario: Unscoped coverage empty
- **WHEN** `map_paths` runs without changed lines
- **THEN** every path has `"changed_lines_covered": []`

### Requirement: Test-listing shape
The `tests` field SHALL be an object with two arrays of test records, `e2e` and `mocks_hop_2`:
- An `e2e` record has `node_id`, `file`, `line`, `match` and `invocation_line`, in that order. `node_id` has the form `<root-relative path>::<qualified test name>`.
- A `mocks_hop_2` record has the same fields, then `patch_file` and `patch_line`, because the hop-2 patch may be in a fixture file.
- Each array is sorted by `(file, line, node_id)`, and each `node_id` appears at most once in it.
- No `node_id` appears in both arrays.
- Both arrays are empty when no test invokes the entry.

#### Scenario: Mocked test listed separately
- **WHEN** a fixture test invokes the entry structurally but patches the hop-2 target
- **THEN** its record appears in `mocks_hop_2`, with `patch_file` and `patch_line`, and not in `e2e`

#### Scenario: Overlap rejected
- **WHEN** the model is built with the same `node_id` in both arrays
- **THEN** model validation raises an error

### Requirement: Every drop is recorded with a reason
Each item of `dropped` SHALL have these fields, in this order:
- `stage`: one of `model_fallback_citation`, `model_fallback_duplicate`, `import_reachability`, `trace_limit`, `trace`, `no_changed_line`, `cap`.
- `reason`: a non-empty code from the stage's closed set:

  | Stage | Reasons |
  |---|---|
  | `model_fallback_citation` | `missing_citation`, `citation_unverified` |
  | `model_fallback_duplicate` | `duplicate` |
  | `import_reachability` | `unreachable` |
  | `trace_limit` | `over_trace_limit` |
  | `trace` | `call_failed`, or any trace-gate break code |
  | `no_changed_line` | `no_changed_line_covered` |
  | `cap` | `over_cap` |

- `detail`: free text for humans, never parsed.
- `entry`: an entry object.

Drop records SHALL be sorted by `stage` in the order listed above, then by entry `location.path`, then `location.line`, then `kind`. When `scoped` is `false`, records with the stages `import_reachability`, `trace_limit`, `no_changed_line` and `cap` SHALL NOT appear.

#### Scenario: Cap drop recorded
- **WHEN** a scoped fixture run has 3 qualifying paths and `cap=2`
- **THEN** `paths` has 2 items
- **AND** `dropped` has exactly one record with `"stage": "cap"` and `"reason": "over_cap"`

#### Scenario: Uncited model entry recorded
- **WHEN** a stub fallback agent returns an entry whose citation does not re-read at the commit
- **THEN** no path has that entry
- **AND** `dropped` has a record with `"stage": "model_fallback_citation"` and `"reason": "citation_unverified"` for it

#### Scenario: Empty reason rejected
- **WHEN** a drop record with `"reason": ""` is validated
- **THEN** validation fails

### Requirement: Empty results are valid
When no entry is found, or none survives scoping, the result SHALL still be a schema-valid document with `"paths": []`.

#### Scenario: Repository with no entry points
- **WHEN** `map_paths` runs on a fixture repository that has no entry points, with a stub `CallFn` whose fallback returns nothing, and the result is written
- **THEN** the written file validates against the schema and has `"paths": []`

### Requirement: `write_paths_file` validates before writing
`write_paths_file(result, path)` SHALL validate the serialised result against `schema/paths.v1.schema.json` before writing. If validation fails, it SHALL raise an error that names the failing JSON pointer. It SHALL leave any existing file at `path` unchanged and SHALL NOT create a new file.

#### Scenario: Invalid result not written
- **WHEN** a test changes the model's `schema_version` to `"bad"` through `model_construct` and calls `write_paths_file` with a path that does not yet exist
- **THEN** an error is raised whose message contains `/schema_version`
- **AND** no file exists at that path

#### Scenario: Existing file preserved on failure
- **WHEN** `write_paths_file` is called with an invalid result and a path that already holds a valid `paths.json`
- **THEN** the file's bytes are unchanged after the call

### Requirement: `write_paths_file` writes atomically
`write_paths_file` SHALL write to a temporary file in the same directory and then rename it over the target. A reader SHALL never see a partially written `paths.json`.

#### Scenario: Interrupted write
- **WHEN** a test patches the serialiser to raise partway through writing
- **THEN** the target path is either absent or holds its previous bytes
- **AND** no temporary file is left in the directory

### Requirement: Byte-stable serialisation
`write_paths_file` SHALL write UTF-8 JSON with these properties:
- keys in schema-declared field order;
- 2-space indentation;
- non-ASCII characters not escaped;
- exactly one trailing newline.

Two runs of `map_paths` with the same inputs and the same answer cache SHALL produce byte-identical files.

#### Scenario: Reproducible run
- **WHEN** a fixture is mapped twice with the same stub `CallFn` answers, and the second run resumes from the first run's cache
- **THEN** the two written files are byte-identical

#### Scenario: Concurrency does not affect output
- **WHEN** the same fixture is mapped with concurrency 1 and with concurrency 8, using a stub `CallFn` that returns answers in random order
- **THEN** the two written files are byte-identical

#### Scenario: Trailing newline
- **WHEN** any result is written
- **THEN** the file ends with exactly one `\n` byte

### Requirement: Round-trip between file and model
Loading a written `paths.json` into the top-level result model, and serialising it again, SHALL reproduce the original bytes. The models SHALL derive from the `code_facts` `_Model` base.

#### Scenario: Round trip
- **WHEN** a written fixture result is read, parsed with `model_validate_json`, and written again with `write_paths_file`
- **THEN** the second file is byte-identical to the first

#### Scenario: Model base
- **WHEN** a test checks `issubclass` for each new result model against `code_facts.models._Model`
- **THEN** every check is `True`

### Requirement: Schema and models agree
Every document produced by `write_paths_file` from a model instance SHALL validate against the schema. Every document that validates against the schema SHALL parse into the model.

#### Scenario: Full-coverage fixture
- **WHEN** a hand-built result that uses every optional and nullable field and every enum value is written and then validated
- **THEN** validation passes

#### Scenario: Schema-valid document parses
- **WHEN** each JSON fixture under `tests/` that validates against the schema is passed to `model_validate_json`
- **THEN** parsing succeeds for every fixture

### Requirement: Schema validation dependency is declared
The JSON Schema validator used by the tests and by `write_paths_file` SHALL be declared in the library's dependencies. A validator used at runtime goes in runtime dependencies. A validator used only in tests goes in dev dependencies. The tests SHALL pass under `uv run pytest` from a clean environment.

#### Scenario: Clean environment
- **WHEN** CI creates a fresh `uv` environment for the library and runs its test suite
- **THEN** the schema tests run and pass without an import error for the validator