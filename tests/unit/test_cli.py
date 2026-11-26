from factory.cli.main import main


def test_config_validate_and_show_redact(capsys):
    assert main(["config", "validate"]) == 0
    assert "config OK" in capsys.readouterr().out
    assert main(["config", "show", "--profile", "sim"]) == 0
    assert '"deny_patterns"' in capsys.readouterr().out


def test_prod_profile_lists_what_to_fill_in(capsys):
    assert main(["config", "validate", "--profile", "prod"]) == 2
    err = capsys.readouterr().err
    assert "workflow.statuses.ready is empty in profile prod" in err
    assert "trace_store.s3.endpoint_url" in err


def test_features_and_agents(capsys):
    assert main(["features", "--agent", "docs-bot"]) == 0
    out = capsys.readouterr().out
    assert "spec" in out and "agent:docs-bot" in out
    assert main(["agents"]) == 0
    assert "frontend-bot" in capsys.readouterr().out


def test_access_policy_cannot_be_disabled_from_cli(capsys):
    assert main(["run", "--agent", "frontend-bot", "--set", "access_policy.enabled=false"]) == 2
    assert "can only be set in the global config file" in capsys.readouterr().err
