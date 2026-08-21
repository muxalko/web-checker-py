# Web Checker Design

## Document purpose

This document is the canonical record of the project's architectural,
engineering, and technical design decisions. Update it whenever a change affects
the system's responsibilities, extension points, data model, operational model,
or delivery plan.

The project is currently in the design and prototype stage. Decisions marked as
initial may be revised when implementation or integration work provides new
evidence.

## Product vision

Web Checker is a scheduled opportunity and published-event monitor. A user
defines a job describing what should be checked and when. The system invokes an
interchangeable provider driver, compares the normalized result with previously
observed state, and sends a notification when a relevant change occurs.

Examples include:

- A ferry sailing becomes reservable.
- A park day pass becomes available.
- A campsite, appointment, ticket, or other limited opportunity becomes
  available.
- A government program publishes a new invitation draw.

The prototype began with a locally controlled mock reservation site and generic
HTML driver. Provider-specific integrations, including BC Parks day-use and
WelcomeBC draws, use static fixtures, mocked transports, and provider-shaped
local routes for deterministic default testing without placing load on external
services.

## Goals

- Support multiple independently scheduled check jobs.
- Keep provider-specific behavior behind interchangeable drivers.
- Normalize provider results enough for shared state comparison and notification.
- Detect meaningful availability transitions and avoid duplicate alerts.
- Make HTTP behavior conservative, observable, and resilient.
- Make the system easy to run locally and in a small self-hosted environment.
- Provide deterministic unit and end-to-end tests.
- Leave room for separately packaged drivers without requiring that complexity in
  the first prototype.

## Non-goals for the first prototype

- Automatically completing a reservation or purchase.
- Supporting login flows, CAPTCHA, or browser automation.
- Executing JavaScript to render monitored pages.
- Providing a public multi-user service.
- Modeling every reservation system through one exhaustive domain schema.
- Allowing arbitrary executable expressions in job configuration.

Automatic reservation is a separate capability with materially greater safety,
policy, authentication, payment, and idempotency requirements. It must not be
embedded in the availability-checking contract.

## Architectural principles

### Provider independence

The core must not contain concepts specific to ferries, parks, campsites, or any
other provider. A driver owns provider-specific communication, parsing, and
configuration validation.

### Small normalized model

The shared model represents stable identity, availability or publication
presence, relevant time, human-readable context, and an optional source or
booking link. Provider-specific details remain in extensible attributes or
inside the driver.

### Separate observation from action

Checking availability is read-only. Any future reservation capability must use a
separate interface and explicit safety design.

### One check as the fundamental operation

The core operation checks one configured job once. A scheduled worker repeatedly
invokes that operation. This keeps behavior easy to test, run manually, retry,
and operate through an external scheduler if desired.

### Conservative external access

Drivers must use timeouts, limited retries with backoff and jitter, and
configurable polling intervals. Tests should use fixtures and the mock provider
instead of repeatedly accessing real websites.

## System context

```text
Job configuration
       |
       v
Scheduler -> Check service -> Driver registry -> Provider driver
                         |                    |
                         |                    v
                         |              Opportunities
                         v                    |
                   State storage <-----------+
                         |
                         v
                Transition detection
                         |
                         v
                  Notification service -> Notifier(s)
```

## Components and responsibilities

### Check service

The check service coordinates one execution of a job:

1. Resolve the configured driver.
2. Validate driver-specific configuration.
3. Invoke the driver.
4. Persist the observation.
5. Compare it with prior state.
6. produce domain transitions.
7. Dispatch matching notifications.
8. Record notification delivery for deduplication.

It does not understand the provider's protocol or document structure.

### Scheduler

The scheduler determines when jobs run. It invokes the same one-shot check
service used by the command-line interface. It must prevent overlapping runs of
the same job unless a future design explicitly permits them. Different jobs run
concurrently, so a slow or failing provider does not delay unrelated jobs.

The prototype uses a small purpose-built asynchronous interval loop rather than
a scheduling framework. Enabled worker jobs run immediately at process startup,
then on a fixed monotonic interval. If a job is still running when its next
interval arrives, that occurrence is skipped and the cadence advances; missed
runs are not queued into a catch-up burst. Calendar or cron syntax can be added
after the basic behavior is proven.

