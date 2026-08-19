from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def welcomebc_fixture():
    def load(name):
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    return load
