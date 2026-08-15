"""Deploy one validated Git revision to the isolated production Compose stack."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

PRODUCTION_PROJECT = "web-checker-production"
PRODUCTION_CONTAINER = f"{PRODUCTION_PROJECT}-checker-1"
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REVISION_LABEL = "org.opencontainers.image.revision"


class DeploymentError(RuntimeError):
    """Raised when a production deployment violates an invariant or fails."""


def validate_revision(value: str) -> str:
    """Return a canonical full Git SHA or reject the deployment."""
    if REVISION_PATTERN.fullmatch(value) is None:
        raise DeploymentError("revision must be a 40-character lowercase Git SHA")
    return value


def validate_external_file(path: Path, repository: Path, label: str) -> Path:
    """Require a regular host file located outside the source checkout."""
    resolved = path.expanduser().resolve()
    repository = repository.resolve()
    if not resolved.is_file() or not os.access(resolved, os.R_OK):
        raise DeploymentError(f"{label} is not a readable regular file: {resolved}")
    try:
        resolved.relative_to(repository)
    except ValueError:
        return resolved
    raise DeploymentError(f"{label} must be stored outside the repository")


def production_environment(
    image: str, config: Path, env_file: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """Build the explicit environment consumed by production Compose."""
    result = dict(environ)
    result.update(
        {
            "WEB_CHECKER_IMAGE": image,
            "WEB_CHECKER_PRODUCTION_CONFIG_PATH": str(config),
            "WEB_CHECKER_PRODUCTION_ENV_PATH": str(env_file),
        }
    )
    return result


def run_command(
    arguments: Sequence[str],
    *,
    repository: Path,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one deployment command without invoking a shell."""
    return subprocess.run(
        list(arguments),
        cwd=repository,
        env=dict(environment) if environment is not None else None,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def ensure_exact_clean_revision(repository: Path, revision: str) -> None:
    """Reject dirty or mismatched sources before building production."""
    head = run_command(
        ("git", "rev-parse", "HEAD"),
        repository=repository,
        capture_output=True,
    ).stdout.strip()
    if head != revision:
        raise DeploymentError(
            f"checked-out revision {head or 'unknown'} does not match {revision}"
        )
    ensure_clean_repository(repository)


def ensure_clean_repository(repository: Path) -> None:
    """Reject source modifications in every production operation."""
    dirty = run_command(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        repository=repository,
        capture_output=True,
    ).stdout
    if dirty:
        raise DeploymentError("production operations require a clean working tree")


def current_container_state(
    repository: Path,
) -> tuple[str | None, str | None, str | None]:
    """Return the current production image, health, and revision label."""
    result = run_command(
        (
            "docker",
            "inspect",
            "--format={{.Config.Image}}|"
            "{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
            f'{{{{index .Config.Labels "{REVISION_LABEL}"}}}}',
            PRODUCTION_CONTAINER,
        ),
        repository=repository,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None, None, None
    image, health, revision = result.stdout.strip().split("|", maxsplit=2)
    return image or None, health or None, revision or None


def compose_command(repository: Path) -> tuple[str, ...]:
    """Return the immutable production Compose command prefix."""
    return (
        "docker",
        "compose",
        "--project-name",
        PRODUCTION_PROJECT,
        "--file",
        str(repository / "compose.production.yaml"),
    )


def validate_state_directory(path: Path, repository: Path) -> Path:
    """Keep deployment coordination and history outside the checkout."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repository.resolve())
    except ValueError:
        return resolved
    raise DeploymentError("production deployment state must be outside the repository")


def revision_from_image(image: str) -> str:
    """Extract and validate the SHA from a production image reference."""
    prefix = "web-checker-production:"
    if not image.startswith(prefix):
        raise DeploymentError("recorded rollback image is not a production image")
    return validate_revision(image.removeprefix(prefix))


def deploy(
    *,
    repository: Path,
    revision: str,
    config: Path,
    env_file: Path,
    state_directory: Path,
) -> str:
    """Build, health-check, and atomically record one production revision."""
    repository = repository.resolve()
    revision = validate_revision(revision)
    config = validate_external_file(config, repository, "production configuration")
    env_file = validate_external_file(env_file, repository, "production environment")
    ensure_exact_clean_revision(repository, revision)

    image = f"web-checker-production:{revision}"
    environment = production_environment(image, config, env_file, os.environ)
    state_directory = validate_state_directory(state_directory, repository)
    state_directory.mkdir(parents=True, exist_ok=True)

    lock_path = state_directory / "deployment.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        print("production deployment waiting for the host lock", flush=True)
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        compose = compose_command(repository)
        run_command(
            (*compose, "config", "--quiet"),
            repository=repository,
            environment=environment,
        )
        previous_image, health, current_revision = current_container_state(repository)
        if (
            previous_image == image
            and health == "healthy"
            and current_revision == revision
        ):
            print(f"production already runs {image}; deployment is a no-op")
            return image

        run_command(
            (
                "docker",
                "build",
                "--target",
                "production",
                "--label",
                f"{REVISION_LABEL}={revision}",
                "--tag",
                image,
                ".",
            ),
            repository=repository,
        )
        label = run_command(
            (
                "docker",
                "image",
                "inspect",
                f'--format={{{{index .Config.Labels "{REVISION_LABEL}"}}}}',
                image,
            ),
            repository=repository,
            capture_output=True,
        ).stdout.strip()
        if label != revision:
            raise DeploymentError("built image does not carry the validated revision")

        try:
            run_command(
                (
                    *compose,
                    "up",
                    "--detach",
                    "--no-build",
                    "--remove-orphans",
                    "--wait",
                    "--wait-timeout",
                    "120",
                ),
                repository=repository,
                environment=environment,
            )
        except subprocess.CalledProcessError as error:
            if previous_image and previous_image != image:
                rollback_environment = production_environment(
                    previous_image, config, env_file, os.environ
                )
                try:
                    run_command(
                        (
                            *compose,
                            "up",
                            "--detach",
                            "--no-build",
                            "--remove-orphans",
                            "--wait",
                            "--wait-timeout",
                            "120",
                        ),
                        repository=repository,
                        environment=rollback_environment,
                    )
                except subprocess.CalledProcessError as rollback_error:
                    raise DeploymentError(
                        "deployment and automatic rollback both failed"
                    ) from rollback_error
                raise DeploymentError(
                    f"deployment failed; production was rolled back to {previous_image}"
                ) from error
            raise DeploymentError(
                "deployment failed and no previous production image was available"
            ) from error

        _write_state(state_directory / "current-image", image)
        if previous_image and previous_image != image:
            _write_state(state_directory / "previous-image", previous_image)
        print(f"production deployment healthy: {image}")
        return image


def rollback(
    *,
    repository: Path,
    config: Path,
    env_file: Path,
    state_directory: Path,
) -> str:
    """Reapply the recorded previous image without rebuilding source."""
    repository = repository.resolve()
    config = validate_external_file(config, repository, "production configuration")
    env_file = validate_external_file(env_file, repository, "production environment")
    ensure_clean_repository(repository)
    state_directory = validate_state_directory(state_directory, repository)
    previous_path = state_directory / "previous-image"
    try:
        previous_image = previous_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise DeploymentError("no previous production image is recorded") from error
    previous_revision = revision_from_image(previous_image)

    lock_path = state_directory / "deployment.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        print("production rollback waiting for the host lock", flush=True)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        current_image, health, current_revision = current_container_state(repository)
        if (
            current_image == previous_image
            and health == "healthy"
            and current_revision == previous_revision
        ):
            print(f"production already runs {previous_image}; rollback is a no-op")
            return previous_image

        label = run_command(
            (
                "docker",
                "image",
                "inspect",
                f'--format={{{{index .Config.Labels "{REVISION_LABEL}"}}}}',
                previous_image,
            ),
            repository=repository,
            capture_output=True,
        ).stdout.strip()
        if label != previous_revision:
            raise DeploymentError("rollback image does not carry its recorded revision")

        environment = production_environment(
            previous_image, config, env_file, os.environ
        )
        compose = compose_command(repository)
        run_command(
            (*compose, "config", "--quiet"),
            repository=repository,
            environment=environment,
        )
        run_command(
            (
                *compose,
                "up",
                "--detach",
                "--no-build",
                "--remove-orphans",
                "--wait",
                "--wait-timeout",
                "120",
            ),
            repository=repository,
            environment=environment,
        )
        _write_state(state_directory / "current-image", previous_image)
        if current_image and current_image != previous_image:
            _write_state(state_directory / "previous-image", current_image)
        print(f"production rollback healthy: {previous_image}")
        return previous_image


def _write_state(path: Path, value: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(f"{value}\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--revision")
    operation.add_argument("--rollback", action="store_true")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=Path.home() / ".local/state/web-checker-production",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        repository = Path(__file__).resolve().parents[1]
        common = {
            "repository": repository,
            "config": arguments.config,
            "env_file": arguments.env_file,
            "state_directory": arguments.state_directory,
        }
        if arguments.rollback:
            rollback(**common)
        else:
            deploy(revision=arguments.revision, **common)
    except (DeploymentError, OSError, subprocess.CalledProcessError) as error:
        print(f"production deployment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
