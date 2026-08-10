# Repository instructions

- Read `DESIGN.md` before making architectural or cross-cutting changes.
- Treat `DESIGN.md` as the canonical technical design record. Update it in the
  same change when a decision, boundary, milestone, or quality gate changes.
- Keep the checker core provider-independent. Provider communication and parsing
  belong behind driver contracts.
- Keep availability checks read-only. Do not add reservation, purchasing,
  authentication, or payment behavior without an explicitly approved design.
- Add or update tests with every feature and bug fix. Use static fixtures, mocked
  transports, or the local mock site; default tests must never contact live
  third-party providers.
- Treat an unexpectedly empty or structurally changed provider response as a
  check failure, not proof that all prior opportunities disappeared.
- Preserve unrelated working-tree changes.

## Verification

The supported runtime is Python 3.11 or newer. After installing `.[dev]`, run:

```console
ruff format --check .
ruff check .
pytest
docker compose config
```

When container behavior or dependencies change, also run:

```console
docker compose build
docker compose run --rm mock-site pytest
```
