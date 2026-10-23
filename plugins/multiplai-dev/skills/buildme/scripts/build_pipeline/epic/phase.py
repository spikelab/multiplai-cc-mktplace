"""What `/buildme <parent>` does when pm-spec split the parent (Decision 11).

pm-spec writes the epic on the parent's feature branch, which it pushes: the
parent's `.change.yaml` lists `children` (child change names,
`<parent>-<id>`, in wave order) and each child's `.change.yaml` lists
`depends_on` (child change names, `[]` for none). buildme reads only those
records, as `origin/<feature branch>` holds them (`is_epic` too), never
pm-spec's own state, and never scans for `parent`: the list says which
children the epic has.

A change pm-spec gave components instead nests them:
`specs/changes/<parent>/components/<id>/` (`component_dirs`), each with its
own `.change.yaml` whose `depends_on` lists sibling ids. The coordinator runs
them the same way, each building as `<parent>/<id>`, in `depends_on` order. A
component merged into the feature branch keeps its dir there; the saved
epic.json says it merged. Once every component has merged, the coordinator
archives the whole change on the feature branch, once; a later run reads the
change from the branch's `specs/archive/` and archives nothing more.

`run_epic_from_specs` checks, writing nothing until every check passes:

- the parent is on a feature branch (`config.feature_branch`); one ready
  only in the checkout is an old handoff, which `pm-spec resume <parent>`
  moves onto its branch;
- every listed child is named `<parent>-<id>`, has its change dir (or
  merged: its change dir is in specs/archive/, or the saved epic.json says
  so), and its `depends_on` names only listed children, with no cycle;
- every listed child's specs are ready (`multiplai_core.specs.is_ready`),
  a merged one's aside.

Any failure prints `SPEC:NOT_READY:<reason>` and the pm-spec line, and exits
2. Then it takes the epic lock, loads or starts `epic.json` with the children
as the change dirs list them now (the saved status of each child still
listed is kept), and runs the coordinator on the feature branch: each child
starts from it and its PR targets it. The parent's run writes nothing into
the parent's change dir: no checkpoint, progress file or board record.
"""

from __future__ import annotations

import logging
import re
import sys
import tempfile
from pathlib import Path

from multiplai_core.specs import (
    archived_change_dirs,
    change_root,
    component_dirs,
    is_ready,
    read_change_meta,
)

from .. import feature_branch, git_ops, readiness
from . import lock as epic_lock
from . import store

log = logging.getLogger(__name__)

EXIT_REFUSED = 2
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _error(message: str) -> None:
    log.error("EPIC %s", message)
    print(f"ERROR:{message}", file=sys.stderr, flush=True)


def _children(change_dir: Path) -> list:
    """The `.change.yaml` `children` of *change_dir*, or [] when it has none or cannot be read."""
    try:
        meta = read_change_meta(Path(change_dir))
    except (OSError, UnicodeError, ValueError):
        return []
    children = meta.get("children")
    return children if isinstance(children, list) else []


def _specs_rel(config) -> tuple[Path, str] | None:
    """The repository and `specs/` relative to it, or None outside one."""
    repo = git_ops.repo_root(Path(config.project_dir))
    if repo is None:
        return None
    rel = feature_branch.rel_to(repo, Path(config.specs_dir))
    return (repo, rel) if rel is not None else None


def is_epic(config) -> bool:
    """True when the change's `.change.yaml` lists a non-empty `children`
    (pm-spec split it, or gave it components): on `origin/<feature branch>`
    when the change has one, else in the checkout. A component never is."""
    if change_root(Path(config.change_dir)) != Path(config.change_dir):
        return False
    where = _specs_rel(config) if config.feature_branch else None
    if where is None:
        return _has_children(Path(config.change_dir))
    repo, rel = where
    name = Path(config.change_dir).name
    with feature_branch.Copy(
        repo, config.feature_branch, f"{rel}/changes/{name}"
    ) as copy:
        if copy is not None:
            return _has_children(copy)
    # A change with components that its coordinator archived on the branch.
    with feature_branch.Copy(repo, config.feature_branch, f"{rel}/archive") as copy:
        return copy is not None and _archived_components(copy.parent, name) is not None


def _has_children(change_dir: Path) -> bool:
    return bool(_children(change_dir) or component_dirs(change_dir))


