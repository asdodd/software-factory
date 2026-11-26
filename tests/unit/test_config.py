import logging
from pathlib import Path

import pytest

from factory.config.checks import filters_may_overlap
from factory.config.loader import Layer, Overrides, load
from factory.config.schema import FilterConfig
from factory.config.validate import ConfigError
from factory.core.container import Container, FeatureDisabled

ROOT = Path(__file__).resolve().parents[2]


def test_layer_precedence_base_profile_env_cli():
    assert load(ROOT, env={}).config.limits.max_attempts == 2
    scen = Layer("scenario", {"limits": {"max_attempts": 3}})
    assert load(ROOT, env={}, extra_layers=[scen]).config.limits.max_attempts == 3
    env = {"FACTORY__LIMITS__MAX_ATTEMPTS": "4"}
    assert load(ROOT, env=env, extra_layers=[scen]).config.limits.max_attempts == 4
    cli = Overrides(sets=("limits.max_attempts=5",))
    assert load(ROOT, env=env, overrides=cli, extra_layers=[scen]).config.limits.max_attempts == 5


def test_feature_sources_and_agent_precedence():
    loaded = load(ROOT, env={"FACTORY_DISABLE": "review"}, overrides=Overrides(enable=("intake.routing",)))
    assert loaded.features["review"].source == "env" and not loaded.features.on("review")
    assert loaded.features["intake.routing"].source == "cli"
    assert loaded.agent_features["docs-bot"]["spec"].source == "agent:docs-bot"
    over = load(ROOT, env={}, overrides=Overrides(enable=("spec",)))
    assert over.agent_features["docs-bot"].on("spec")


def test_unknown_keys_and_features_rejected():
    with pytest.raises(ConfigError, match="limits.max_attempt: unknown key"):
        load(ROOT, env={}, overrides=Overrides(sets=("limits.max_attempt=3",)))
    with pytest.raises(ConfigError, match="unknown feature 'reviw'"):
        load(ROOT, env={}, overrides=Overrides(enable=("reviw",)))


def test_dependency_error_names_the_feature():
    with pytest.raises(ConfigError, match="feature 'spec' requires 'triage', which is disabled"):
        load(ROOT, env={}, overrides=Overrides(disable=("triage",)))


def test_literal_secret_in_config_rejected():
    layer = Layer("scenario", {"plugins": {"ticket_system": {"settings": {"token": "abc123"}}}})
    with pytest.raises(ConfigError, match="secrets must be a reference"):
        load(ROOT, env={}, extra_layers=[layer])


def test_deny_patterns_extend_but_never_shrink():
    layer = Layer("scenario", {"access_policy": {"deny_patterns": ["export-controlled"]}})
    loaded = load(ROOT, env={}, extra_layers=[layer])
    assert loaded.config.access_policy.deny_patterns == ["proprietary", "export-controlled"]
    empty = load(ROOT, env={}, extra_layers=[Layer("scenario", {"access_policy": {"deny_patterns": []}})])
    assert "proprietary" in empty.config.access_policy.deny_patterns


def test_disabled_ports_are_never_built(tmp_path):
    c = Container(load(ROOT, env={}), {}, tmp_path)
    with pytest.raises(FeatureDisabled):
        c.port("mailbox")
    assert "mailbox" not in c.instantiated


def test_secret_values_never_reach_logs(tmp_path, caplog):
    from factory.cli.main import RedactingFilter
    c = Container(load(ROOT, env={}), {"MY_TOKEN": "s3cr3t-value-xyz"}, tmp_path)
    c.secrets.resolve({"env": "MY_TOKEN"})
    log = logging.getLogger("factory.test")
    log.addFilter(RedactingFilter(c.redactor))
    with caplog.at_level(logging.INFO):
        log.info("connecting with %s", "s3cr3t-value-xyz")
    assert "s3cr3t-value-xyz" not in caplog.text and "[REDACTED]" in caplog.text


def test_filter_overlap_detection():
    a = FilterConfig(labels_any=["ai-ready-frontend"], projects=["A"])
    assert not filters_may_overlap(a, FilterConfig(projects=["B"]))
    assert not filters_may_overlap(a, FilterConfig(labels_none=["ai-ready-frontend"]))
    assert filters_may_overlap(a, FilterConfig(labels_any=["other"]))