Each job has a bounded retry policy. One scheduled execution may make up to the
configured total attempt count. Failed attempts use capped exponential backoff
with symmetric jitter. The driver itself still makes one request attempt, which
avoids nested retry loops. Exhausting retries is isolated to that job and is
reported as an operational event; later intervals continue normally.

SIGINT and SIGTERM stop new scheduling. In-progress checks receive a 30-second
grace period and are then cancelled, including checks waiting in retry backoff.
Scheduler time, sleep, random, and event boundaries are injectable so unit tests
remain deterministic and do not make external requests.

### Driver registry

The initial registry is an explicit mapping of names to driver implementations:

```python
DRIVERS = {
    "bcparks_dayuse": BCParksDayUseDriver,
    "generic_html": GenericHtmlDriver,
}
```

An explicit registry is preferable initially because it is simple and
inspectable. If drivers later need to be installed as independent packages, the
registry may discover Python package entry points under a namespace such as
`web_checker.drivers`.

### Drivers

A driver:

- Validates its provider-specific configuration.
- Communicates with the provider.
- Parses the provider response.
- Converts provider data into normalized opportunities.
- Reports errors without hiding their cause.

A driver does not schedule itself, compare historical state, or send
notifications.

Within a provider-specific driver, transport and interpretation should be kept
separate where useful:

- `client.py` handles HTTP and session behavior.
- `parser.py` interprets responses.
- `driver.py` validates configuration and produces normalized results.
- `models.py` contains internal provider models when needed.

### State storage

SQLite is the initial persistent store. It will hold:

- Job execution records.
- Current and historical opportunity observations.
- Availability transitions.
- Notification delivery and deduplication records.

Database access should be hidden behind a repository or store interface so core
logic can be tested with an in-memory implementation.

Each successful result is recorded as a complete immutable snapshot. Recording
uses one SQLite `BEGIN IMMEDIATE` transaction: load the latest snapshot for the
job, calculate transitions, insert the new snapshot and transition records, and
commit. A serialization or database failure rolls back the entire check. Driver
failures never reach the store, so the latest successful snapshot remains the
comparison baseline.

### Transition detection

The transition detector compares a driver's current normalized results with the
previous completed observation for the same job. Initial transition types are:

- `initially_available`
- `became_available`
- `became_unavailable`
- `appeared`
- `disappeared`
- `availability_unknown`

Notifications are policy-driven. The default useful policy is to notify on
`became_available`, not on every successful observation. Jobs may additionally
select `initially_available` to notify for each available opportunity in the
first complete snapshot; unavailable and unknown initial states emit nothing.

Append-only publication drivers represent each published event as an
always-available opportunity and notify on `appeared`. Their first successful
snapshot establishes a baseline of existing publications, so only later IDs
produce alerts.

An incomplete or failed check must not be interpreted as every opportunity
disappearing.

The first successful check for a job creates a baseline and emits an
`initially_available` transition for each available opportunity. Existing quiet
baseline behavior is preserved because jobs notify only on explicitly selected
transition types. Later checks emit `appeared` and `disappeared` in addition to
availability-state changes. Changes to titles, links, times, or attributes alone
are retained in the new snapshot but do not currently produce transitions.

### Notifiers

Notifiers are interchangeable delivery adapters. Initial implementations are:

- Console notifier for local development.
- SMTP email notifier for real delivery using the generic title and source link.
- Fake/capturing notifier for tests.

Telegram, Pushover, Slack, or generic webhooks can be added without
changing drivers or transition detection.

Provider-specific attributes remain an opaque JSON mapping on stored
opportunity observations. Notification outbox rows deliberately contain a fixed
provider-independent subset; the email adapter does not read or copy the
attributes blob. SMTP connection and address settings come from environment
variables, with secret values excluded from job configuration and logs.

Notification policy is configured per job as a set of transition types and
named channels. Matching delivery intents are written to a durable SQLite outbox
in the same transaction as the snapshot and transition. The dispatcher marks a
delivery complete only after its adapter returns successfully. Failed or unknown
channels remain pending with attempt and error metadata and are retried on a
later dispatch pass.

The outbox uniquely identifies each transition/channel pair, preventing a
successfully recorded delivery from being emitted again. Delivery is
at-least-once rather than exactly-once across a process crash: if an external
adapter succeeds but the process stops before SQLite records success, that item
will be retried. Adapters added in the future should use provider idempotency keys
where available.

