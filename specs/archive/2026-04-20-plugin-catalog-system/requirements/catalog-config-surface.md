## ADDED Requirements

### Requirement: Catalog model configuration
The `plugin.json` file MUST expose a `catalog_model` entry under `userConfig` that specifies which model to use for LLM-based catalog generation calls via `model_client`.

#### Scenario: Default catalog model
- **WHEN** `plugin.json` is loaded and no user override is set for `catalog_model`
- **THEN** the default value is `"claude-sonnet-4-6"`

#### Scenario: User overrides catalog model
- **WHEN** a user sets `catalog_model` to `"claude-haiku-4-5"` in their config
- **THEN** all catalog generation LLM calls use `"claude-haiku-4-5"` as the model parameter passed to `model_client`

#### Scenario: Invalid model value
- **WHEN** a user sets `catalog_model` to an empty string or a value not recognized by `model_client`
- **THEN** catalog generation fails with a descriptive error message indicating the invalid model configuration

---

### Requirement: Reasoning effort configuration
The `plugin.json` file MUST expose a `catalog_reasoning_effort` entry under `userConfig` that controls the reasoning effort level passed to the LLM for catalog generation.

#### Scenario: Default reasoning effort
- **WHEN** `plugin.json` is loaded and no user override is set for `catalog_reasoning_effort`
- **THEN** the default value is `"medium"`

#### Scenario: Valid reasoning effort values
- **WHEN** a user sets `catalog_reasoning_effort` to `"low"`, `"medium"`, or `"high"`
- **THEN** the value is accepted and passed through to `model_client` calls during catalog generation

#### Scenario: Invalid reasoning effort value
- **WHEN** a user sets `catalog_reasoning_effort` to a value outside `["low", "medium", "high"]`
- **THEN** config validation rejects the value and the default `"medium"` is used instead

---

### Requirement: Catalog TTL configuration
The `plugin.json` file MUST expose a `catalog_ttl_hours` entry under `userConfig` that specifies how many hours a generated catalog remains valid before being considered stale.

#### Scenario: Default TTL
- **WHEN** `plugin.json` is loaded and no user override is set for `catalog_ttl_hours`
- **THEN** a sensible default TTL is defined (e.g., `24`)

#### Scenario: Custom TTL is respected
- **WHEN** a user sets `catalog_ttl_hours` to `12`
- **THEN** catalogs older than 12 hours are treated as stale and eligible for regeneration

#### Scenario: TTL of zero forces always-regenerate
- **WHEN** a user sets `catalog_ttl_hours` to `0`
- **THEN** catalogs are always considered stale and regenerated on every trigger (equivalent to force mode)

#### Scenario: Negative TTL is rejected
- **WHEN** a user sets `catalog_ttl_hours` to a negative number
- **THEN** config validation rejects the value and the default is used instead

---

### Requirement: Diary window size configuration
The `plugin.json` file MUST expose a `diary_window_days` entry under `userConfig` that controls how many days of diary history the diary catalog generator processes.

#### Scenario: Default diary window
- **WHEN** `plugin.json` is loaded and no user override is set for `diary_window_days`
- **THEN** a default lookback window is defined (e.g., `30`)

#### Scenario: Custom diary window
- **WHEN** a user sets `diary_window_days` to `7`
- **THEN** the diary catalog generator only processes diary entries from the last 7 days

#### Scenario: Diary window of zero
- **WHEN** a user sets `diary_window_days` to `0`
- **THEN** no diary entries are processed and the diary catalog is generated as empty

#### Scenario: Negative diary window is rejected
- **WHEN** a user sets `diary_window_days` to a negative number
- **THEN** config validation rejects the value and the default is used instead

---

### Requirement: Skills catalog gate configuration
The `plugin.json` file MUST expose an `enable_skills_catalog` boolean entry under `userConfig` that gates whether the skills catalog generator runs.

#### Scenario: Skills catalog disabled by default
- **WHEN** `plugin.json` is loaded and no user override is set for `enable_skills_catalog`
- **THEN** the default value is `false` and the skills catalog generator is skipped during dispatch

#### Scenario: Skills catalog enabled
- **WHEN** a user sets `enable_skills_catalog` to `true`
- **THEN** the catalog dispatcher includes the skills catalog generator in its run

#### Scenario: Skills catalog explicitly disabled
- **WHEN** a user sets `enable_skills_catalog` to `false`
- **THEN** the catalog dispatcher skips the skills catalog generator entirely and does not produce or update a skills catalog file

---

### Requirement: Resources catalog gate configuration
The `plugin.json` file MUST expose an `enable_resources_catalog` boolean entry under `userConfig` that gates whether the resources catalog generator runs, in conjunction with `resources_dir`.

#### Scenario: Resources catalog disabled by default
- **WHEN** `plugin.json` is loaded and no user override is set for `enable_resources_catalog`
- **THEN** the default value is `false` and the resources catalog generator is skipped during dispatch

#### Scenario: Resources catalog enabled with resources_dir set
- **WHEN** a user sets `enable_resources_catalog` to `true` and `resources_dir` is configured to a valid directory path
- **THEN** the catalog dispatcher includes the resources catalog generator in its run

#### Scenario: Resources catalog enabled but resources_dir not set
- **WHEN** a user sets `enable_resources_catalog` to `true` but `resources_dir` is not configured or is empty
- **THEN** the resources catalog generator is skipped and a warning is logged indicating that `resources_dir` must be set

---

### Requirement: Config entries follow plugin.json userConfig schema
All new config entries MUST be declared in the `userConfig` section of `plugin.json` with proper type, default value, and description fields conforming to the existing plugin config schema.

#### Scenario: All config entries present in plugin.json
- **WHEN** `plugin.json` is parsed
- **THEN** the `userConfig` section contains entries for `catalog_model`, `catalog_reasoning_effort`, `catalog_ttl_hours`, `diary_window_days`, `enable_skills_catalog`, and `enable_resources_catalog`, each with `type`, `default`, and `description` fields

#### Scenario: Config types are correct
- **WHEN** `plugin.json` is validated
- **THEN** `catalog_model` has type `string`, `catalog_reasoning_effort` has type `string`, `catalog_ttl_hours` has type `number`, `diary_window_days` has type `number`, `enable_skills_catalog` has type `boolean`, and `enable_resources_catalog` has type `boolean`

---

### Requirement: Config values are accessible to generators via config module
All catalog config entries MUST be readable through `scripts/lib/config.py` so that generators can retrieve their values at runtime.

#### Scenario: Config module exposes catalog settings
- **WHEN** a generator calls the config module to read `catalog_model`
- **THEN** it receives the user-configured value or the default if no override is set

#### Scenario: Config module reflects user overrides
- **WHEN** a user has overridden `diary_window_days` to `14` in their config
- **THEN** calling the config module for `diary_window_days` returns `14`

#### Scenario: Config module returns defaults for unset values
- **WHEN** no user overrides exist for any catalog config entry
- **THEN** the config module returns the default value declared in `plugin.json` for each entry