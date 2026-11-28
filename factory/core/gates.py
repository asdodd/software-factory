"""Hard PR criteria as mechanical checks. Nuance becomes a rule or it does not
gate. Thresholds come from config; per-repo settings may only add protected paths."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch

from factory.config.schema import FactoryConfig, RepoRules
from factory.core.workspace import Change, LineStats
from factory.ports.types import Ticket

ALWAYS_PROTECTED = (".git/**", ".gitmodules")
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", ";", "<!--", '"""', "'''")
MR_SECTIONS = ("What changed", "Why", "Blast radius", "How verified", "Architecture diff")


@dataclass
class GateReport:
    failures: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def extend(self, other: GateReport) -> None:
        self.failures.extend(other.failures)
        self.flags.extend(other.flags)


def glob_match(path: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch(path, pattern[3:])):
            return True
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return True
        if "/" not in pattern and fnmatch(path.rsplit("/", 1)[-1], pattern):
            return True
    return False


def ticket_flag(ticket: Ticket, name: str) -> bool:
    value = ticket.fields.get(name)
    return value is True or str(value).lower() in ("true", "yes", "1")


def check_changes(found: Sequence[Change], stats: LineStats, ticket: Ticket, cfg: FactoryConfig, rules: RepoRules,
                  submodule_paths: Sequence[str], size: bool = True) -> GateReport:
    """Gates for one repo's change. size=False leaves the size limits to check_size
    over the whole ticket (several repos)."""
    report = GateReport()
    gates = cfg.gates
    paths = [c.path for c in found]
    if not found:
        report.failures.append("no changes were produced")
        return report
    protected = (*ALWAYS_PROTECTED, *gates.protected_paths, *rules.protected_paths)
    for c in found:
        if glob_match(c.path, protected):
            report.failures.append(f"protected path changed: {c.path}")
        if c.is_symlink:
            report.failures.append(f"symlinks may not be added or changed: {c.path}")
    for sub in submodule_paths:
        touched = [p for p in paths if p == sub or p.startswith(sub.rstrip("/") + "/")]
        if not touched:
            continue
        if cfg.git.submodules.allow_modifications and ticket_flag(ticket, "submodule_change"):
            report.flags.append(f"submodule {sub} changed as the ticket requests; human review required")
        else:
            report.failures.append(f"submodule {sub} is outside this agent's scope (changes inside submodules are not committed)")
    if gates.forbid_new_doc_files and not ticket_flag(ticket, "docs_requested"):
        for c in found:
            if c.status == "A" and glob_match(c.path, gates.doc_file_patterns):
                report.failures.append(f"unsolicited documentation file added: {c.path}")
    for c in found:
        if c.status == "D" and glob_match(c.path, gates.test_file_patterns):
            report.failures.append(f"test file deleted: {c.path}")
    if size:
        report.extend(check_size(len(found), stats.added + stats.deleted, cfg))
    code_lines = [line.strip() for line in stats.added_lines if line.strip()]
    if len(code_lines) >= 10:
        comments = sum(1 for line in code_lines if line.startswith(COMMENT_PREFIXES))
        density = comments / len(code_lines)
        if density > gates.max_comment_density:
            report.failures.append(f"comment density {density:.2f} exceeds {gates.max_comment_density:.2f}")
    skipped = [line for line in stats.added_lines if any(m in line for m in SKIP_MARKERS)]
    if skipped:
        report.failures.append(f"{len(skipped)} added line(s) skip or disable tests")
    return report


def check_size(files: int, lines: int, cfg: FactoryConfig) -> GateReport:
    report = GateReport()
    if files > cfg.limits.max_files_changed:
        report.failures.append(f"{files} files changed (limit {cfg.limits.max_files_changed})")
    if lines > cfg.limits.max_diff_lines:
        report.failures.append(f"{lines} diff lines (limit {cfg.limits.max_diff_lines})")
    return report


SKIP_MARKERS = ("@pytest.mark.skip", "@unittest.skip", "it.skip(", "describe.skip(", "xit(", "@Disabled", "t.Skip(",
                "#[ignore]", "pytest.skip(")


def check_template(description: str) -> GateReport:
    report = GateReport()
    for section in MR_SECTIONS:
        if f"## {section}" not in description:
            report.failures.append(f"MR template section missing: {section}")
    return report
