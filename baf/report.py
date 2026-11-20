"""Publish reproducible first-attempt and retry evidence for the mounted benchmark."""
from __future__ import annotations

import argparse
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import tempfile
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from baf.run import task_identifier, task_paths

PERSONAL_PATH = re.compile(r"/(?:Users|home)/[^/\s\"']+|/(?:private/)?var/folders/[^\s\"']+")
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_BYTES = 32 * 1024 * 1024


def public_value(value):
    if isinstance(value, str):
        local = value.replace(str(Path.cwd().resolve()), "<REPO>")
        return PERSONAL_PATH.sub("<LOCAL_PATH>", local)
    if isinstance(value, list):
        return [public_value(item) for item in value]
    if isinstance(value, dict):
        return {public_value(key): public_value(item) for key, item in value.items()}
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(public_value(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_lines(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(public_value(row), ensure_ascii=False) + "\n" for row in values),
                    encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path):
    if not path.is_file():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.strip() and line.endswith(b"\n")]


def artifact_privacy_issue(data, suffix):
    decoded = data.decode("utf-8", errors="replace")
    if public_value(decoded) != decoded:
        return "personal path"
    if suffix != ".xlsx":
        return "unsupported binary artifact" if b"\0" in data else None
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 100 or sum(member.file_size for member in members) > MAX_ARTIFACT_BYTES:
                return "oversized workbook contents"
            for member in members:
                text = archive.read(member).decode("utf-8", errors="replace")
                if public_value(text) != text or public_value(member.filename) != member.filename:
                    return "personal path inside workbook"
    except (BadZipFile, CompressionError, EOFError, RuntimeError, NotImplementedError):
        return "unreadable workbook"
    return None


def copy_workspace(source, destination):
    """Only publish regular bounded synthetic artifacts, without following symlinks."""
    omitted = []
    if source.is_symlink():
        return ["workspace symlink"]
    if not source.is_dir():
        return omitted
    destination.mkdir(parents=True, exist_ok=True)
    published_bytes = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_symlink():
            omitted.append(f"{relative}: symlink")
            continue
        if path.is_dir():
            continue
        if (any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
                or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES):
            omitted.append(f"{relative}: non-public or oversized artifact")
            continue
        data = path.read_bytes()
        issue = artifact_privacy_issue(data, relative.suffix.lower())
        if issue or published_bytes + len(data) > MAX_WORKSPACE_BYTES:
            omitted.append(f"{relative}: {issue or 'workspace size limit'}")
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        published_bytes += len(data)
    return omitted


def numeric_cost(raw):
    if type(raw) not in (float, int):
        return None
    value = Decimal(str(raw))
    return value if value.is_finite() and value >= 0 else None


def cost_total(values):
    known = [item for item in values if item is not None]
    return float(sum(known, Decimal("0"))) if known else None


def task_record(result, events):
    count = 0
    trace_charges = []
    native_charges = []
    for event in events:
        if event.get("type") != "model":
            continue
        usage = event.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        count += 1
        trace_charges.append(numeric_cost(usage.get("cost_usd")))
        native_charges.append(numeric_cost(usage.get("cost")))
    trace_complete = bool(count) and result.get("cost_complete") is True and all(
        charge is not None for charge in trace_charges
    )
    native_complete = bool(count) and result.get("cost_complete") is True and all(
        charge is not None for charge in native_charges
    )
    return {
        "pass": result["pass"], "reason": result.get("reason"),
        "status": result.get("state", {}).get("status", "error"),
        "seconds": result["seconds"], "tokens_in": result.get("tokens_in"),
        "tokens_out": result.get("tokens_out"), "model_events": count,
        "recorded_cost_usd": result.get("recorded_cost_usd"),
        "trace_cost_usd": cost_total(trace_charges) if trace_complete else None,
        "known_trace_cost_usd": cost_total(trace_charges),
        "provider_raw_cost_usd": cost_total(native_charges) if native_complete else None,
        "cost_complete": trace_complete,
    }


