"""Secrets are referenced from config by env var name or file path and resolved
only when an adapter asks for them. Every resolved value is registered with
the redactor so it can never reach a log, comment, or trace."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from factory.config.schema import SecretRef
from factory.redaction import Redactor

SECRET_KEYS = frozenset({
    "token", "password", "api_key", "secret", "secret_access_key", "access_key_id", "session_token",
})


class SecretError(ValueError):
    pass


def as_secret_ref(value: object) -> SecretRef | None:
    if isinstance(value, SecretRef):
        return value
    if isinstance(value, Mapping) and value and set(value) <= {"env", "file"}:
        return SecretRef(env=str(value.get("env", "")), file=str(value.get("file", "")))
    return None


class SecretResolver:
    def __init__(self, env: Mapping[str, str], redactor: Redactor) -> None:
        self._env = env
        self._redactor = redactor

    def resolve(self, ref: SecretRef | Mapping[str, str], name: str = "secret") -> str:
        secret = as_secret_ref(ref)
        if secret is None:
            raise SecretError(f"{name}: not a secret reference")
        if secret.env:
            value = self._env.get(secret.env)
            if value is None:
                raise SecretError(f"{name}: environment variable {secret.env} is not set")
        elif secret.file:
            try:
                value = Path(secret.file).read_text().strip()
            except OSError as exc:
                raise SecretError(f"{name}: cannot read secret file {secret.file}: {exc.strerror}") from None
        else:
            raise SecretError(f"{name}: empty secret reference")
        self._redactor.add_secret(value)
        return value


def find_literal_secrets(data: object, path: str = "") -> list[str]:
    """Keys that look like secrets but hold something other than a reference."""
    problems: list[str] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if key in SECRET_KEYS and as_secret_ref(value) is None:
                problems.append(f"{here}: secrets must be a reference like {{env = \"NAME\"}} or {{file = \"/path\"}}")
            else:
                problems.extend(find_literal_secrets(value, here))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            problems.extend(find_literal_secrets(value, f"{path}[{i}]"))
    return problems