def _archived_components(specs: Path, name: str) -> Path | None:
    """The newest `specs/archive/<date>-<name>/` when it holds components,
    else None."""
    archived = archived_change_dirs(specs, name)
    return archived[-1] if archived and component_dirs(archived[-1]) else None


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle as a path of ids (first id repeated at the end), or None."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return path[path.index(dep) :] + [dep]
            if dep not in state:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def _read_children(
    name: str, listed: list, changes: Path, saved: store.Epic | None = None
) -> tuple[list[store.EpicChild], tuple[str, str] | None, set[str]]:
    """The listed children as `EpicChild`s (ids, and `depends_on` as ids), or
    the refusal `(advice change, reason)` for the first check that fails, and
    the listed children whose change dir is gone because they merged.

    A merged `--auto` child's PR moves its change dir to specs/archive/. A
    listed child whose change dir is missing is taken as merged when the
    archive holds it (`archived_change_dirs`; its `depends_on` read there) or
    *saved* (the epic.json of an earlier run) says it merged (its saved
    `depends_on`); otherwise it is refused."""
    prefix = f"{name}-"
    names: list[str] = []
    for raw in listed:
        child = raw if isinstance(raw, str) else ""
        if not child.startswith(prefix) or not ID_RE.match(child[len(prefix) :]):
            return (
                [],
                (
                    name,
                    f"the parent {name}'s children list names {raw!r}, which is not {prefix}<id>",
                ),
                set(),
            )
        if child in names:
            return (
                [],
                (name, f"the parent {name}'s children list names {child} twice"),
                set(),
            )
        names.append(child)
    children: list[store.EpicChild] = []
    merged: set[str] = set()
    for child in names:
        child_dir = changes / child
        if not child_dir.is_dir():
            cid = child[len(prefix) :]
            archived = archived_change_dirs(changes.parent, child)
            was = saved.child_status.get(cid) if saved is not None else None
            if archived:
                child_dir = archived[-1]
            elif was is not None and was.status == "merged":
                deps = next((c.depends_on for c in saved.children if c.id == cid), [])
                children.append(store.EpicChild(id=cid, depends_on=list(deps)))
                merged.add(child)
                continue
            else:
                return (
                    [],
                    (name, f"child {child}: its change dir {child_dir} does not exist"),
                    set(),
                )
            merged.add(child)
        try:
            deps = read_change_meta(child_dir).get("depends_on")
        except (OSError, UnicodeError, ValueError) as e:
            return [], (child, f"child {child}: {e}"), set()
        deps = [] if deps is None else deps
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            return (
                [],
                (
                    child,
                    f"child {child}: depends_on in its .change.yaml is not a list of change names",
                ),
                set(),
            )
        for dep in deps:
            if dep not in names:
                return (
                    [],
                    (
                        name,
                        f"child {child} depends on {dep}, which the parent {name} does not list "
                        "as a child",
                    ),
                    set(),
                )
        children.append(
            store.EpicChild(
                id=child[len(prefix) :],
                depends_on=list(dict.fromkeys(d[len(prefix) :] for d in deps)),
            )
        )
    cycle = _cycle({c.id: c.depends_on for c in children})
    if cycle:
        return (
            [],
            (
                name,
                "the children's depends_on have a cycle: "
                + " -> ".join(prefix + c for c in cycle),
            ),
            set(),
        )
    return children, None, merged


def _read_components(
    name: str, change_dir: Path
) -> tuple[list[store.EpicChild], tuple[str, str] | None]:
    """The components of the change *name* (`component_dirs`, each
    `components/<id>/` holding a `plan.md`) as `EpicChild`s, their
    `depends_on` the sibling ids their `.change.yaml` lists, or the refusal
    `(advice change, reason)` for the first check that fails: an id that is
    not kebab-case, a `depends_on` that is not a list of ids or names an id
    the change has no component for, a dependency cycle. Listed in
    dependency order, then by id."""
    children: list[store.EpicChild] = []
    dirs = component_dirs(change_dir)
    ids = [d.name for d in dirs]
    for d in dirs:
        cid = d.name
        if not ID_RE.match(cid):
            return [], (name, f"component {cid} of {name}: its id is not kebab-case")
        try:
            deps = read_change_meta(d).get("depends_on")
        except (OSError, UnicodeError, ValueError) as e:
            return [], (name, f"component {name}/{cid}: {e}")
        deps = [] if deps is None else deps
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
            return [], (
                name,
                f"component {name}/{cid}: depends_on in its .change.yaml is not a list "
                "of component ids",
            )
        for dep in deps:
            if dep not in ids:
                return [], (
                    name,
                    f"component {name}/{cid} depends on {dep}, which is not a component "
                    f"of {name}",
                )
        children.append(store.EpicChild(id=cid, depends_on=list(dict.fromkeys(deps))))
    cycle = _cycle({c.id: c.depends_on for c in children})
    if cycle:
        return [], (
            name,
            f"the components of {name} depend on each other in a cycle: "
            + " -> ".join(cycle),
        )
    level: dict[str, int] = {}

    def depth(cid: str) -> int:
        if cid not in level:
            deps = next(c.depends_on for c in children if c.id == cid)
            level[cid] = 1 + max((depth(d) for d in deps), default=-1)
        return level[cid]

    return sorted(children, key=lambda c: (depth(c.id), c.id)), None


