<p align="center">
  <img src="img/logo.png" alt="Web Checker — Get that spot …" width="900">
</p>

# web-checker-py

[![CI](https://github.com/muxalko/web-checker-py/actions/workflows/ci.yml/badge.svg?branch=development)](https://github.com/muxalko/web-checker-py/actions/workflows/ci.yml)

An extensible scheduled checker that detects registration and reservation
opportunities and sends notifications when availability changes.

The checker core is provider-independent, with interchangeable drivers. The
default development stack uses a controlled, production-shaped BC Parks mock;
the BC Parks driver can be pointed at the real public read-only API only through
explicit operator configuration.

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
WEB_CHECKER_IMAGE=web-checker-production:validation \
  WEB_CHECKER_PRODUCTION_CONFIG_PATH=/dev/null \
  WEB_CHECKER_PRODUCTION_ENV_PATH=/dev/null \
  docker compose --file compose.production.yaml config --quiet
```

The same secret scan, formatting, linting, test, and Compose validation gates run
in GitHub Actions for every pull request into `development` and every push to
that branch. Gitleaks uses its default rules through [`.gitleaks.toml`](.gitleaks.toml);
do not add broad allowlists or bypass the hook for real credentials. Revoke and
rotate any credential that is ever committed, even if the commit is later
removed.

## Scheduled operation

[`jobs.example.yaml`](jobs.example.yaml) defines a Golden Ears South Beach AM
job using the production-shaped BC Parks mock. It runs immediately when the
worker starts and every five minutes afterward. The default Compose file is
exclusively the development test stack. It builds the current working tree,
including uncommitted changes, under the fixed project name
`web-checker-development`:

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
  interval_seconds: 300
  retry:
    max_attempts: 3
    initial_delay_seconds: 5
    max_delay_seconds: 60
    jitter_fraction: 0.2
```

Different jobs can run concurrently, but a job is never overlapped with itself.
If an interval arrives while its previous execution or retry is still running,
that occurrence is skipped rather than queued into a catch-up burst.

## Worker operations cheatsheet

Run these commands from the repository root. They operate the local development
stack in [`compose.yaml`](compose.yaml), not a production deployment. The worker
does not currently have a management UI or a `status` subcommand, so use Compose
for process status, worker logs for execution status, and SQLite for the last
successfully persisted state.

Start the worker and mock provider in the background:

```console
docker compose up --build --detach
```

Stop the containers while preserving the `checker-data` state volume:

```console
docker compose down
```

### Validate configuration

Validate the example configuration with:

```console
docker compose run --rm --no-deps checker \
  web-checker --config jobs.example.yaml \
  --database /data/web-checker.db validate-config
```

`validate-config` checks the YAML structure, enabled integrations, parser
settings, and configured schedule and retry bounds without contacting the
provider or writing checker state.

### Inspect worker status and activity

Check whether the containers are running, then inspect recent or live worker
events:

```console
docker compose ps
docker compose logs --tail=100 checker
docker compose logs --follow checker
```

`docker compose ps` reports container/process status only. A normally operating
worker logs `worker event=started` and a recurring `event=check_succeeded` for
each job. Investigate `event=retry_scheduled` and `event=check_failed` entries.
An `event=overlap_skipped` entry means the preceding execution of that job was
still running when its next interval arrived.

### Inspect persisted state

With the worker running, query its SQLite database in read-only mode:

```console
docker compose exec -T checker python3 - <<'PY'
import sqlite3

connection = sqlite3.connect(
    "file:/data/web-checker.db?mode=ro",
    uri=True,
)
queries = (
    (
        "Latest successful observations",
        """
        WITH latest AS (
            SELECT job_id, MAX(id) AS run_id
            FROM check_runs
            GROUP BY job_id
        )
        SELECT latest.job_id, runs.checked_at,
               observations.opportunity_id, observations.availability
        FROM latest
        JOIN check_runs AS runs ON runs.id = latest.run_id
        JOIN opportunity_observations AS observations
          ON observations.run_id = latest.run_id
        ORDER BY latest.job_id, observations.position
        """,
    ),
    (
        "Pending notifications",
        """
        SELECT job_id, channel, opportunity_id, attempts, checked_at
        FROM notification_outbox
        WHERE delivered_at IS NULL
        ORDER BY id
        """,
    ),
)
for heading, query in queries:
    rows = connection.execute(query).fetchall()
    print(f"{heading}:")
    for row in rows:
        print("  " + " | ".join(str(value) for value in row))
    if not rows:
        print("  (none)")
connection.close()
PY
```

The first report shows every opportunity in each job's latest successful
snapshot. Failed checks intentionally do not replace that state, so consult the
logs to determine whether later attempts failed. The second report lists
notification deliveries still awaiting a successful send.

### Run one job immediately

Run the job once with:

```console
docker compose run --rm checker \
  web-checker --config jobs.example.yaml \
  --database /data/web-checker.db check golden-ears-south-beach-am
```

Compose starts the mock-site dependency automatically. The command prints each
normalized opportunity, records the complete snapshot in SQLite, reports
transitions from the previous successful check, and exits. The named Compose
volume preserves `/data/web-checker.db`.

The example job sends a console notification when an opportunity becomes
available. Notification intents are persisted with the transition, successful
deliveries are deduplicated, and failed deliveries remain pending for retry on a
later check.

## BC Parks day-use driver

The `bcparks_dayuse` driver reads only the anonymous public configuration, park,
facility, and reservation endpoints. It never submits a reservation, starts a
checkout, authenticates, handles CAPTCHA, cancels a pass, or sends personal
information. A job selects one park, facility, and slot while the driver exposes
the provider's rolling booking window as dated opportunities in chronological
order.

```yaml
driver: bcparks_dayuse
config:
  base_url: http://mock-site:8080/bcparks
  park_id: "0008"
  facility: Alouette Lake South Beach Day-Use Parking Lot
  slot: AM
  date_strategy: rolling_window
```

BC Parks time is interpreted in `America/Vancouver`. Inventory returned before
the facility's opening hour is recorded as `unknown`, not available. `Full`
with a zero maximum is unavailable; the known `Low`, `Medium`, and `High`
states with a positive maximum are available after opening. Empty responses,
unknown capacity labels, inconsistent limits, missing dates or slots, and closed
or hidden parks and facilities fail the check without replacing the last good
snapshot.

The base URL is deliberately configurable so all development and automated
tests use the local mock. A manual live validation requires a separate operator
configuration with `base_url: https://reserve.bcparks.ca`; it must be run
explicitly, conservatively, and never as part of the default test suite. The
optional `app_version` field sends the public frontend's `X-App-Version` value
when the provider requires it.

## Mock reservation site

The development provider reproduces the captured BC Parks API paths, identifiers,
facility options, booking days, slots, configured capacities, rolling responses,
and seasonal park status. It also retains the original generic HTML fixture for
generic-driver regression tests. Start it with:

```console
docker compose up --build mock-site
```

Then open `http://localhost:8080/bcparks/dayuse/`. The simple selection UI is for
inspection only and cannot make a reservation. The Compose environment enables
development-only controls for deterministic scenarios:

```console
curl -X PATCH http://localhost:8080/__control/bcparks/reservations \
  -H 'Content-Type: application/json' \
  -d '{"park_id":"0008","facility":"Alouette Lake South Beach Day-Use Parking Lot","date":"2026-08-16","slot":"AM","capacity":"Low","max":1}'

curl -X PATCH http://localhost:8080/__control/bcparks/clock \
  -H 'Content-Type: application/json' \
  -d '{"current_time":"2026-08-15T14:00:00Z"}'

curl -X PATCH http://localhost:8080/__control/bcparks/behavior \
  -H 'Content-Type: application/json' \
  -d '{"status_code":503}'

curl -X POST http://localhost:8080/__control/bcparks/reset
```

The BC Parks behavior control also accepts `delay_seconds`, `malformed`, and
`empty`. Moving the mock clock to another provider-local date rolls the three-day
reservation window with it. Park and facility controls can simulate closures or
hidden entries.
All control routes return HTTP 404 unless
`MOCK_SITE_CONTROLS_ENABLED=true`.

The original generic HTML scenario remains available at
`http://localhost:8080/reservations/2026-08-22` with its existing controls:

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

Inspired by

- https://www.geeksforgeeks.org/python-script-to-monitor-website-changes/
- https://docs.docker.com/language/python/containerize/
