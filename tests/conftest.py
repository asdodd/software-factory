from factory.testing import netguard


def pytest_sessionstart(session):
    netguard.install()


def pytest_sessionfinish(session, exitstatus):
    netguard.uninstall()
    if netguard.violations:
        session.exitstatus = 1
        print(f"\nnetwork guard: blocked non-local connections: {netguard.violations}")
