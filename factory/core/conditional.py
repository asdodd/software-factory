"""Conditional text blocks for skills and rules, resolved against the
deployment's capabilities before any text reaches an agent:

    <!-- factory:if subagents -->
    ...text kept only when sub-agents are allowed...
    <!-- factory:else -->
    ...text kept otherwise...
    <!-- factory:end -->

Conditions are capability names, optionally negated with "not" (for example
"not internet"). Blocks do not nest. The markers are HTML comments, so files
still read normally anywhere else. Unknown conditions and unbalanced markers
are errors, so a typo cannot silently drop or keep text."""

from __future__ import annotations

import re
from collections.abc import Mapping

MARKER = re.compile(r"<!--\s*factory:(if\s+(?:not\s+)?[a-z_]+|else|end)\s*-->[ \t]*\n?")


class ConditionalError(ValueError):
    pass


def _test(expr: str, capabilities: Mapping[str, bool]) -> bool:
    parts = expr.split()
    negate = parts[0] == "not"
    name = parts[-1]
    if name not in capabilities:
        raise ConditionalError(f"unknown capability {name!r} in a factory:if block (known: {', '.join(sorted(capabilities))})")
    return capabilities[name] != negate


def render(text: str, capabilities: Mapping[str, bool], source: str = "") -> str:
    out: list[str] = []
    pos = 0
    state = None  # None outside a block; else (keep_if, in_else)
    keep = True
    for m in MARKER.finditer(text):
        if keep:
            out.append(text[pos:m.start()])
        token = m.group(1)
        where = f" in {source}" if source else ""
        if token.startswith("if"):
            if state is not None:
                raise ConditionalError(f"nested factory:if{where}")
            cond = _test(token[2:].strip(), capabilities)
            state = (cond, False)
            keep = cond
        elif token == "else":
            if state is None or state[1]:
                raise ConditionalError(f"factory:else without factory:if{where}")
            state = (state[0], True)
            keep = not state[0]
        else:
            if state is None:
                raise ConditionalError(f"factory:end without factory:if{where}")
            state = None
            keep = True
        pos = m.end()
    if state is not None:
        raise ConditionalError("unterminated factory:if" + (f" in {source}" if source else ""))
    out.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out))
