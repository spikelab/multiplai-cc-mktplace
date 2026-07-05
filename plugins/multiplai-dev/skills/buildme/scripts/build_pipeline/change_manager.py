"""Change manager — manages the specs/ directory for buildme.

Handles change directory creation, artifact status tracking (dependency DAG),
template/context assembly, archiving with delta spec merging, and change listing.

Directory layout:
    specs/
      config.yaml          — project context
      changes/<name>/      — active changes
        proposal.md
        design.md
        tasks.md
        rubric.md
        requirements/      — BDD scenarios (one file per capability)
          <capability>.md
      registry/            — merged main state (one file per capability)
        <capability>.md
      archive/<date>-<name>/ — completed changes
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import date
from pathlib import Path

import yaml

from .models import ArtifactInfo, ArtifactStatus, ChangeStatus

log = logging.getLogger(__name__)

# The spec-driven artifact DAG — hardcoded since we only use one schema.
ARTIFACT_DAG: dict[str, dict] = {
    "proposal": {"generates": "proposal.md", "requires": []},
    "requirements": {"generates": "requirements/*.md", "requires": ["proposal"]},
    "design": {"generates": "design.md", "requires": ["proposal"]},
    "tasks": {"generates": "tasks.md", "requires": ["requirements", "design"]},
    "rubric": {"generates": "rubric.md", "requires": ["tasks"]},
}

# Templates for each artifact type
TEMPLATES: dict[str, str] = {
    "proposal": """\
## Why

<!-- Explain the motivation for this change. What problem does this solve? Why now? -->

## What Changes

<!-- Describe what will change. Be specific about new capabilities, modifications, or removals. -->

## Capabilities

### New Capabilities
<!-- Capabilities being introduced. Each creates requirements/<name>.md -->
- `<name>`: <brief description>

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS are changing. Leave empty if none. -->

## Impact

<!-- Affected code, APIs, dependencies, systems -->
""",
    "requirements": """\
## ADDED Requirements

### Requirement: <!-- requirement name -->
<!-- requirement text -->

#### Scenario: <!-- scenario name -->
- **WHEN** <!-- condition -->
- **THEN** <!-- expected outcome -->
""",
    "design": """\
## Context

<!-- Background and current state -->

## Goals / Non-Goals

**Goals:**
<!-- What this design aims to achieve -->

**Non-Goals:**
<!-- What is explicitly out of scope -->

## Decisions

<!-- Key design decisions and rationale -->

## Risks / Trade-offs

<!-- Known risks and trade-offs -->
""",
    "tasks": """\
## 1. <!-- Block Name -->

<!-- 2-4 sentences: what this block delivers, key behaviors, acceptance criteria. -->

Satisfies: <!-- spec references -->
""",
    "rubric": """\
# Evaluation Rubric: <!-- change-name -->

## Code Architecture (weight: 2)
| Score | Criteria |
|-------|----------|
| 5 | Modules have single responsibility, clear boundaries, minimal coupling. |
| 3 | Reasonable structure but some modules do too much. |
| 1 | God objects, circular dependencies, ignores established patterns. |

## Test Quality (weight: 1)
| Score | Criteria |
|-------|----------|
| 5 | Tests verify behavior, not implementation. Edge cases covered. |
| 3 | Happy path tested, some edge cases. |
| 1 | Coverage theater — tests pass but verify nothing meaningful. |

