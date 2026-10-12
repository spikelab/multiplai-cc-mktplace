"""`open`, `edit` and `render`: build the body, gate it, write it.

Each subcommand logs one start line and one done or failed line through
`multiplai_core.log_utils.log_event` (subcommand, PR, template path, gate
outcome, diff size). No body text goes in the log.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core.log_utils import log_event

from . import fill, gates, github, markers
from .template import Section, TemplateError, discover, load, load_config

log = logging.getLogger(__name__)


class Refused(Exception):
    """Refused, or `gh`/`git`/the model call failed. Exit code 2."""


class GateFailed(Exception):
    """A gate rejected the body; nothing was written. Exit code 1."""


@dataclass
class Rendered:
    body: str
    template: Path
    standard: bool
    sections: list[Section]
    assembled: fill.Assembled
    sources: fill.Sources
    diagnoses: list[str] = field(default_factory=list)


def _ask(prompt: str) -> str:
    try:
        return asyncio.run(fill._run_model(prompt))
    except Exception as e:  # AgentRunError, a missing CLI, no credentials: all end this run the same way
        raise Refused(f"the model call that writes the template's other sections failed: {e}") from e


def render_body(repo: Path, change_dir: Path, *, base: str | None, head: str, ticket_url: str | None,
                section_files: list[Path]) -> Rendered:
    """Template + sources (+ one model call when the template needs prose) → a gated body.

    A failed gate, or an answer that does not parse, is re-asked once with
    the reason; a second failure raises GateFailed."""
    from multiplai_core.structured import StructuredAnswerError, parse_answer, reformat_prompt

    try:
        found = discover(repo)
        sections = load(found.path)
    except TemplateError as e:
        raise Refused(str(e)) from e
    try:
        src = fill.read_sources(repo, change_dir, base=base, head=head, ticket_url=ticket_url,
                                section_files=section_files)
    except fill.SourceError as e:
        raise Refused(str(e)) from e
    diagnoses = [found.diagnosis] if found.diagnosis else []
    if src.size is None and src.size_note:
        diagnoses.append(f"diff size not measured: {src.size_note}")
    plans = fill.plan_sections(sections)
    box_ids = {b.id for s in sections for b in s.checkboxes}
    needs_model = any(p.kind == "model" for p in plans) or bool(box_ids)

    if not needs_model:
        done = fill.assemble(sections, plans, src, {}, set())
        result = gates.run_open_gates(sections, done.body, done.owned, set(), {}, [])
        if not result.ok:
            raise GateFailed(result.message())
        return Rendered(done.body, found.path, found.standard, sections, done, src, diagnoses)

    prompt = fill.build_prompt(sections, plans, src, gates.MODEL_TARGET_WORDS)
    text = _ask(prompt)
    problem = ""
    for attempt in (1, 2):
        try:
            answer = parse_answer(text, fill.FillAnswer)
        except (StructuredAnswerError, ValueError) as e:
            problem = f"the answer was not the JSON object asked for: {e}"
            if attempt == 2:
                break
            text = _ask(reformat_prompt(text, fill.FillAnswer, e))
            continue
        model_text = fill.model_sections(answer, sections, plans)
        ticks = fill.verified_ticks(answer, box_ids, src)
        done = fill.assemble(sections, plans, src, model_text, ticks)
        result = gates.run_open_gates(sections, done.body, done.owned, ticks, model_text, done.model_keys)
        if result.ok:
            return Rendered(done.body, found.path, found.standard, sections, done, src, diagnoses)
        problem = result.message()
        if attempt == 2:
            break
        text = _ask(fill.build_prompt(sections, plans, src, gates.MODEL_TARGET_WORDS, problem=problem))
    raise GateFailed(problem)


def _resolve_repo(path: str) -> Path:
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir() or github.repo_root(repo) is None:
        raise Refused(f"--repo {path} is not a git repository")
    return repo


def _change_dir(path: str) -> Path:
    d = Path(path).expanduser().resolve()
    if not d.is_dir():
        raise Refused(f"--change-dir {path} is not a directory")
    return d


def _body_file(body: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".md", prefix="pr-body-", delete=False)
    with f:
        f.write(body)
    return Path(f.name)


def _diagnose(lines: list[str], stream=None) -> None:
    for line in lines:
        print(f"PR_DIAGNOSIS:{line}", file=stream or sys.stdout, flush=True)


def _template_rel(r: Rendered, repo: Path) -> str:
    return "standard" if r.standard else str(r.template.relative_to(repo) if r.template.is_relative_to(repo)
                                             else r.template)


def _size_field(r: Rendered | None) -> str:
    s = r.sources.size if r else None
    return f"{s.files} files, +{s.added} -{s.removed}" if s else ""


# --- render -------------------------------------------------------------------


def cmd_render(args) -> int:
    repo = _resolve_repo(args.repo)
    change_dir = _change_dir(args.change_dir)
    head = github.current_branch(repo)
    base = github.git_default_branch(repo)
    r = render_body(repo, change_dir, base=base, head=head, ticket_url=None,
                    section_files=[Path(p) for p in args.section])
    if r.sources.size:
        print(r.sources.size.line(), file=sys.stderr, flush=True)
    _diagnose(r.diagnoses, sys.stderr)
    args._log_fields = {"template": _template_rel(r, repo), "diff": _size_field(r)}
    sys.stdout.write(r.body)
    sys.stdout.flush()
    return 0


# --- open ---------------------------------------------------------------------


def cmd_open(args) -> int:
    repo = _resolve_repo(args.repo)
    change_dir = _change_dir(args.change_dir)
    head = args.head or github.current_branch(repo)
    if not head or head == "HEAD":
        raise Refused("no branch to open the PR from: pass --head, or check out a branch")
    try:
        config = load_config(repo)
    except TemplateError as e:
        raise Refused(str(e)) from e
    draft = args.draft or (not args.ready and config.draft)
    base = args.base or github.gh_default_branch(repo) or github.git_default_branch(repo)
    r = render_body(repo, change_dir, base=base, head=head, ticket_url=args.ticket_url,
                    section_files=[Path(p) for p in args.section])
    title = args.title or f"buildme: {r.sources.change_name}"
    if r.sources.size:
        print(r.sources.size.line(), flush=True)
    _diagnose(r.diagnoses)

    body_file = Path("<body-file>") if args.dry_run else _body_file(r.body)
    create = github.create_argv(title, body_file, head, args.base, draft)
    if args.dry_run:
        if not args.no_push:
            print("DRY_RUN:" + github.Result(github.push_argv(head), 0, "", "").command, flush=True)
        print("DRY_RUN:" + github.Result(create, 0, "", "").command, flush=True)
        print("DRY_RUN_BODY:", flush=True)
        sys.stdout.write(r.body)
        return 0

    try:
        if not args.no_push:
            res = github.push(repo, head)
            if not res.ok:
                raise Refused(f"`{res.command}` failed: {res.why()}")
            print(f"PUSHED:{head}", flush=True)
        res = github.run_write(repo, create)
        if not res.ok:
            raise Refused(f"`gh pr create` failed (exit {res.returncode}): {res.why()}")
    finally:
        body_file.unlink(missing_ok=True)
    url = github.pr_url(res.stdout)
    if url:
        print(f"PR:{url}", flush=True)
    args._log_fields = {"pr": url or "", "template": _template_rel(r, repo), "diff": _size_field(r)}
    return 0


# --- edit ---------------------------------------------------------------------


def _can_edit(repo: Path, pr: dict) -> tuple[bool, str]:
    permission = github.viewer_permission(repo)
    if permission in github.EDIT_ROLES:
        return True, permission
    author = (pr.get("author") or {}).get("login") or ""
    login = github.viewer_login(repo)
    return bool(author and login and author == login), permission or "unknown"


def _adopt(current: str, done: fill.Assembled) -> str:
    """A hand-opened body: the managed lines on top, the existing text unchanged, then the
    skill's sections."""
    rendered = done.body.split("\n\n", 1)[1] if "\n\n" in done.body else done.body
    header = f"{markers.MANAGED}\n{markers.written_line(done.written)}\n\n"
    existing = current.rstrip("\n")
    return header + (existing + "\n\n" if existing else "") + rendered


