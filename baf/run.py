"""Run tasks and preserve every attempt, including errors; never treat replay as quality."""
import argparse
import datetime
from decimal import Decimal
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

from client.cli import Client
from baf.tool_evidence import check_tool_evidence

SMOKE_TASKS = tuple(f"B{number}.1" for number in range(1, 10))
TASK_COUNT = 35
COURSE_COMMIT = "818740966b0d5b08fc9e46c3f6e449c824367ba9"
CHECKER_IMAGE = "itmo-sandbox:0.1"
COST_SOURCE = "https://openrouter.ai/docs/cookbook/administration/usage-accounting"


class MountedBafClient:
    """Run an attempt against its own synthetic directory on the client machine."""

    def __init__(self, client):
        self.client = client

    def preflight(self, **expectations):
        return self.client.preflight(**expectations)

    def execute(self, fixture, instruction, destination, **options):
        from client.mounted import MountedSession

        if any(path.is_symlink() for path in fixture.rglob("*")):
            raise ValueError("fixture must not contain symlinks")
        shutil.copytree(fixture, destination)
        with MountedSession.open(self.client, destination.resolve(), coordinated=True) as session:
            return session.execute(instruction, **options)


def task_paths():
    return sorted(Path("baf").glob("B[1-9]-*/B*/task.yaml"))


def task_identifier(path):
    return path.parent.name.split("-", 1)[0]