### Controlled mock providers

The mock site contains fake providers, not checker-core behavior and not drivers.
It presents a small generic reservation page, a provider-shaped WelcomeBC Skills
Immigration page, and a production-shaped BC Parks surface under `/bcparks` with
anonymous config, park, facility, and reservation endpoints plus a human-readable
selection page. The BC Parks mock contains sanitized park, facility, slot,
capacity, booking-day, and rolling-window shapes captured during provider
research. Raw HAR captures are ignored and are not test fixtures.

A development-only control API changes its state deterministically. It should
support scenarios including:

- Sold out to available.
- Available to sold out.
- Capacity changes.
- Opportunity appearance and disappearance.
- Server error.
- Slow response.
- Malformed or unexpectedly structured HTML or JSON.
- Provider-local clock changes and pre-opening inventory.
- Park and facility closures or visibility changes.
- Publication of one new High Economic Impact draw.

Control endpoints must not be enabled in a production deployment.

### Generic HTML driver

The first driver reads a deliberately simple HTML page using declarative CSS
selector configuration. Its initial scope is:

- HTTP GET.
- Configurable request headers.
- CSS selection of repeated opportunities.
- Text and attribute extraction.
- Mapping provider values to normalized availability.
- Relative URL resolution.
- Request timeouts and bounded, streaming response reads.

It will not initially support authentication workflows, JavaScript rendering,
form submission, CAPTCHA, booking, or arbitrary configuration expressions.

The driver performs one HTTP attempt. Retry policy belongs to the check service
or scheduler so retries are observable and consistent across provider drivers,
and so nested retry loops cannot multiply external traffic.

Example configuration:

```yaml
driver: generic_html
config:
  url: http://mock-site:8080/reservations/2026-08-22
  opportunities:
    selector: "[data-opportunity-id]"
    id:
      attribute: data-opportunity-id
    title:
      selector: ".title"
      extract: text
    availability:
      selector: ".status"
      attribute: data-status
      mapping:
        available: available
        sold-out: unavailable
    starts_at:
      selector: time
      attribute: datetime
    booking_url:
      selector: "a.book"
      attribute: href
```

### BC Parks day-use driver

The `bcparks_dayuse` driver uses the public anonymous config, park, facility, and
reservation JSON endpoints. A job chooses a park ID, exact facility name, and
one of the provider's `AM`, `PM`, or `DAY` slots. The `rolling_window` date
strategy returns each dated opportunity in chronological order, allowing the
normal transition engine to alert on the earliest newly available date without
adding provider concepts to the core.

The base URL is configurable. Development, CI, and default example configuration
target the local mock; live access is an explicit operator action and never part
of the default test suite. Requests are bounded anonymous `GET`s with a timeout,
response-size limit, optional public `X-App-Version` header, and no cookies,
credentials, form submission, authentication, or reservation behavior.

Provider time is interpreted in `America/Vancouver`. Inventory is `unknown`
until its booking opening time. After opening, `Full` with a zero maximum maps to
`unavailable`, while the known positive `Low`, `Medium`, `Moderate`, and `High`
states map to `available`. Non-booking days also remain `unknown`. Empty or
incomplete catalogs, missing dates or slots, unknown states, inconsistent
capacity, and closed or hidden selections fail the check so the last good
snapshot is not replaced by a false disappearance.

### WelcomeBC High Economic Impact driver

The provider-specific `welcomebc_high_impact` driver performs one anonymous GET
of the public BC PNP Invitations to Apply page. It interprets only Skills
Immigration entries labeled `Innovate: High Economic Impact`, groups multiple
selection routes under one date, and emits one always-available opportunity with
a stable date-based ID. The opportunity source link is the final response URL;
provider details such as route factors, minimum scores, invitation counts, and
an exact total remain in attributes.

The driver also recognizes older same-page narrative entries so table-to-prose
movement does not create duplicate draw IDs. The structured table is
authoritative if the same date appears in both formats. Missing target sections,
changed headers, zero target draws, malformed dates or counts, inconsistent
duplicates, non-HTML content, oversized bodies, and request failures fail the
check. Tests use minimal fixtures, mocked HTTP transports, and the local
provider-shaped mock; live access is manual and opt-in.

