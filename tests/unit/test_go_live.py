"""Going live is a config-only change: fill in the prod profile and every port
is built from the real adapters, with no code edits and no connection made
(the network guard is active for the whole test session)."""

import re
import shutil
from pathlib import Path

from factory.adapters.gitlab.git_host import GitlabGitHost
from factory.adapters.jira.ticket_system import JiraTicketSystem
from factory.adapters.models.openai_compatible import OpenAICompatibleProvider
from factory.adapters.models.sqlite_accounting import SqliteAccounting
from factory.adapters.trace.s3 import S3TraceStore
from factory.config.checks import OPTIONAL_EMPTY
from factory.config.loader import load
from factory.core.container import Container
from factory.core.notify import TicketCommentNotifier
from factory.core.sqlite_state import SqliteStateStore

ROOT = Path(__file__).resolve().parents[2]
SECRETS = {"FACTORY_JIRA_TOKEN": "jira-token-placeholder", "FACTORY_GITLAB_TOKEN": "gitlab-token-placeholder",
           "FACTORY_MAIL_PASSWORD": "mail-placeholder", "FACTORY_MODEL_API_KEY": "model-key-placeholder",
           "FACTORY_TRACE_WRITE_KEY_ID": "write-id", "FACTORY_TRACE_WRITE_SECRET": "write-secret-placeholder",
           "FACTORY_TRACE_READ_KEY_ID": "read-id", "FACTORY_TRACE_READ_SECRET": "read-secret-placeholder"}


def filled_config(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    prod = tmp_path / "config" / "profiles" / "prod.toml"
    lines = []
    for line in prod.read_text().splitlines():
        m = re.match(r'^(\s*)([\w.]+)\s*=\s*""\s*$', line)
        if m and m.group(2) not in OPTIONAL_EMPTY:
            value = "https://placeholder.invalid" if m.group(2).endswith(("url", "_host")) else f"placeholder-{m.group(2)}"
            line = f'{m.group(1)}{m.group(2)} = "{value}"'
        lines.append(line)
    text = "\n".join(lines).replace('kms_key_id = ""', 'kms_key_id = "placeholder-kms"')
    prod.write_text(text + "\n")
    return tmp_path


def test_filled_prod_profile_builds_every_port_from_real_adapters(tmp_path):
    root = filled_config(tmp_path)
    loaded = load(root, "prod", env=SECRETS)
    container = Container(loaded, SECRETS, base_dir=tmp_path)
    built = {name: container.port(name) for name in sorted(container.allowed_ports)}
    assert isinstance(built["ticket_system"], JiraTicketSystem)
    assert isinstance(built["git_host"], GitlabGitHost)
    assert isinstance(built["accounting"], SqliteAccounting)
    assert isinstance(built["state_store"], SqliteStateStore)
    assert isinstance(built["notifier"], TicketCommentNotifier)
    assert isinstance(container.provider("primary"), OpenAICompatibleProvider)
    assert isinstance(container.trace_store(), S3TraceStore)
    assert "mailbox" not in built, "email intake is off, so the mailbox is never built"


def test_unfilled_prod_profile_lists_what_to_fill(tmp_path):
    import pytest

    from factory.config.validate import ConfigError
    with pytest.raises(ConfigError) as err:
        load(ROOT, "prod", env=SECRETS)
    assert any("workflow.statuses.ready is empty in profile prod" in p for p in err.value.problems)
