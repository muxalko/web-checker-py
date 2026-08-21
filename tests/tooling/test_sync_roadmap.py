from collections.abc import Mapping
from typing import Any

from scripts.sync_roadmap import MILESTONES, sync_roadmap


class FakeApi:
    def __init__(self, milestones=()):
        self.milestones = list(milestones)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "GET":
            return self.milestones
        if path == "milestones":
            milestone = {"number": len(self.milestones) + 1, **payload}
            self.milestones.append(milestone)
            return milestone
        return {"number": int(path.removeprefix("issues/")), **payload}


def test_creates_three_milestones_and_assigns_every_task() -> None:
    api = FakeApi()

    assert sync_roadmap(api) == (3, 6)

    assigned = [call for call in api.calls if call[1].startswith("issues/")]
    assert [int(path.removeprefix("issues/")) for _, path, _ in assigned] == [
        issue for _, _, issues in MILESTONES for issue in issues
    ]


def test_reuses_existing_milestones_idempotently() -> None:
    existing = [
        {"number": number, "title": title}
        for number, (title, _, _) in enumerate(MILESTONES, start=10)
    ]
    api = FakeApi(existing)

    assert sync_roadmap(api) == (0, 6)
    assert not any(method == "POST" for method, _, _ in api.calls)