## Initial domain model

The following illustrates the intended contract. Exact Python types may change
during implementation, but the boundary and responsibilities should remain.

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class Availability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Opportunity:
    id: str
    title: str
    availability: Availability
    starts_at: datetime | None = None
    booking_url: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    checked_at: datetime
    opportunities: tuple[Opportunity, ...]
```

Opportunity IDs must be stable within a job. A driver must not use transient
content, ordering, or an entire page hash as the identity.

The availability driver contract will provide configuration validation and an
asynchronous `check` operation. A future reservation interface, if approved,
will be separate and optional.

## Job configuration

Jobs are declarative. Common fields are validated by the core; the selected
driver validates the contents of `config`.

```yaml
jobs:
  - id: mock-morning-pass
    enabled: true
    driver: generic_html
    schedule:
      interval_seconds: 60
      retry:
        max_attempts: 3
        initial_delay_seconds: 5
        max_delay_seconds: 60
        jitter_fraction: 0.2
    config:
      url: http://mock-site:8080/reservations/2026-08-22
      # Driver-specific extraction configuration is omitted here.
```

The common schema contains `id`, `enabled`, `driver`, `config`, optional `notify`,
and optional `schedule`. A schedule remains optional for manual one-shot jobs,
but every enabled job loaded by `worker` must have one. Unknown fields and
invalid numeric bounds are rejected before the worker starts.

Secrets must not be stored directly in committed job configuration. Future
drivers or notifiers requiring secrets should reference environment variables or
a secret provider.

## Proposed repository layout

```text
web_checker/
|-- core/
|   |-- models.py
|   |-- scheduler.py
|   |-- service.py
|   `-- transitions.py
|-- drivers/
|   |-- base.py
|   |-- registry.py
|   `-- generic_html/
|       |-- driver.py
|       `-- parser.py
|-- notifications/
|   |-- base.py
|   |-- console.py
|   |-- fake.py
|   |-- models.py
|   |-- registry.py
|   `-- service.py
|-- config.py
|-- storage.py
`-- cli.py

mock_site/
|-- app.py
`-- templates/

tests/
|-- core/
|-- drivers/
|   `-- generic_html/
|       `-- fixtures/
|-- integration/
`-- notifications/
```

The initial package uses a top-level `web_checker/` directory rather than a
`src/` layout. The package is installed in editable mode during development, so
tests exercise the installed project metadata and declared dependencies.

## Commands and runtime modes

The desired command-line surface is:

```text
web-checker validate-config
web-checker check <job-id>
web-checker check-all
web-checker worker
```

`check` and `check-all` perform one-shot operations. `worker` runs the scheduler.
All modes use the same application services.

The local Docker Compose environment is expected to contain:

- `checker`: the scheduled checker.
- `mock-site`: the controlled reservation provider.
- `mailpit`: a local-only SMTP and web inbox used to capture development email.

## Error handling and resilience

- Every outbound request must have a timeout.
- Retries must be bounded and limited to appropriate transient failures.
- Retry delays should use exponential backoff and jitter.
- A failed check records a failure but does not replace the last good state.
- Exceptions must include actionable context in logs without exposing secrets.
- Repeated failures should eventually produce a health alert through a policy
  separate from availability notifications.
- The scheduler must avoid a tight retry loop.
- Graceful shutdown should allow an in-progress check a bounded time to finish.

## Observability

Logs should be structured and include, where applicable:

- Job ID.
- Driver name.
- Check execution ID.
- Start time and duration.
- Outcome and opportunity count.
- Transition count.
- Notification outcome.

Logs must not contain credentials, payment data, session cookies, or full
provider responses by default.

Initial health reporting may be a CLI status command or container health check.
A web dashboard is not required for the prototype.

## Security and safety

- Treat all fetched content as untrusted input.
- Do not execute code or templates obtained from monitored sites.
- Bound response sizes to prevent excessive memory use.
- Validate schemes and URLs. If this becomes a multi-user service, generic URL
  fetching creates an SSRF risk and will require strict network controls.
- Keep control endpoints for the mock site development-only.
- Keep secrets out of configuration files, logs, fixtures, and Git.
- Do not add automated purchasing or reservation without a dedicated design
  covering authorization, provider policy, confirmation, price limits,
  idempotency, cancellation risk, and audit history.

