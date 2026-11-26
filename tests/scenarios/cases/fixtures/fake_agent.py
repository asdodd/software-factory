"""A stand-in for a bring-your-own CLI agent. It checks what it can see, asks
the factory's loopback gateway for an edit, applies it, and reports."""

import json
import os
import subprocess
import sys
import urllib.request

problems = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d != ".git"]
    for name in files:
        if "proprietary" in os.path.join(root, name).lower():
            problems.append(f"saw {os.path.join(root, name)}")
history = subprocess.run(["git", "log", "-p", "--all"], capture_output=True, text=True).stdout
if "TOPSECRET" in history:
    problems.append("history leaks restricted content")
for var in os.environ:
    if var.startswith(("FACTORY_", "AWS_", "SIM_")) or var.lower().endswith("_proxy"):
        problems.append(f"env leaks {var}")

url, token, model, dialect = (os.environ["GATEWAY_URL"], os.environ["GATEWAY_TOKEN"], os.environ["MODEL"],
                              os.environ.get("DIALECT", "openai_chat"))
task = open(os.environ["INSTRUCTIONS"]).read()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
if dialect == "anthropic_messages":
    body = {"model": "whatever-the-cli-wants", "max_tokens": 1024, "stream": True, "system": task,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Return the edit as JSON."}]}]}
    req = urllib.request.Request(url + "/v1/messages", json.dumps(body).encode(),
                                 {"x-api-key": token, "content-type": "application/json"})
    raw = opener.open(req).read().decode()
    text = "".join(json.loads(line[6:])["delta"]["text"] for line in raw.splitlines()
                   if line.startswith("data: ") and '"text_delta"' in line)
else:
    body = {"model": model, "messages": [{"role": "system", "content": task},
                                         {"role": "user", "content": "Return the edit as JSON."}]}
    req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    text = json.loads(opener.open(req).read())["choices"][0]["message"]["content"]

edit = json.loads(text)
with open(edit["path"], "w") as fh:
    fh.write(edit["content"])
summary = "ISOLATION-OK" if not problems else "ISOLATION-FAIL: " + "; ".join(problems)
with open(os.environ["RESULT"], "w") as fh:
    json.dump({"status": "done", "summary": f"{summary} via {dialect}"}, fh)
sys.exit(0)
