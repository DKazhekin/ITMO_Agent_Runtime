"""Exercise a stale model decision against a changing, real mounted file."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from client.cli import Client
from client.mounted import MountedSession


MODEL = r'''
import json
import re
import threading
import uvicorn
from fastapi import FastAPI, HTTPException

app = FastAPI()
waiting = threading.Event()
resume = threading.Event()
audit = []

@app.get("/health")
def health():
    return {"status": "ready", "mode": "mock", "model": None}

@app.get("/audit")
def inspect():
    return {"waiting": waiting.is_set(), "events": audit}

@app.post("/release")
def release():
    resume.set()
    return {"released": True}

def tool(name, arguments):
    return {"role": "assistant", "tool_calls": [{
        "id": str(len(audit)), "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}

@app.post("/completion")
def complete(data: dict):
    assert data["expected_mode"] == "mock"
    messages = data["messages"]
    reads = [json.loads(item["content"])["stdout"] for item in messages if item["role"] == "tool"]
    step = len(audit)
    if step == 0:
        audit.append({"offered": "read"})
        message = tool("ReadFile", {"path": "port.txt"})
    elif step == 1:
        audit.append({"observed": re.search(r"port=(\d+)", reads[-1])[1], "offered": "stale_write"})
        waiting.set()
        if not resume.wait(40):
            raise HTTPException(504, "model barrier not released")
        message = tool("WriteFile", {"path": "stale-command.txt", "content": reads[-1]})
    elif step == 2:
        assert messages[-1]["role"] == "user" and "port.txt" in messages[-1]["content"]
        audit.append({"notification": True, "offered": "reread"})
        message = tool("ReadFile", {"path": "port.txt"})
    elif step == 3:
        assert len(reads) == 2
        observed = re.search(r"port=(\d+)", reads[-1])[1]
        audit.append({"observed": observed, "offered": "answer"})
        message = {"role": "assistant", "content": "Current port: " + observed}
    else:
        raise HTTPException(400, "unexpected extra request")
    return {"message": message, "mode": "mock", "model": None,
            "usage": {"prompt_tokens": 8, "completion_tokens": 2}}

uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
'''


RECOVERY_STEPS = r'''
    if RECOVERY:
        if not audit:
            audit.append({"offered": "write"})
            message = tool("WriteFile", {"path": "once.txt", "content": "synthetic-write-once"})
        else:
            audit.append({"observed": "write result", "offered": "barrier"})
            assert messages[-1]["role"] == "tool" and json.loads(messages[-1]["content"])["status"] == "success"
            waiting.set()
            if not resume.wait(40):
                raise HTTPException(504, "model barrier not released")
            message = tool("WriteFile", {"path": "repeated.txt", "content": "incorrect retry"})
        return {"message": message, "mode": "mock", "model": None, "usage": {}}
'''

def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def containers(docker, project, service=None):
    filters = ["--filter", f"label=com.docker.compose.project={project}"]
    if service:
        filters.extend(("--filter", f"label=com.docker.compose.service={service}"))
    return command(*docker, "ps", "-aq", *filters).splitlines()


def audit(docker, model, path, method="GET"):
    code = ("import urllib.request as r; import sys; "
            "print(r.urlopen(r.Request('http://127.0.0.1:8000/'+sys.argv[1],method=sys.argv[2]),timeout=4)"
            ".read().decode())")
    return json.loads(command(*docker, "exec", model, "python", "-c", code, path, method))


def wait_barrier(docker, model):
    deadline = time.monotonic() + 20
    while not audit(docker, model, "audit")["waiting"]:
        if time.monotonic() > deadline:
            raise TimeoutError("model did not reach the barrier")
        time.sleep(0.1)


def scenario(docker, model, client, root):
    marker = "PrivateSynthetic_" + secrets.token_hex(12)
    port = root / "port.txt"
    port.write_text("port=8000\n" + marker + "\n")
    client.preflight("mock")
    with MountedSession.open(client, root) as session:
        run = session.submit("Read the port and report its current value.")
        wait_barrier(docker, model)
        assert session.status(run)["status"] == "running"
        port.write_text("port=9000\n" + marker + "\n")
        audit(docker, model, "release", "POST")
        result, events = session.follow(run, timeout=35)
        checks = audit(docker, model, "audit")["events"]
        assert [row.get("observed") for row in checks if "observed" in row] == ["8000", "9000"]
        assert result["status"] == "success" and result["answer"] == "Current port: 9000"
        assert [event["tool"] for event in events if event["type"] == "tool_call"] == ["ReadFile", "ReadFile"]
        assert not (root / "stale-command.txt").exists()
        changed = [event for event in events if event["type"] == "file_changed"]
        assert changed and changed[0]["path"] == "port.txt" and changed[0]["version"] == "0"
        assert sum(event["type"] == "result" for event in events) == 1
        cursor = events[0]["seq"]
        assert session.events(run, cursor) == events[1:]
        assert marker in json.dumps(events)
    history = session.history()
    terminal = session.status(run)
    assert marker not in history and "Current port: 9000" not in history
    assert terminal["status"] == "success" and "answer" not in terminal
    return {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "scenario": "Actual mounted Mac folder, real runtime services, deterministic local model",
        "observed_model_steps": checks,
        "public_events": [{key: event[key] for key in ("seq", "type", "tool", "path", "file_version") if key in event}
                          for event in events],
        "final_answer": result["answer"],
        "stale_command_executed": False,
        "technical_history_excludes_raw_text": True,
        "technical_terminal": terminal,
        "restart_tested": False,
    }, history


def recovery_scenario(docker, model, client, root, service, manager):
    instruction = "Create the synthetic file once."
    external_id = secrets.token_hex(16)
    expected_error = "harness_restarted" if service == "harness" else "mount_lost"
    client.preflight("mock")
    with MountedSession.open(client, root) as session:
        run = session.submit(instruction, request_id=external_id)
        wait_barrier(docker, model)
        path = root / "once.txt"
        assert path.read_text() == "synthetic-write-once"
        before = path.stat()
        assert session.status(run)["status"] == "running"
        subprocess.run([*docker, "restart", manager], check=True, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                client.preflight("mock")
                break
            except (httpx.TransportError, httpx.HTTPStatusError):
                time.sleep(0.2)
        else:
            raise TimeoutError("runtime did not become healthy after restart")
        audit(docker, model, "release", "POST")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                state = session.status(run)
                if state["status"] != "running":
                    break
            except (httpx.TransportError, httpx.HTTPStatusError):
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError("run did not settle after restart")
        assert state["status"] == "error" and state["error"] == expected_error, state
        try:
            session.submit(instruction, request_id=external_id)
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 409
        else:
            raise AssertionError("accepted repeat after losing the mounted session")
        after = path.stat()
        assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
        assert path.read_text() == "synthetic-write-once" and not (root / "repeated.txt").exists()
        events = session.events(run)
        assert [event["tool"] for event in events if event["type"] == "tool_call"] == ["WriteFile"]
        assert sum(event["type"] == "result" for event in events) == 1
    history = session.history()
    assert "synthetic-write-once" not in history
    assert sum(event.get("tool") == "WriteFile" and event["source"] == "sandbox"
               for event in map(json.loads, history.splitlines()) if event["type"] == "tool_call") == 1
    return {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "restart_service": service,
        "restart_method": "docker restart after confirmed publication while model was blocked",
        "status": state,
        "repeat_status": 409,
        "one_write_before_restart": True,
        "original_inode_and_mtime_preserved": True,
        "second_write_executed": False,
        "terminal_events": 1,
        "technical_history_excludes_raw_text": True,
    }, history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--image-project", default="itmo-cp2-ci")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--restart-service", choices=("harness", "sandbox-manager"))
    parser.add_argument("--manager-source", type=Path, help="Replace only sandbox.py in a local test image")
    args = parser.parse_args()
    project = "itmo-cp2-fault"
    docker = ["docker", "--context", args.context]
    label = f"label=com.docker.compose.project={project}"
    if containers(docker, project) or any(
        command(*docker, resource, "ls", "-q", "--filter", label) for resource in ("volume", "network")
    ):
        parser.error("The isolated test project already has resources.")
    env = {**os.environ, "DOCKER_CONTEXT": args.context, "COMPOSE_PROJECT_NAME": project,
           "GATEWAY_PORT": "18126", "CLIENT_TOKEN": "local-checkpoint-token", "MODEL_MODE": "mock",
           "MODEL_API_KEY": "", "MODEL_BASE_URL": "", "MODEL_NAME": "", "MODEL_BUDGET_ID": ""}
    services = {}
    for service in ("sandbox-manager", "llm-proxy", "harness", "gateway"):
        (reference,) = containers(docker, args.image_project, service)
        image = command(*docker, "inspect", "--format", "{{.Config.Image}}", reference)
        services[service] = {"image": image}
    model_source = MODEL
    if args.restart_service:
        start = "    reads = "
        model_source = "RECOVERY = True\n" + MODEL.replace(start, RECOVERY_STEPS.lstrip("\n") + start, 1)
    services["llm-proxy"]["command"] = ["python", "-c", model_source]
    with tempfile.TemporaryDirectory(prefix="check-mounted-change-") as directory:
        test_image = None
        manager_source_sha256 = None
        if args.manager_source:
            source = args.manager_source.read_bytes()
            manager_source_sha256 = hashlib.sha256(source).hexdigest()
            build = Path(directory) / "image"
            build.mkdir()
            original = services["sandbox-manager"]["image"]
            (build / "sandbox.py").write_bytes(source)
            (build / "Dockerfile").write_text(f"FROM {original}\nCOPY sandbox.py /app/runtime/sandbox.py\n")
            test_image = f"itmo-cp2-fault-manager:{secrets.token_hex(8)}"
            subprocess.run([*docker, "build", "--pull=false", "--network=none", "-t", test_image, str(build)],
                           check=True)
            services["sandbox-manager"]["image"] = test_image
        config = Path(directory) / "offline-model.json"
        config.write_text(json.dumps({"services": services}))
        compose = ["docker-compose", "-p", project, "-f", "compose.yaml", "-f", "compose.offline.yaml",
                   "-f", str(config)]
        try:
            subprocess.run([*compose, "up", "-d", "--wait", "--no-build", "--pull", "never"],
                           env=env, check=True)
            (model,) = containers(docker, project, "llm-proxy")
            client = Client("http://127.0.0.1:18126", token=env["CLIENT_TOKEN"])
            folder = Path(directory) / "workspace"
            folder.mkdir()
            try:
                if args.restart_service:
                    (component,) = containers(docker, project, args.restart_service)
                    results, history = recovery_scenario(
                        docker, model, client, folder, args.restart_service, component,
                    )
                else:
                    results, history = scenario(docker, model, client, folder)
            finally:
                client.http.close()
            results["test_model_sha256"] = hashlib.sha256(model_source.encode()).hexdigest()
            if manager_source_sha256:
                results["sandbox_manager_source_sha256"] = manager_source_sha256
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "result.json").write_text(json.dumps(results, indent=2) + "\n")
            (args.output / "technical.jsonl").write_text(history)
            print(json.dumps(results, indent=2), flush=True)
        except Exception as exc:
            args.output.mkdir(parents=True, exist_ok=True)
            failure = {"collected_at_utc": datetime.now(timezone.utc).isoformat(),
                       "restart_service": args.restart_service, "error": type(exc).__name__,
                       "message": str(exc), "preceding_error": str(exc.__context__) if exc.__context__ else None}
            (args.output / "failure.json").write_text(json.dumps(failure, indent=2) + "\n")
            for service in ("sandbox-manager", "harness"):
                for container in containers(docker, project, service):
                    output = subprocess.run([*docker, "logs", "--tail", "70", container],
                                            capture_output=True, text=True)
                    (args.output / (service + ".log")).write_text(output.stdout + output.stderr)
            raise
        finally:
            try:
                subprocess.run([*compose, "down", "--volumes"], env=env, check=True)
            finally:
                if test_image:
                    subprocess.run([*docker, "image", "rm", test_image], check=True)


if __name__ == "__main__":
    main()