## Testing strategy

Testing is part of implementation, not a final hardening phase. Every feature or
architectural stage must add or update tests in the same change. A milestone is
not complete until its relevant automated tests pass.

Tests should be placed at the narrowest useful level:

- Pure domain and parsing behavior uses fast unit tests.
- Component boundaries such as HTTP clients, registries, configuration, and
  persistence use focused integration tests.
- A small number of end-to-end tests prove the complete user-visible workflow.

Tests must not depend on live third-party websites. Provider integrations should
use sanitized response fixtures, mocked transports, or the controlled mock site.
Live checks are manual, opt-in, conservative, and excluded from the default test
suite.

### Unit tests

- Configuration validation.
- Generic HTML extraction from saved fixtures.
- BC Parks contract parsing and availability interpretation from sanitized
  fixtures.
- Normalized opportunity identity.
- Transition detection.
- Failed/incomplete check semantics.
- Notification policy and deduplication.
- Scheduler overlap prevention.

### Integration tests

- Checker HTTP client against the mock site.
- BC Parks driver against the production-shaped mock API.
- SQLite persistence across check executions.
- Driver registry resolution.
- Configuration-to-notification flow.

### End-to-end acceptance test

1. Start with a mock opportunity marked unavailable.
2. Run a check and persist that state without an availability alert.
3. Change the opportunity to available through the mock control API.
4. Run another check.
5. Observe exactly one `became_available` notification.
6. Run another unchanged check and observe no duplicate notification.

Published-event drivers add an analogous acceptance path: establish a baseline,
publish one event through the local mock control, observe exactly one `appeared`
notification, and confirm an unchanged follow-up emits none.

Tests for external providers should primarily use sanitized captured fixtures.
Live-provider tests, if ever added, must be explicit, infrequent, and excluded
from the default test suite.

### Stage quality gates

Each implementation stage has explicit minimum coverage:

1. **Domain model and contracts:** construction, validation, immutability, and
   driver-registry behavior.
2. **Generic HTML parser:** extraction, relative URLs, optional fields, invalid
   mappings, duplicate IDs, empty results, and malformed-page behavior using
   saved fixtures.
3. **Generic HTML driver:** successful checks, timeouts, non-success status
   codes, response-size limits, and parser failures using an injected or mocked
   HTTP transport.
4. **One-shot CLI:** valid configuration, invalid configuration, known and
   unknown drivers, successful output, and failure exit codes.
5. **State and transitions:** initial baselines, every supported transition,
   failed-check semantics, persistence across executions, and deduplication.
6. **Notifications:** message formatting, channel selection, delivery failures,
   and exactly-once deduplication semantics within the guarantees of the local
   store.
7. **Scheduler:** due-job selection, overlap prevention, isolation between jobs,
   retry timing, and graceful shutdown using a controllable clock rather than
   real sleeps.
8. **Complete workflow:** an end-to-end unavailable-to-available scenario
   against the mock site producing exactly one notification.

The default local and GitHub Actions verification gates run secret scanning,
formatting, linting, unit and integration tests, and Docker Compose configuration
validation for both isolated deployment definitions. Container builds and
end-to-end tests remain separate documented commands because they materially
increase execution time.

## Delivery plan

### Milestone 1: project foundation

- Introduce package and test structure. **Implemented.**
- Choose packaging and dependency tooling. **Implemented.**
- Define domain models and driver interfaces. **Implemented; notifier contract
  will be added with notifications.**
- Add configuration loading and validation. **Implemented for one-shot jobs and
  generic HTML driver rules.**
- Add basic CI checks. **Implemented with GitHub Actions on pull requests into
  `development` and pushes to that branch.**
- Add tests for every foundation component as it is introduced. **Implemented
  for the current domain and registry components.**

### Milestone 2: controlled provider

- Implement the mock reservation site. **Implemented.**
- Add its development-only control API. **Implemented.**
- Provide deterministic scenarios and fixtures. **Implemented for the current
  mock-site and parser scenarios.**

### Milestone 3: working checker

- Implement the generic HTML parser. **Implemented.**
- Implement the generic HTML HTTP driver. **Implemented.**
- Implement one-shot checks. **Implemented through the CLI.**
- Add SQLite state and transition detection. **Implemented.**
- Add console and fake notifiers. **Implemented with per-job policy and a durable
  delivery outbox.**
