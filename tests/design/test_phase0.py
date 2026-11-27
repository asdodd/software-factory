import ast
import dataclasses
import tomllib
import typing
import unittest
from pathlib import Path

from factory.config import schema
from factory.config.validate import ConfigError, build
from factory.features.definitions import FEATURES, FEATURES_BY_NAME
from factory.ports import PORTS

ROOT = Path(__file__).resolve().parents[2]
SECRET_KEYS = {"token", "password", "api_key", "secret_access_key", "access_key_id", "session_token"}


def load(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


class ConfigExamplesTest(unittest.TestCase):
    def test_base_and_profiles_validate(self):
        for path in [ROOT / "config/base.toml", *sorted((ROOT / "config/profiles").glob("*.toml"))]:
            with self.subTest(path=path.name):
                build(schema.FactoryConfig, load(path))

    def test_agents_validate(self):
        paths = sorted((ROOT / "config/agents").glob("*.toml"))
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            with self.subTest(path=path.name):
                agent = build(schema.AgentConfig, load(path))
                self.assertEqual(agent.name, path.stem)

    def test_unknown_key_named_with_full_path(self):
        with self.assertRaises(ConfigError) as ctx:
            build(schema.FactoryConfig, {"limits": {"max_attempt": 3}})
        self.assertEqual(ctx.exception.problems, ["limits.max_attempt: unknown key"])

    def test_wrong_type_and_literal_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            build(schema.FactoryConfig, {"limits": {"lease_minutes": "2h"}, "output": {"mode": "auto_merge"}})
        joined = "\n".join(ctx.exception.problems)
        self.assertIn("limits.lease_minutes: expected an integer", joined)
        self.assertIn("output.mode: 'auto_merge' is not one of", joined)

    def test_secrets_are_references_never_literals(self):
        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    here = f"{path}.{key}" if path else key
                    if key in SECRET_KEYS:
                        self.assertIsInstance(value, dict, f"{here} must be {{env=...}} or {{file=...}}")
                        self.assertLessEqual(set(value), {"env", "file"}, here)
                    walk(value, here)

        for path in (ROOT / "config").rglob("*.toml"):
            walk(load(path), "")

    def test_defaults_match_spec(self):
        cfg = schema.FactoryConfig()
        self.assertEqual(cfg.output.mode, "merge_request")
        self.assertEqual(cfg.limits.lease_minutes, 120)
        self.assertEqual(cfg.limits.max_tokens_per_ticket, 2_000_000)
        self.assertFalse(cfg.intake.email.allow_updates)
        self.assertFalse(cfg.mr.resolve_threads)
        self.assertEqual(cfg.models.stages.implement.harness, "claude")
        self.assertEqual(cfg.trace_store.s3.endpoint_url, "")
        self.assertEqual(cfg.collaboration.allowed_commenters, schema.CommenterRule())

    def test_non_sim_profiles_ship_empty_commenters(self):
        for name in ("staging", "prod"):
            data = load(ROOT / f"config/profiles/{name}.toml")
            self.assertNotIn("allowed_commenters", data.get("collaboration", {}), name)

    def test_no_factory_side_access_policy(self):
        """Restricted files are the agents' job (the agent kit), so there is no
        factory config for them anywhere."""
        self.assertNotIn("access_policy", {f.name for f in dataclasses.fields(schema.FactoryConfig)})
        with self.assertRaises(ConfigError):
            build(schema.FactoryConfig, {"access_policy": {"enabled": False}})


class FeatureTableTest(unittest.TestCase):
    def test_dependencies_known_and_acyclic(self):
        def visit(name, seen):
            self.assertNotIn(name, seen, f"cycle through {name}")
            for dep in FEATURES_BY_NAME[name].requires:
                self.assertIn(dep, FEATURES_BY_NAME)
                visit(dep, seen | {name})

        for feature in FEATURES:
            visit(feature.name, frozenset())

    def test_spec_declared_dependencies(self):
        self.assertIn("triage", FEATURES_BY_NAME["spec"].requires)
        self.assertIn("jira_comments.post", FEATURES_BY_NAME["jira_comments.read_replies"].requires)
        for name in ("mr.read_comments", "mr.respond", "mr.auto_fix"):
            self.assertEqual(FEATURES_BY_NAME[name].requires_output_mode, "merge_request")
        for name in ("mr.respond", "mr.auto_fix"):
            self.assertIn("mr.read_comments", FEATURES_BY_NAME[name].requires)

    def test_restricted_files_is_not_a_feature(self):
        self.assertFalse(any(f.name.startswith(("access_policy", "restricted")) for f in FEATURES))

    def test_config_paths_exist_in_schema(self):
        for feature in FEATURES:
            if feature.config_path.startswith("features."):
                continue
            cls = schema.FactoryConfig
            *parents, leaf = feature.config_path.split(".")
            for part in parents:
                cls = typing.get_type_hints(cls)[part]
            self.assertIs(typing.get_type_hints(cls)[leaf], bool, feature.config_path)

    def test_base_config_sets_every_features_table_entry(self):
        table = load(ROOT / "config/base.toml")["features"]
        expected = {f.name for f in FEATURES if f.config_path.startswith("features.")}
        self.assertEqual(set(table), expected)


class ArchitectureTest(unittest.TestCase):
    def _imports(self, package: str):
        for path in (ROOT / "factory" / package).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    yield path, [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    yield path, [node.module]

    def test_core_and_ports_never_import_adapters(self):
        for package in ("core", "ports", "config", "features"):
            for path, modules in self._imports(package):
                for module in modules:
                    self.assertFalse(module.startswith("factory.adapters"), f"{path} imports {module}")

    def test_ports_are_protocols(self):
        for name, port in PORTS.items():
            self.assertTrue(getattr(port, "_is_protocol", False), name)

    def test_schema_plugin_slots_match_ports(self):
        slots = {f.name for f in dataclasses.fields(schema.PluginsConfig)} - {"aliases"}
        self.assertEqual(slots, set(PORTS) - {"trace_store", "model_provider", "agent_harness"})


def merged(*layers: dict) -> dict:
    out: dict = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merged(out[key], value)
            else:
                out[key] = value
    return out


class BringYourOwnTest(unittest.TestCase):
    STAGES = [f.name for f in dataclasses.fields(schema.StageModels)]

    def _effective(self, profile: str, agent: str | None = None) -> schema.FactoryConfig:
        layers = [load(ROOT / "config/base.toml"), load(ROOT / f"config/profiles/{profile}.toml")]
        if agent:
            overrides = load(ROOT / f"config/agents/{agent}.toml").get("overrides", {})
            layers.append({k: v for k, v in overrides.items() if k != "labels"})
        return build(schema.FactoryConfig, merged(*layers))

    def test_every_stage_names_a_declared_provider_and_harness(self):
        cases = [(p, None) for p in ("sim", "staging", "prod")]
        cases += [("sim", a.stem) for a in (ROOT / "config/agents").glob("*.toml")]
        for profile, agent in cases:
            cfg = self._effective(profile, agent)
            for stage in self.STAGES:
                with self.subTest(profile=profile, agent=agent, stage=stage):
                    sm = getattr(cfg.models.stages, stage)
                    self.assertIn(sm.provider, cfg.models.providers)
                    if sm.harness:
                        self.assertIn(sm.harness, cfg.harnesses)

    def test_several_providers_can_coexist(self):
        cfg = build(schema.FactoryConfig, {
            "models": {
                "providers": {
                    "gov": {"adapter": "model_provider.openai_compatible"},
                    "gateway": {"adapter": "my_pkg.models:GatewayProvider"},
                },
                "stages": {
                    "implement": {"provider": "gov", "model": "big", "harness": "mine"},
                    "review": {"provider": "gateway", "model": "other"},
                },
            },
            "harnesses": {"mine": {"adapter": "harness.command", "settings": {"argv": ["my-agent", "{instructions_file}"]}}},
        })
        self.assertNotEqual(cfg.models.stages.implement.provider, cfg.models.stages.review.provider)

    def test_harness_task_carries_no_upstream_credentials(self):
        from factory.ports.types import HarnessTask, ModelEndpoint
        self.assertEqual({f.name for f in dataclasses.fields(ModelEndpoint)}, {"base_url", "token", "model", "dialects"})
        names = {f.name for f in dataclasses.fields(HarnessTask)}
        self.assertFalse(names & {"api_key", "credentials", "repo_token", "ticket_system", "git_host"})


if __name__ == "__main__":
    unittest.main()
