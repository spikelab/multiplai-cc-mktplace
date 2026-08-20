"""Memory bank CLI — list, sync, check, contribute, adopt.

Every verb that changes something outside this process is **dry-run by
default** and needs an explicit ``--apply``. That is not caution theatre:
``contribute`` puts the user's writing into somebody else's git repository, and
``adopt`` deletes the user's own memory. Those are the two operations in the
whole memory system with a blast radius outside the machine, so both of them
ask twice.

    memory_bank.py list
    memory_bank.py sync [--bank NAME] [--force]
    memory_bank.py check
    memory_bank.py contribute --proposal PATH [--bank NAME] [--apply]
    memory_bank.py adopt NAME [--file f.md ...] [--apply]

``sync`` is also what SessionStart launches, detached: it is TTL-gated, it
fast-forwards only, and a failure is logged and swallowed. A bank that cannot
be reached is stale, not broken, and a session must never fail because a
teammate's remote is down.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import load_yaml, save_yaml
from multiplai_core.log_utils import log_event, setup_logging
from multiplai_core.paths import get_paths

from lib import bank_adopt, bank_proposals
from lib.bank_collisions import find_collisions, render_report
from lib.bank_git import head_sha, is_git_repo, pull_ff_only
from lib.bank_policy import load_policy
from lib.banks import configured_banks, shared_banks, sync_ttl_hours

logger = setup_logging("memory_bank")

_STATE_FILE = "bank_sync_state.yaml"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_path() -> Path:
    return get_paths().data_dir() / _STATE_FILE


def _load_state() -> dict:
    try:
        return load_yaml(_state_path()) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        save_yaml(path, state)
    except Exception:
        logger.debug("Could not persist bank sync state", exc_info=True)


def _due(state: dict, name: str, ttl_hours: float) -> bool:
    raw = ((state.get("banks") or {}).get(name) or {}).get("last_sync")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return _now() - last >= timedelta(hours=ttl_hours)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def cmd_list(args) -> int:
    banks = configured_banks()
    rows = []
    for bank in banks:
        present = bank.path.exists()
        rows.append(
            {
                "name": bank.name,
                "mode": bank.mode,
                "shared": bank.is_shared,
                "path": str(bank.path),
                "present": present,
                "remote": bank.remote,
                "files": len(list(bank.path.glob("*.md"))) if present else 0,
                "head": head_sha(bank.path)[:12] if present and is_git_repo(bank.path) else "",
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{len(rows)} memory bank(s):\n")
    for row in rows:
        flag = "shared" if row["shared"] else "personal"
        state = "" if row["present"] else "  [MISSING — run `sync`]"
        print(f"  {row['name']:<20} {flag:<9} mode={row['mode']:<8} "
              f"{row['files']:>3} file(s){state}")
        print(f"  {'':<20} {row['path']}")
        if row["remote"]:
            print(f"  {'':<20} remote: {row['remote']}  head: {row['head'] or '?'}")
        if row["shared"] and row["present"]:
            policy = load_policy(Path(row["path"]), bank=row["name"])
            print(f"  {'':<20} {policy.summary}")
        print()
    return 0


def cmd_sync(args) -> int:
    """Fast-forward each shared bank. Never fails the caller."""
    ttl = sync_ttl_hours()
    state = _load_state()
    state.setdefault("banks", {})
    banks = [b for b in shared_banks() if not args.bank or b.name == args.bank]
    if not banks:
        logger.info("No shared banks to sync")
        return 0
    synced, skipped, failed = 0, 0, 0
    for bank in banks:
        if not bank.remote:
            skipped += 1
            continue
        if not args.force and not _due(state, bank.name, ttl):
            skipped += 1
            continue
        if not bank.path.exists():
            logger.warning(
                "Bank %s is not cloned at %s — clone it once with "
                "`git clone %s %s`", bank.name, bank.path, bank.remote, bank.path,
            )
            failed += 1
            continue
        result = pull_ff_only(bank.path)
        if result.ok:
            synced += 1
            state["banks"][bank.name] = {
                "last_sync": _now().isoformat(),
                "head": head_sha(bank.path),
            }
        else:
            failed += 1
            # Stale-but-working: the previously synced content is untouched.
            logger.warning("Bank %s did not sync: %s", bank.name, result.detail)
    _save_state(state)
    log_event(
        "banks", "sync",
        f"synced {synced}, skipped {skipped}, failed {failed}",
        level="WARNING" if failed else "INFO",
        synced=synced, skipped=skipped, failed=failed,
    )
    if not args.quiet:
        print(f"banks: synced={synced} skipped={skipped} failed={failed}")
    # Deliberately 0 even on failure: this runs detached from SessionStart and
    # a non-zero exit there is noise about a network, not a defect.
    return 0


def _catalog_entries(name: str) -> list[dict]:
    path = get_paths().catalogs_dir() / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


def cmd_check(args) -> int:
    """Report cross-bank collisions. A defect report, never a resolution."""
    personal = [{**e, "bank": e.get("bank") or "personal"} for e in _catalog_entries("memory")]
    banked = _catalog_entries("banks")
    collisions = find_collisions(personal + banked)
    if not collisions:
        print("No cross-bank collisions. A fact lives in exactly one bank.")
        return 0
    print(render_report(collisions))
    return 1


def cmd_contribute(args) -> int:
    from lib import dream_triage

    proposal_path = Path(args.proposal).expanduser()
    try:
        proposal = proposal_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"Cannot read proposal: {e}", file=sys.stderr)
        return 2

    # `classify` alone, deliberately: no judge, no model. A bank contribution
    # is the user's own text going to their own team; the semantic verdict is
    # about local writes, and this path must work with no SDK client at all.
    triage = dream_triage.classify(proposal)
    items = dream_triage.shared_bank_items(triage)
    if args.bank:
        items = tuple(i for i in items if i.target.startswith(f"{args.bank}/"))
    if not items:
        print("No shared-bank items in this proposal.")
        return 0

    plans = bank_proposals.plan_contributions(items)
    dreams_dir = get_paths().dreams_dir()
    out_dir = dreams_dir / "banks"
    exit_code = 0
    for plan in plans:
        body = bank_proposals.render_contribution_file(plan)
        if not args.dry_run_only:
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            (out_dir / f"{stamp}-{plan.bank.name}-contribution.md").write_text(
                body, encoding="utf-8"
            )
        print(body)
        report = bank_proposals.submit(plan, dry_run=not args.apply)
        print(json.dumps(report, indent=2))
        if report.get("errors"):
            exit_code = 1
    return exit_code


def cmd_adopt(args) -> int:
    bank = bank_adopt.resolve_bank(args.name)
    if bank is None:
        print(f"No subscribed shared bank named '{args.name}'.", file=sys.stderr)
        return 2
    memory_dir = get_paths().memory_dir()
    plan = bank_adopt.plan_adoption(
        bank,
        memory_dir=memory_dir,
        personal_entries=[
            {**e, "bank": e.get("bank") or "personal"} for e in _catalog_entries("memory")
        ],
        bank_entries=[e for e in _catalog_entries("banks") if e.get("bank") == bank.name],
    )
    for err in plan.errors:
        print(f"! {err}", file=sys.stderr)

    if not args.file:
        print(f"# Adoption plan — bank `{bank.name}`\n")
        if not plan.candidates:
            print("No personal memory overlaps this bank's declared domains.")
            return 0
        for candidate in plan.candidates:
            print(f"- {candidate.label}")
            for reason in candidate.reasons:
                print(f"    - {reason}")
        print()
        if plan.is_conversation:
            print(
                f"This would touch {len(plan.candidates)} personal files. That is "
                "more than a handful — talk it through before adopting the rest.\n"
            )
        print(
            "Adoption is two steps. First contribute the content "
            "(`memory_bank.py contribute --proposal … --apply`) and get the PR "
            "merged and pulled. Then, and only then:\n"
            f"  memory_bank.py adopt {bank.name} --file <name.md> --apply\n"
            "Nothing is deleted that is not already in the bank, line for line."
        )
        return 0

    report = bank_adopt.finalize(
        bank, args.file, memory_dir=memory_dir, dry_run=not args.apply
    )
    receipt = bank_adopt.render_receipt(bank, report, plan)
    print(receipt)
    if args.apply:
        receipts = get_paths().dreams_dir() / "applied"
        receipts.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = receipts / f"{stamp}-bank-adopt-{bank.name}-receipt.md"
        n = 2
        while path.exists():
            path = receipts / f"{stamp}-bank-adopt-{bank.name}-receipt-{n}.md"
            n += 1
        path.write_text(receipt, encoding="utf-8")
        print(f"Receipt: {path}")
    return 1 if report.get("errors") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_bank.py", description="Manage shared memory banks"
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="Show configured banks and their state")
    p_list.add_argument("--json", action="store_true", help="Machine-readable output")
    p_list.set_defaults(func=cmd_list)

    p_sync = sub.add_parser("sync", help="Fast-forward shared banks (TTL-gated)")
    p_sync.add_argument("--bank", default="", help="Only this bank")
    p_sync.add_argument("--force", action="store_true", help="Ignore the TTL")
    p_sync.add_argument("--quiet", action="store_true", help="No stdout")
    p_sync.set_defaults(func=cmd_sync)

    p_check = sub.add_parser("check", help="Report cross-bank collisions")
    p_check.set_defaults(func=cmd_check)

    p_contrib = sub.add_parser(
        "contribute", help="Turn shared-bound proposal items into a bank pull request"
    )
    p_contrib.add_argument("--proposal", required=True, help="Dream proposal path")
    p_contrib.add_argument("--bank", default="", help="Only this bank")
    p_contrib.add_argument(
        "--apply", action="store_true",
        help="Actually branch, commit, push and open the PR (default: report only)",
    )
    p_contrib.add_argument(
        "--dry-run-only", action="store_true",
        help="Do not even write the contribution record to dreams/banks/",
    )
    p_contrib.set_defaults(func=cmd_contribute)

    p_adopt = sub.add_parser(
        "adopt", help="Migrate personal memory into a bank and delete the local copy"
    )
    p_adopt.add_argument("name", help="Bank name")
    p_adopt.add_argument(
        "--file", action="append", default=[],
        help="A personal memory file to adopt. Repeatable. Required to act — "
             "there is no adopt-everything.",
    )
    p_adopt.add_argument(
        "--apply", action="store_true",
        help="Actually delete the named personal files (default: report only)",
    )
    p_adopt.set_defaults(func=cmd_adopt)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