- Pass the end-to-end acceptance test. **Implemented and verified through
  Compose: one unavailable-to-available transition produces one notification,
  and the next unchanged check produces none.**
- Maintain parser, driver, CLI, persistence, transition, and notifier tests as
  part of their implementation changes.

### Milestone 4: scheduled operation

- Implement the worker scheduler. **Implemented with an asynchronous monotonic
  interval loop and a `worker` CLI command.**
- Add retry/backoff behavior and overlap prevention. **Implemented with bounded
  exponential backoff, jitter, per-job serialization, and failure isolation.**
- Add container health and operational documentation. **Operational startup and
  graceful shutdown are documented; active health reporting remains pending.**
- Validate restart and persistence behavior through Docker Compose.
  **Implemented and verified: SIGTERM produced a clean worker shutdown, restart
  reused the SQLite baseline, and the immediate post-restart check emitted no
  false transitions.**

### Milestone 5: first external provider

- Research candidate providers and their policies. **BC Parks day-use endpoint
  behavior and public catalog shapes have been captured and sanitized; the
  public WelcomeBC Invitations to Apply page has also been evaluated.**
- Prefer an official API or structured endpoint where permitted. **Implemented
  with anonymous structured endpoints for BC Parks day-use and a bounded
  anonymous GET of WelcomeBC's server-rendered public HTML.**
- Implement a provider-specific driver using fixtures first. **Implemented for
  BC Parks day-use and High Economic Impact draw publication with strict fixture
  parsers and provider-shaped local mocks.**
- Add a real notification channel. **Implemented with generic SMTP email, an
  environment-backed registry, and a provider-shaped Mailpit acceptance test.**

## Recorded decisions

### D-001: Retain the repository and redesign in place

**Status:** Accepted

Python and Docker remain appropriate, but the tutorial code is not an
architectural foundation. The repository identity and history will be retained
while implementation is replaced incrementally.

### D-002: Use interchangeable provider drivers

**Status:** Accepted

Provider behavior is isolated behind a small availability-driver contract. The
core remains provider-independent.

### D-003: Begin with a mock provider and generic HTML driver

**Status:** Accepted

The first complete implementation will use a locally controlled mock reservation
site. This enables deterministic development and prevents unnecessary traffic to
real providers.

### D-004: Keep reservation execution separate

**Status:** Accepted

The first system is read-only and notification-oriented. Any future ability to
reserve or purchase will use a separate optional contract and dedicated safety
design.

### D-005: Use SQLite for initial persistence

**Status:** Initial decision

SQLite is sufficient for a personal, single-instance prototype and supports
transactional state comparison and deduplication without another service.

### D-006: Start with an explicit driver registry

**Status:** Accepted

Dynamic package discovery is deferred until independently distributed drivers
are needed. Interfaces should not preclude later use of Python entry points.

### D-007: Make a one-shot check the core execution unit

**Status:** Accepted

Scheduling wraps the same operation exposed through the CLI. This improves
testability and permits internal or external scheduling.

### D-008: Use standard Python packaging with focused tooling

**Status:** Accepted

The project uses `pyproject.toml` with setuptools, supports Python 3.11 or newer,
and declares runtime and development dependencies in project metadata. Pytest is
the test runner and Ruff provides formatting and linting. A top-level package
layout is sufficient for the current application.

### D-009: Treat structurally empty parsed pages as failures

**Status:** Accepted

The generic HTML parser raises an unexpected-page error when no configured
opportunity containers match. This prevents provider redesigns or access-denied
pages from being interpreted as every known opportunity disappearing.

### D-010: Use HTTPX with bounded streaming reads

**Status:** Accepted

The generic HTML driver uses HTTPX's asynchronous client and follows redirects.
It applies an explicit timeout and reads response chunks only up to a configured
size limit before parsing. Tests inject HTTPX's in-memory transport, keeping the
default suite deterministic and offline. The driver makes one request attempt;
cross-driver retry policy will be implemented above the driver boundary.

### D-011: Validate YAML in two layers

**Status:** Accepted

Configuration is loaded with PyYAML's safe loader. The application layer
strictly validates common job fields and keeps provider configuration deeply
immutable. The selected driver owns conversion and validation of its nested
configuration. Notification and scheduling policies now have common, strictly
validated schemas.

