"""Bring-your-own harness presets for known CLI agents: Claude Code, Codex,
and Cline, launched directly or through the `jam` launcher (`jam claude ...`).

Every preset is pointed at the factory's loopback gateway with a per-run token,
so the agent never holds upstream model credentials. The agent works in the
isolated workspace; the factory diffs and gates the result as for any harness.

Settings ([harnesses.<name>.settings]):
  agent        claude | codex | cline (required)
  launcher     direct (default) | jam
  executable   override the agent binary for direct launch (default: the agent name)
  jam_executable / jam_name   the jam binary (default "jam") and the name jam is given (default: agent)
  args         replace the preset's arguments entirely (same placeholders as harness.command)
  extra_args   appended to the preset's arguments
  env          extra environment variables (placeholders allowed)

The agent ends its final message with one JSON line
{"status": "done|needs_info|blocked|failed", "summary": "...", "questions": [...]};
otherwise exit code 0 means done."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from factory.ports.context import AdapterContext
from factory.ports.harness import HarnessRuntime
from factory.ports.types import Command, HarnessResult, HarnessStatus, HarnessTask

PLACEHOLDERS = ("workspace", "instructions_file", "result_file", "gateway_url", "gateway_token", "model",
                "max_turns", "prompt_file", "output_file", "prompt")
MAX_ARG_PROMPT = 100_000

FOOTER = (
    "\n\n## Reporting\nWork only inside the current directory. Do not commit, push, or contact any service. "
    "When you are done, end your final message with exactly one line of JSON: "
    '{"status": "done" | "needs_info" | "blocked" | "failed", "summary": "<one paragraph>", '
    '"questions": ["<numbered questions when status is needs_info>"]}\n'
)


@dataclass(frozen=True)
class Preset:
    executable: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    dialect: str
    prompt_via: str  # stdin | arg
    output: str  # claude_json | output_file | stdout
    notes: str = ""


PRESETS: dict[str, Preset] = {
    "claude": Preset(
        executable="claude",
        args=("-p", "--output-format", "json", "--max-turns", "{max_turns}", "--model", "{model}",
              "--dangerously-skip-permissions"),
        env={
            "ANTHROPIC_BASE_URL": "{gateway_url}",
            "ANTHROPIC_API_KEY": "{gateway_token}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
        },
        dialect="anthropic_messages",
        prompt_via="stdin",
        output="claude_json",
        notes="Permission prompts are skipped because the agent runs in the factory sandbox on an isolated copy.",
    ),
    "codex": Preset(
        executable="codex",
        args=("exec", "--full-auto", "--skip-git-repo-check", "--model", "{model}",
              "-c", 'model_provider="factory"',
              "-c", 'model_providers.factory.name="factory"',
              "-c", 'model_providers.factory.base_url="{gateway_url}/v1"',
              "-c", 'model_providers.factory.env_key="FACTORY_GATEWAY_TOKEN"',
              "-c", 'model_providers.factory.wire_api="chat"',
              "--output-last-message", "{output_file}", "-"),
        env={"FACTORY_GATEWAY_TOKEN": "{gateway_token}", "OPENAI_BASE_URL": "{gateway_url}/v1",
             "OPENAI_API_KEY": "{gateway_token}"},
        dialect="openai_chat",
        prompt_via="stdin",
        output="output_file",
    ),
    "cline": Preset(
        executable="cline",
        args=("--yolo", "--model", "{model}", "{prompt}"),
        env={"OPENAI_BASE_URL": "{gateway_url}/v1", "OPENAI_API_KEY": "{gateway_token}"},
        dialect="openai_chat",
        prompt_via="arg",
        output="stdout",
        notes="Confirm the Cline CLI flags on the box; override with settings.args if they differ.",
    ),
}


class CliAgentHarness:
    def __init__(self, agent: str, launcher: str = "direct", executable: str = "", jam_executable: str = "jam",
                 jam_name: str = "", args: list[str] | None = None, extra_args: list[str] | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        problems = self.validate_settings({"agent": agent, "launcher": launcher})
        if problems:
            raise ValueError("; ".join(problems))
        self.agent = agent
        self.preset = PRESETS[agent]
        self.launcher = launcher
        self.executable = executable or self.preset.executable
        self.jam_executable = jam_executable
        self.jam_name = jam_name or agent
        self.args = tuple(args) if args is not None else self.preset.args
        self.extra_args = tuple(extra_args or ())
        self.env = {**self.preset.env, **dict(env or {})}

    @staticmethod
    def validate_settings(settings: Mapping[str, object]) -> list[str]:
        problems = []
        agent = settings.get("agent")
        if agent not in PRESETS:
            problems.append(f"settings.agent must be one of {', '.join(sorted(PRESETS))} (got {agent!r})")
        if settings.get("launcher", "direct") not in ("direct", "jam"):
            problems.append(f"settings.launcher must be direct or jam (got {settings.get('launcher')!r})")
        for key in ("args", "extra_args"):
            value = settings.get(key)
            if value is not None and not (isinstance(value, list) and all(isinstance(a, str) for a in value)):
                problems.append(f"settings.{key} must be a list of strings")
        return problems

    @staticmethod
    def required_dialect(settings: Mapping[str, object]) -> str | None:
        preset = PRESETS.get(str(settings.get("agent")))
        return preset.dialect if preset else None

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> CliAgentHarness:
        return cls(
            agent=str(settings.get("agent", "")), launcher=str(settings.get("launcher", "direct")),
            executable=str(settings.get("executable", "")), jam_executable=str(settings.get("jam_executable", "jam")),
            jam_name=str(settings.get("jam_name", "")),
            args=list(settings["args"]) if "args" in settings else None,
            extra_args=list(settings.get("extra_args", [])),
            env={str(k): str(v) for k, v in dict(settings.get("env", {})).items()},
        )

    @property
    def native_skill_dirs(self) -> tuple[str, ...]:
        """Where this CLI loads skills from, relative to its home (the sandbox root)."""
        return {"claude": (".claude/skills",), "codex": (".codex/skills",)}.get(self.agent, ())

    @property
    def dialect(self) -> str:
        return self.preset.dialect

    def argv(self, values: Mapping[str, str]) -> tuple[str, ...]:
        head = [self.jam_executable, self.jam_name] if self.launcher == "jam" else [self.executable]
        return tuple(_fill(a, values) for a in (*head, *self.args, *self.extra_args))

    def run(self, task: HarnessTask, runtime: HarnessRuntime) -> HarnessResult:
        if self.dialect not in task.endpoint.dialects:
            return HarnessResult(HarnessStatus.FAILED,
                                 f"{self.agent} needs the {self.dialect} gateway dialect; add it to gateway.dialects")
        factory_dir = Path(task.result_file).parent
        prompt = Path(task.instructions_file).read_text() + FOOTER
        prompt_file = factory_dir / "prompt.md"
        prompt_file.write_text(prompt)
        output_file = factory_dir / f"{self.agent}-last-message.txt"
        output_file.unlink(missing_ok=True)
        arg_prompt = prompt if len(prompt) <= MAX_ARG_PROMPT else (
            f"Read the task in {prompt_file} and follow it exactly.{FOOTER}")
        values = {
            "workspace": str(task.workspace.root), "instructions_file": str(task.instructions_file),
            "result_file": str(task.result_file), "gateway_url": task.endpoint.base_url,
            "gateway_token": task.endpoint.token, "model": task.endpoint.model, "max_turns": str(task.max_turns),
            "prompt_file": str(prompt_file), "output_file": str(output_file), "prompt": arg_prompt,
        }
        command = Command(self.argv(values), ".", {k: _fill(v, values) for k, v in self.env.items()},
                          task.timeout_seconds, str(prompt_file) if self.preset.prompt_via == "stdin" else "")
        result = runtime.run(command)
        if result.timed_out:
            return HarnessResult(HarnessStatus.TIMED_OUT, f"{self.agent} timed out after {task.timeout_seconds:.0f}s")
        if result.exit_code == 127:
            launcher = self.jam_executable if self.launcher == "jam" else self.executable
            return HarnessResult(HarnessStatus.FAILED, f"{launcher} is not installed in the sandbox")
        final = self._final_message(result.stdout, output_file)
        parsed = _status_line(final)
        guard = runtime.guard
        if parsed is not None:
            status, summary, questions = parsed
            return HarnessResult(status, guard.filter_output(summary).text,
                                 tuple(guard.filter_output(q).text for q in questions))
        status = HarnessStatus.DONE if result.exit_code == 0 else HarnessStatus.FAILED
        return HarnessResult(status, guard.filter_output(final[-2000:] or result.stderr[-2000:]).text)

    def _final_message(self, stdout: str, output_file: Path) -> str:
        if self.preset.output == "claude_json":
            for line in reversed(stdout.strip().splitlines()):
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(doc, dict) and "result" in doc:
                    return str(doc["result"])
            return stdout
        if self.preset.output == "output_file" and output_file.is_file():
            return output_file.read_text()
        return stdout


def _fill(text: str, values: Mapping[str, str]) -> str:
    for name in PLACEHOLDERS:
        text = text.replace("{" + name + "}", values.get(name, "{" + name + "}"))
    return text


def _status_line(message: str) -> tuple[HarnessStatus, str, tuple[str, ...]] | None:
    for line in reversed(message.strip().splitlines()):
        line = line.strip().strip("`")
        if not (line.startswith("{") and '"status"' in line):
            continue
        try:
            doc = json.loads(line)
            return (HarnessStatus(doc.get("status", "done")), str(doc.get("summary", "")),
                    tuple(str(q) for q in doc.get("questions", ())))
        except (json.JSONDecodeError, ValueError, AttributeError):
            return None
    return None
