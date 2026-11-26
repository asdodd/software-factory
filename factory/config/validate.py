"""Strict mapping -> dataclass conversion. Unknown keys and wrong types are
errors that name the full dotted key. No I/O, no third-party dependencies."""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from typing import Any, Literal, TypeVar, Union

T = TypeVar("T")


class ConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("invalid config:\n  " + "\n  ".join(problems))
        self.problems = problems


def build(cls: type[T], data: Mapping[str, Any], root: str = "") -> T:
    problems: list[str] = []
    result = _convert(cls, data, root, problems)
    if problems:
        raise ConfigError(problems)
    return result


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _convert(tp: Any, value: Any, path: str, problems: list[str]) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if tp is Any or tp is object:
        return value
    if dataclasses.is_dataclass(tp):
        return _convert_dataclass(tp, value, path, problems)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _convert(non_none[0], value, path, problems)
        problems.append(f"{path}: unsupported union type")
        return None
    if origin is Literal:
        if value not in args:
            allowed = ", ".join(repr(a) for a in args)
            problems.append(f"{path}: {value!r} is not one of {allowed}")
        return value
    if origin is list:
        if not isinstance(value, list):
            problems.append(f"{path}: expected a list, got {type(value).__name__}")
            return []
        return [_convert(args[0], v, f"{path}[{i}]", problems) for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, Mapping):
            problems.append(f"{path}: expected a table, got {type(value).__name__}")
            return {}
        return {k: _convert(args[1], v, _join(path, str(k)), problems) for k, v in value.items()}
    if tp is bool:
        if not isinstance(value, bool):
            problems.append(f"{path}: expected true or false, got {value!r}")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{path}: expected an integer, got {value!r}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{path}: expected a number, got {value!r}")
            return value
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            problems.append(f"{path}: expected a string, got {value!r}")
        return value
    problems.append(f"{path}: unsupported schema type {tp!r}")
    return value


def _convert_dataclass(cls: Any, value: Any, path: str, problems: list[str]) -> Any:
    if not isinstance(value, Mapping):
        problems.append(f"{path or '<root>'}: expected a table, got {type(value).__name__}")
        return cls()
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    for key in value:
        if key not in known:
            problems.append(f"{_join(path, str(key))}: unknown key")
    kwargs = {k: _convert(hints[k], v, _join(path, k), problems) for k, v in value.items() if k in known}
    return cls(**kwargs)
