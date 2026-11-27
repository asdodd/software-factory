"""Fake claude / codex / cline / jam executables for scenario tests.

Each fake checks the invocation its preset should produce, asks the factory
gateway for an edit (in the dialect the real tool speaks), applies it, checks
its workspace carries the agent kit and that the kit's hook blocks restricted
files, and reports like the real tool."""

import json
import os
import subprocess
import sys
import urllib.request

tool, args = sys.argv[1], sys.argv[2:]
if tool == "jam":
    os.environ["JAM_USED"] = "1"
    target = args[0]
    os.execvp(target, [target, *args[1:]])

problems = []


def need(cond, what):
    if not cond:
        problems.append(what)


MARKER = "propri" + "etary"


def hook_blocks(path):
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": path}, "cwd": os.getcwd()})
    proc = subprocess.run([sys.executable, ".claude/hooks/restricted_files.py"], input=payload, capture_output=True,
                          text=True)
    return proc.returncode == 2


for kit in (".claude/settings.json", ".claude/hooks/restricted_files.py", ".claude/skills/restricted-files/SKILL.md",
            ".clinerules/hooks/PreToolUse"):
    need(os.path.isfile(kit), f"kit file {kit} missing")
for note in ("AGENTS.md", "CLAUDE.md"):
    with open(note) as fh:
        need("agent-kit:restricted-files" in fh.read(), f"{note} lacks the kit rule")
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", ".claude", ".clinerules")]
    for name in files:
        path = os.path.join(root, name)
        if MARKER in path.lower():
            need(hook_blocks(path), f"hook allowed {path}")
need(not hook_blocks("src/calc.py"), "hook blocked an ordinary file")
need(subprocess.run(["git", "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip() == "1",
     "history beyond the baseline commit")
for var in os.environ:
    need(not var.startswith(("AWS_", "SIM_")) and not var.lower().endswith("_proxy"), f"env leaks {var}")

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, body, headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    return json.loads(opener.open(req).read())


if tool == "claude":
    need("-p" in args and "--dangerously-skip-permissions" in args, f"claude flags {args}")
    need(args[args.index("--output-format") + 1] == "json", "claude output format")
    prompt = sys.stdin.read()
    base, key = os.environ["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_API_KEY"]
    need(os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1", "claude telemetry not disabled")
    doc = post(base + "/v1/messages", {"model": "claude-x", "max_tokens": 2048, "system": prompt,
                                        "messages": [{"role": "user", "content": "go"}]}, {"x-api-key": key})
    text = "".join(b["text"] for b in doc["content"] if b["type"] == "text")
elif tool == "codex":
    need(args[0] == "exec" and args[-1] == "-" and "--full-auto" in args, f"codex flags {args}")
    conf = dict(a.split("=", 1) for a in (args[i + 1] for i, x in enumerate(args) if x == "-c"))
    base = json.loads(conf["model_providers.factory.base_url"])
    token = os.environ[json.loads(conf["model_providers.factory.env_key"])]
    need(json.loads(conf["model_providers.factory.wire_api"]) == "chat", "codex wire api")
    out_file = args[args.index("--output-last-message") + 1]
    prompt = sys.stdin.read()
    doc = post(base + "/chat/completions", {"model": "gpt-x", "messages": [{"role": "system", "content": prompt},
                                                                       {"role": "user", "content": "go"}]},
               {"Authorization": f"Bearer {token}"})
    text = doc["choices"][0]["message"]["content"]
elif tool == "cline":
    need("--yolo" in args, f"cline flags {args}")
    prompt = args[-1]
    doc = post(os.environ["OPENAI_BASE_URL"] + "/chat/completions",
               {"model": "x", "messages": [{"role": "user", "content": prompt}]},
               {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    text = doc["choices"][0]["message"]["content"]
else:
    sys.exit(f"unknown fake tool {tool}")

need("## Reporting" in prompt, "prompt lacks the reporting footer")
edit = json.loads(text)
if "path" in edit:
    with open(edit["path"], "w") as fh:
        fh.write(edit["content"])
via = f"{tool} (jam)" if os.environ.get("JAM_USED") else tool
summary = ("ISOLATION-OK" if not problems else "ISOLATION-FAIL: " + "; ".join(problems)) + f" via {via}"
status = {"status": edit.get("status", "done"), "summary": summary, "questions": edit.get("questions", [])}
final = f"I made the change.\n{json.dumps(status)}"
if tool == "claude":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final}))
elif tool == "codex":
    with open(out_file, "w") as fh:
        fh.write(final)
    print("codex: working...\ncodex: done")
else:
    print(final)
