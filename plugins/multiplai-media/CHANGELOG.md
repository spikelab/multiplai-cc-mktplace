# Changelog

(First CHANGELOG for this plugin — history before 0.1.5 lives in git log.)

## 0.1.5 — 2026-07-17

Fixes from the 07-12→16 PR audit (`INBOX/pr-audit-multiplai-2026-07-12-to-16.md`).

### Fixed
- **host-browser `hb`: `humantype`/`fillform` no longer die at the gateway on
  control characters.** `has_meta` now routes text containing ANY control
  char (tab, CR, escape, …) — not just newline — through the `eval -b`
  insertion path.
- **host-browser `hb mail code` can no longer print garbage as an OTP.**
  Only a 4–8-digit string is ever printed; `null`/`undefined`/error-string
  eval hiccups are treated as "no OTP yet" and polled past. A 401 from
  mail.tm (expired/revoked JWT) is now distinguished from "no OTP yet" and
  fails fast with a re-run hint instead of polling until timeout.
- **host-browser `hb waitfor` usage message is reachable again** — bare
  `$1`/`$2` under `set -u` aborted with "unbound variable" before the usage
  text; args are now guarded.
- **youtube-transcript: yt-dlp self-heal is multiplai-container-only.**
  Auto-install (`uv tool install --upgrade yt-dlp`) now runs only when
  `MULTIPLAI_CONTAINER=1`; on macOS/plain Linux/generic Docker the script
  prints per-platform install instructions and exits **3** (missing
  dependency — distinct from exit 2 = "no subtitles", per
  `docs/degradation-contract.md`) instead of installing software onto the
  user's machine as a side effect. SKILL.md documents the new exit code.
