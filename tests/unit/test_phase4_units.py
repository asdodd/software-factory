import sys

from factory.core import metrics_report
from factory.core.monitors import CommandMonitor


def test_command_monitor_parses_json_lines(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(
        "import json\n"
        "print('starting probe')\n"
        "print(json.dumps({'id': 'p1', 'summary': 'health check failed', 'repo': 'web/portal', 'severity': 'high'}))\n"
        "print(json.dumps({'summary': ''}))\n"
        "print('{not json')\n"
    )
    findings = CommandMonitor("probe", [sys.executable, str(script)]).poll_findings().items
    assert [(f.event_id, f.source, f.summary, f.severity) for f in findings] == [
        ("p1", "probe", "health check failed", "high")]


def test_command_monitor_survives_a_missing_binary():
    assert CommandMonitor("x", ["/nonexistent/probe"]).poll_findings().items == ()


def test_prometheus_output():
    report = {"runs": 3, "cost_per_ticket_tokens": 120.5, "mr_acceptance_rate": 0.5, "review_rounds_mean": None,
              "intake_override_rate": 0.25, "tool_call_success_rate": 0.9, "outcomes": {"mr_opened": 2, "failed": 1},
              "stage_seconds_mean": {"implement": 1.5}}
    text = metrics_report.prometheus(report)
    assert "factory_runs 3" in text and 'factory_run_outcomes{outcome="failed"} 1' in text
    assert "review_rounds_mean" not in text and 'factory_stage_seconds_mean{stage="implement"} 1.5' in text