### D-012: Persist and compare complete successful snapshots atomically

**Status:** Accepted

SQLite stores per-job check runs, ordered opportunity observations, and detected
transition records. The latest snapshot is compared and the next snapshot is
written in one immediate transaction to prevent concurrent checks from using the
same baseline. Initial observations establish a baseline and emit only the
normalized `initially_available` events described by D-023. Failed driver checks
and failed persistence transactions do not replace the latest successful state.

### D-013: Use a transactional notification outbox

**Status:** Accepted

For every transition selected by a job's notification policy, SQLite creates one
outbox row per channel inside the snapshot transaction. A dispatcher uses the
explicit notifier registry, marks successful rows delivered, and retains failed
rows for retry. The uniqueness constraint on transition and channel deduplicates
completed work. Cross-process delivery semantics are at-least-once because an
external send and the local delivered marker cannot share one transaction.

### D-014: Use a purpose-built monotonic interval scheduler

**Status:** Accepted

The prototype uses a small asynchronous scheduling loop rather than an external
scheduling dependency. It runs jobs immediately at startup, maintains their
fixed interval cadence with a monotonic clock, serializes executions per job,
and permits unrelated jobs to execute concurrently. Missed occurrences caused
by a still-running job are skipped. Cross-driver retries are bounded and apply
capped exponential backoff with jitter. Signal-driven shutdown waits up to 30
seconds before cancelling unfinished work.

### D-015: Enforce pull-request quality gates with GitHub Actions

**Status:** Accepted

GitHub Actions runs the supported Python 3.11 environment for every pull request
into `development` and every push to that branch. One required quality job checks
for committed secrets, verifies Ruff formatting and linting, executes the
complete offline pytest suite, and validates the Docker Compose configuration.
The workflow has read-only repository permissions, cancels superseded runs for
the same ref, and pins external Actions to immutable release commit SHAs.
Container builds and live Compose acceptance tests remain explicit gates because
they are slower and are not required on every prototype commit.

### D-016: Require owner-approved pull requests for development

**Status:** Accepted

The `development` branch is protected against direct pushes, force pushes, and
deletion. Changes must arrive through pull requests authored by a contributor or
the repository-scoped `muxalko-web-checker-codex` GitHub App. Merging requires a
passing `Python quality gates` check, resolved review conversations, and a fresh
code-owner approval from `@muxalko`; the `Change governance` check also enforces
issue-first traceability. New reviewable commits dismiss an earlier approval.
Automation commits are authored and pushed with the App installation identity so
the human owner remains an independent reviewer. Repository administrators do
not bypass these requirements.

### D-017: Scan staged changes and repository history for secrets

**Status:** Accepted

Pre-commit runs an immutable Gitleaks release before every local commit. One hook
scans the staged diff to block newly introduced credentials, tokens, keys, and
other detected secrets; a second hook scans the complete reachable history of
the current branch so existing committed findings also fail verification.
GitHub Actions uses a full-depth checkout and runs the same hooks. Findings are
redacted, the default Gitleaks rules remain enabled through the checked-in
configuration, and exceptions require narrow review rather than broad path or
rule exclusions. Secret scanning reduces risk but does not prove that a tree is
free of every possible secret; exposed credentials must still be revoked and
rotated.

### D-018: Require issue-first, branch-based pull-request delivery

**Status:** Accepted

Every code, documentation, tooling, or configuration change begins with an open
GitHub issue that defines its outcome and acceptance criteria. A working branch
is then created from current `development` and named
`type/<issue-number>-description`. The pull request targets `development` and
uses a supported GitHub closing keyword for the same issue number so the issue
closes only when the change merges.

A read-only GitHub Actions job validates the base branch, branch name, closing
reference, issue type, and open state through the GitHub API. Its unique
`Change governance` status is required by branch protection alongside the
quality gates and owner approval. Read-only investigation does not require an
issue because it does not change repository state. GitHub cannot guarantee that
an issue remains open after a successful check without another event, so owner
review also confirms it is still open immediately before merge.

### D-019: Isolate local testing from post-merge production deployment

**Status:** Accepted

