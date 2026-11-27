"""Builds adapters from config through the registry, lazily, and only for
ports that enabled features need. A disabled feature's adapter is never
imported, instantiated, or contacted."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from factory.config.checks import required_ports
from factory.config.loader import LoadedConfig
from factory.config.registry import resolve_class
from factory.config.schema import PluginSelection
from factory.config.secrets import SecretResolver
from factory.core.trace_writers import NullTraceWriter, write_only
from factory.metrics import Metrics
from factory.ports import PORTS, AgentHarness, ModelProvider, TraceStore
from factory.ports.context import AdapterContext
from factory.redaction import Redactor


class FeatureDisabled(RuntimeError):
    pass


class Container:
    def __init__(self, loaded: LoadedConfig, env: Mapping[str, str] | None = None, base_dir: Path | None = None) -> None:
        cfg = loaded.config
        self.loaded = loaded
        self.base_dir = Path(base_dir or loaded.root)
        self.metrics = Metrics()
        self.redactor = Redactor(cfg.trace_store.redaction.extra_patterns)
        self.secrets = SecretResolver(dict(os.environ if env is None else env), self.redactor)
        self.ctx = AdapterContext(cfg, self.secrets, self.redactor, self.base_dir, self.metrics)
        self.instantiated: list[str] = []
        self._ports: dict[str, object] = {}
        self._providers: dict[str, object] = {}
        self._harnesses: dict[str, object] = {}
        self._trace: object | None = None
        allowed = set(required_ports(loaded.features))
        for name in loaded.enabled_agents():
            allowed |= set(required_ports(loaded.agent_features[name]))
        self.allowed_ports = allowed
        self.ctx.port = self.port
        self.ctx.clock = self.port("clock")
        self.ctx.ids = self.port("id_source")

    def _build(self, label: str, selection: PluginSelection, protocol: type) -> object:
        cls = resolve_class(selection.adapter, self.loaded.config.plugins.aliases)
        factory = getattr(cls, "from_settings", None)
        obj = factory(dict(selection.settings), self.ctx) if factory else cls()
        if not isinstance(obj, protocol):
            raise TypeError(f"{label}: {selection.adapter} does not implement {protocol.__name__}")
        self.instantiated.append(label)
        return obj

    def port(self, name: str):
        if name not in self._ports:
            if name not in self.allowed_ports:
                raise FeatureDisabled(f"port {name} is not needed by any enabled feature and will not be created")
            selection = getattr(self.loaded.config.plugins, name)
            self._ports[name] = self._build(name, selection, PORTS[name])
        return self._ports[name]

    def provider(self, name: str):
        if name not in self._providers:
            selection = self.loaded.config.models.providers[name]
            self._providers[name] = self._build(f"model_provider:{name}", selection, ModelProvider)
        return self._providers[name]

    def harness(self, name: str):
        if name not in self._harnesses:
            h = self.loaded.config.harnesses[name]
            self._harnesses[name] = self._build(f"harness:{name}", PluginSelection(h.adapter, h.settings), AgentHarness)
        return self._harnesses[name]

    def trace_store(self):
        if self._trace is None:
            backend = self.loaded.config.trace_store.backend
            self._trace = self._build(f"trace_store:{backend}", PluginSelection(f"trace_store.{backend}"), TraceStore)
        return self._trace

    def trace_writer(self, enabled: bool):
        if not enabled:
            return NullTraceWriter()
        return write_only(self.trace_store())
