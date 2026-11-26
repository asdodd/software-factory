from pathlib import Path

import pytest

from factory.testing.scenario import run_file

CASES = sorted(p for p in (Path(__file__).parent / "cases").glob("*.toml") if not p.name.startswith("_"))


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_scenario(path):
    result = run_file(path)
    assert result.passed, "\n".join(result.failures)
