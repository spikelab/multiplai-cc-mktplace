"""What `/buildme <parent>` does when pm-spec split the parent (Decision 11).

pm-spec writes the epic: the parent's `.change.yaml` lists `children` (child
change names, `<parent>-<id>`, in wave order) and each child's `.change.yaml`
lists `depends_on` (child change names, `[]` for none). buildme reads only
those records, never pm-spec's own state, and never scans for `parent`: the
list says which children the epic has.

`run_epic_from_specs` checks, writing nothing until every check passes:

- every listed child is named `<parent>-<id>`, has its change dir (or
  merged: its change dir is in specs/archive/, or the saved epic.json says
  so), and its `depends_on` names only listed children, with no cycle;
- every listed child's specs are ready (`multiplai_core.specs.is_ready`),
  a merged one's aside;
- the parent's change dir and every listed child's (a merged one's aside)
  have no uncommitted change (`readiness.uncommitted`) and are in HEAD,
  because each child that depends on nothing branches from that commit.

Any failure prints `SPEC:NOT_READY:<reason>` and the pm-spec line, and exits
2. Then it reads HEAD's sha once (the commit that holds the ready specs),
takes the epic lock, loads or starts `epic.json` with the children as the
change dirs list them now (the saved status of each child still listed is
kept), and runs the coordinator. The parent's run writes nothing into the
parent's change dir: no checkpoint, progress file or board record, which
would leave it uncommitted for the next run.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from multiplai_core.specs import archived_change_dirs, is_ready, read_change_meta

from .. import git_ops, readiness
from . import lock as epic_lock
from . import store

log = logging.getLogger(__name__)

EXIT_REFUSED = 2
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _error(message: str) -> None:
    log.error("EPIC %s", message)
    print(f"ERROR:{message}", file=sys.stderr, flush=True)


def _children(config) -> list:
    """The parent's `.change.yaml` `children`, or [] when it has none or cannot be read."""
    try:
        meta = read_change_meta(Path(config.change_dir))
    except (OSError, UnicodeError, ValueError):
        return []
    children = meta.get("children")
    return children if isinstance(children, list) else []


