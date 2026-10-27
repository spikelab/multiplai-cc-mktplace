# Design

Four small modules under `app/`, each a function over a `Store`:
`users`, `orders`, `customers` and `report`. Later modules call the
earlier ones rather than reading the store twice.

## Decisions
- No new dependencies.
