"""A sandbox that declares it can hide paths (for deny_read validation tests only)."""

from factory.adapters.sandbox.local_dir import LocalDirSandbox


class HidingSandbox(LocalDirSandbox):
    HIDES_PATHS = True
