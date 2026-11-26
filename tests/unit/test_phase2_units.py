import json
from datetime import UTC, datetime

from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.state_store import MemoryStateStore
from factory.adapters.fake.ticket_system import FakeTicketSystem
from factory.core import architecture, quality
from factory.core.notify import TicketCommentNotifier
from factory.core.stateindex import Index, Records
from factory.ports.types import Notification, NotificationKind

COBERTURA = """<?xml version="1.0"?>
<coverage><sources><source>/repo</source></sources><packages><package><classes>
<class filename="src/calc.py"><lines><line number="5" hits="3"/><line number="6" hits="0"/></lines></class>
</classes></package></packages></coverage>"""


def test_cobertura_and_lcov_changed_line_coverage(tmp_path):
    added = {"src/calc.py": {4, 5, 6}}
    cov = quality.parse_cobertura(COBERTURA)
    assert quality.changed_line_coverage(cov, added, tmp_path) == (1, 2)
    lcov = quality.parse_lcov("SF:src/calc.py\nDA:5,1\nDA:6,2\nend_of_record\n")
    assert quality.changed_line_coverage(lcov, added, tmp_path) == (2, 2)


def test_mutation_scores(tmp_path):
    stryker = tmp_path / "m.json"
    stryker.write_text(json.dumps({"files": {
        "src/a.py": {"mutants": [{"status": "Killed"}, {"status": "Survived"}]},
        "src/untouched.py": {"mutants": [{"status": "Survived"}] * 5},
    }}))
    assert quality.mutation_score(stryker, "stryker", {"src/a.py"}, tmp_path) == (0.5, 2)
    plain = tmp_path / "s.json"
    plain.write_text('{"score": 0.9, "mutants": 10}')
    assert quality.mutation_score(plain, "score", set(), tmp_path) == (0.9, 10)


def test_architecture_diagram_links_components(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "core").mkdir()
    (tmp_path / "api/handlers.py").write_text("from core.calc import add\n")
    (tmp_path / "core/calc.py").write_text("def add(): ...\n")
    uml = architecture.diagram([("M", "api/handlers.py")], tmp_path, ["api/handlers.py", "core/calc.py"])
    assert uml.startswith("@startuml") and uml.endswith("@enduml")
    assert "api --> core" in uml and "M handlers.py" in uml


def test_ticket_comment_notifier_mentions_people():
    ts = FakeTicketSystem(FakeClock(datetime(2026, 1, 5, tzinfo=UTC)))
    ts.seed("SIM-1", "x")
    n = TicketCommentNotifier(ts, "[~{user}]")
    note = Notification(NotificationKind.SPEC_REVIEW, "SIM-1", ("carol", "alice"), "Spec ready", "", "link")
    n.notify(note)
    n.notify(note)
    bodies = [c.body for c in ts.comments["SIM-1"]]
    assert bodies == ["[~carol] [~alice] Spec ready\nlink"]


def test_records_and_index():
    state = MemoryStateStore()
    idx = Index(state, "ns", "bot")
    idx.add("SIM-1")
    idx.add("SIM-2")
    idx.add("SIM-1")
    idx.remove("SIM-2")
    assert idx.members() == ["SIM-1"]
    rec = Records(state, "r")
    rec.put("k", {"a": 1})
    assert rec.get("k") == {"a": 1}
    rec.delete("k")
    assert rec.get("k") is None
