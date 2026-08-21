# Local VM deployment runbook

For the rationale, current price comparison, and migration triggers, see
[Hosting options](hosting-options.md).

The local VM hosts two isolated Docker Compose projects. They share only the
Docker daemon:

| Environment | Compose project | Source | Services | State |
| --- | --- | --- | --- | --- |
| Development testing | `web-checker-development` | Current working tree, including uncommitted changes | Checker and controlled mock site | Development-only Compose volume |
| Production | `web-checker-production` | Exact `development` merge SHA validated by CI | Checker only | Production-only Compose volume |

The project name prefixes container, network, and Compose-managed volume names.
Never override these names with `docker compose --project-name` or
`COMPOSE_PROJECT_NAME` in routine use.

## Development testing before commit

From the repository working tree, run:

```console
docker compose up --build --detach --wait
docker compose ps
docker compose logs --follow checker
```

This always selects `compose.yaml` and therefore
`web-checker-development`. It is safe to rebuild or remove this stack without
touching production:

```console
docker compose down --volumes
```

The mock site and its development-only control endpoints exist only in this
stack.

## Production host preparation

Production deployment requires Git, Python 3.11 or newer, Docker Engine, and
Docker Compose v2 on the VM. Create a dedicated operating-system account for the
self-hosted GitHub Actions runner. That account needs Docker access.

Docker daemon access is effectively root-level access to the VM. The runner is
therefore a trusted production principal, not a general build runner. It must:

- be registered only in `muxalko/web-checker-py`;
- carry the custom label `web-checker-production` in addition to the default
  `self-hosted`, `linux`, and `x64` labels;
- execute only the post-merge production workflow from the protected default
  branch;
- never be selected by pull-request, ad-hoc branch, or third-party workflows;
- run under its dedicated account rather than a personal login.

Use **Repository settings → Actions → Runners → New self-hosted runner** to get
GitHub's current installation and short-lived registration commands. Add the
`web-checker-production` label during registration and install the runner as a
service. Do not commit or copy the registration token into a file.

Create the host configuration directory before starting the runner:

```console
sudo install -d -m 0755 /etc/web-checker-production
sudo install -m 0644 /path/to/reviewed/jobs.yaml \
  /etc/web-checker-production/jobs.yaml
sudo install -m 0600 -o web-checker-deploy -g web-checker-deploy /dev/null \
  /etc/web-checker-production/worker.env
```

Replace `web-checker-deploy` if the dedicated runner account has a different
name. Job configuration must not contain secret values. Put provider or notifier
environment values in `worker.env`, one `NAME=value` per line. The current
drivers do not require secrets. An enabled job using the `email` channel requires
`WEB_CHECKER_SMTP_HOST`, `WEB_CHECKER_SMTP_FROM`, and `WEB_CHECKER_SMTP_TO`.
Production SMTP commonly also requires `WEB_CHECKER_SMTP_USERNAME` and
`WEB_CHECKER_SMTP_PASSWORD`; both must be set together.

`WEB_CHECKER_SMTP_SECURITY` accepts `starttls` (the default), `implicit-tls`, or
`none`, with optional `WEB_CHECKER_SMTP_PORT` and
`WEB_CHECKER_SMTP_TIMEOUT_SECONDS`. Use `none` only for trusted local capture.
Keep the environment file mode `0600`. Validate it without printing resolved
values:

```console
WEB_CHECKER_IMAGE=web-checker-production:validation \
  WEB_CHECKER_PRODUCTION_CONFIG_PATH=/etc/web-checker-production/jobs.yaml \
  WEB_CHECKER_PRODUCTION_ENV_PATH=/etc/web-checker-production/worker.env \
  docker compose --file compose.production.yaml config --quiet
```

Create a GitHub Environment named `production` and restrict it to protected
branches. Pull-request approval remains the human production approval gate; the
deployment workflow itself receives read-only repository permission.

## Automated production deployment

`.github/workflows/deploy-production.yml` listens only for completion of the
`CI` workflow. Its deployment job runs only when all of these are true:

- CI concluded successfully;
- CI was triggered by a push, not a pull request;
- the validated branch was `development`.

The workflow checks out `workflow_run.head_sha` directly. The deployment script
then independently requires a clean checkout whose `HEAD` equals that full SHA.
It builds the Dockerfile's `production` target as
`web-checker-production:<sha>`, verifies the OCI revision label, and deploys
`compose.production.yaml` with `--no-build`.

A host file lock in
`~/.local/state/web-checker-production/deployment.lock` serializes every queued
deployment without cancelling an active or waiting revision. Docker health must
become `healthy` within 120 seconds. Production state remains in the
`web-checker-production_checker-data` volume across image changes and rollback.
The independently restarted `maintenance` service takes an online backup before
each retention pass. Backups use the separate
`web-checker-production_checker-backups` volume.

Defaults are a daily cycle, 90 days of observations, 30 days of completed
notification records, and 14 backups. Override them with
`WEB_CHECKER_MAINTENANCE_INTERVAL_SECONDS`,
`WEB_CHECKER_OBSERVATION_RETENTION_DAYS`,
`WEB_CHECKER_NOTIFICATION_RETENTION_DAYS`, and
`WEB_CHECKER_BACKUP_RETENTION_COUNT`. Pending deliveries and each job's latest
baseline are always retained.

## Inspection

The production Compose file requires explicit external inputs even for
inspection. Load the last deployed image without printing the environment file:

```console
export WEB_CHECKER_IMAGE="$(sed -n '1p' \
  "$HOME/.local/state/web-checker-production/current-image")"
export WEB_CHECKER_PRODUCTION_CONFIG_PATH=/etc/web-checker-production/jobs.yaml
export WEB_CHECKER_PRODUCTION_ENV_PATH=/etc/web-checker-production/worker.env
docker compose --file compose.production.yaml ps
docker compose --file compose.production.yaml logs --tail=100 checker
docker compose --file compose.production.yaml logs --tail=20 maintenance
docker compose --file compose.production.yaml run --rm maintenance \
  web-checker --database /data/web-checker.db status \
  --backup-directory /backups
```

Do not run `docker compose config` without `--quiet` on the production host;
resolved environment values can otherwise be printed.

## Backup restore drill

Run this read-only drill after first deployment and after storage changes:

```console
docker run --rm \
  -v web-checker-production_checker-backups:/backups:ro \
  -v web-checker-restore-test:/restore \
  web-checker-production:<validated-sha> \
  sh -c 'cp "$(ls -1t /backups/web-checker-*.db | head -1)" /restore/restored.db && web-checker validate-restore /restore/restored.db'
docker volume rm web-checker-restore-test
```

For actual recovery, stop both production services, preserve the damaged
database, copy a validated backup to `/data/web-checker.db` in the data volume,
then restart the stack and inspect both service logs.

## Failure recovery and rollback

If a new container fails its health gate, the deployment script automatically
reapplies the preceding image and fails the GitHub Actions job visibly. If both
deployment and automatic rollback fail, inspect the production logs and Docker
daemon before making another change. Never delete the production volume during
recovery.

For a manual rollback after a logically incorrect but healthy deployment, use a
clean checkout of `development` and the same guarded script:

```console
python3 scripts/deploy_production.py \
  --rollback \
  --config /etc/web-checker-production/jobs.yaml \
  --env-file /etc/web-checker-production/worker.env
```

The script verifies the image's revision label, takes the same deployment lock,
waits for health, and swaps the current and previous image records. Confirm that
`web-checker-production-checker-1` is healthy and record the rollback in the
tracking issue. Images are retained locally for rollback; do not run an
unreviewed image-pruning command on the production VM.
