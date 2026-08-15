# Repository instructions

- Read `DESIGN.md` before making architectural or cross-cutting changes.
- Treat `DESIGN.md` as the canonical technical design record. Update it in the
  same change when a decision, boundary, milestone, or quality gate changes.
- Before modifying repository files, ensure an open GitHub issue describes the
  intended outcome and acceptance criteria. Read-only investigation is exempt.
- Create the working branch from current `development` after the issue exists.
  Name it `type/<issue-number>-description`, for example
  `agent/123-add-provider`.
- Merge changes only through a pull request targeting `development`. The pull
  request body must contain `Closes #<issue-number>` matching the branch, and the
  issue must remain open until the pull request is merged.
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
pre-commit run --all-files
pytest
docker compose config
WEB_CHECKER_IMAGE=web-checker-production:validation \
  WEB_CHECKER_PRODUCTION_CONFIG_PATH=/dev/null \
  WEB_CHECKER_PRODUCTION_ENV_PATH=/dev/null \
  docker compose --file compose.production.yaml config --quiet
```

When container behavior or dependencies change, also run:

```console
docker compose build
docker compose run --rm mock-site pytest
```