def cmd_edit(args) -> int:
    repo = _resolve_repo(args.repo)
    change_dir = _change_dir(args.change_dir)
    pr, res = github.view_pr(repo, args.pr)
    if pr is None:
        raise Refused(f"`{res.command}` failed: {res.why()}")
    number = str(pr.get("number") or args.pr)
    url = pr.get("url") or ""
    current = (pr.get("body") or "").replace("\r\n", "\n")
    head = pr.get("headRefName") or github.current_branch(repo)
    base = pr.get("baseRefName") or github.git_default_branch(repo)
    args._log_fields = {"pr": url or number}

    managed = markers.is_managed(current)
    try:
        markers.spans(current)
    except markers.MarkerError as e:
        raise Refused(f"PR {number}'s body has markers this skill cannot edit safely: {e}") from e
    if not managed and markers.has_pr_token(current):
        raise Refused(f"PR {number}'s body holds `<!-- pr:` markers but no `{markers.MANAGED}` line; "
                      "not editing it")

    if not managed:
        ok, permission = _can_edit(repo, pr)
        if not ok:
            raise Refused(f"no rights to edit PR {number} (viewerPermission={permission})")

    r = render_body(repo, change_dir, base=base, head=head, ticket_url=None,
                    section_files=[Path(p) for p in args.section])
    args._log_fields.update(template=_template_rel(r, repo), diff=_size_field(r))
    if r.sources.size:
        print(r.sources.size.line(), flush=True)
    _diagnose(r.diagnoses)

    done = r.assembled
    if managed:
        owned = done.owned
        if not r.sources.ticket_url:
            # `edit` has no --ticket-url, and ticket.md has no `url:` line: keep the link that
            # `open --ticket-url` wrote instead of overwriting it with "not applicable".
            ticket_keys = {r.sections[p.index].key for p in fill.plan_sections(r.sections)
                           if p.kind == "code:ticket"}
            owned = {k: v for k, v in owned.items() if k not in ticket_keys}
        merged = markers.merge(current, owned, done.template_keys, done.tails)
        body, changed = merged.body, merged.changed
        _diagnose([f"section removed by a human, not restored: {k}" for k in merged.not_restored])
        _diagnose([f"section not in the PR body, not added: {k}" for k in merged.not_added])
    else:
        body, changed = _adopt(current, done), list(done.owned)

    result = gates.GateResult(gates.check_words(gates.edit_word_counts(body), what="the text this skill owns"))
    if not result.ok:
        raise GateFailed(result.message())

    comment = None
    if not managed:
        comment = ("The pr skill (`/multiplai-dev:pr`) edited this PR's description. It kept the existing "
                   "text unchanged and added these sections below it: "
                   + ", ".join(f"`{k}`" for k in changed) + ".\n")

    body_file = Path("<body-file>") if args.dry_run else _body_file(body)
    comment_file = Path("<comment-file>") if args.dry_run else (_body_file(comment) if comment else None)
    edit = github.edit_argv(number, body_file)
    if args.dry_run:
        if args.push:
            print("DRY_RUN:" + github.Result(github.push_argv(head), 0, "", "").command, flush=True)
        print("DRY_RUN:" + github.Result(edit, 0, "", "").command, flush=True)
        if comment:
            print("DRY_RUN:" + github.Result(github.comment_argv(number, comment_file), 0, "", "").command,
                  flush=True)
        print("DRY_RUN_BODY:", flush=True)
        sys.stdout.write(body)
        return 0

    try:
        if args.push:
            res = github.push(repo, head)
            if not res.ok:
                raise Refused(f"`{res.command}` failed: {res.why()}")
            print(f"PUSHED:{head}", flush=True)
        res = github.run_write(repo, edit)
        if not res.ok:
            raise Refused(f"`gh pr edit` failed (exit {res.returncode}): {res.why()}")
        if comment_file is not None:
            res = github.run_write(repo, github.comment_argv(number, comment_file))
            if not res.ok:
                raise Refused(f"the description was edited, but `gh pr comment` failed "
                              f"(exit {res.returncode}): {res.why()}")
    finally:
        body_file.unlink(missing_ok=True)
        if comment_file is not None:
            comment_file.unlink(missing_ok=True)
    print(f"PR_EDITED:{url or number}", flush=True)
    args._log_fields["changed"] = ",".join(changed)
    return 0


