<p align="center">
  <img src="img/logo.png" alt="Web Checker — Get that spot …" width="900">
</p>

# web-checker-py

[![CI](https://github.com/muxalko/web-checker-py/actions/workflows/ci.yml/badge.svg?branch=development)](https://github.com/muxalko/web-checker-py/actions/workflows/ci.yml)

An extensible scheduled checker that detects registration, reservation, and
published-event opportunities and sends notifications when relevant changes
occur.

The checker core is provider-independent, with interchangeable drivers. The
default development stack uses controlled provider-shaped mocks; real provider
access is always an explicit operator configuration choice.

See [DESIGN.md](DESIGN.md) for the product scope, architecture, engineering
decisions, testing strategy, and delivery plan. It is the canonical record of
the project's technical design and should be updated as decisions change. See
[the roadmap](docs/ROADMAP.md) for prioritized future milestones and tasks.

## Contribution workflow

Every repository change follows the same auditable sequence:

1. Open a GitHub issue describing the desired outcome, scope, and acceptance
   criteria.
2. From current `development`, create a branch named
   `type/<issue-number>-description`, such as `agent/123-add-provider`.
3. Implement and verify the change on that branch.
4. Open a pull request targeting `development` whose body contains
   `Closes #<issue-number>` for the same open issue. Add a closing reference for
   every other issue the pull request fully completes.
5. Merge only after the `Change governance` and `Python quality gates` checks
   pass and the code owner approves the latest revision.

The governance check rejects a mismatched branch or closing reference, a closed
issue, a pull request number used in place of an issue, or a base branch other
than `development`. After a successful merge, a write-scoped workflow closes
every referenced open issue because GitHub's native closing behavior applies
only when changes reach the default branch.

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
job using the production-shaped BC Parks mock, an enabled generic HTML mock job,
and a disabled WelcomeBC example. Enabled jobs run immediately when the worker
starts, then every five minutes for BC Parks and every 60 seconds for the generic
mock. The default Compose file is exclusively the development test stack. It
builds the current working tree, including uncommitted changes, under the fixed
project name
`web-checker-development`:

```console
docker compose up --build
```

Its services include the checker, the mock provider, and a local Mailpit email
capture server. The worker writes state to
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
available. Related transitions from one check are grouped into a digest for each
selected channel. Notification intents are persisted with the transitions,
successful deliveries are deduplicated, and failed deliveries remain pending for
retry on a later check.

## WelcomeBC High Economic Impact draws

The `welcomebc_high_impact` driver monitors the public
[WelcomeBC Invitations to Apply page](https://www.welcomebc.ca/immigrate-to-b-c/about-the-bc-provincial-nominee-program/invitations-to-apply)
for `Innovate: High Economic Impact` Skills Immigration draws. Multiple
selection-factor rows under one date are grouped into one opportunity with a
stable date-based ID. Historical draws establish the first baseline without an
alert; a newly published date later produces one `appeared` transition.

The example is disabled so the development stack and default tests never contact
WelcomeBC. Copy it to the operator-owned production configuration, review the
URL and interval, and explicitly enable it when ready. The hourly cadence makes
24 requests per day and normally detects a publication within 60 minutes:

```yaml
- id: welcomebc-high-impact-itas
  enabled: true
  driver: welcomebc_high_impact
  notify:
    channels: [email]
    on: [appeared]
  schedule:
    interval_seconds: 3600
  config:
    url: https://www.welcomebc.ca/immigrate-to-b-c/about-the-bc-provincial-nominee-program/invitations-to-apply
    timeout_seconds: 10
    max_response_bytes: 1048576
```

The driver makes one anonymous, bounded HTTP GET per attempt and does not access
BC PNP profiles or submit applications. Missing, empty, malformed, or changed
provider structure fails the check and preserves the last successful baseline.
The worker checks immediately on startup and then hourly. Its first successful
response establishes the historical baseline without sending an email. The
official page supplies a draw date rather than an exact publication time, so the
checker does not claim a more precise issuance time.

## Email notifications

Notification policy may opt into an already-available opportunity on the first
successful check as well as later availability changes:

```yaml
notify:
  channels: [email]
  on: [initially_available, became_available]
```

`initially_available` is emitted once for each available opportunity in the
first complete snapshot. Unavailable and unknown initial states do not match it,
and an unchanged later check does not send a duplicate. Omitting it preserves
the quiet-baseline behavior. Driver failures and incomplete responses never
establish a baseline or enqueue an initial notification.

The `email` channel sends a plain provider-independent digest. Every item includes
the normalized transition, current availability, relevant time, and source link;
missing optional values are explicit. For example:

```text
Availability changes for job: welcomebc-high-impact-itas
Checked at: 2026-08-13T20:00:00+00:00

1. BC PNP High Economic Impact draw on August 13, 2026 (450 invitations)
   Transition: appeared
   Availability: available
   Starts at: 2026-08-13T00:00:00
   Link: https://www.welcomebc.ca/immigrate-to-b-c/about-the-bc-provincial-nominee-program/invitations-to-apply
```

Items are ordered by relevant time, then title and stable opportunity ID. A
digest contains at most 25 items; a larger check is split into deterministically
numbered parts so no transition is discarded. The adapter does not inspect the
driver's provider-specific `attributes` JSON. Set these environment variables
before enabling a job that selects `email`:

| Variable | Required | Meaning |
| --- | --- | --- |
| `WEB_CHECKER_SMTP_HOST` | Yes | SMTP server hostname |
| `WEB_CHECKER_SMTP_FROM` | Yes | Plain sender email address |
| `WEB_CHECKER_SMTP_TO` | Yes | Comma-separated plain recipient addresses |
| `WEB_CHECKER_SMTP_PORT` | No | Defaults to 587 for STARTTLS |
| `WEB_CHECKER_SMTP_SECURITY` | No | `starttls` (default), `implicit-tls`, or `none` |
| `WEB_CHECKER_SMTP_USERNAME` | No | Authentication username; requires password |
| `WEB_CHECKER_SMTP_PASSWORD` | No | Authentication secret; requires username |
| `WEB_CHECKER_SMTP_TIMEOUT_SECONDS` | No | Connection timeout; defaults to 10 seconds |

Use `starttls` on port 587 or `implicit-tls` on port 465 according to the SMTP
provider. The `none` mode is intended only for a trusted local capture server.
Any partial or invalid SMTP configuration fails startup without printing the
password. Credentials belong in the operator-owned production environment file,
never in `jobs.yaml` or Git.

Normal digest deliveries are deduplicated through the durable outbox. A failed
SMTP send leaves the complete digest part pending and it is attempted after a
later successful check. Delivery is at-least-once across a process crash, so the
narrow interval after an SMTP server accepts a message but before SQLite records
success can produce a duplicate.

## BC Parks day-use driver

The `bcparks_dayuse` driver reads only the anonymous public configuration, park,
facility, and reservation endpoints. It never submits a reservation, starts a
checkout, authenticates, handles CAPTCHA, cancels a pass, or sends personal
information. A job selects one park, facility, and slot while the driver exposes
the provider's rolling booking window as dated opportunities in chronological
order. `rolling_window` is the only supported date strategy for this driver.

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
with a zero maximum is unavailable; the known `Low`, `Medium`, `Moderate`, and
`High` states with a positive maximum are available after opening. Empty
responses, unknown capacity labels, inconsistent limits, missing dates or slots,
and closed or hidden parks and facilities fail the check without replacing the
last good snapshot.

The base URL is deliberately configurable so all development and automated
tests use the local mock. A manual live validation requires a separate operator
configuration with `base_url: https://reserve.bcparks.ca`; it must be run
explicitly, conservatively, and never as part of the default test suite. The
optional `app_version` field sends the public frontend's `X-App-Version` value
when the provider requires it. Anonymous provider requests include an `Origin`
header derived from the configured base URL because the live reservation route
uses same-origin request context to select its JSON API behavior.

## Mock providers

The development provider reproduces the captured BC Parks API paths, identifiers,
facility options, booking days, slots, configured capacities, rolling responses,
and seasonal park status. It also retains the original generic HTML fixture for
generic-driver regression tests. Start it with:

```console
docker compose up --build mock-site
```

Then open:

- `http://localhost:8080/bcparks/dayuse/` for the BC Parks selection UI;
- `http://localhost:8080/reservations/2026-08-22` for the generic provider; or
- `http://localhost:8080/welcomebc/invitations-to-apply` for WelcomeBC.

The BC Parks selection UI is for inspection only and cannot make a reservation.
The Compose environment enables development-only controls for deterministic
scenarios:

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
hidden entries. The generic and WelcomeBC controls are available as well:

```console
curl -X PATCH http://localhost:8080/__control/opportunities/midday-pass \
  -H 'Content-Type: application/json' \
  -d '{"availability":"available","capacity":1}'

curl -X PATCH http://localhost:8080/__control/behavior \
  -H 'Content-Type: application/json' \
  -d '{"status_code":503}'

curl -X POST http://localhost:8080/__control/welcomebc/publish

curl -X PATCH http://localhost:8080/__control/welcomebc/behavior \
  -H 'Content-Type: application/json' \
  -d '{"malformed":true}'

curl -X POST http://localhost:8080/__control/reset
```

Publishing the mock WelcomeBC draw is idempotent; reset removes it and restores
all default behavior. Set a reservation opportunity's `enabled` field to `false`
to simulate its disappearance. The provider controls can simulate delay,
malformed content, or an HTTP error. All control routes return HTTP 404 unless
`MOCK_SITE_CONTROLS_ENABLED=true`.
If port 8080 is already occupied, set another host port, for example
`MOCK_SITE_PORT=18080 docker compose up mock-site`.

Run the complete test suite inside the project image with:

```console
docker compose run --rm --no-deps checker pytest
```

Mailpit captures development email without sending anything externally. Its UI
is available at `http://localhost:8025` after `docker compose up`. Run the
opt-in end-to-end scenario—which establishes a mock WelcomeBC baseline,
publishes one draw, captures one email, and verifies no duplicate—with:

```console
docker compose up --build --detach mailpit
docker compose run --rm \
  -e WEB_CHECKER_TEST_SMTP_HOST=mailpit \
  -e WEB_CHECKER_TEST_MAILPIT_API=http://mailpit:8025 \
  mock-site pytest -q \
  tests/drivers/welcomebc_high_impact/test_email_mailpit_integration.py
```

If ports 1025 or 8025 are occupied, set `MAILPIT_SMTP_PORT` or
`MAILPIT_HTTP_PORT`; communication between the containers continues to use the
fixed internal ports.

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
