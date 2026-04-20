## ADDED Requirements

### Requirement: Rename context_router.py to context_manager.py
The file `scripts/context_router.py` must be renamed to `scripts/context_manager.py`. The new filename must reflect the broader responsibility of the module (context assembly, fallback logic, catalog reads) rather than the narrower "routing" metaphor.

#### Scenario: File exists at new path
- **WHEN** the plugin repository is checked after the rename
- **THEN** `scripts/context_manager.py` exists and `scripts/context_router.py` does not exist

#### Scenario: File content is preserved
- **WHEN** `scripts/context_manager.py` is read after the rename
- **THEN** its content is identical to the previous `scripts/context_router.py` (no logic changes, no added/removed lines beyond what is required for internal self-references)

---

### Requirement: Update hooks.json path reference
All references to `context_router.py` in `hooks.json` must be updated to `context_manager.py` so that hook dispatch resolves to the renamed file.

#### Scenario: hooks.json points to renamed file
- **WHEN** `hooks.json` is parsed after the rename
- **THEN** every command or path entry that previously referenced `context_router.py` now references `context_manager.py`

#### Scenario: No stale references remain in hooks.json
- **WHEN** `hooks.json` is searched for the string `context_router`
- **THEN** zero matches are found

#### Scenario: Hook execution still works after rename
- **WHEN** a hook that invokes the context manager is triggered (e.g., the context-assembly hook fires)
- **THEN** the hook executes `scripts/context_manager.py` successfully with exit code 0 (given valid inputs)

---

### Requirement: Update all intra-plugin imports referencing context_router
Any Python import statement or module reference within the plugin that refers to `context_router` must be updated to `context_manager`.

#### Scenario: Python imports resolve after rename
- **WHEN** any plugin Python file that previously imported from `context_router` is executed
- **THEN** the import succeeds without `ModuleNotFoundError`

#### Scenario: No stale Python imports remain
- **WHEN** all `.py` files under the plugin directory are searched for the string `context_router`
- **THEN** zero matches are found (excluding comments that document the rename history, if any)

---

### Requirement: Update all test references to context_router
Test files that reference `context_router` — whether by import, path string, or fixture — must be updated to use `context_manager`.

#### Scenario: Tests reference the new module name
- **WHEN** all test files (under `tests/` or equivalent) are searched for `context_router`
- **THEN** zero matches are found (excluding comments documenting the rename)

#### Scenario: Existing tests pass after rename
- **WHEN** the full test suite is executed after the rename
- **THEN** no test fails due to a missing `context_router` module or unresolved `context_router` path

---

### Requirement: Update skill and markdown references to context_router
Any `.md` skill file, documentation, or prompt template that mentions `context_router` or `context_router.py` must be updated to `context_manager` / `context_manager.py`.

#### Scenario: Skill files use new name
- **WHEN** all `.md` files under `skills/` are searched for the string `context_router`
- **THEN** zero matches are found

#### Scenario: CLAUDE.md or plugin documentation uses new name
- **WHEN** all markdown files at the project root and in documentation directories are searched for `context_router`
- **THEN** zero matches are found (excluding a changelog entry or migration note, if present)

---

### Requirement: No functional behavior change from the rename
The rename is strictly a refactor. No business logic, function signatures, class names, or public API surface may change as part of this capability.

#### Scenario: Module public interface is unchanged
- **WHEN** the set of public functions and classes exported by `scripts/context_manager.py` is compared to the pre-rename `scripts/context_router.py`
- **THEN** the sets are identical (same names, same signatures)

#### Scenario: End-to-end context assembly produces identical output
- **WHEN** the context manager is invoked with the same inputs before and after the rename
- **THEN** the output (assembled context) is byte-identical

---

### Requirement: Internal self-references within the module are updated
If `context_router.py` contains any self-referential strings (e.g., logging statements that include the module name, `__name__` comparisons, or error messages citing `context_router`), these must be updated to `context_manager`.

#### Scenario: Log messages reference new module name
- **WHEN** `scripts/context_manager.py` emits log output that includes its own module name
- **THEN** the log output contains `context_manager`, not `context_router`

#### Scenario: No hardcoded self-references to old name
- **WHEN** `scripts/context_manager.py` is searched for the literal string `context_router`
- **THEN** zero matches are found