COMMANDS = {"open": cmd_open, "edit": cmd_edit, "render": cmd_render}


def run(args) -> int:
    """Run one subcommand; map its outcome onto the exit codes and the log."""
    args._log_fields = {}
    pr_arg = getattr(args, "pr", "") or ""
    log_event("pr", "start", f"pr {args.command} started", subcommand=args.command, pr=pr_arg)
    try:
        code = COMMANDS[args.command](args)
    except GateFailed as e:
        print(f"PR_DIAGNOSIS:a gate rejected the body; nothing was written: {e}", flush=True)
        log_event("pr", "failed", f"pr {args.command}: a gate rejected the body", level="WARNING",
                  subcommand=args.command, gate="rejected", **args._log_fields)
        return 1
    except Refused as e:
        print(f"PR_DIAGNOSIS:{e}", flush=True)
        log_event("pr", "failed", f"pr {args.command} refused or failed", level="WARNING",
                  subcommand=args.command, gate="passed-or-not-run", reason=str(e)[:300], **args._log_fields)
        return 2
    except Exception as e:  # an unreadable file, or a bug: exit 2, never Python's 1, which means a gate
        log.exception("pr %s failed", args.command)
        print(f"PR_DIAGNOSIS:pr {args.command} failed: {type(e).__name__}: {e}", flush=True)
        log_event("pr", "failed", f"pr {args.command} failed: {type(e).__name__}", level="ERROR",
                  subcommand=args.command, gate="not-run", **args._log_fields)
        return 2
    log_event("pr", "done", f"pr {args.command} done", subcommand=args.command, gate="passed",
              **args._log_fields)
    return code
