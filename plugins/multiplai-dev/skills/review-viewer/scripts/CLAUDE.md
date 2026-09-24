# review-viewer scripts — orientation

A stdlib HTTP server and a static page. The server never calls a model: it
serves the page, answers read-only questions about one review, and carries
messages between the page and the Claude Code session through JSONL files.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/review-viewer/scripts python -m review_viewer --help
uv run --directory plugins/multiplai-dev/skills/review-viewer/scripts python -m pytest tests/ -q
```

The page-logic tests need `node`; they fail (not skip) without it.

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `serve`, `reply`, `list`, `stop`, `validate`, `export-schema`. Calls `setup_logging` once. Owns the stdout contract. |
| `models.py` | The `findings.json` v1 pydantic models (source of truth for `../schema/findings.v1.schema.json`), `finding_id()`, and the mailbox row models. |
| `gitdata.py` | Read-only git: `parse_unified()`, `file_view()`, `allowed_paths()`, `diff_target()`. Fixed argv, no shell, stdin closed. |
| `mailbox.py` | Append-only JSONL rows, `decisions.json` by atomic replace, 0600 files. |
| `server.py` | `ThreadingHTTPServer` subclass (`allow_reuse_address = False`), request checks, routes, idle watchdog. |
| `registry.py` | Finds live viewers: `/api/whoami` probes in parallel over 8765–8784, authenticated with the token from each mailbox. |
| `netinfo.py` | Container detection (degradation contract rule 2), bind host, URLs to print. |
| `static/` | `index.html`, `boot.js` (takes the token out of the address bar), `logic.js` (pure functions, tested under node), `app.js`, `app.css`. |

After changing `models.py`, run `python -m review_viewer export-schema` and
commit the schema; `test_models.py` fails while they differ.

## The token rule

A token (`secrets.token_urlsafe(32)`) is created per server start. It exists
only in `<mailbox>/server.token` and `<mailbox>/open.html` (both 0600, deleted
on exit) and in the page's memory. It never reaches stdout, stderr, a log
line, `activity.*`, `server.json`, or any other file. The session's stdout is
its transcript, which is kept. `test_server.py` greps for it.

## Request checks, in order

1. A request with an `Origin` header that does not match its `Host` → 403.
2. `/api/*` without a matching `X-Review-Token` (`hmac.compare_digest`) → 401.
3. A `POST` whose `Content-Type` is not `application/json` → 415. That forces a
   CORS preflight, which is never answered with allow headers.

`GET /` and `/static/*` need no token and contain no review data. The file
route serves only paths in `allowed_paths()` (changed files, finding files,
citation paths) — anything else is 404.

## Protocol 1: `findings.json` v1

See `models.py` or the committed schema. Unknown keys are rejected at every
level. Line numbers are 1-based, at `head_sha`. `finding_id` is the first 10
hex of `sha1(f"{file}\0{line_start}\0{claim}")`.

## Protocol 2: the mailbox (`<dir of findings.json>/viewer/`)

| File | Writer | Row |
|---|---|---|
| `inbox.jsonl` | server | `{"v":1,"id":"q-<utc>-<4 hex>","ts","target","kind":"question"\|"decision","finding_id","anchor":{"path","line_start","line_end"}\|null,"text","decision"}` |
| `outbox.jsonl` | `reply` | `{"v":1,"reply_to","ts","text","done"}` |
| `decisions.json` | server | `{finding_id: {"decision","note","ts"}}` |
| `server.json` | server | `{"url_path_only","port","pid","session_id","started","targets"}` |
| `server.token`, `open.html` | server | 0600, deleted on exit |

One writer per file; each row is one `write()` under 64 KiB on an `O_APPEND`
descriptor. Readers skip lines that do not parse. `reply` splits answers that
would exceed the row limit into several rows; only the last one can carry
`done: true`.

Plain-diff mode puts the mailbox under `<workspace INBOX or cwd>/review-viewer/<slug>/viewer/`.

## Logging

`setup_logging("review-viewer", propagate_loggers=("review_viewer", "multiplai_core"))`
writes `review-viewer.log`; every module logs to `logging.getLogger(__name__)`.
Request lines go to DEBUG. Unexpected handler errors log at ERROR with
`exc_info` (so they reach `hook-errors.log`); the client gets `500
{"error": "internal"}`.

`log_event("review-viewer", …)` fires for exactly these events. No field ever
holds question or answer text.

| event | message (example) | fields |
|---|---|---|
| `start` | `viewer started for <slug> on port 8765` | `port, targets, host, container` |
| `reuse` | `viewer already running for <slug>; reused it` | `port, owner_session` |
| `question` | `question q-… on finding 3fa2c91b0e` | `target, finding_id, chars` |
| `decision` | `finding 3fa2c91b0e rejected` | `target, finding_id, decision` |
| `reply` | `reply to q-… (final)` | `target, reply_to, chars, done` |
| `idle_stop` | `viewer stopped after 30 min with no open page` | `idle_minutes` |
| `stop` | `viewer stopped by stop --box` | `reason` |
| `rejected_request` | `refused request: bad token` (WARNING, at most once a minute per status) | `status, route` |

## Tests

`tests/fixture_repo.py` builds the two-commit repository the tests review
(fixed author and dates, so the shas in `fixtures/findings.example.json` are
stable). `python tests/fixture_repo.py <dir>` builds it anywhere.
