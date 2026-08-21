# Web Checker roadmap

## Purpose

This roadmap sequences the next improvements after the first production BC Parks
and WelcomeBC monitors. `DESIGN.md` remains the canonical technical design
record; this document tracks delivery order, dependencies, and completion gates.
Each deliverable has a GitHub issue and must follow the repository's issue,
branch, pull-request, review, and verification workflow.

## Priority order

1. Bound data growth and prove recovery before observation history becomes
   operationally expensive.
2. Make provider, worker, and notification failures visible before adding more
   monitored jobs.
3. Reduce notification noise while retaining durable delivery semantics.
4. Improve configuration and provider discovery after the operational foundation
   is measurable and recoverable.

## Milestone 6: Operational resilience

Goal: keep the single-instance production service recoverable and make failures
actionable.

- [#35 — Add observation retention and tested production backups](https://github.com/muxalko/web-checker-py/issues/35)
- [#36 — Add operational status and repeated-failure alerts](https://github.com/muxalko/web-checker-py/issues/36)

Dependencies and order:

1. Define retention and backup/restore behavior in #35.
2. Build status and alerting in #36 on the resulting storage and backup metrics.

Completion gate: production has bounded state growth, automated retained backups,
a tested restore path, per-job success/failure visibility, notification backlog
visibility, and deduplicated repeated-failure alerts.

## Milestone 7: Notification experience

Goal: keep alerts useful as the number of jobs and opportunities grows.

- [#37 — Group related availability changes into digest notifications](https://github.com/muxalko/web-checker-py/issues/37)

Dependency: operational backlog visibility from #36 should land first so digest
delivery failures remain observable.

Completion gate: transitions from one job/check can be delivered as one bounded,
deterministically ordered message without weakening outbox retry or deduplication.

## Milestone 8: Configuration and provider ergonomics

Goal: make safe monitoring easier to configure while reducing unnecessary
provider traffic.

- [#38 — Support declarative job matrices for facilities and slots](https://github.com/muxalko/web-checker-py/issues/38)
- [#39 — Add read-only provider discovery and inspection commands](https://github.com/muxalko/web-checker-py/issues/39)
- [#40 — Add schedule windows and adaptive polling policies](https://github.com/muxalko/web-checker-py/issues/40)

Dependencies and order:

1. Add read-only discovery in #39 so configuration uses exact provider values.
2. Add deterministic matrix expansion in #38 using stable discovered identifiers.
3. Add provider-independent schedule windows in #40 after expanded job identity
   and status reporting are stable.

Completion gate: operators can inspect supported provider values, express
facility/slot combinations without duplicated jobs, and use timezone-aware
polling windows without moving provider rules into the scheduler.

## Deferred decisions

- A general opportunity-matching rule language remains deferred until multiple
  drivers demonstrate requirements that cannot be expressed by driver config and
  normalized transition policy.
- Additional providers and notification channels should not outrank the
  operational-resilience milestone unless a concrete need changes the priority.