## Spec Compliance (weight: 3)
| Score | Criteria |
|-------|----------|
| 5 | Every WHEN/THEN scenario from specs is demonstrably implemented and tested. |
| 3 | Most scenarios implemented, 1-2 edge cases missing. |
| 1 | Core scenarios missing or incorrectly implemented. |
""",
}

# Instructions for each artifact type (guidance for LLM generation)
INSTRUCTIONS: dict[str, str] = {
    "proposal": (
        "Create the proposal document that establishes WHY this change is needed. "
        "Sections: Why, What Changes, Capabilities (with kebab-case names for specs), Impact. "
        "Keep concise (1-2 pages). Focus on the 'why' not the 'how'."
    ),
    "requirements": (
        "Create one requirements file per capability listed in the proposal, "
        "at requirements/<capability>.md. "
        "Use WHEN/THEN format for scenarios. Each requirement MUST have at least one scenario. "
        "Scenarios must be testable — you know when they pass."
    ),
    "design": (
        "Create the design document explaining HOW to implement the change. "
        "Sections: Context, Goals/Non-Goals, Decisions (with alternatives), Risks/Trade-offs."
    ),
    "tasks": (
        "Create the task list breaking down implementation into blocks. "
        "Each block maps to one spec's worth of work with 2-4 sentence descriptions."
    ),
    "rubric": (
        "Create the evaluation rubric tailored to this change type. "
        "Include Code Architecture (weight 2), Test Quality (weight 1), Spec Compliance (weight 3). "
        "Add change-specific dimensions (UI: design fidelity; API: endpoint design)."
    ),
}


class ChangeManager:
    """Manages spec-driven change directories, artifact tracking, and archiving."""

    def __init__(self, specs_dir: Path):
        self.specs_dir = specs_dir
        self.changes_dir = specs_dir / "changes"
        self.registry_dir = specs_dir / "registry"
        self.archive_dir = specs_dir / "archive"

    def init_specs(self) -> None:
        """Initialize the specs/ directory structure."""
        self.changes_dir.mkdir(parents=True, exist_ok=True)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def create_change(self, name: str) -> Path:
        """Create a change directory with metadata."""
        name = self._normalize_name(name)
        change_dir = self.changes_dir / name
        if change_dir.exists():
            log.info("Change '%s' already exists", name)
            return change_dir

        change_dir.mkdir(parents=True, exist_ok=True)
        metadata = {"schema": "spec-driven", "created": date.today().isoformat()}
        (change_dir / ".change.yaml").write_text(yaml.dump(metadata, default_flow_style=False))
        log.info("Created change '%s' at %s", name, change_dir)
        return change_dir

    def artifact_status(self, change_dir: Path) -> dict[str, ArtifactStatus]:
        """Check which artifacts are done/ready/blocked."""
        result: dict[str, ArtifactStatus] = {}
        for artifact_id, spec in ARTIFACT_DAG.items():
            if self._artifact_exists(change_dir, spec["generates"]):
                result[artifact_id] = ArtifactStatus.DONE
            elif all(result.get(dep) == ArtifactStatus.DONE for dep in spec["requires"]):
                result[artifact_id] = ArtifactStatus.READY
            else:
                result[artifact_id] = ArtifactStatus.BLOCKED
        return result

    def ready_artifacts(self, change_dir: Path) -> list[str]:
        """Return IDs of artifacts that can be created next."""
        status = self.artifact_status(change_dir)
        return [aid for aid, s in status.items() if s == ArtifactStatus.READY]

    def change_status(self, change_dir: Path) -> ChangeStatus:
        """Full status object."""
        status = self.artifact_status(change_dir)
        artifacts = [
            ArtifactInfo(
                id=aid,
                generates=ARTIFACT_DAG[aid]["generates"],
                requires=ARTIFACT_DAG[aid]["requires"],
                status=s,
            )
            for aid, s in status.items()
        ]
        return ChangeStatus(
            change_name=change_dir.name,
            artifacts=artifacts,
            is_complete=all(s == ArtifactStatus.DONE for s in status.values()),
        )

    def artifact_template(self, artifact_id: str) -> str:
        """Return the markdown template for an artifact type."""
        return TEMPLATES.get(artifact_id, "")

    def artifact_context(self, change_dir: Path, artifact_id: str) -> dict:
        """Assemble context for LLM generation of an artifact.

        Returns dict with: template, instruction, context (from config.yaml),
        dependencies (paths to completed dependency artifacts).
        """
        spec = ARTIFACT_DAG.get(artifact_id, {})

        # Load project context from config.yaml
        config_path = self.specs_dir / "config.yaml"
        project_context = ""
        if config_path.exists():
            try:
                data = yaml.safe_load(config_path.read_text()) or {}
                project_context = data.get("context", "")
            except yaml.YAMLError:
                pass

        # Resolve dependency file paths
        dep_paths: dict[str, str] = {}
        for dep_id in spec.get("requires", []):
            dep_generates = ARTIFACT_DAG[dep_id]["generates"]
            if "*" in dep_generates:
                matches = list(change_dir.glob(dep_generates))
                dep_paths[dep_id] = ", ".join(str(m.relative_to(change_dir)) for m in matches)
            else:
                dep_paths[dep_id] = dep_generates

        return {
            "template": self.artifact_template(artifact_id),
            "instruction": INSTRUCTIONS.get(artifact_id, ""),
            "context": project_context,
            "dependencies": dep_paths,
            "output_path": spec.get("generates", ""),
        }

    def list_changes(self) -> list[dict]:
        """List active changes with status."""
        if not self.changes_dir.exists():
            return []
        result = []
        for d in sorted(self.changes_dir.iterdir()):
            if d.is_dir() and (d / ".change.yaml").exists():
                status = self.artifact_status(d)
                done = sum(1 for s in status.values() if s == ArtifactStatus.DONE)
                result.append({
                    "name": d.name,
                    "path": str(d),
                    "artifacts_done": done,
                    "artifacts_total": len(ARTIFACT_DAG),
                    "is_complete": done == len(ARTIFACT_DAG),
                })
        return result

    def archive_change(self, change_dir: Path, merge_specs: bool = True) -> Path:
        """Archive a completed change.

        Moves change to archive/YYYY-MM-DD-<name>/ and optionally merges
        delta requirements into the main registry/.
        """
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_name = f"{date.today().isoformat()}-{change_dir.name}"
        dest = self.archive_dir / archive_name

        if merge_specs:
            self._merge_delta_requirements(change_dir)

        shutil.move(str(change_dir), str(dest))
        log.info("Archived change '%s' to %s", change_dir.name, dest)
        return dest

    # --- Internal helpers ---

    def _artifact_exists(self, change_dir: Path, generates: str) -> bool:
        """Check if an artifact's output file(s) exist."""
        if "*" in generates:
            return len(list(change_dir.glob(generates))) > 0
        return (change_dir / generates).exists()

    def _normalize_name(self, name: str) -> str:
        """Normalize a change name to kebab-case."""
        name = re.sub(r"[^\w\s-]", "", name.lower())
        return re.sub(r"[\s_]+", "-", name).strip("-")

    def _merge_delta_requirements(self, change_dir: Path) -> None:
        """Merge delta requirements from a change into the main registry.

        Reads flat files from changes/<name>/requirements/<capability>.md and
        merges them into registry/<capability>.md.
        """
        delta_dir = change_dir / "requirements"
        if not delta_dir.exists():
            return

        self.registry_dir.mkdir(parents=True, exist_ok=True)
        for delta_file in sorted(delta_dir.glob("*.md")):
            capability_name = delta_file.stem
            target_file = self.registry_dir / f"{capability_name}.md"

            delta_content = delta_file.read_text()
            if not target_file.exists():
                # New capability — copy the delta as the main registry file
                target_file.write_text(delta_content)
                log.info("Created main registry: %s", target_file)
                continue

            # Merge into existing main registry file
            main_content = target_file.read_text()
            merged = self._apply_delta(main_content, delta_content)
            target_file.write_text(merged)
            log.info("Merged delta into: %s", target_file)

    def _apply_delta(self, main: str, delta: str) -> str:
        """Apply delta spec operations (ADDED/MODIFIED/REMOVED) to main spec."""
        # Extract ADDED requirements — append to main
        added_match = re.search(
            r"## ADDED Requirements\s*\n(.*?)(?=\n## (?:MODIFIED|REMOVED|RENAMED) Requirements|\Z)",
            delta, re.DOTALL,
        )
        if added_match:
            added_block = added_match.group(1).strip()
            if added_block:
                main = main.rstrip() + "\n\n" + added_block + "\n"

        # Extract REMOVED requirements — remove matching blocks from main
        removed_match = re.search(
            r"## REMOVED Requirements\s*\n(.*?)(?=\n## (?:ADDED|MODIFIED|RENAMED) Requirements|\Z)",
            delta, re.DOTALL,
        )
        if removed_match:
            removed_block = removed_match.group(1)
            req_names = re.findall(r"### Requirement:\s*(.+)", removed_block)
            for name in req_names:
                pattern = re.compile(
                    rf"### Requirement:\s*{re.escape(name.strip())}.*?"
                    rf"(?=### Requirement:|\Z)",
                    re.DOTALL,
                )
                main = pattern.sub("", main)

        # Extract MODIFIED requirements — replace matching blocks
        modified_match = re.search(
            r"## MODIFIED Requirements\s*\n(.*?)(?=\n## (?:ADDED|REMOVED|RENAMED) Requirements|\Z)",
            delta, re.DOTALL,
        )
        if modified_match:
            mod_block = modified_match.group(1)
            mod_reqs = re.split(r"(?=### Requirement:)", mod_block)
            for req in mod_reqs:
                name_match = re.match(r"### Requirement:\s*(.+)", req)
                if not name_match:
                    continue
                name = name_match.group(1).strip()
                pattern = re.compile(
                    rf"### Requirement:\s*{re.escape(name)}.*?"
                    rf"(?=### Requirement:|\Z)",
                    re.DOTALL,
                )
                # Use a function replacement so backslash sequences in the
                # requirement text (e.g. a regex like `\d+` or `\1` in a BDD
                # scenario) are inserted literally instead of being interpreted
                # as regex backreferences (which would raise re.error or corrupt
                # the output).
                replacement = req.strip() + "\n\n"
                main = pattern.sub(lambda _m: replacement, main)

        return main.strip() + "\n"
