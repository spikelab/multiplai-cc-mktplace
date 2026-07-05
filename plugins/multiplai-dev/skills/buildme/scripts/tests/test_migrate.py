"""Tests for the openspec → specs migration."""

from build_pipeline.migrate import migrate_project


def test_no_openspec_dir(tmp_path):
    report = migrate_project(tmp_path)
    assert "no_openspec_dir" in report["actions"]


def test_already_migrated(tmp_path):
    (tmp_path / "specs").mkdir()
    report = migrate_project(tmp_path)
    assert "already_migrated" in report["actions"]


def test_refuses_to_overwrite(tmp_path):
    (tmp_path / "openspec").mkdir()
    (tmp_path / "specs").mkdir()
    report = migrate_project(tmp_path)
    assert any("ERROR" in a for a in report["actions"])


def test_full_migration(tmp_path):
    """End-to-end migration of a realistic legacy openspec/ tree."""
    legacy = tmp_path / "openspec"
    legacy.mkdir()

    # Top-level config
    (legacy / "config.yaml").write_text("schema: spec-driven")

    # Active change with the full layout
    change = legacy / "changes" / "add-auth"
    change.mkdir(parents=True)
    (change / ".openspec.yaml").write_text("schema: spec-driven\ncreated: 2026-04-08")
    (change / "proposal.md").write_text("## Why\nadd auth")
    (change / "design.md").write_text("## Decisions")
    (change / "tasks.md").write_text("## 1. Setup")
    (change / "rubric.md").write_text("# Rubric")

    # BDD scenarios in nested capability dirs
    (change / "specs" / "user-login").mkdir(parents=True)
    (change / "specs" / "user-login" / "spec.md").write_text("## ADDED Requirements\n### Requirement: Login")
    (change / "specs" / "session-mgmt").mkdir(parents=True)
    (change / "specs" / "session-mgmt" / "spec.md").write_text("## ADDED Requirements\n### Requirement: Session")

    # Archived change
    archived = legacy / "changes" / "archive" / "2026-03-01-old-feature"
    archived.mkdir(parents=True)
    (archived / ".openspec.yaml").write_text("schema: spec-driven")
    (archived / "specs" / "old-cap").mkdir(parents=True)
    (archived / "specs" / "old-cap" / "spec.md").write_text("# Old spec")

    # Main spec registry (the root-level specs/)
    (legacy / "specs" / "user-login").mkdir(parents=True)
    (legacy / "specs" / "user-login" / "spec.md").write_text("# Login spec (registry)")
    (legacy / "specs" / "session-mgmt").mkdir(parents=True)
    (legacy / "specs" / "session-mgmt" / "spec.md").write_text("# Session spec (registry)")

    report = migrate_project(tmp_path)

    # Should have moved openspec/ → specs/
    target = tmp_path / "specs"
    assert target.exists()
    assert not legacy.exists()

    # config.yaml preserved
    assert (target / "config.yaml").read_text() == "schema: spec-driven"

    # Active change layout
    new_change = target / "changes" / "add-auth"
    assert new_change.exists()
    assert (new_change / ".change.yaml").exists()
    assert not (new_change / ".openspec.yaml").exists()
    assert (new_change / "proposal.md").exists()

    # Requirements flattened from specs/<cap>/spec.md → requirements/<cap>.md
    assert (new_change / "requirements" / "user-login.md").exists()
    assert (new_change / "requirements" / "session-mgmt.md").exists()
    assert "Requirement: Login" in (new_change / "requirements" / "user-login.md").read_text()
    assert not (new_change / "specs").exists()

    # Archive moved up
    assert (target / "archive" / "2026-03-01-old-feature").exists()
    assert not (target / "changes" / "archive").exists()

    # Archived change also got internal migration
    new_archived = target / "archive" / "2026-03-01-old-feature"
    assert (new_archived / ".change.yaml").exists()
    assert (new_archived / "requirements" / "old-cap.md").exists()

    # Main registry: specs/specs/ → specs/registry/, flattened
    assert (target / "registry" / "user-login.md").exists()
    assert (target / "registry" / "session-mgmt.md").exists()
    assert "Login spec (registry)" in (target / "registry" / "user-login.md").read_text()
    assert not (target / "specs").exists()  # Old name gone


def test_dry_run_no_changes(tmp_path):
    legacy = tmp_path / "openspec"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("x")
    (legacy / "changes" / "test").mkdir(parents=True)
    (legacy / "changes" / "test" / ".openspec.yaml").write_text("x")

    report = migrate_project(tmp_path, dry_run=True)

    # Nothing actually moved
    assert legacy.exists()
    assert not (tmp_path / "specs").exists()
    assert report["dry_run"] is True
    assert len(report["actions"]) > 0
