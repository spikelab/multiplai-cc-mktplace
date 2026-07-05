"""Migration: legacy openspec/ layout → new specs/ layout.

The legacy layout was:
    openspec/
      config.yaml
      changes/
        archive/                          ← archive nested under changes
        <name>/
          .openspec.yaml                  ← per-change metadata
          proposal.md, design.md, ...
          specs/                          ← BDD scenarios (nested by capability)
            <capability>/
              spec.md
      specs/                              ← main spec registry (also "specs"!)
        <capability>/
          spec.md

The new layout is:
    specs/
      config.yaml
      changes/
        <name>/
          .change.yaml                    ← renamed
          proposal.md, design.md, ...
          requirements/                   ← renamed from specs/
            <capability>.md               ← flattened
      registry/                           ← renamed from specs/
        <capability>.md                   ← flattened
      archive/                            ← moved to top level
        <date>-<name>/

This module performs the rename in place. Idempotent — safe to re-run.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


def migrate_project(project_dir: Path, dry_run: bool = False) -> dict:
    """Migrate a project from openspec/ to specs/ layout.

    Returns a dict describing what was (or would be) changed.
    """
    project_dir = project_dir.resolve()
    legacy = project_dir / "openspec"
    target = project_dir / "specs"

    report: dict = {
        "project": str(project_dir),
        "legacy_dir": str(legacy),
        "target_dir": str(target),
        "actions": [],
        "dry_run": dry_run,
    }

    if not legacy.exists():
        if target.exists():
            report["actions"].append("already_migrated")
            return report
        report["actions"].append("no_openspec_dir")
        return report

    if target.exists():
        report["actions"].append(
            f"ERROR: both openspec/ and specs/ exist — refusing to overwrite {target}"
        )
        return report

    # In dry-run, inspect the legacy tree and report what would change.
    # In real run, do the moves and recurse into the new tree.

    if dry_run:
        _preview(legacy, report)
        return report

    # 1. Move openspec/ → specs/
    shutil.move(str(legacy), str(target))
    report["actions"].append(f"moved {legacy.name}/ → {target.name}/")

    # 2. Rename specs/specs/ (main registry) → specs/registry/
    legacy_registry = target / "specs"
    new_registry = target / "registry"
    if legacy_registry.exists():
        shutil.move(str(legacy_registry), str(new_registry))
        report["actions"].append("renamed specs/ → registry/")
        _flatten_capability_dirs(new_registry, report)

    # 3. Move changes/archive/ → archive/
    legacy_archive = target / "changes" / "archive"
    new_archive = target / "archive"
    if legacy_archive.exists():
        if new_archive.exists():
            # Merge contents
            for item in legacy_archive.iterdir():
                shutil.move(str(item), str(new_archive / item.name))
            legacy_archive.rmdir()
        else:
            shutil.move(str(legacy_archive), str(new_archive))
        report["actions"].append("moved changes/archive/ → archive/")

    # 4. Migrate each active change
    changes_dir = target / "changes"
    if changes_dir.exists():
        for change in sorted(changes_dir.iterdir()):
            if not change.is_dir():
                continue
            _migrate_change(change, dry_run, report)

    # 5. Migrate each archived change (same internal layout migration)
    if new_archive.exists():
        for archived in sorted(new_archive.iterdir()):
            if not archived.is_dir():
                continue
            _migrate_change(archived, dry_run, report)

    return report


def _preview(legacy: Path, report: dict) -> None:
    """Inspect a legacy openspec/ tree and report what migration would do."""
    report["actions"].append(f"would move {legacy.name}/ → specs/")

    # Main registry
    if (legacy / "specs").exists():
        report["actions"].append("would rename specs/ → registry/ and flatten")

    # Archive nesting
    if (legacy / "changes" / "archive").exists():
        report["actions"].append("would move changes/archive/ → archive/")

    # Per-change migrations
    for parent_label, parent in [
        ("changes", legacy / "changes"),
        ("archive", legacy / "changes" / "archive"),
    ]:
        if not parent.exists():
            continue
        for change in sorted(parent.iterdir()):
            if not change.is_dir() or change.name == "archive":
                continue
            actions = []
            if (change / ".openspec.yaml").exists():
                actions.append(".openspec.yaml → .change.yaml")
            if (change / "specs").exists():
                actions.append("specs/ → requirements/ (flattened)")
            if actions:
                report["actions"].append(
                    f"{parent_label}/{change.name}: " + ", ".join(actions)
                )


def _migrate_change(change_dir: Path, dry_run: bool, report: dict) -> None:
    """Migrate a single change directory in place."""
    # Rename .openspec.yaml → .change.yaml
    legacy_meta = change_dir / ".openspec.yaml"
    new_meta = change_dir / ".change.yaml"
    if legacy_meta.exists() and not new_meta.exists():
        if not dry_run:
            legacy_meta.rename(new_meta)
        report["actions"].append(f"{change_dir.name}: .openspec.yaml → .change.yaml")

    # Rename specs/ → requirements/ + flatten
    legacy_reqs = change_dir / "specs"
    new_reqs = change_dir / "requirements"
    if legacy_reqs.exists() and not new_reqs.exists():
        if not dry_run:
            legacy_reqs.rename(new_reqs)
            _flatten_capability_dirs(new_reqs, report)
        report["actions"].append(f"{change_dir.name}: specs/ → requirements/ (flattened)")


def _flatten_capability_dirs(parent: Path, report: dict) -> None:
    """Flatten <capability>/spec.md → <capability>.md inside parent dir."""
    for item in list(parent.iterdir()):
        if not item.is_dir():
            continue
        spec_file = item / "spec.md"
        if spec_file.exists():
            target_file = parent / f"{item.name}.md"
            if target_file.exists():
                report["actions"].append(
                    f"WARN: {target_file} already exists, skipping {spec_file}"
                )
                continue
            spec_file.rename(target_file)
            # Remove the now-empty (or near-empty) capability dir
            try:
                # Remove any leftover files first (e.g., README, notes)
                remaining = list(item.iterdir())
                if not remaining:
                    item.rmdir()
                else:
                    log.warning(
                        "Capability dir %s has extra files: %s — left in place",
                        item, [f.name for f in remaining],
                    )
            except OSError as e:
                log.warning("Could not remove %s: %s", item, e)


def run_migrate(project_dir: Path, dry_run: bool = False) -> int:
    """CLI entry point. Returns exit code (0 = success)."""
    report = migrate_project(project_dir, dry_run=dry_run)

    print(f"Project: {report['project']}")
    print(f"Legacy:  {report['legacy_dir']}")
    print(f"Target:  {report['target_dir']}")
    if dry_run:
        print("DRY RUN — no changes made")
    print()
    print("Actions:")
    if not report["actions"]:
        print("  (none)")
    else:
        for action in report["actions"]:
            print(f"  - {action}")

    if any("ERROR" in a for a in report["actions"]):
        return 1
    return 0
