from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "drivers" / "generic_html" / "fixtures"


@pytest.fixture
def fixture_html():
    def load(name):
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    return load
