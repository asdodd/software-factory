from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from factory.config.schema import FactoryConfig
from factory.features.definitions import FEATURES, FEATURES_BY_NAME, Feature


@dataclass(frozen=True)
class FeatureState:
    name: str
    enabled: bool
    source: str
    requires: tuple[str, ...]
    requires_output_mode: str | None


class FeatureStates(Mapping[str, FeatureState]):
    def __init__(self, states: dict[str, FeatureState], output_mode: str) -> None:
        self._states = states
        self.output_mode = output_mode

    def __getitem__(self, name: str) -> FeatureState:
        return self._states[name]

    def __iter__(self):
        return iter(self._states)

    def __len__(self) -> int:
        return len(self._states)

    def on(self, name: str) -> bool:
        return self._states[name].enabled

    def problems(self) -> list[str]:
        out = []
        for state in self._states.values():
            if not state.enabled:
                continue
            for dep in state.requires:
                if not self._states[dep].enabled:
                    out.append(f"feature '{state.name}' requires '{dep}', which is disabled")
            if state.requires_output_mode and self.output_mode != state.requires_output_mode:
                out.append(
                    f"feature '{state.name}' requires output.mode = {state.requires_output_mode} "
                    f"(currently {self.output_mode}); disable it or change the mode"
                )
        return out


def path_parts(path: str) -> list[str]:
    if path.startswith("features."):
        return ["features", path[len("features."):]]
    return path.split(".")


def lookup(data: Mapping[str, object], path: str) -> tuple[bool, object]:
    node: object = data
    for part in path_parts(path):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def feature_value(config: FactoryConfig, feature: Feature) -> bool:
    if feature.config_path.startswith("features."):
        return bool(config.features.get(feature.name, feature.default))
    node: object = config
    for part in feature.config_path.split("."):
        node = getattr(node, part)
    return bool(node)


def resolve(config: FactoryConfig, layers: Sequence[tuple[str, Mapping[str, object]]]) -> FeatureStates:
    """layers: (source label, raw data), lowest precedence first."""
    states = {}
    for feature in FEATURES:
        source = "default"
        for label, data in layers:
            found, _ = lookup(data, feature.config_path)
            if found:
                source = label
        states[feature.name] = FeatureState(
            feature.name, feature_value(config, feature), source, feature.requires, feature.requires_output_mode
        )
    return FeatureStates(states, config.output.mode)


__all__ = ["FEATURES_BY_NAME", "FeatureState", "FeatureStates", "feature_value", "lookup", "path_parts", "resolve"]
