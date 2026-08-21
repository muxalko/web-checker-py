"""Create roadmap milestones and assign their tracked GitHub issues."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

MILESTONES = (
    (
        "Milestone 6: Operational resilience",
        "Bound state growth, prove recovery, and surface operational failures.",
        (35, 36),
    ),
    (
        "Milestone 7: Notification experience",
        "Make durable notifications concise and useful as monitoring expands.",
        (37,),
    ),
    (
        "Milestone 8: Configuration and provider ergonomics",
        "Reduce configuration repetition and improve safe provider operation.",
        (38, 39, 40),
    ),
)


class RoadmapSyncError(RuntimeError):
    """Raised when roadmap metadata cannot be synchronized."""


ApiCall = Callable[[str, str, Mapping[str, Any] | None], Any]


def sync_roadmap(api_call: ApiCall) -> tuple[int, int]:
    """Create missing milestones and assign every roadmap issue."""
    existing = api_call("GET", "milestones?state=all", None)
    if not isinstance(existing, Sequence) or isinstance(existing, (str, bytes)):
        raise RoadmapSyncError("GitHub returned an invalid milestone list")
    by_title = {
        item.get("title"): item
        for item in existing
        if isinstance(item, Mapping) and isinstance(item.get("title"), str)
    }

    created = 0
    assigned = 0
    for title, description, issues in MILESTONES:
        milestone = by_title.get(title)
        if milestone is None:
            milestone = api_call(
                "POST",
                "milestones",
                {"title": title, "description": description},
            )
            created += 1
        if not isinstance(milestone, Mapping) or not isinstance(
            milestone.get("number"), int
        ):
            raise RoadmapSyncError(f"GitHub returned an invalid milestone {title!r}")
        for issue_number in issues:
            api_call(
                "PATCH",
                f"issues/{issue_number}",
                {"milestone": milestone["number"]},
            )
            assigned += 1
    return created, assigned


def github_api(
    repository: str,
    token: str,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None,
) -> Any:
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
            "User-Agent": "web-checker-roadmap-sync",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RoadmapSyncError(
            f"GitHub returned HTTP {exc.code} for {method} {path}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RoadmapSyncError(f"GitHub request failed: {exc.reason}") from exc


def main() -> int:
    """Synchronize roadmap metadata using the GitHub Actions token."""
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GITHUB_TOKEN"]
        created, assigned = sync_roadmap(
            lambda method, path, payload: github_api(
                repository, token, method, path, payload
            )
        )
    except (KeyError, RoadmapSyncError) as exc:
        print(f"Roadmap sync failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Roadmap synchronized: {created} milestone(s) created, "
        f"{assigned} issues assigned"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
