# Design

One module per concern under `app/` (`audit`, `stock`, `mailer`,
`loyalty`), each registering one handler on `app.events` at import and
keeping its state in a module-level container.

## Decisions
- No new dependencies.
