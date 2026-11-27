"""Layered config loading.

Precedence, lowest to highest: built-in defaults, config/base.toml, profile,
[scenario layers], [agent overrides], environment, CLI.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from factory.config.schema import AgentConfig, FactoryConfig
from factory.config.secrets import find_literal_secrets
from factory.config.validate import ConfigError, build
from factory.features import resolver
from factory.features.definitions import FEATURES_BY_NAME
from factory.features.resolver import FeatureStates

DEFAULT_PROFILE = "sim"
ENV_PREFIX = "FACTORY__"


@dataclass(frozen=True)
class Overrides:
    sets: tuple[str, ...] = ()
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    dry_run: bool = False
    output_mode: str | None = None


@dataclass(frozen=True)
class Layer:
    source: str
    data: dict


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "error" fails startup; "alert" is an error-level warning; "warning"
    message: str


@dataclass
class LoadedConfig:
    root: Path
    profile: str
    config: FactoryConfig
    features: FeatureStates
    agents: dict[str, AgentConfig]
    agent_configs: dict[str, FactoryConfig]
    agent_features: dict[str, FeatureStates]
    diagnostics: list[Diagnostic]
    merged: dict
    layers: list[Layer] = field(default_factory=list)

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.merged, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level != "error"]

    def enabled_agents(self) -> list[str]:
        return [name for name, agent in self.agents.items() if agent.enabled]


def parse_value(text: str) -> object:
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def set_path(data: dict, path: str, value: object) -> None:
    parts = resolver.path_parts(path)
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def deep_merge(base: Mapping, top: Mapping) -> dict:
    out = copy.deepcopy(dict(base))
    for key, value in top.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError([f"{path}: file not found"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path}: {exc}"]) from None


def _feature_path(name: str, problems: list[str], where: str) -> str | None:
    feature = FEATURES_BY_NAME.get(name)
    if feature is None:
        problems.append(f"{where}: unknown feature '{name}'")
        return None
    return feature.config_path


def env_layer(env: Mapping[str, str], problems: list[str]) -> Layer:
    data: dict = {}
    for key, value in env.items():
        if key.startswith(ENV_PREFIX):
            path = ".".join(p.lower() for p in key[len(ENV_PREFIX):].split("__"))
            set_path(data, path, parse_value(value))
    for var, flag in (("FACTORY_ENABLE", True), ("FACTORY_DISABLE", False)):
        for name in filter(None, (n.strip() for n in env.get(var, "").split(","))):
            path = _feature_path(name, problems, var)
            if path:
                set_path(data, path, flag)
    return Layer("env", data)


def cli_layer(overrides: Overrides, problems: list[str]) -> Layer:
    data: dict = {}
    for item in overrides.sets:
        key, sep, value = item.partition("=")
        if not sep or not key:
            problems.append(f"--set {item!r}: expected key=value")
            continue
        set_path(data, key.strip(), parse_value(value.strip()))
    for names, flag in ((overrides.enable, True), (overrides.disable, False)):
        for name in names:
            path = _feature_path(name, problems, "--enable" if flag else "--disable")
            if path:
                set_path(data, path, flag)
    if overrides.dry_run:
        set_path(data, "features.dry_run", True)
    if overrides.output_mode:
        set_path(data, "output.mode", overrides.output_mode)
    return Layer("cli", data)


def agent_layer(agent_raw: Mapping, name: str, problems: list[str]) -> Layer:
    overrides = agent_raw.get("overrides", {}) if isinstance(agent_raw, Mapping) else {}
    data: dict = {}
    for section, value in overrides.items():
        if section == "features":
            for fname, flag in value.items():
                path = _feature_path(fname, problems, f"agent {name}: overrides.features")
                if path is None:
                    continue
                if not FEATURES_BY_NAME[fname].per_agent:
                    problems.append(f"agent {name}: feature '{fname}' is global and cannot be overridden per agent")
                    continue
                set_path(data, path, flag)
        else:
            data[section] = copy.deepcopy(value)
    return Layer(f"agent:{name}", data)


def _merge_layers(layers: Sequence[Layer]) -> dict:
    merged: dict = {}
    for layer in layers:
        merged = deep_merge(merged, layer.data)
    return merged


def load(
    root: Path | str,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Overrides | None = None,
    extra_layers: Sequence[Layer] = (),
    agents_dir: Path | str | None = None,
) -> LoadedConfig:
    from factory.config.checks import check

    root = Path(root)
    overrides = overrides or Overrides()
    env = dict(os.environ if env is None else env)
    profile = profile or env.get("FACTORY_PROFILE") or DEFAULT_PROFILE
    problems: list[str] = []

    base = Layer("file:base", _read_toml(root / "config" / "base.toml"))
    prof = Layer(f"file:profile:{profile}", _read_toml(root / "config" / "profiles" / f"{profile}.toml"))
    env_l = env_layer(env, problems)
    cli_l = cli_layer(overrides, problems)
    lower = [base, prof, *extra_layers]
    upper = [env_l, cli_l]

    merged = _merge_layers([*lower, *upper])
    problems.extend(find_literal_secrets(merged))
    if problems:
        raise ConfigError(problems)
    config = build(FactoryConfig, merged)
    features = resolver.resolve(config, [(layer.source, layer.data) for layer in (*lower, *upper)])

    agents: dict[str, AgentConfig] = {}
    agent_configs: dict[str, FactoryConfig] = {}
    agent_features: dict[str, FeatureStates] = {}
    adir = Path(agents_dir) if agents_dir else root / "config" / "agents"
    for path in sorted(adir.glob("*.toml")) if adir.is_dir() else []:
        raw = _read_toml(path)
        try:
            agent = build(AgentConfig, raw)
        except ConfigError as exc:
            problems.extend(f"agent file {path.name}: {p}" for p in exc.problems)
            continue
        if agent.name != path.stem:
            problems.append(f"agent file {path.name}: name {agent.name!r} must match the file name")
            continue
        alayer = agent_layer(raw, agent.name, problems)
        layers = [*lower, alayer, *upper]
        amerged = _merge_layers(layers)
        try:
            agent_configs[agent.name] = build(FactoryConfig, amerged, root="")
        except ConfigError as exc:
            problems.extend(f"agent {agent.name}: {p}" for p in exc.problems)
            continue
        agents[agent.name] = agent
        agent_features[agent.name] = resolver.resolve(
            agent_configs[agent.name], [(layer.source, layer.data) for layer in layers]
        )
    if problems:
        raise ConfigError(problems)

    loaded = LoadedConfig(root, profile, config, features, agents, agent_configs, agent_features, [], merged,
                          [base, prof, *extra_layers, env_l, cli_l])
    loaded.diagnostics = check(loaded)
    errors = [d.message for d in loaded.diagnostics if d.level == "error"]
    if errors:
        raise ConfigError(errors)
    return loaded
