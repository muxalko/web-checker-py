"""Close every issue linked from a pull request merged into development."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scripts.check_pr_governance import extract_closing_issue_numbers


class IssueClosureError(RuntimeError):
    """Raised when a merged pull request cannot be processed safely."""


def issues_to_close(
    event: Mapping[str, Any],
    issue_lookup: Callable[[int], Mapping[str, Any]],
) -> tuple[int, ...]:
    """Return open issue numbers referenced by a merged development PR."""
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, Mapping):
        return ()
    base = pull_request.get("base")
    if not pull_request.get("merged") or not isinstance(base, Mapping):
        return ()
    if base.get("ref") != "development":
        return ()

    closable = []
    for number in extract_closing_issue_numbers(pull_request.get("body")):
        issue = issue_lookup(number)
        if "pull_request" not in issue and issue.get("state") == "open":
            closable.append(number)
    return tuple(closable)


def github_request(
    repository: str,
    token: str,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Send one bounded GitHub API request."""
    repository_path = urllib.parse.quote(repository, safe="/")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository_path}/{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "web-checker-issue-closure",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise IssueClosureError(
            f"GitHub returned HTTP {exc.code} for {method} {path}"
        ) from exc
    except urllib.error.URLError as exc:
        raise IssueClosureError(f"GitHub request failed: {exc.reason}") from exc
    if not isinstance(result, Mapping):
        raise IssueClosureError(f"GitHub returned invalid data for {method} {path}")
    return result


def main() -> int:
    """Close linked issues for the current pull-request event."""
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        repository = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GITHUB_TOKEN"]
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            pull_request_number = int(os.environ["PULL_REQUEST_NUMBER"])
            event = {
                "pull_request": github_request(
                    repository,
                    token,
                    "GET",
                    f"pulls/{pull_request_number}",
                )
            }
        numbers = issues_to_close(
            event,
            lambda number: github_request(repository, token, "GET", f"issues/{number}"),
        )
        for number in numbers:
            github_request(
                repository,
                token,
                "PATCH",
                f"issues/{number}",
                {"state": "closed", "state_reason": "completed"},
            )
    except (
        IssueClosureError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Issue closure failed: {exc}", file=sys.stderr)
        return 1

    print("Closed linked issues: " + (", ".join(f"#{n}" for n in numbers) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
