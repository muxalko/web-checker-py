from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_yaml(path: str, *, base: bool = False):
    loader = yaml.BaseLoader if base else yaml.SafeLoader
    return yaml.load((ROOT / path).read_text(encoding="utf-8"), Loader=loader)


def test_compose_projects_are_explicit_and_isolated() -> None:
    development = load_yaml("compose.yaml")
    production = load_yaml("compose.production.yaml")

    assert development["name"] == "web-checker-development"
    assert set(development["services"]) == {"checker", "mailpit", "mock-site"}
    assert development["services"]["checker"]["environment"] == {
        "WEB_CHECKER_SMTP_FROM": "alerts@example.test",
        "WEB_CHECKER_SMTP_HOST": "mailpit",
        "WEB_CHECKER_SMTP_PORT": "1025",
        "WEB_CHECKER_SMTP_SECURITY": "none",
        "WEB_CHECKER_SMTP_TO": "operator@example.test",
    }
    assert development["services"]["checker"]["depends_on"] == {
        "mailpit": {"condition": "service_healthy"},
        "mock-site": {"condition": "service_healthy"},
    }
    assert development["services"]["mailpit"]["image"] == ("axllent/mailpit:v1.30.7")
    assert development["services"]["mock-site"]["environment"] == {
        "MOCK_SITE_CONTROLS_ENABLED": "true"
    }

    assert production["name"] == "web-checker-production"
    assert set(production["services"]) == {"checker"}
    checker = production["services"]["checker"]
    assert "build" not in checker
    assert "ports" not in checker
    assert "depends_on" not in checker
    assert "healthcheck" in checker
    assert checker["volumes"] == [
        "checker-data:/data",
        {
            "type": "bind",
            "source": "${WEB_CHECKER_PRODUCTION_CONFIG_PATH:?Set "
            "WEB_CHECKER_PRODUCTION_CONFIG_PATH}",
            "target": "/etc/web-checker/jobs.yaml",
            "read_only": True,
        },
    ]


def test_production_workflow_deploys_only_successful_development_pushes() -> None:
    workflow = load_yaml(".github/workflows/deploy-production.yml", base=True)
    trigger = workflow["on"]["workflow_run"]
    deploy = workflow["jobs"]["deploy"]

    assert trigger == {"workflows": ["CI"], "types": ["completed"]}
    assert "conclusion == 'success'" in deploy["if"]
    assert "event == 'push'" in deploy["if"]
    assert "head_branch == 'development'" in deploy["if"]
    assert deploy["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "web-checker-production",
    ]
    checkout = deploy["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert "scripts/deploy_production.py" in deploy["steps"][1]["run"]


def test_ci_validates_both_compose_definitions() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "docker compose config --quiet" in workflow
    assert "docker compose --file compose.production.yaml config --quiet" in workflow


def test_dockerfile_has_separate_runtime_targets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM base AS production" in dockerfile
    assert "python -m pip install ." in dockerfile
    assert "FROM base AS development" in dockerfile
    assert 'python -m pip install --editable ".[dev]"' in dockerfile
