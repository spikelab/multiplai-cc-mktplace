# log-doctor — behavioural contract

Run with:

```
uv run --script ../../../multiplai-dev/skills/skill-creator/scripts/promote_skill.py . --contract
```

log-doctor is the skill most likely to be invoked when something is *already*
broken, which is the worst moment to discover the tool itself no longer accepts
the flags its SKILL.md documents. These assertions pin the entry points the
SKILL.md tells the model to reach for — `--list`, `--injections`, `--json`, and
the probe pair — without depending on any particular log content, since the
logs directory is empty on a fresh install and full of unrelated noise on a
used one.

Each case is a shell command plus a substring that must appear in its output.
Commands run with this directory as cwd.

### the documented flags all still exist

```sh
uv run --all-packages --project ../../../.. ../../scripts/log_doctor.py --help
```

Expect: --injections

### --list exits clean on an empty logs directory instead of crashing

Asserted on the exit status, not on the output: with no logs there is nothing
to list, and the only text emitted is log_doctor's own START line. Matching a
substring of that line would pass even if `--list` had stopped working
entirely — which is exactly the vacuous assertion this contract exists to
avoid.

```sh
uv run --all-packages --project ../../../.. ../../scripts/log_doctor.py --list --logs-dir "$(mktemp -d)" >/dev/null 2>&1 && echo list-ok
```

Expect: list-ok

### --json emits parseable JSON

```sh
uv run --all-packages --project ../../../.. ../../scripts/log_doctor.py --json --logs-dir "$(mktemp -d)" 2>/dev/null | python3 -c "import json,sys; json.load(sys.stdin); print('json-ok')"
```

Expect: json-ok
