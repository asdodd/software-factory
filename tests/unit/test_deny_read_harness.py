import shutil
from pathlib import Path

import pytest

from factory.config.loader import Layer, load
from factory.config.validate import ConfigError

ROOT = Path(__file__).resolve().parents[2]
BYO = {"harnesses": {"cli": {"adapter": "harness.command", "settings": {"argv": ["agent"]}}},
       "models": {"stages": {"implement": {"provider": "scripted", "model": "m", "harness": "cli"}}}}


def deny_read_root(tmp_path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    base = tmp_path / "config" / "base.toml"
    base.write_text(base.read_text().replace('mode = "strip"', 'mode = "deny_read"'))
    return tmp_path


def test_deny_read_with_external_harness_is_a_startup_error(tmp_path):
    root = deny_read_root(tmp_path)
    with pytest.raises(ConfigError, match="deny_read cannot be enforced for harness 'cli'"):
        load(root, env={}, extra_layers=[Layer("scenario", BYO)])


def test_deny_read_with_builtin_harness_is_allowed(tmp_path):
    root = deny_read_root(tmp_path)
    builtin = {"models": {"stages": {"implement": {"provider": "scripted", "model": "m", "harness": "builtin"}}}}
    assert load(root, env={}, extra_layers=[Layer("scenario", builtin)]).config.access_policy.mode == "deny_read"
