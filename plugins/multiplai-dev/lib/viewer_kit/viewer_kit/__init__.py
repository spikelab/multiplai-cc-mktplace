"""viewer_kit: the parts of a local viewer server that do not depend on what it shows.

`mailbox` (the JSONL files between the page and the session), `netinfo` (where
to bind and which URLs to print), `request_checks` (origin, token and body
checks as functions) and `people` (per-person tokens from `people.json`).
Shared by review-viewer and pipeline-viewer.
"""
