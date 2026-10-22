"""Change manager — manages the specs/ directory for buildme.

Handles artifact status tracking (dependency DAG), template/context
assembly, archiving with delta spec merging, and change listing.

Directory layout:
    specs/
      config.yaml          — project context
      changes/<name>/      — active changes
        .change.yaml       — pm-spec's record, `spec_status: ready` included
        proposal.md        — pm-spec's (an input buildme checks for)
        use-cases.md       — pm-spec's: personas + the use cases they must observe
        requirements/      — pm-spec's: BDD scenarios (one file per capability)
          <capability>.md
        design.md          — buildme's, from here down
        unknowns.md
        tasks.md
        rubric.md
      registry/            — merged main state (one file per capability)
        <capability>.md
      archive/<date>-<name>/ — completed changes
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import yaml

# The spec format is multiplai-core's; the rest of build_pipeline imports these
# names from this module.
from multiplai_core.specs import (
    archive_change as _archive_change,
)
from multiplai_core.specs import (
    archived_change_dirs,  # noqa: F401
    expected_capabilities,
    merge_delta_requirements,
    normalize_change_name,
    valid_requirement,
    valid_requirement_text,  # noqa: F401
)

from .models import ArtifactInfo, ArtifactStatus, ChangeStatus

log = logging.getLogger(__name__)


# The spec-driven artifact DAG — hardcoded since we only use one schema.
_GLOBAL_CONSTRAINTS_RE = re.compile(
    r"^##\s+Global Constraints\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def extract_global_constraints(design_text: str) -> str:
    """Verbatim body of design.md's `## Global Constraints` section ("" when
    absent). These are the project-wide rules (version floors, naming, exact
    literals) every block's agents must honor — threaded into agent and
    review prompts by the TDD engine."""
    m = _GLOBAL_CONSTRAINTS_RE.search(design_text)
    return m.group(1).strip() if m else ""


# The "what" documents pm-spec writes and marks ready: buildme reads them as
# required inputs, checks that they are there, and never generates them.
INPUTS: dict[str, str] = {
    "proposal": "proposal.md",
    "use-cases": "use-cases.md",
    "requirements": "requirements/*.md",
}

# buildme's planning artifacts. Insertion order is load-bearing:
# `artifact_status` resolves each node against the statuses it has already
# computed, so every node must appear AFTER the nodes it requires or it reads
# its dependency as absent and reports BLOCKED. A requirement may also name an
# input (`INPUTS`), done when its file is there.
ARTIFACT_DAG: dict[str, dict] = {
    "design": {"generates": "design.md", "requires": ["proposal", "requirements"]},
    # Explainer gate (B1): before the build depends on anything new to this
    # project, write down its contract and edge cases. Sits between design and
    # tasks so the edge cases are on disk while the task breakdown is written —
    # and so the test_writer can turn them into tests.
    "unknowns": {"generates": "unknowns.md", "requires": ["design"]},
    # `tasks` is what carries the use cases: a use case is delivered by a block
    # or by nothing at all, so the breakdown is written with the use cases on
    # disk — that is also what PLAN_REVIEW checks block coverage against.
    "tasks": {
        "generates": "tasks.md",
        "requires": ["proposal", "requirements", "design", "unknowns", "use-cases"],
    },
    "rubric": {"generates": "rubric.md", "requires": ["tasks"]},
}


def _generates(node: str) -> str:
    return INPUTS[node] if node in INPUTS else ARTIFACT_DAG[node]["generates"]


# Templates for each artifact type
TEMPLATES: dict[str, str] = {
    "design": """\
## Context

<!-- Background and current state -->

## Decisions

<!-- Key design decisions and rationale -->

## Risks / Trade-offs

<!-- Known risks and trade-offs -->
""",
    "unknowns": """\
# Unknowns — what we are about to depend on

<!-- One `## <dependency>` section per dependency that is new to this project. -->

## <dependency-name>

### What it is
<!-- One paragraph: what this is and what job it does here. -->

### The contract we rely on
<!-- The exact API/behavior this change calls, and what it promises to return. -->

### Edge cases & failure modes
<!-- One bullet per case. Cover: empty input, malformed input, oversized input,
     concurrent use, and offline/unavailable. Name the OBSERVED behavior, not
     "may fail" — e.g. "Whisper transcribes silence as 'thanks for watching'". -->
- <case>: <what actually happens>

### Assumptions we are making
<!-- One bullet per assumption, each phrased so it can be proven wrong. -->
- <assumption stated as a falsifiable claim>

