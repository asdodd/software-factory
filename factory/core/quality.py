"""Coverage on changed lines and mutation score, read from standard report
formats the repo's own commands produce. Language-agnostic: LCOV, Cobertura
XML, the Stryker mutation-testing-report schema, or a plain {"score": x}."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(patch: str) -> dict[str, set[int]]:
    """Map path -> new-side line numbers added, from a unified diff with -U0."""
    out: dict[str, set[int]] = {}
    path = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            if path:
                out.setdefault(path, set())
        elif path and (m := HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out[path].update(range(start, start + count))
    return {p: lines for p, lines in out.items() if lines}


def parse_lcov(text: str) -> dict[str, dict[int, int]]:
    cov: dict[str, dict[int, int]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            current = cov.setdefault(line[3:].strip(), {})
        elif line.startswith("DA:") and current is not None:
            num, hits = line[3:].split(",")[:2]
            current[int(num)] = int(float(hits))
    return cov


def parse_cobertura(text: str) -> dict[str, dict[int, int]]:
    root = ET.fromstring(text)
    sources = [s.text.strip().rstrip("/") + "/" for s in root.iter("source") if s.text and s.text.strip()]
    cov: dict[str, dict[int, int]] = {}
    for cls in root.iter("class"):
        name = cls.get("filename", "")
        lines = cov.setdefault(name, {})
        for ln in cls.iter("line"):
            lines[int(ln.get("number", 0))] = int(ln.get("hits", 0))
        for src in sources:
            cov.setdefault(src + name, lines)
    return cov


def load_coverage(path: Path) -> dict[str, dict[int, int]]:
    text = path.read_text(errors="replace")
    return parse_cobertura(text) if text.lstrip().startswith("<") else parse_lcov(text)


def _lookup(cov: dict[str, dict[int, int]], path: str, root: Path) -> dict[int, int] | None:
    if path in cov:
        return cov[path]
    for name, lines in cov.items():
        candidate = Path(name)
        try:
            rel = str(candidate.resolve().relative_to(root.resolve())) if candidate.is_absolute() else name
        except ValueError:
            continue
        if rel == path or rel.endswith("/" + path) or path.endswith("/" + rel):
            return lines
    return None


def changed_line_coverage(cov: dict[str, dict[int, int]], added: dict[str, set[int]], root: Path) -> tuple[int, int]:
    """(covered, instrumented) over added lines that the report instruments."""
    covered = instrumented = 0
    for path, lines in added.items():
        file_cov = _lookup(cov, path, root)
        if not file_cov:
            continue
        for n in lines:
            if n in file_cov:
                instrumented += 1
                covered += file_cov[n] > 0
    return covered, instrumented


def mutation_score(path: Path, fmt: str, changed: set[str], root: Path) -> tuple[float | None, int]:
    """(score, mutants considered). Stryker reports are limited to changed files."""
    doc = json.loads(path.read_text())
    if fmt == "score":
        return float(doc["score"]), int(doc.get("mutants", 0))
    killed = survived = 0
    for name, entry in doc.get("files", {}).items():
        rel = name
        if Path(name).is_absolute():
            try:
                rel = str(Path(name).resolve().relative_to(root.resolve()))
            except ValueError:
                continue
        if changed and rel not in changed:
            continue
        for mutant in entry.get("mutants", []):
            status = mutant.get("status")
            if status in ("Killed", "Timeout"):
                killed += 1
            elif status in ("Survived", "NoCoverage"):
                survived += 1
    total = killed + survived
    return (killed / total if total else None), total