The local VM runs two Docker Compose projects that share no containers,
networks, configuration, or data. `web-checker-development` is a manually
started, disposable environment built from the current working tree. It includes
the controlled mock site and is the only environment where development control
endpoints are enabled. `web-checker-production` contains only the checker, reads
its configuration and environment file from fixed host paths outside the
repository, and persists SQLite state in its own Compose volume.

Production deployment follows a successful post-merge `CI` workflow run for a
push to `development`. A dedicated repository-scoped self-hosted runner with the
`web-checker-production` label checks out the exact validated SHA. The deployment
script rejects a dirty or mismatched checkout, builds the Dockerfile's lean
production target, tags and labels the local image with the full SHA, and starts
Compose with image building disabled. Pull-request workflows never target this
runner. The runner's Docker access is treated as privileged production access.

Deployments use a host file lock rather than cancellable GitHub concurrency so
every queued merge is serialized. Re-delivery of the currently healthy SHA is
idempotent. Compose health gating makes startup failures visible, retains the
previous image locally, and automatically reapplies it after a failed update.
Production data is never removed during deployment or rollback.

### D-020: Model append-only publications with appeared opportunities

**Status:** Accepted

Published provider events use the existing opportunity and transition model
rather than introducing provider-specific core concepts. A driver returns every
publication still present in its trusted source with a stable event ID and
`available` state. The initial complete snapshot establishes a baseline; a new
ID later produces `appeared`, which a job selects explicitly in its notification
policy. Publication metadata remains in attributes and the source page occupies
the existing link field.

Drivers must group multiple provider rows that describe one event before crossing
the core boundary. An unexpectedly empty, structurally changed, or inconsistent
feed fails instead of replacing the baseline. A provider removing older events
may create `disappeared` transitions, but publication jobs do not notify on them
by default.

### D-021: Keep delivery adapters on a narrow generic payload

**Status:** Accepted

Opportunity observations combine fixed provider-independent fields with an
opaque provider-specific attributes mapping serialized as JSON. Notification
delivery adapters consume only the fixed `PendingNotification` projection. The
first SMTP adapter formats the existing opportunity title and source link and
does not interpret or duplicate the attributes mapping.

This keeps email reusable across all drivers and avoids a storage migration for
the first real channel. A future richer notification contract must be justified
as a provider-independent capability rather than exposing arbitrary driver data
to adapters.

### D-022: Use anonymous structured BC Parks endpoints behind a local mock

**Status:** Accepted

The BC Parks day-use integration uses anonymous config, park, facility, and
reservation endpoints instead of automating the site's browser UI. The driver
performs only bounded `GET` requests and keeps all protocol parsing, provider
time, slot, capacity, and booking-window rules behind the driver contract. It
does not authenticate, reserve, cancel, purchase, or send personal information.
Requests include a same-origin `Origin` header derived from the configured base
URL because the provider's reservation route otherwise serves its HTML frontend
shell instead of the anonymous JSON representation.

A configurable base URL and production-shaped local mock make fixture and mock
testing the default. The mock covers the captured park/facility catalog and
deterministic availability, clock, closure, empty, malformed, slow, and error
scenarios. Raw traffic captures are not committed. Live validation is a manual,
explicit operator action; routine development and CI never contact the provider.

Pre-opening inventory and non-booking days normalize to `unknown`. Known positive
inventory becomes `available` only after opening, and `Full` with zero capacity
becomes `unavailable`. Unexpectedly empty, incomplete, or structurally changed
responses fail the check, preserving the last known-good snapshot rather than
reporting false disappearances.

### D-023: Model opt-in initial availability as a normalized transition

**Status:** Accepted

The first complete successful snapshot emits `initially_available` for each
opportunity whose normalized availability is `available`. Jobs opt into delivery
through the existing `notify.on` transition list, so configurations that omit it
retain quiet baselines. Initial unavailable and unknown opportunities do not
emit this transition.

The transition and any matching durable outbox rows are persisted atomically
with the baseline snapshot. An unchanged later check emits nothing, preserving
deduplication across repeated checks and restarts. Driver failures and incomplete
responses never reach storage and therefore cannot create an initial alert.

## Open decisions

These should be resolved with implementation evidence rather than assumed now:

- Retention period for observation history.
- Whether opportunity matching needs a core rule language or should remain
  entirely driver-specific initially.
