"""Validate the issue-first pull-request workflow for GitHub Actions."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

BRANCH_PATTERN = re.compile(
    r"^[a-z][a-z0-9._-]*/(?P<issue>[1-9][0-9]*)-[a-z0-9][a-z0-9._-]*$"
)
CLOSING_REFERENCE_PATTERN = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(?P<issue>[1-9][0-9]*)\b",
    re.IGNORECASE,
)


class GovernanceError(ValueError):
    """Raised when a pull request violates repository governance."""


def validate_pull_request(
    event: Mapping[str, Any],
    issue_lookup: Callable[[int], Mapping[str, Any]],
) -> int:
    """Validate a pull-request event and return its tracking issue number."""
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, Mapping):
        raise GovernanceError("event does not contain a pull request")

    base = pull_request.get("base")
    base_ref = base.get("ref") if isinstance(base, Mapping) else None
    if base_ref != "development":
        raise GovernanceError("pull request must target development")

    head = pull_request.get("head")
    head_ref = head.get("ref") if isinstance(head, Mapping) else None
    if not isinstance(head_ref, str):
        raise GovernanceError("pull request does not contain a head branch")

    branch_match = BRANCH_PATTERN.fullmatch(head_ref)
    if branch_match is None:
        raise GovernanceError(
            "branch must match type/<issue-number>-description "
            "(for example, agent/123-add-check)"
        )
    issue_number = int(branch_match.group("issue"))

    body = pull_request.get("body")
    closing_issues = {
        int(match.group("issue"))
        for match in CLOSING_REFERENCE_PATTERN.finditer(body or "")
    }
    if issue_number not in closing_issues:
        raise GovernanceError(f"pull request body must contain Closes #{issue_number}")

    issue = issue_lookup(issue_number)
    if "pull_request" in issue:
        raise GovernanceError(f"#{issue_number} is a pull request, not an issue")
    if issue.get("state") != "open":
        raise GovernanceError(f"issue #{issue_number} must remain open until merge")

    return issue_number


def fetch_issue(repository: str, token: str, issue_number: int) -> Mapping[str, Any]:
    """Fetch an issue using the read-only GitHub Actions token."""
    repository_path = urllib.parse.quote(repository, safe="/")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository_path}/issues/{issue_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "web-checker-pr-governance",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise GovernanceError(
            f"unable to read issue #{issue_number}: GitHub returned HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise GovernanceError(
            f"unable to read issue #{issue_number}: {exc.reason}"
        ) from exc

    if not isinstance(result, Mapping):
        raise GovernanceError(f"GitHub returned an invalid issue #{issue_number}")
    return result


def main() -> int:
    """Load the GitHub event, validate it, and produce an actionable result."""
    try:
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        repository = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GITHUB_TOKEN"]
    except KeyError as exc:
        print(
            f"PR governance failed: missing environment variable {exc.args[0]}",
            file=sys.stderr,
        )
        return 1

    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
        issue_number = validate_pull_request(
            event,
            lambda number: fetch_issue(repository, token, number),
        )
    except (GovernanceError, json.JSONDecodeError, OSError) as exc:
        print(f"PR governance failed: {exc}", file=sys.stderr)
        return 1

    print(f"PR governance passed: linked open issue #{issue_number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
