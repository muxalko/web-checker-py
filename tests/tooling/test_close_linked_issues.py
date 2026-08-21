import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.check_pr_governance import extract_closing_issue_numbers
from scripts.close_linked_issues import issues_to_close


def event(
    *,
    merged: bool = True,
    base: str = "development",
    body: str | None = "Closes #6\nFixes: #7",
) -> dict[str, Any]:
    return {
        "pull_request": {
            "merged": merged,
            "base": {"ref": base},
            "body": body,
        }
    }


def lookup(issues: Mapping[int, Mapping[str, Any]]):
    return lambda number: issues[number]


def test_extracts_all_unique_closing_references() -> None:
    assert extract_closing_issue_numbers(
        "Closes #8, fixes: #7; RESOLVED #8; close #6"
    ) == (6, 7, 8)


def test_returns_every_open_linked_issue() -> None:
    issues = {
        6: {"number": 6, "state": "open"},
        7: {"number": 7, "state": "open"},
    }

    assert issues_to_close(event(), lookup(issues)) == (6, 7)


def test_skips_closed_issues_and_pull_requests() -> None:
    issues = {
        6: {"number": 6, "state": "closed"},
        7: {"number": 7, "state": "open", "pull_request": {}},
    }

    assert issues_to_close(event(), lookup(issues)) == ()


def test_unmerged_or_non_development_pull_request_closes_nothing() -> None:
    def unexpected_lookup(_number: int) -> Mapping[str, Any]:
        raise AssertionError("issue lookup should not run")

    assert issues_to_close(event(merged=False), unexpected_lookup) == ()
    assert issues_to_close(event(base="main"), unexpected_lookup) == ()
    assert issues_to_close({}, unexpected_lookup) == ()


def test_module_entry_point_resolves_scripts_package() -> None:
    repository = Path(__file__).parents[2]

    result = subprocess.run(
        [sys.executable, "-m", "scripts.close_linked_issues"],
        cwd=repository,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Issue closure failed:" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