### How we would find out cheaply
<!-- The smallest experiment that tests the assumptions above. -->
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
| 5 | Tests verify behavior through observable outcomes, not implementation. Edge cases covered. Mocks honor the real collaborator's contract. |
| 3 | Happy path tested, some edge cases. Occasional mock-heavy test. |
| 1 | Coverage theater — assertions only interrogate mocks (`.called`, `assert_called_*`), mock setup outweighs assertions, tests sleep instead of polling, or tests pass while verifying nothing meaningful. |

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
    "design": (
        "Create the design document explaining HOW to implement the change. "
        "Sections: Context, Decisions (with alternatives), Risks/Trade-offs. "
        "Goals and Non-Goals belong to the proposal's What Changes section — "
        "do not restate them here."
    ),
    "unknowns": (
        "Write one section per dependency that is new to this project, covering: "
        "What it is, The contract we rely on, Edge cases & failure modes, "
        "Assumptions we are making, How we would find out cheaply. "
        "Every Edge cases list and every Assumptions list has at least one bullet. "
        "Edge cases name observed behavior on empty, malformed, oversized, "
        "concurrent, and offline/unavailable input. Assumptions are phrased as "
        "falsifiable claims."
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
        metadata_path = change_dir / ".change.yaml"
        if change_dir.exists() and metadata_path.exists():
            log.info("Change '%s' already exists", name)
            return change_dir

        if change_dir.exists():
            # A directory without .change.yaml is not a real change — e.g. junk
            # left by an older pipeline version. Repair it instead of silently
            # early-returning, which would suppress the metadata forever.
            log.warning(
                "Change dir '%s' exists without .change.yaml — writing the missing metadata",
                name,
            )
        change_dir.mkdir(parents=True, exist_ok=True)
        metadata = {"schema": "spec-driven", "created": date.today().isoformat()}
        metadata_path.write_text(yaml.dump(metadata, default_flow_style=False))
        log.info("Created change '%s' at %s", name, change_dir)
        return change_dir

    def artifact_status(
        self, change_dir: Path, stale: list[str] | None = None
    ) -> dict[str, ArtifactStatus]:
        """Check which artifacts are done/ready/blocked."""
        result: dict[str, ArtifactStatus] = {}
        done = {
            name
            for name, generates in INPUTS.items()
            if self._artifact_exists(change_dir, generates)
        }
        for artifact_id, spec in ARTIFACT_DAG.items():
            if not all(
                dep in done or result.get(dep) == ArtifactStatus.DONE
                for dep in spec["requires"]
            ):
                result[artifact_id] = ArtifactStatus.BLOCKED
            elif artifact_id not in (stale or []) and self._artifact_exists(
                change_dir, spec["generates"]
            ):
                result[artifact_id] = ArtifactStatus.DONE
            else:
                result[artifact_id] = ArtifactStatus.READY
        return result

    def missing_inputs(self, change_dir: Path) -> list[str]:
        """The required inputs (`INPUTS`) not on disk, as their file patterns.
        Requirements count only when every capability the proposal names has
        a valid file."""
        return [
            generates
            for generates in INPUTS.values()
            if not self._artifact_exists(change_dir, generates)
        ]

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
        dependencies (paths to completed dependency artifacts), registry (the
        sorted capability names in specs/registry/).
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
            dep_generates = _generates(dep_id)
            if "*" in dep_generates:
                matches = list(change_dir.glob(dep_generates))
                dep_paths[dep_id] = ", ".join(
                    str(m.relative_to(change_dir)) for m in matches
                )
            else:
                dep_paths[dep_id] = dep_generates

        return {
            "template": self.artifact_template(artifact_id),
            "instruction": INSTRUCTIONS.get(artifact_id, ""),
            "context": project_context,
            "dependencies": dep_paths,
            "output_path": spec.get("generates", ""),
            "registry": sorted(p.stem for p in self.registry_dir.glob("*.md")),
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
                result.append(
                    {
                        "name": d.name,
                        "path": str(d),
                        "artifacts_done": done,
                        "artifacts_total": len(ARTIFACT_DAG),
                        "is_complete": done == len(ARTIFACT_DAG),
                    }
                )
        return result

    def archive_change(self, change_dir: Path, merge_specs: bool = True) -> Path:
        """Archive a completed change: `multiplai_core.specs.archive_change`."""
        return _archive_change(change_dir, self.specs_dir, merge_specs=merge_specs)

    # --- Internal helpers ---

    def _artifact_exists(self, change_dir: Path, generates: str) -> bool:
        """Check if an artifact's output file(s) exist."""
        if generates == "requirements/*.md":
            proposal = change_dir / "proposal.md"
            if not proposal.is_file():
                return False
            capabilities = expected_capabilities(proposal.read_text())
            return bool(capabilities) and all(
                valid_requirement(change_dir / "requirements" / f"{name}.md")
                for name in capabilities
            )
        path = change_dir / generates
        return path.is_file() and bool(path.read_text().strip())

    def _normalize_name(self, name: str) -> str:
        """Normalize a change name to kebab-case."""
        return normalize_change_name(name)

    def _merge_delta_requirements(self, change_dir: Path) -> None:
        """Merge the change's delta requirements into registry/:
        `multiplai_core.specs.merge_delta_requirements`."""
        merge_delta_requirements(change_dir, self.registry_dir)
