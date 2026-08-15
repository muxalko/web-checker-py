# web-checker-py

[![CI](https://github.com/muxalko/web-checker-py/actions/workflows/ci.yml/badge.svg?branch=development)](https://github.com/muxalko/web-checker-py/actions/workflows/ci.yml)

An extensible scheduled checker that detects registration and reservation
opportunities and sends notifications when availability changes.

The prototype is provider-independent, with interchangeable drivers. It is
developed against a controlled mock reservation site and a generic HTML driver
before integrating real providers.

See [DESIGN.md](DESIGN.md) for the product scope, architecture, engineering
decisions, testing strategy, and delivery plan. It is the canonical record of
the project's technical design and should be updated as decisions change.

## Contribution workflow

Every repository change follows the same auditable sequence:

1. Open a GitHub issue describing the desired outcome, scope, and acceptance
   criteria.
2. From current `development`, create a branch named
   `type/<issue-number>-description`, such as `agent/123-add-provider`.
3. Implement and verify the change on that branch.
4. Open a pull request targeting `development` whose body contains
   `Closes #<issue-number>` for the same open issue.
5. Merge only after the `Change governance` and `Python quality gates` checks
   pass and the code owner approves the latest revision.

The governance check rejects a mismatched branch or closing reference, a closed
issue, a pull request number used in place of an issue, or a base branch other
than `development`. The issue is closed automatically when its linked pull
request merges.

## Development

The supported runtime is Python 3.11 or newer. Create a virtual environment and
install the project with its development tools:

```console
python3.11 -m venv .venv
.venv/bin/python -m pip install --editable '.[dev]'
.venv/bin/pre-commit install
```

The installed Git hook uses Gitleaks to scan both staged changes and the full
reachable history of the current branch. Findings are redacted so detected
secret values are not echoed to the terminal.

Run the default verification suite with:

```console
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pre-commit run --all-files
.venv/bin/pytest
docker compose config
```

The same secret scan, formatting, linting, test, and Compose validation gates run
in GitHub Actions for every pull request into `development` and every push to
that branch. Gitleaks uses its default rules through [`.gitleaks.toml`](.gitleaks.toml);
do not add broad allowlists or bypass the hook for real credentials. Revoke and
rotate any credential that is ever committed, even if the commit is later
removed.

## Scheduled operation

[`jobs.example.yaml`](jobs.example.yaml) defines a generic HTML job for the mock
reservation site. It runs immediately when the worker starts and every 60
seconds afterward. The default Compose file is exclusively the development test
stack. It builds the current working tree, including uncommitted changes, under
the fixed project name `web-checker-development`:

```console
docker compose up --build
```

Its containers are named `web-checker-development-checker-1` and
`web-checker-development-mock-site-1`. The worker writes state to
`/data/web-checker.db` in a development-only volume. Stop it with `Ctrl+C` or
`docker compose down`; SIGINT and SIGTERM initiate a bounded graceful shutdown
so an in-progress check can finish. `docker compose down --volumes` removes only
development data and must never be used with the production Compose file.

Each enabled worker job must define an interval schedule. A retry policy is
optional and defaults to three total attempts with capped exponential backoff
and jitter:

```yaml
schedule:
  interval_seconds: 60
  retry:
    max_attempts: 3
    initial_delay_seconds: 5
    max_delay_seconds: 60
    jitter_fraction: 0.2
```

Different jobs can run concurrently, but a job is never overlapped with itself.
If an interval arrives while its previous execution or retry is still running,
that occurrence is skipped rather than queued into a catch-up burst.

## Configuration and one-shot checks

Validate the example configuration with:

```console
docker compose run --rm checker \
  web-checker --config jobs.example.yaml \
  --database /data/web-checker.db validate-config
```

Run the job once with:

```console
docker compose run --rm checker \
  web-checker --config jobs.example.yaml \
  --database /data/web-checker.db check mock-adventure-passes
```

Compose starts the mock-site dependency automatically. The command prints each
normalized opportunity, records the complete snapshot in SQLite, reports
transitions from the previous successful check, and exits. The named Compose
volume preserves `/data/web-checker.db`.

The example job sends a console notification when an opportunity becomes
available. Notification intents are persisted with the transition, successful
deliveries are deduplicated, and failed deliveries remain pending for retry on a
later check.

Inspired by

- https://www.geeksforgeeks.org/python-script-to-monitor-website-changes/
- https://docs.docker.com/language/python/containerize/

## Mock reservation site

The first development provider is a controlled local reservation website. Start
it with:

```console
docker compose up --build mock-site
```

Then open `http://localhost:8080/reservations/2026-08-22`. The Compose environment
enables a development-only control API:

```console
curl -X PATCH http://localhost:8080/__control/opportunities/midday-pass \
  -H 'Content-Type: application/json' \
  -d '{"availability":"available","capacity":1}'

curl -X PATCH http://localhost:8080/__control/behavior \
  -H 'Content-Type: application/json' \
  -d '{"status_code":503}'

curl -X POST http://localhost:8080/__control/reset
```

Set an opportunity's `enabled` field to `false` to simulate its disappearance.
The controls return HTTP 404 unless `MOCK_SITE_CONTROLS_ENABLED=true`.
If port 8080 is already occupied, set another host port, for example
`MOCK_SITE_PORT=18080 docker compose up mock-site`.

Run the complete test suite inside the project image with:

```console
docker compose run --rm --no-deps checker pytest
```

## Production deployment

Production is a separate checker-only Compose project named
`web-checker-production`. It does not run the mock site, expose its control API,
build from the developer's working tree, or share development state.

After a protected pull request is merged, CI first validates the exact
`development` merge commit. A successful post-merge CI run then dispatches the
production workflow to a dedicated self-hosted runner carrying the
`web-checker-production` label. The runner builds a production image tagged and
labeled with that full commit SHA, serializes host deployments, waits for the
worker health check, and automatically restores the preceding image when an
update fails. Re-running delivery for an already healthy SHA is a no-op.

Production configuration, environment values, SQLite data, deployment state,
and runner registration credentials remain on the VM and outside this
repository. See [the production deployment runbook](docs/deployment.md) for the
one-time runner setup, trust boundary, validation, recovery, and rollback steps.
