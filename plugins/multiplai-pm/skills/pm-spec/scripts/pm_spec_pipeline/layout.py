"""Where a change's specs sit, in either layout, read through `multiplai_core.specs`.

A change with several components holds them in `components/<id>/`, each with
its `plan.md` and `specs.md`; a change with one component keeps `plan.md` and
`specs.md` in the change dir itself. A change written in the old layout (one
file per capability, read by `requirement_texts`) is still read: pm-spec
adopts such a change and checks it, but never writes that layout.

The spec files of a change are its `units`: per spec dir (each component, or
the change dir), `specs.md`, or else each old-layout capability file. Paths are
relative to the change dir. The revision of the whole change is the change
dir's `spec_revision` together with each component's: the change dir's ready
record does not cover its components' files, so each component gets its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from multiplai_core.specs import (
    component_dirs,
    is_ready,
    mark_draft,
    mark_ready,
    read_change_meta,
    spec_revision,
    split_capabilities,
    valid_requirement_text,
    valid_specs_text,
)

SPECS_FILE = "specs.md"
PLAN_FILE = "plan.md"
OLD_DIR = "requirements"


def spec_dirs(change_dir: Path) -> list[Path]:
    """Each component's dir, or the change dir when it has no components."""
    return component_dirs(change_dir) or [Path(change_dir)]


def rel(change_dir: Path, path: Path) -> str:
    return Path(path).relative_to(change_dir).as_posix()


def units(change_dir: Path) -> list[str]:
    """The spec files of the change, relative to it: per spec dir, `specs.md`
    when it is there, else the old layout's capability files."""
    out: list[str] = []
    for d in spec_dirs(change_dir):
        if (d / SPECS_FILE).is_file():
            out.append(rel(change_dir, d / SPECS_FILE))
        else:
            out += [rel(change_dir, p) for p in sorted((d / OLD_DIR).glob("*.md"))]
    return out


def unit_text_valid(unit: str, text: str) -> bool:
    if Path(unit).name == SPECS_FILE:
        return valid_specs_text(text)
    return valid_requirement_text(text)


def unit_capabilities(unit: str, text: str) -> list[str]:
    """The capability names one spec file holds; `[]` when they cannot be read."""
    if Path(unit).name != SPECS_FILE:
        return [Path(unit).stem]
    try:
        return list(split_capabilities(text))
    except ValueError:
        return []


def capability_units(change_dir: Path) -> dict[str, list[str]]:
    """`{capability: [spec files that hold it]}`."""
    out: dict[str, list[str]] = {}
    for unit in units(change_dir):
        for cap in unit_capabilities(unit, (Path(change_dir) / unit).read_text()):
            out.setdefault(cap, []).append(unit)
    return out


def all_specs_text(change_dir: Path) -> str:
    """Every spec file of the change, each under a `### <path>` heading."""
    parts = [
        f"### {u}\n{(Path(change_dir) / u).read_text()}" for u in units(change_dir)
    ]
    return "\n\n".join(parts) or "(none)"


_OUTLINE_LINE = re.compile(r"^(# Capability:|##\s|### Requirement:)")


def outline(text: str) -> str:
    """The `# Capability:`, `## ... Requirements` and `### Requirement:` lines
    of a spec file: its capability names and requirement titles."""
    lines = [line.rstrip() for line in text.splitlines() if _OUTLINE_LINE.match(line)]
    return "\n".join(lines) or "(no capability or requirement headings)"


def specs_text(change_dir: Path, full: set[str], *, placeholder: str) -> str:
    """Every spec file of the change under a `### <path>` heading: in full when
    it is in *full*, else its `outline` under the heading followed by
    *placeholder*."""
    parts = []
    for u in units(change_dir):
        text = (Path(change_dir) / u).read_text()
        if u in full:
            parts.append(f"### {u}\n{text}")
        else:
            parts.append(f"### {u} {placeholder}\n{outline(text)}")
    return "\n\n".join(parts) or "(none)"


def plans_text(change_dir: Path) -> str:
    """Every component's `plan.md` (or the change's own), each under a `### <path>` heading."""
    parts = [
        f"### {rel(change_dir, d / PLAN_FILE)}\n{(d / PLAN_FILE).read_text()}"
        for d in spec_dirs(change_dir)
        if (d / PLAN_FILE).is_file()
    ]
    return "\n\n".join(parts) or "(none)"


def revision(change_dir: Path) -> str:
    """The change dir's `spec_revision`, or, with components, one hash over it
    and each component's (by its path)."""
    change_dir = Path(change_dir)
    comps = component_dirs(change_dir)
    root = spec_revision(change_dir)
    if not comps:
        return root
    digest = hashlib.sha256(root.encode())
    for d in comps:
        digest.update(f"\0{rel(change_dir, d)}\0{spec_revision(d)}".encode())
    return digest.hexdigest()


def dependency_digest(change_dir: Path) -> str:
    """A sha256 over each component's `depends_on` (by its path) and the change's
    `components.md`, which `revision` does not hash: a block saves it, so an
    edit that only orders the components reopens the step (row S58). A
    `.change.yaml` that cannot be read is hashed as its text."""
    change_dir = Path(change_dir)
    digest = hashlib.sha256()
    for d in component_dirs(change_dir):
        try:
            deps = json.dumps(
                sorted(str(x) for x in read_change_meta(d).get("depends_on") or [])
            )
        except ValueError:
            deps = (d / ".change.yaml").read_text(errors="replace")
        digest.update(f"{rel(change_dir, d)}\0{deps}\0".encode())
    components_md = change_dir / "components.md"
    if components_md.is_file():
        digest.update(b"components.md\0" + components_md.read_bytes())
    return digest.hexdigest()


def ready(change_dir: Path) -> tuple[bool, str]:
    """`is_ready` of the change dir and of every component."""
    for d in [Path(change_dir), *component_dirs(change_dir)]:
        ok, reason = is_ready(d)
        if not ok:
            where = "" if d == Path(change_dir) else f"{rel(change_dir, d)}: "
            return False, where + reason
    return True, ""


def mark_all_ready(change_dir: Path) -> str:
    """The ready record on each component, then on the change dir; the change's `revision`."""
    for d in component_dirs(change_dir):
        mark_ready(d)
    mark_ready(change_dir)
    return revision(change_dir)


def mark_all_draft(change_dir: Path) -> None:
    for d in [Path(change_dir), *component_dirs(change_dir)]:
        if (d / ".change.yaml").is_file():
            mark_draft(d)
