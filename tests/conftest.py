import tomllib
from pathlib import Path

import pytest

from factory.testing import netguard

ROOT = Path(__file__).resolve().parents[1]


def pytest_sessionstart(session):
    with (ROOT / "config" / "base.toml").open("rb") as fh:
        if not tomllib.load(fh).get("access_policy", {}).get("enabled", True):
            raise pytest.UsageError("access_policy.enabled is false in config/base.toml; tests refuse to run")
    netguard.install()


def pytest_sessionfinish(session, exitstatus):
    netguard.uninstall()
    if netguard.violations:
        session.exitstatus = 1
        print(f"\nnetwork guard: blocked non-local connections: {netguard.violations}")
