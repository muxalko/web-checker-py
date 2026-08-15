import subprocess
from pathlib import Path

import pytest

from scripts import deploy_production
from scripts.deploy_production import DeploymentError

REVISION = "a" * 40


def test_validate_revision_requires_canonical_full_sha() -> None:
    assert deploy_production.validate_revision(REVISION) == REVISION

    for invalid in ("a" * 39, "A" * 40, "main", "../revision"):
        with pytest.raises(DeploymentError, match="40-character lowercase Git SHA"):
            deploy_production.validate_revision(invalid)


def test_rollback_image_must_be_an_exact_production_revision() -> None:
    image = f"web-checker-production:{REVISION}"

    assert deploy_production.revision_from_image(image) == REVISION
    with pytest.raises(DeploymentError, match="not a production image"):
        deploy_production.revision_from_image(f"untrusted:{REVISION}")


def test_external_configuration_must_not_be_in_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    internal = repository / "jobs.yaml"
    internal.write_text("jobs: []\n", encoding="utf-8")
    external = tmp_path / "jobs.yaml"
    external.write_text("jobs: []\n", encoding="utf-8")

    assert (
        deploy_production.validate_external_file(
            external, repository, "production configuration"
        )
        == external
    )
    with pytest.raises(DeploymentError, match="outside the repository"):
        deploy_production.validate_external_file(
            internal, repository, "production configuration"
        )


def test_production_environment_sets_explicit_compose_inputs(
    tmp_path: Path,
) -> None:
    environment = deploy_production.production_environment(
        "web-checker-production:abc",
        tmp_path / "jobs.yaml",
        tmp_path / "worker.env",
        {"PATH": "/bin"},
    )

    assert environment == {
        "PATH": "/bin",
        "WEB_CHECKER_IMAGE": "web-checker-production:abc",
        "WEB_CHECKER_PRODUCTION_CONFIG_PATH": str(tmp_path / "jobs.yaml"),
        "WEB_CHECKER_PRODUCTION_ENV_PATH": str(tmp_path / "worker.env"),
    }


def test_healthy_current_revision_is_an_idempotent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, config, env_file, state = deployment_paths(tmp_path)
    image = f"web-checker-production:{REVISION}"
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        deploy_production, "ensure_exact_clean_revision", lambda *_: None
    )
    monkeypatch.setattr(
        deploy_production,
        "current_container_state",
        lambda _repository: (image, "healthy", REVISION),
    )

    def fake_run(arguments, **_kwargs):
        commands.append(tuple(arguments))
        return completed(arguments)

    monkeypatch.setattr(deploy_production, "run_command", fake_run)

    assert (
        deploy_production.deploy(
            repository=repository,
            revision=REVISION,
            config=config,
            env_file=env_file,
            state_directory=state,
        )
        == image
    )
    assert [command[-2:] for command in commands] == [("config", "--quiet")]


def test_failed_update_rolls_back_to_previous_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, config, env_file, state = deployment_paths(tmp_path)
    previous = "web-checker-production:" + "b" * 40
    deployment_environments: list[str] = []
    up_attempts = 0

    monkeypatch.setattr(
        deploy_production, "ensure_exact_clean_revision", lambda *_: None
    )
    monkeypatch.setattr(
        deploy_production,
        "current_container_state",
        lambda _repository: (previous, "healthy", "b" * 40),
    )

    def fake_run(arguments, **kwargs):
        nonlocal up_attempts
        command = tuple(arguments)
        if command[:3] == ("docker", "image", "inspect"):
            return completed(arguments, stdout=f"{REVISION}\n")
        if "up" in command:
            up_attempts += 1
            deployment_environments.append(kwargs["environment"]["WEB_CHECKER_IMAGE"])
            if up_attempts == 1:
                raise subprocess.CalledProcessError(1, command)
        return completed(arguments)

    monkeypatch.setattr(deploy_production, "run_command", fake_run)

    with pytest.raises(DeploymentError, match="rolled back"):
        deploy_production.deploy(
            repository=repository,
            revision=REVISION,
            config=config,
            env_file=env_file,
            state_directory=state,
        )

    assert deployment_environments == [
        f"web-checker-production:{REVISION}",
        previous,
    ]


def deployment_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    config = tmp_path / "jobs.yaml"
    config.write_text("jobs: []\n", encoding="utf-8")
    env_file = tmp_path / "worker.env"
    env_file.write_text("", encoding="utf-8")
    return repository, config, env_file, tmp_path / "state"


def completed(arguments, *, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")
