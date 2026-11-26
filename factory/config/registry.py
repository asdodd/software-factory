"""Plugin registry: dotted class paths ("package.module:Class"), with short
aliases from config. Works from a source checkout, offline, with no install
step. Resolving imports a module but never instantiates or connects."""

from __future__ import annotations

import importlib
from collections.abc import Mapping


class PluginError(LookupError):
    pass


def class_path(spec: str, aliases: Mapping[str, str]) -> str:
    target = aliases.get(spec, spec)
    if ":" not in target:
        known = ", ".join(sorted(aliases)) or "none"
        raise PluginError(f"adapter {spec!r} is neither a known alias ({known}) nor a 'module:Class' path")
    return target


def resolve_class(spec: str, aliases: Mapping[str, str]) -> type:
    target = class_path(spec, aliases)
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginError(f"adapter {spec!r}: cannot import {module_name}: {exc}") from None
    try:
        cls = getattr(module, attr)
    except AttributeError:
        raise PluginError(f"adapter {spec!r}: {module_name} has no attribute {attr}") from None
    if not isinstance(cls, type):
        raise PluginError(f"adapter {spec!r}: {target} is not a class")
    return cls