def is_epic(config) -> bool:
    """True when the change's `.change.yaml` lists a non-empty `children` (pm-spec split it)."""
    return bool(_children(config))


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle as a path of ids (first id repeated at the end), or None."""
    state: dict[str, int] = {}          # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
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


def _read_children(name: str, listed: list, changes: Path, saved: store.Epic | None = None
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
        if not child.startswith(prefix) or not ID_RE.match(child[len(prefix):]):
            return [], (name, f"the parent {name}'s children list names {raw!r}, which is not {prefix}<id>"), set()
        if child in names:
            return [], (name, f"the parent {name}'s children list names {child} twice"), set()
        names.append(child)
    children: list[store.EpicChild] = []
    merged: set[str] = set()
    for child in names:
        child_dir = changes / child
        if not child_dir.is_dir():
            cid = child[len(prefix):]
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
                return [], (name, f"child {child}: its change dir {child_dir} does not exist"), set()
            merged.add(child)
        try:
            deps = read_change_meta(child_dir).get("depends_on")
        except (OSError, UnicodeError, ValueError) as e:
            return [], (child, f"child {child}: {e}"), set()
        deps = [] if deps is None else deps
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            return [], (child, f"child {child}: depends_on in its .change.yaml is not a list of change names"), set()
        for dep in deps:
            if dep not in names:
                return [], (name, f"child {child} depends on {dep}, which the parent {name} does not list "
                                  "as a child"), set()
        children.append(store.EpicChild(id=child[len(prefix):],
                                        depends_on=list(dict.fromkeys(d[len(prefix):] for d in deps))))
    cycle = _cycle({c.id: c.depends_on for c in children})
    if cycle:
        return [], (name, "the children's depends_on have a cycle: " + " -> ".join(prefix + c for c in cycle)), set()
    return children, None, merged


def _uncommitted_reason(project: Path, change_dir: Path) -> str | None:
    """Why *change_dir* cannot be built from HEAD: uncommitted changes, or git cannot tell."""
    try:
        paths = readiness.uncommitted(project, change_dir)
    except git_ops.GitLifecycleError as e:
        return f"cannot tell whether {change_dir} is committed: {e}"
    if paths:
        return (f"{change_dir} has uncommitted changes ({', '.join(paths)}); the children branch from the "
                "commit that holds the specs, so commit them first")
    return None


async def run_epic_from_specs(config, args) -> int:
    """Build the children pm-spec split *config*'s change into, in dependency
    order, with the coordinator's review, fix and merge loops. Returns the
    coordinator's exit code (`coordinator.run_epic`), or 2 when a check
    refused (`SPEC:NOT_READY`) or another process holds the epic lock, with
    nothing written."""
    name = config.change_name
    parent_dir = Path(config.change_dir)
    project = Path(config.project_dir)
    changes = parent_dir.parent

    repo = git_ops.repo_root(project) or project
    epic_dir = store.epic_dir_for(repo, name)
    children, refused, merged = _read_children(name, _children(config), changes, _saved(epic_dir))
    if refused:
        return readiness.refuse_not_ready(*refused)
    live = [c for c in children if f"{name}-{c.id}" not in merged]
    for c in live:
        child = f"{name}-{c.id}"
        ready, why = is_ready(changes / child)
        if not ready:
            return readiness.refuse_not_ready(child, f"child {child}: {why}")
    for change_dir, advice in ((parent_dir, name), *((changes / f"{name}-{c.id}", f"{name}-{c.id}")
                                                     for c in live)):
        why = _uncommitted_reason(project, change_dir)
        if why:
            return readiness.refuse_not_ready(advice, why)

    specs_sha = git_ops.rev_parse_head(project)
    if specs_sha is None:
        return readiness.refuse_not_ready(name, f"cannot read HEAD in {project}, the commit that holds the specs")
    for c in live:
        rel = _rel(repo, changes / f"{name}-{c.id}")
        if rel is None or _tree(repo, specs_sha, rel) is None:
            return readiness.refuse_not_ready(
                f"{name}-{c.id}", f"child {name}-{c.id}: its change dir is not in HEAD ({specs_sha[:12]}); "
                "the children branch from the commit that holds the specs, so commit it first")

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
    epic.child_status = {cid: s for cid, s in epic.child_status.items() if any(c.id == cid for c in children)}
    log.info("EPIC %s: %d children from the change dirs, specs at %s", name, len(children), specs_sha[:12])
    # The coordinator runs under the lock this process holds now (item 6).
    from . import coordinator
    try:
        return await coordinator.run_epic(config, args, epic_dir, epic, specs_sha)
    except Exception as e:      # the summary is written; exit 1 as any build's unexpected error
        log.error("FAIL epic change=%s error=%s", name, e, exc_info=True)
        print(f"ERROR:{e}", file=sys.stderr, flush=True)
        return 1


def _saved(epic_dir: Path) -> store.Epic | None:
    """The epic.json an earlier run saved, read before the lock for the
    children checks only; None when there is none or it cannot be read."""
    try:
        return store.load_epic(epic_dir) if (epic_dir / store.EPIC_FILE).exists() else None
    except store.EpicFileError:
        return None


def _rel(repo: Path, path: Path) -> str | None:
    """*path* relative to the repository root, in POSIX form; None outside it."""
    try:
        return Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return None


def _tree(repo: Path, rev: str, rel: str) -> str | None:
    """The object id of *rel* at *rev*; None when *rev* does not hold it or git cannot tell."""
    res = git_ops._run(["git", "rev-parse", "--verify", "--quiet", f"{rev}:{rel}"], cwd=repo)
    out = res.stdout.strip()
    return out if res.ok and out else None
