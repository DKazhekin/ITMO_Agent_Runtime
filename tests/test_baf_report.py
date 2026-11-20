"""Verify first-attempt scoring, separate charge sources, and public path handling."""
import json

from baf.report import publish


def write_round(source, name, task, passed, *, complete=True, recorded=0.002, native=None):
    round_path = source / name
    folder = round_path / task
    work = folder / "work"
    work.mkdir(parents=True)
    manifest = {"mode": "real", "workspace": "mounted",
                "model_evidence": {"model": "test", "approval": "private approval"},
                "tasks": [f"baf/{task}/{task}-fixture/task.yaml"]}
    (round_path / "manifest.json").write_text(json.dumps(manifest))
    record = {
        "task_id": task, "pass": passed, "seconds": 1,
        "state": {"status": "success" if passed else "error"},
        "reason": "ok" if passed else "problem at /Users/private-account/path",
        "cost_complete": complete, "recorded_cost_usd": recorded,
    }
    (round_path / "results.jsonl").write_text(json.dumps(record) + "\n")
    usage = {"cost_usd": recorded}
    if native is not None:
        usage["cost"] = native
    (folder / "trace.jsonl").write_text(json.dumps({"type": "model", "usage": usage}) + "\n")
    (work / "output.txt").write_text("synthetic artifact")
    (work / "private.txt").write_text("/home/private-account/file")
    (work / "linked.txt").symlink_to(work / "output.txt")
    return round_path


def test_reports_original_failure_and_separate_retry(tmp_path):
    source = tmp_path / "source"
    write_round(source, "20261120T113000Z-a", "B1.1", False, complete=False)
    write_round(source, "20261120T113100Z-b", "B1.1", True)
    output = tmp_path / "output"
    report = publish(source, output, ["B1.1", "B9.1"])
    assert report["attempted"] == 1 and report["passed"] == 0
    assert report["retries"] == 1 and report["not_run"] == ["B9.1"]
    task = report["tasks"]["B1.1"]
    assert task["first"]["pass"] is False and task["repeats"][0]["pass"] is True
    assert report["costs"]["trace_cost_usd"] is None
    assert report["costs"]["recorded_cost_usd"] == 0.004
    assert report["costs"]["known_trace_cost_usd"] == 0.004
    assert report["costs"]["unknown_record_paths"] == [task["first"]["result"]]
    assert "private-account" not in (output / "report.json").read_text()
    manifest = output / report["rounds"][0]["manifest"]
    assert "private approval" not in manifest.read_text()
    work = output / task["first"]["workspace"]
    assert sorted(item.name for item in work.iterdir()) == ["output.txt"]
    assert len(task["first"]["omitted_artifacts"]) == 2


def test_native_and_rounded_costs_do_not_mix(tmp_path):
    source = tmp_path / "source"
    write_round(source, "20261120T113000Z-a", "B1.1", True, recorded=0.002, native=0.0019998)
    write_round(source, "20261120T113100Z-b", "B9.1", True, recorded=0.003, native=0.0029998)
    report = publish(source, tmp_path / "output", ["B1.1", "B9.1"])
    assert report["costs"]["complete"]
    assert report["costs"]["trace_cost_usd"] == report["costs"]["recorded_cost_usd"] == 0.005
    assert report["costs"]["provider_raw_cost_usd"] == 0.0049996
