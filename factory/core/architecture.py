"""Deterministic architecture diff: which components a change touches and how
the changed files depend on each other, as PlantUML. Components are top-level
directories; dependencies come from import statements in common languages."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

IMPORT_PATTERNS = (
    re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M),           # Python
    re.compile(r"""(?:import\s[^'"]*from\s*|require\(\s*|import\(\s*)['"]([^'"]+)['"]"""),  # JS/TS
    re.compile(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', re.M),                               # Go
    re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+);", re.M),                            # Java/Kotlin
    re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M),                                     # C/C++
    re.compile(r"^\s*use\s+(?:crate::)?([\w:]+)", re.M),                                  # Rust
)


def component(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "(root)"


def _alias(name: str) -> str:
    return re.sub(r"\W", "_", name) or "root"


def references(text: str) -> set[str]:
    refs = set()
    for pattern in IMPORT_PATTERNS:
        for match in pattern.finditer(text):
            ref = next((g for g in match.groups() if g), "")
            refs.add(ref.replace("::", "/").replace(".", "/").lstrip("./"))
    return refs


def diagram(changes: Sequence[tuple[str, str]], root: Path, all_paths: Sequence[str]) -> str:
    """changes: (status, path). all_paths: every file in the tree, to resolve imports."""
    touched: dict[str, list[tuple[str, str]]] = {}
    for status, path in changes:
        touched.setdefault(component(path), []).append((status, path))
    stems = {}
    for p in all_paths:
        stem = str(PurePosixPath(p).with_suffix(""))
        stems[stem] = component(p)
        stems.setdefault(PurePosixPath(p).stem, component(p))
    edges = set()
    for status, path in changes:
        if status == "D" or not (root / path).is_file():
            continue
        src = component(path)
        for ref in references((root / path).read_text(errors="ignore")):
            target = stems.get(ref) or next((c for s, c in stems.items() if s.endswith("/" + ref) or ref.endswith("/" + s)), None)
            if target and target != src:
                edges.add((src, target))
    lines = ["@startuml", "skinparam componentStyle rectangle"]
    names = sorted(set(touched) | {t for _, t in edges})
    for name in names:
        files = touched.get(name, [])
        label = name + ("\\n" + "\\n".join(f"{s} {PurePosixPath(p).name}" for s, p in files[:8]) if files else "")
        style = " #LightYellow" if files else ""
        lines.append(f'component "{label}" as {_alias(name)}{style}')
    for src, dst in sorted(edges):
        lines.append(f"{_alias(src)} --> {_alias(dst)}")
    lines.append("@enduml")
    return "\n".join(lines)