def publish(source, destination, assigned):
    tasks = {task: {"first": None, "repeats": []} for task in assigned}
    rounds = []
    all_records = []
    status_by_task = {}
    for session in sorted(path for path in source.iterdir() if path.is_dir() and not path.is_symlink()):
        manifest_path = session / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        if manifest["mode"] != "real" or manifest.get("workspace") != "mounted":
            raise ValueError(f"Not a mounted real attempt: {session.name}")
        evidence = manifest.get("model_evidence", {})
        for field in ("approval", "budget_id", "api_key", "token"):
            evidence.pop(field, None)
        session_output = destination / "attempts" / session.name
        write_json(session_output / "manifest.json", manifest)
        summary_path = session / "summary.json"
        round_summary = {}
        if summary_path.is_file():
            round_summary = read_json(summary_path)
            write_json(session_output / "summary.json", round_summary)
        rows = read_lines(session / "results.jsonl")
        write_lines(session_output / "results.jsonl", rows)
        planned = [task_identifier(Path(path)) for path in manifest.get("tasks", [])]
        stop = round_summary.get("stopped_reason")
        rounds.append({"id": session.name, "manifest": f"attempts/{session.name}/manifest.json",
                       "summary": f"attempts/{session.name}/summary.json" if summary_path.is_file() else None,
                       "results": f"attempts/{session.name}/results.jsonl",
                       "scheduled": planned, "recorded": len(rows), "stopped_reason": stop})
        for task in planned:
            status_by_task[task] = {"attempt": session.name, "stopped_reason": stop,
                                    "reason": "not started after the round stopped" if stop else "no final result yet"}
        seen = set()
        for result in rows:
            task = result["task_id"]
            if task not in tasks or task in seen:
                raise ValueError(f"Unexpected or repeated ID in {session.name}: {task}")
            seen.add(task)
            task_source = session / task
            task_output = session_output / task
            events = read_lines(task_source / "trace.jsonl")
            prefix = f"attempts/{session.name}/{task}"
            write_json(task_output / "result.json", result)
            write_lines(task_output / "trace.jsonl", events)
            omitted = copy_workspace(task_source / "work", task_output / "work")
            record = {
                **task_record(result, events), "attempt": session.name,
                "result": f"{prefix}/result.json", "trace": f"{prefix}/trace.jsonl",
                "workspace": f"{prefix}/work" if (task_output / "work").is_dir() else None,
                "omitted_artifacts": omitted,
            }
            if tasks[task]["first"] is None:
                tasks[task]["first"] = record
            else:
                tasks[task]["repeats"].append(record)
            all_records.append(record)
    matrix = {}
    for task, entry in tasks.items():
        group = task.split(".", 1)[0]
        data = matrix.setdefault(group, {"assigned": 0, "attempted": 0, "passed": 0})
        data["assigned"] += 1
        if entry["first"] is not None:
            data["attempted"] += 1
            data["passed"] += int(entry["first"]["pass"])
    attempted = sum(group["attempted"] for group in matrix.values())
    passed = sum(group["passed"] for group in matrix.values())
    missing = [task for task, entry in tasks.items() if entry["first"] is None]
    for task in missing:
        tasks[task]["unattempted"] = status_by_task.get(task, {"reason": "not scheduled"})
    unknown = [record["result"] for record in all_records if not record["cost_complete"]]
    complete_cost = bool(all_records) and not unknown and not missing
    raw_native = [numeric_cost(record["provider_raw_cost_usd"]) for record in all_records]
    report = {
        "kind": "mounted_real_baf", "cohort": "first recorded attempt per task",
        "assigned": len(assigned), "attempted": attempted, "passed": passed,
        "attempt_records": len(all_records), "retries": len(all_records) - attempted,
        "not_run": missing, "matrix": matrix, "tasks": tasks, "rounds": rounds,
        "costs": {
            "currency": "USD",
            "recorded_cost_usd": cost_total(numeric_cost(record["recorded_cost_usd"]) for record in all_records),
            "known_trace_cost_usd": cost_total(numeric_cost(record["known_trace_cost_usd"]) for record in all_records),
            "trace_cost_usd": cost_total(numeric_cost(record["trace_cost_usd"]) for record in all_records) if complete_cost else None,
            "provider_raw_cost_usd": cost_total(raw_native) if not missing and all(v is not None for v in raw_native) else None,
            "complete": complete_cost,
            "unknown_record_paths": unknown,
            "source": "Trace usage.cost_usd is the proxy-recorded rounded charge. Native usage.cost is a separate field.",
        },
    }
    write_json(destination / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("results/checkpoint-2/baf/real"))
    parser.add_argument("--output", type=Path, default=Path("evidence/checkpoint-2/baf"))
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    if args.output.is_symlink():
        parser.error("output must not be a symlink")
    output = args.output.resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("source and output must be disjoint")
    marker = output / "report.json"
    if output.exists() and (not marker.is_file() or read_json(marker).get("kind") != "mounted_real_baf"):
        parser.error("output must be new or contain a generated mounted BAF report")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".baf-report-", dir=output.parent) as staging:
        stage = Path(staging) / "next"
        stage.mkdir()
        report = publish(source, stage, [task_identifier(path) for path in task_paths()])
        previous = Path(staging) / "previous"
        if output.exists():
            output.rename(previous)
        try:
            stage.rename(output)
        except BaseException:
            if previous.exists():
                previous.rename(output)
            raise
    print(json.dumps({key: report[key] for key in ("assigned", "attempted", "passed", "retries")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
