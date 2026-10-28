# Design

Four modules under `app/`: `pricing`, `lines`, `basket` and
`price_list`. All arithmetic is in integer cents through
`app.money.to_cents`.

## Decisions
- No new dependencies.
