from collections.abc import Mapping
from typing import Any

import pytest

from scripts.check_pr_governance import GovernanceError, validate_pull_request


def pull_request_event(
    *,
    base: str = "development",
    branch: str = "agent/6-repository-governance",
    body: str | None = "Closes #6",
) -> dict[str, Any]:
    return {
        "pull_request": {
            "base": {"ref": base},
            "head": {"ref": branch},
            "body": body,
        }
    }


def issue_lookup(
    issue: Mapping[str, Any] | None = None,
):
    result = issue or {"number": 6, "state": "open"}
    return lambda _number: result


def test_accepts_matching_branch_closing_reference_and_open_issue() -> None:
    issue_number = validate_pull_request(
        pull_request_event(body="CLOSES: #6"),
        issue_lookup(),
    )

    assert issue_number == 6


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({}, "event does not contain a pull request"),
        (pull_request_event(base="main"), "must target development"),
        (
            pull_request_event(branch="agent/repository-governance"),
            "branch must match",
        ),
        (pull_request_event(body=None), "must contain Closes #6"),
        (pull_request_event(body="Closes #7"), "must contain Closes #6"),
    ],
)
def test_rejects_invalid_pull_request_metadata(
    event: Mapping[str, Any], message: str
) -> None:
    with pytest.raises(GovernanceError, match=message):
        validate_pull_request(event, issue_lookup())


def test_rejects_closed_issue() -> None:
    with pytest.raises(GovernanceError, match="must remain open until merge"):
        validate_pull_request(
            pull_request_event(),
            issue_lookup({"number": 6, "state": "closed"}),
        )


def test_rejects_pull_request_number_used_as_issue() -> None:
    with pytest.raises(GovernanceError, match="not an issue"):
        validate_pull_request(
            pull_request_event(),
            issue_lookup(
                {"number": 6, "state": "open", "pull_request": {"url": "example"}}
            ),
        )