def preflight_code_checker():
    result = subprocess.run(
        ["docker", "image", "inspect", CHECKER_IMAGE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
    )
    if result.returncode:
        raise ValueError(f"isolated code check requires local Docker image {CHECKER_IMAGE}")


def cost_sum(results, key):
    values = [item[key] for item in results if item.get(key) is not None]
    return float(sum(Decimal(str(value)) for value in values)) if values else None


def write_summary(root, tasks, results, mode, pair, stopped_reason=None):
    """Keep the latest partial summary readable while a longer attempt is running."""
    attempted = {item["task"]: item for item in results}
    groups = {}
    for task in tasks:
        task_id = task_identifier(task)
        group = task_id.split(".", 1)[0]
        current = groups.setdefault(group, {"assigned": 0, "attempted": 0, "passed": 0})
        current["assigned"] += 1
        if task_id in attempted:
            current["attempted"] += 1
            current["passed"] += int(attempted[task_id]["pass"])
    full = len(attempted) == len(tasks)
    confirmed_real = mode == "real" and any(item.get("modes") == ["real"]
        and item.get("models") == [pair["model"]] for item in results)
    cost_complete = full and all(item.get("cost_complete") for item in results)
    summary = {
        "pair": pair,
        "passed": sum(item["pass"] for item in results),
        "total": len(results),
        "requested_total": len(tasks),
        "complete": full,
        "not_run": [str(task) for task in tasks if task_identifier(task) not in attempted],
        "quality_evidence": confirmed_real,
        "cost_usd": cost_sum(results, "cost_usd") if cost_complete else None,
        "recorded_cost_usd": cost_sum(results, "recorded_cost_usd"),
        "cost_complete": cost_complete,
        "matrix": groups,
        "results": results,
    }
    if stopped_reason:
        summary["stopped_reason"] = stopped_reason
    temp = root / ".summary.tmp"
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    temp.replace(root / "summary.json")
    return summary


def run_task(task_path, root, client, mode="real", expected_model=None):
    task = yaml.safe_load(task_path.read_text())
    task_id = task.get("id", task_path.parent.name.split("-")[0])
    output = root / task_id
    output.mkdir(parents=True)
    started = time.monotonic()
    result = {"task": task_id, "pass": False}
    trace = []
    try:
        instruction = task.get("instruction") or "\n".join((m if isinstance(m, str) else m["content"]) for m in task["messages"])
        if mode == "mock":
            instruction += f"\n[replay:{task_id}]"
        def preserve(event):
            trace.append(event)
            with (output / "trace.jsonl").open("a") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        state, trace = client.execute(task_path.parent / "fixture", instruction, output / "work",
            timeout=task.get("timeout_seconds", task.get("timeout_s", 240)), on_event=preserve,
            expected_mode=mode, expected_model=expected_model)
        result["state"] = state
        if state["status"] == "success":
            work = output / "work"
            if work.is_symlink() or any(p.is_symlink() for p in work.rglob("*")):
                raise ValueError("symlink in collected work")
            checked = subprocess.run([sys.executable, str(task_path.parent / "check.py"), str(work)],
                capture_output=True, text=True, timeout=30)
            result["checker"] = {"exit_code": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr}
            verdict = json.loads(checked.stdout)
            result["checker"]["verdict"] = verdict
            evidence_ok, evidence_reason = check_tool_evidence(task_id, trace)
            result["tool_evidence"] = {"pass": evidence_ok, "reason": evidence_reason}
            result["pass"] = checked.returncode == 0 and verdict.get("pass") is True and evidence_ok
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
    finally:
        if trace:
            (output / "trace.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in trace))
        result["duration_seconds"] = time.monotonic() - started
    model_events = [event for event in trace if event.get("type") == "model"]
    usages = [event.get("usage", {}) for event in model_events]
    replay_only = bool(model_events) and all(event.get("mode") == "mock" for event in model_events)
    completed = bool(usages) and result.get("state", {}).get("status") == "success"
    tokens_complete = replay_only or (
        completed
        and all(isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens")) for usage in usages)
    )
    charges = [usage.get("cost_usd") if isinstance(usage, dict) else None for usage in usages]
    confirmed_charges = [value for value in charges if type(value) in (int, float)
                         and math.isfinite(value) and value >= 0]
    cost_complete = replay_only or (completed and len(confirmed_charges) == len(charges))
    recorded = 0 if replay_only else (
        float(sum(Decimal(str(value)) for value in confirmed_charges)) if confirmed_charges else None
    )
    result.update(task_id=task_id, repeat=1, seconds=result["duration_seconds"],
                  tokens_in=0 if replay_only else sum(x["prompt_tokens"] for x in usages) if tokens_complete else None,
                  tokens_out=0 if replay_only else sum(x["completion_tokens"] for x in usages) if tokens_complete else None,
                  tokens_complete=tokens_complete,
                  cost_usd=recorded if cost_complete else None, recorded_cost_usd=recorded,
                  cost_complete=cost_complete,
                  cost_source="OpenRouter usage.cost recorded by the model proxy" if recorded is not None and not replay_only else None,
                  outcome=result.get("state", {}).get("status", "error"),
                  models=sorted({e["model"] for e in trace if e.get("model")}),
                  modes=sorted({e["mode"] for e in trace if e.get("mode")}))
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def validate_evidence(evidence):
    """Validate recorded approval metadata; this is not spending accounting or enforcement."""
    if not isinstance(evidence, dict):
        raise ValueError("model evidence must be an object")
    for field in ("model", "version", "index_date", "access", "approval"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(f"model evidence requires nonempty {field}")
    try:
        date = datetime.date.fromisoformat(evidence["index_date"])
        if date.isoformat() != evidence["index_date"]:
            raise ValueError()
    except ValueError:
        raise ValueError("model evidence index_date must use YYYY-MM-DD") from None
    for field in ("index", "budget_limit"):
        value = evidence.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"model evidence {field} must be a finite nonnegative number")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "real"], required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("results/baf"))
    parser.add_argument("--model-evidence", type=Path)
    parser.add_argument("--workspace", choices=["mounted", "snapshot"])
    args = parser.parse_args()
    if args.mode == "real":
        if not args.model_evidence:
            parser.error("real runs require --model-evidence with model/version/index/date/access/budget")
        try:
            evidence = json.loads(args.model_evidence.read_text())
            validate_evidence(evidence)
        except (OSError, ValueError) as exc:
            parser.error(f"{exc}; the evidence file records approval, not spending accounting or enforcement")
    else:
        evidence = {"mode": "mock", "quality_evidence": False}
    workspace = args.workspace or ("mounted" if args.mode == "real" else "snapshot")
    if args.mode == "real" and workspace != "mounted":
        parser.error("real runs require a mounted client workspace")
    tasks = task_paths()
    if args.mode == "mock" and not args.task:
        tasks = [path for path in tasks if task_identifier(path) in SMOKE_TASKS]
        if {task_identifier(path) for path in tasks} != set(SMOKE_TASKS):
            parser.error("expected one simple task for each of the nine basic functions")
    if args.task:
        tasks = [path for path in tasks if task_identifier(path) in args.task]
        missing = sorted(set(args.task) - {task_identifier(path) for path in tasks})
        if missing:
            parser.error("unknown task IDs: " + ", ".join(missing))
    if not tasks:
        parser.error("no matching tasks")
    if args.mode == "real" and not args.task and len(tasks) != TASK_COUNT:
        parser.error(f"expected all {TASK_COUNT} basic-function tasks")
    attempt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = args.output / args.mode / attempt
    root.mkdir(parents=True)
    pair = {"harness": "sequential", "model": evidence.get("model"),
            "model_version": evidence.get("version"), "reasoning_effort": evidence.get("reasoning_effort"),
            "provider": evidence.get("provider"), "quantization": evidence.get("quantization")}
    manifest = {"attempt": attempt, "mode": args.mode, "workspace": workspace, "model_evidence": evidence,
        "pair": pair, "course_commit": COURSE_COMMIT, "cost_source": COST_SOURCE if args.mode == "real" else None,
        "tasks": [str(t) for t in tasks], "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("runtime", "baf", "client") for p in Path(folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    client = Client()
    if workspace == "mounted":
        client = MountedBafClient(client)
    expected_model = evidence.get("model") if args.mode == "real" else None
    results = []
    stopped_reason = None
    (root / "results.jsonl").touch()
    write_summary(root, tasks, results, args.mode, pair)
    try:
        client.preflight(expected_mode=args.mode, expected_model=expected_model)
        if any(task_identifier(task).startswith("B6.") for task in tasks):
            preflight_code_checker()
    except Exception as exc:
        stopped_reason = "preflight failed: " + type(exc).__name__ + ": " + str(exc)[:1000]
    if stopped_reason is None:
        for task in tasks:
            result = run_task(task, root, client, args.mode, expected_model=expected_model)
            state = result.get("state", {})
            state_failure = state.get("status") in ("error", "cancelled")
            local_failure = state.get("error") in {"step_limit", "timeout", "sandbox_error", "mount_lost",
                                                   "write_outcome_unknown", "write_conflict", "write_unverified"}
            mode = state.get("model_mode")
            modes = result.get("modes", [])
            if (mode is not None and mode != args.mode) or (modes != [args.mode] and not (local_failure and not modes)):
                result["pass"] = False
                result["mode_error"] = "runtime mode differs from requested run mode or is unconfirmed"
            models = result.get("models", [])
            if args.mode == "real" and models != [evidence["model"]] and not (local_failure and not models):
                result["pass"] = False
                result["mode_error"] = "observed model differs from approved evidence or is unconfirmed"
            tool_evidence = result.get("tool_evidence", {})
            evidence_failure = tool_evidence.get("reason") if tool_evidence.get("pass") is False else None
            state_reason = None
            if state_failure:
                fields = (state.get("error") or state.get("status"), state.get("detail"))
                state_reason = ": ".join(str(field) for field in fields if field)
            checker = result.get("checker", {})
            result["reason"] = (result.get("error") or state_reason or result.get("mode_error") or evidence_failure
                                or checker.get("verdict", {}).get("reason") or checker.get("stdout", "no checker"))
            (root / result["task"] / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            results.append(result)
            with (root / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            write_summary(root, tasks, results, args.mode, pair)
            print(json.dumps({"task": result["task"], "pass": result["pass"], "reason": result["reason"]},
                             ensure_ascii=False), file=sys.stderr, flush=True)
            if result.get("mode_error") or (state_failure and not local_failure):
                stopped_reason = result["reason"]
                break
    summary = write_summary(root, tasks, results, args.mode, pair, stopped_reason)
    print(json.dumps({"path": str(root), "passed": summary["passed"], "total": len(results),
                      "requested_total": len(tasks), "stopped_reason": stopped_reason}))
    raise SystemExit(bool(stopped_reason) or summary["passed"] != len(tasks))



if __name__ == "__main__":
    main()