async def run_epic_from_specs(config, args) -> int:
    """Build the children pm-spec split *config*'s change into, in dependency
    order, with the coordinator's review, fix and merge loops. Returns the
    coordinator's exit code (`coordinator.run_epic`), or 2 when a check
    refused (`SPEC:NOT_READY`) or another process holds the epic lock, with
    nothing written."""
    name = config.change_name
    project = Path(config.project_dir)
    repo = git_ops.repo_root(project) or project
    branch = config.feature_branch
    where = _specs_rel(config)
    if not branch or where is None:
        return readiness.refuse_not_ready(
            name,
            f"the epic {name} is ready in {config.change_dir} but on no feature branch "
            f"(origin has no branch <TICKET>-{name}); its children start from the branch "
            "pm-spec pushed the specs to",
            resume=True,
            ticket_ref=not config.tickets,
        )
    if not config.tickets:
        return readiness.refuse_not_ready(
            name,
            f"the epic {name} on {branch} records no ticket ref; buildme names its children's "
            "branches <TICKET>-<child>",
            resume=True,
            ticket_ref=True,
        )
    epic_dir = store.epic_dir_for(repo, name)
    with tempfile.TemporaryDirectory(prefix="buildme-epic-") as tmp:
        _, rel = where
        try:
            for sub in ("changes", "archive"):
                feature_branch.specs_at(repo, branch, f"{rel}/{sub}", Path(tmp))
        except git_ops.GitLifecycleError as e:
            return readiness.refuse_not_ready(
                name, f"cannot read {rel} on origin/{branch}: {e}"
            )
        changes = Path(tmp) / rel / "changes"
        # A change whose components all merged is archived on the branch by
        # the coordinator; a later run reads it there.
        source = changes / name
        if not source.is_dir():
            source = _archived_components(changes.parent, name) or source
        nested = bool(component_dirs(source))
        if nested:
            # pm-spec nested the components in the change: each builds as
            # `<name>/<id>`; one merged into the feature branch, as the saved
            # epic.json says, is not checked again.
            children, refused = _read_components(name, source)
            saved = _saved(epic_dir)
            merged = {
                f"{name}/{c.id}"
                for c in children
                if saved is not None
                and (was := saved.child_status.get(c.id)) is not None
                and was.status == "merged"
            }
        else:
            children, refused, merged = _read_children(
                name, _children(changes / name), changes, _saved(epic_dir)
            )
        if refused:
            return readiness.refuse_not_ready(*refused)
        live = [c for c in children if child_change(name, c.id, nested) not in merged]
        for c in live:
            child = child_change(name, c.id, nested)
            ready, why = is_ready(
                source / "components" / c.id if nested else changes / child
            )
            if not ready:
                return readiness.refuse_not_ready(
                    child,
                    f"{'component' if nested else 'child'} {child} on origin/{branch}: {why}",
                )

    try:
        epic_lock.hold(epic_dir, name)
    except epic_lock.EpicLocked as e:
        _error(str(e))
        return EXIT_REFUSED
    if (epic_dir / store.EPIC_FILE).exists():
        try:
            epic = store.load_epic(epic_dir)
        except store.EpicFileError as e:
            _error(str(e))
            return 1
    else:
        epic = store.Epic(name=name)
    epic.children = children
    epic.child_status = {
        cid: s
        for cid, s in epic.child_status.items()
        if any(c.id == cid for c in children)
    }
    log.info(
        "EPIC %s: %d children from the change dirs on origin/%s",
        name,
        len(children),
        branch,
    )
    # The coordinator runs under the lock this process holds now (item 6).
    from . import coordinator

    try:
        return await coordinator.run_epic(
            config, args, epic_dir, epic, branch, nested=nested
        )
    except (
        Exception
    ) as e:  # the summary is written; exit 1 as any build's unexpected error
        log.error("FAIL epic change=%s error=%s", name, e, exc_info=True)
        print(f"ERROR:{e}", file=sys.stderr, flush=True)
        return 1


def child_change(name: str, cid: str, nested: bool) -> str:
    """The `--change` a child builds as: `<name>/<id>` for a component
    nested in the change, `<name>-<id>` for a sibling child change."""
    return f"{name}/{cid}" if nested else f"{name}-{cid}"


def _saved(epic_dir: Path) -> store.Epic | None:
    """The epic.json an earlier run saved, read before the lock for the
    children checks only; None when there is none or it cannot be read."""
    try:
        return (
            store.load_epic(epic_dir) if (epic_dir / store.EPIC_FILE).exists() else None
        )
    except store.EpicFileError:
        return None
