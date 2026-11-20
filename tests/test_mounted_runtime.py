"""Exercise mounted access and journal policy at the actual HTTP boundaries."""

import importlib
import json
import threading
import time
from types import SimpleNamespace

import docker
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


class Engine:
    def __init__(self):
        self.containers = self.api = self
        self.commands = []
        self.mounts = []
        self.running = set()
        self.output = b""

    def list(self, **options):
        return [SimpleNamespace(remove=lambda force, name=name: self.running.discard(name))
                for name in self.running]

    def run(self, image, command, **options):
        self.mounts.append(options["mounts"])
        name = options["name"]
        assert name not in self.running
        self.running.add(name)
        return SimpleNamespace(id=name, remove=lambda force: self.running.discard(name))

    def exec_create(self, container, command, **options):
        assert container in self.running
        self.commands.append(command)
        return {"Id": container}

    def exec_start(self, execution, **options):
        if options.get("socket"):
            return SimpleNamespace(
                _sock=SimpleNamespace(settimeout=lambda timeout: None, sendall=lambda data: None,
                                      shutdown=lambda mode: None),
                read=lambda size: b"", close=lambda: None,
            )
        return iter([(self.output, None)])

    def exec_inspect(self, execution):
        return {"ExitCode": 0}


class Helper:
    def __init__(self, error, engine):
        self.error = error
        self.engine = engine
        self.bound = {}
        self.external = {}
        self.allowed = True
        self.updates = []
        self.started = []
        self.receipt = {"status": "unchanged", "outcomes": []}
        self.finish_lost = False
        self.query_count = 0

    async def attach(self, session, descriptor):
        self.bound[session] = descriptor["generation"]
        self.external[session] = descriptor["client_session_id"]
        return {"path": f"/var/lib/itmo-mount/generation-{session}/work",
                "generation": descriptor["generation"], "readonly": descriptor["readonly"]}

    async def check(self, session, generation):
        return self.bound.get(session) == generation and self.allowed

    async def changes(self, session, generation, run):
        return self.updates

    async def begin(self, session, generation, run, operation, kind, expected):
        assert await self.check(session, generation)
        self.started.append((operation, kind, expected))

    async def finish(self, session, generation, operation, success):
        assert not self.engine.running
        if self.finish_lost:
            raise self.error("receipt lost")
        return self.receipt

    async def query(self, session, generation, operation):
        self.query_count += 1
        if self.receipt is None:
            raise self.error("receipt unavailable")
        return self.receipt

    async def cancel(self, session, generation, operation):
        return None

    async def detach(self, session, generation):
        self.bound.pop(session, None)


class Model:
    def __init__(self):
        self.messages = []
        self.responses = []

    def __call__(self, path, payload):
        if path == "/health":
            return {"mode": "mock", "model": None}
        assert path == "/completion" and payload["expected_mode"] == "mock"
        self.messages.append(payload["messages"][:])
        message = self.responses.pop(0)
        return {"message": message() if callable(message) else message,
                "mode": "mock", "model": None, "usage": {"prompt_tokens": 7, "completion_tokens": 2}}


@pytest.fixture
def stack(tmp_path, monkeypatch):
    engine = Engine()
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CLIENT_TOKEN", "shared-installation-token")
    monkeypatch.setattr(docker, "from_env", lambda timeout: engine)
    harness = importlib.import_module("runtime.harness")
    sandbox = importlib.import_module("runtime.sandbox")
    gateway = importlib.import_module("runtime.gateway")
    from runtime.common import Journal
    from runtime.session_journal import SessionJournal
    from runtime.session_store import SessionStore

    helper = Helper(sandbox.MountError, engine)
    model = Model()
    monkeypatch.setattr(sandbox, "engine", engine)
    monkeypatch.setattr(sandbox, "mount_client", helper)
    monkeypatch.setattr(sandbox, "mounted_sessions", {})
    monkeypatch.setattr(sandbox, "containers", {})
    monkeypatch.setattr(sandbox, "active", set())
    monkeypatch.setattr(harness, "busy", {})
    monkeypatch.setattr(harness, "cancelled", set())
    monkeypatch.setattr(harness, "active_operations", set())
    resources = (
        (harness, "journal", Journal("harness")),
        (harness, "owners", SessionStore()),
        (harness, "protected", SessionJournal("harness-mounted")),
        (sandbox, "journal", Journal("sandbox")),
        (sandbox, "mounted_journal", SessionJournal("sandbox-mounted")),
    )
    for module, name, value in resources:
        monkeypatch.setattr(module, name, value)
    harness_client = TestClient(harness.app)
    sandbox_client = TestClient(sandbox.app)
    original_async_client = httpx.AsyncClient

    def call(target, path, payload=None, method=None, timeout=75, headers=None):
        if target == harness.MODEL:
            return model(path, payload)
        assert target in (harness.SANDBOX, gateway.HARNESS)
        client = sandbox_client if target == harness.SANDBOX else harness_client
        response = client.request(method or ("POST" if payload is not None else "GET"), path,
                                  json=payload, headers=headers)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.json()["detail"])
        return response.json()

    monkeypatch.setattr(harness, "call", call)
    monkeypatch.setattr(gateway, "call", call)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: original_async_client(
        **kwargs, transport=httpx.ASGITransport(app=harness.app),
    ))
    token = {"Authorization": "Bearer shared-installation-token"}
    clients = {
        "owner": TestClient(gateway.app, headers={**token, "X-Session-Key": "owner" * 12}),
        "other": TestClient(gateway.app, headers={**token, "X-Session-Key": "other" * 12}),
        "legacy": TestClient(gateway.app, headers=token),
    }
    try:
        yield SimpleNamespace(clients=clients, model=model, helper=helper, engine=engine,
                              harness=harness, sandbox=sandbox, state=tmp_path, transport=sandbox_client)
    finally:
        for client in (*clients.values(), harness_client, sandbox_client):
            client.close()
        for _, _, value in resources:
            value.db.close()


def provision(client, session="ext-session", readonly=False):
    body = {"session_id": session, "mount": {"generation": "a" * 32,
            "reverse_port": 23456, "host_key": "test-host-key", "readonly": readonly}}
    response = client.post("/sessions", json=body)
    assert response.status_code == 200, response.text
    return body, response.json()["session_id"]


def run_body(session, run="ext-run"):
    return {"session_id": session, "request_id": run, "instruction": "Complete the fixture."}


def command(name, arguments):
    return {"role": "assistant", "tool_calls": [
        {"id": "tool", "function": {"name": name, "arguments": json.dumps(arguments)}}
    ]}


def wait_result(client, run):
    for _ in range(300):
        response = client.get(f"/runs/{run}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] != "running":
            return result
        time.sleep(0.01)
    pytest.fail("run did not finish")


def test_owner_routes_repetition_and_private_history(stack):
    marker = "SensitiveFixtureText2026"
    external_session, external_run = f"session_{marker}", f"run_{marker}"
    owner, other, legacy = (stack.clients[name] for name in ("owner", "other", "legacy"))
    body, session = provision(owner, external_session)
    assert session != external_session
    assert stack.helper.external[session] == external_session
    assert owner.post("/sessions", json=body).json()["session_id"] == session
    assert other.post("/sessions", json=body).status_code == 403
    for identifier in (external_session, session):
        assert legacy.post("/sessions", json={"session_id": identifier}).status_code == 403
        assert owner.post("/sessions", json={"session_id": identifier}).status_code == 409
        assert other.get(f"/sessions/{identifier}/files").status_code == 403
        assert other.get(f"/sessions/{identifier}/history").status_code == 403
        assert other.delete(f"/sessions/{identifier}").status_code == 403
    assert owner.post(f"/sessions/{session}/changes", json={}).status_code == 404
    assert owner.get(f"/sessions/{session}/files").status_code == 409

    blocked, resume = threading.Event(), threading.Event()

    def answer():
        blocked.set()
        assert resume.wait(4)
        return {"role": "assistant", "content": marker}

    stack.engine.output = marker.encode()
    stack.model.responses = [command("ReadFile", {"path": marker + ".txt"}), answer]
    request = run_body(session, external_run)
    run = owner.post("/runs", json=request).json()["request_id"]
    assert run != external_run
    assert blocked.wait(3)
    try:
        assert owner.post("/runs", json=request).json()["status"] == "running"
        assert owner.post("/runs", json={**request, "instruction": "different"}).status_code == 409
        for identifier in (external_run, run):
            assert owner.post("/runs", json=run_body("unknown-fixture", identifier)).status_code == 409
            for client in (legacy, other):
                assert client.get(f"/runs/{identifier}").status_code == 403
                assert client.get(f"/runs/{identifier}/events").status_code == 403
                assert client.get(f"/runs/{identifier}/stream").status_code == 403
                assert client.post(f"/runs/{identifier}/cancel", json={}).status_code == 403
                assert client.post("/runs", json=run_body("unknown-fixture", identifier)).status_code == 403
        live = owner.get(f"/runs/{run}/events").json()["events"]
        cursor = live[0]["seq"]
        assert marker in json.dumps(live)
        export = owner.get(f"/sessions/{session}/history")
        assert export.headers["content-type"].startswith("application/x-ndjson")
        assert marker not in export.text
    finally:
        resume.set()
    result = wait_result(owner, external_run)
    assert result["answer"] == marker
    response = owner.get(f"/runs/{run}/stream", headers={"Last-Event-ID": str(cursor)})
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events == owner.get(f"/runs/{run}/events", params={"after": cursor}).json()["events"]
    assert sum(event["type"] == "result" for event in events) == 1
    terminal = owner.get(f"/runs/{run}/stream", headers={"Last-Event-ID": str(events[-1]["seq"])})
    assert terminal.status_code == 200 and terminal.text == ""
    assert owner.post("/runs", json=request).json() == result

    assert owner.delete(f"/sessions/{session}").status_code == 200
    assert owner.delete(f"/sessions/{external_session}").status_code == 200
    assert owner.post("/runs", json=request).status_code == 409
    assert owner.post(f"/runs/{run}/cancel", json={}).status_code == 409
    assert owner.get(f"/runs/{run}").json()["status"] == "success"
    assert "answer" not in owner.get(f"/runs/{run}").json()
    after = owner.get(f"/runs/{run}/events", params={"after": cursor}).json()["events"]
    assert [event["seq"] for event in after] == [event["seq"] for event in events]
    assert marker not in json.dumps(after)
    history = owner.get(f"/sessions/{session}/history").text
    exported = [json.loads(line) for line in history.splitlines()]
    assert {event["source"] for event in exported} == {"harness", "sandbox"}
    assert any(event.get("usage") == {"prompt_tokens": 7, "completion_tokens": 2} for event in exported)
    operations = {event["operation_id"] for event in exported if event["type"] == "tool_call"}
    assert len(operations) == 1
    assert marker not in history
    for file in stack.state.glob("*.db*"):
        assert marker.encode() not in file.read_bytes(), file
    assert not stack.harness.protected.records and not stack.sandbox.mounted_journal.records


@pytest.mark.parametrize("receipt,error", [
    ({"status": "conflict", "outcomes": [{"status": "conflict", "path": "port.txt"}]}, "write_conflict"),
    (None, "write_outcome_unknown"),
])
def test_write_outcome_is_synchronized_before_completion(stack, receipt, error):
    owner = stack.clients["owner"]
    _, session = provision(owner)
    stack.helper.finish_lost = True
    stack.helper.receipt = receipt
    stack.model.responses = [command("RunCommand", {"command": "printf 8080 > port.txt"})]
    if error == "write_conflict":
        stack.model.responses.append({"role": "assistant", "content": "The original was preserved."})
    request = run_body(session)
    owner.post("/runs", json=request).raise_for_status()
    result = wait_result(owner, request["request_id"])
    assert result["status"] == "error" and result["error"] == error
    assert stack.helper.query_count == 1
    events = owner.get("/runs/ext-run/events").json()["events"]
    tool = next(event["result"] for event in events if event["type"] == "tool_result")
    assert tool["exit_code"] == 0
    if receipt:
        assert [item["decision"] for item in events if item["type"] == "write_decision"] == receipt["outcomes"]
        assert any(message["role"] == "tool" and "write_conflict" in message["content"]
                   for message in stack.model.messages[-1])
        assert "kept the current version" in stack.model.messages[-1][-1]["content"]
    assert owner.delete(f"/sessions/{session}").status_code == 200


def test_stale_tool_is_discarded_and_confirmed_write_runs(stack):
    owner = stack.clients["owner"]
    _, session = provision(owner)

    def stale():
        stack.helper.updates = [{"path": "port.txt", "version": "new-version"}]
        return command("WriteFile", {"path": "port.txt", "content": "old"})

    stack.helper.receipt = {"status": "applied", "outcomes": [{"status": "applied", "path": "port.txt"}]}
    stack.model.responses = [stale, command("WriteFile", {"path": "port.txt", "content": "new"}),
                             {"role": "assistant", "content": "written"}]
    owner.post("/runs", json=run_body(session)).raise_for_status()
    assert wait_result(owner, "ext-run")["status"] == "success"
    contents = [json.loads(cmd[-2]) for cmd in stack.engine.commands]
    assert contents == [{"path": "port.txt", "content": "new"}]
    assert any(message["role"] == "user" and "new-version" in message["content"]
               for message in stack.model.messages[1])
    assert stack.helper.started[0][2]["length"] == 3
    events = owner.get("/runs/ext-run/events").json()["events"]
    changed = next(event for event in events if event["type"] == "file_changed")
    assert changed["version"] == "0" and changed["file_version"] == "new-version"
    mount = stack.engine.mounts[0][0]
    assert mount["Target"] == "/work" and not mount["ReadOnly"]


def test_readonly_binding_and_lost_mount(stack):
    owner = stack.clients["owner"]
    _, session = provision(owner, readonly=True)
    stack.model.responses = [command("ReadFile", {"path": "port.txt"}),
                             {"role": "assistant", "content": "read"}]
    owner.post("/runs", json=run_body(session)).raise_for_status()
    assert wait_result(owner, "ext-run")["status"] == "success"
    assert stack.engine.mounts[0][0]["ReadOnly"]
    stack.helper.allowed = False
    owner.post("/runs", json=run_body(session, "later")).raise_for_status()
    result = wait_result(owner, "later")
    assert result["status"] == "error" and result["error"] == "mount_lost"
    assert len(stack.model.messages) == 2 and len(stack.engine.commands) == 1


def test_fixture_keeps_public_contract(stack):
    client = stack.clients["legacy"]
    session = "fixture"
    response = client.post("/sessions", json={"session_id": session})
    assert response.json() == {"version": "0", "session_id": session, "status": "ready"}
    stack.model.responses = [{"role": "assistant", "content": "legacy answer"}]
    client.post("/runs", json=run_body(session)).raise_for_status()
    result = wait_result(client, "ext-run")
    assert result["session_id"] == session and result["request_id"] == "ext-run"
    assert result["answer"] == "legacy answer"
    assert client.delete(f"/sessions/{session}").status_code == 200


def test_recovery_after_reopening_both_journals(stack, monkeypatch):
    from runtime.session_journal import SessionJournal
    from runtime.session_store import SessionStore

    owner = stack.clients["owner"]
    _, session = provision(owner)
    scope = stack.harness.owners
    run = scope.claim_run("ext-run", session, "owner" * 12)
    operation = run + "-0-0"
    mounted = stack.sandbox.mounted_journal
    harness = stack.harness.protected
    scopes = (
        (harness, run, run, {"request_id": run}),
        (mounted, "op:" + operation, operation, {"operation_id": operation, "tool": "WriteFile"}),
    )
    for store, key, trace, fields in scopes:
        assert store.reserve(key, fields, {"version": "0", "status": "running",
                             "session_id": session, **fields})[0]
        store.event(trace, "started", session_id=session)
        store.event(trace, "tool_call", operation_id=operation, tool="WriteFile",
                    arguments={"path": "fixture", "content": "something private"})
        store.db.close()
    scope.db.close()

    replacements = (
        (stack.harness, "owners", SessionStore()),
        (stack.harness, "protected", SessionJournal("harness-mounted")),
        (stack.sandbox, "mounted_journal", SessionJournal("sandbox-mounted")),
        (stack.sandbox, "mounted_sessions", {}),
    )
    for module, name, value in replacements:
        monkeypatch.setattr(module, name, value)
    try:
        stack.sandbox.recover()
        stack.harness.recover()
        stack.sandbox.recover()
        stack.harness.recover()
        result = owner.get(f"/runs/{run}").json()
        assert result["status"] == "error" and result["error"] == "harness_restarted"
        assert owner.post("/runs", json=run_body(session)).status_code == 409
        assert session not in stack.helper.bound
        export = owner.get(f"/sessions/{session}/history").text
        assert "something private" not in export
        events = [json.loads(line) for line in export.splitlines()]
        finals = {event["source"]: event["result"] for event in events if event["type"] == "result"}
        assert len([event for event in events if event["type"] == "result"]) == 2
        assert finals["sandbox"]["error"] == "sandbox_restarted"
        assert finals["harness"]["error"] == "harness_restarted"
        assert all(result["duration_seconds"] >= 0 for result in finals.values())
    finally:
        for _, _, value in replacements[:-1]:
            value.db.close()


def test_close_waiting_model_drops_private_data_and_prevents_tool(stack):
    owner = stack.clients["owner"]
    _, session = provision(owner)
    private = "ClosedWhileModelWasWaiting"
    model_waiting, allow_model = threading.Event(), threading.Event()

    def late_answer():
        model_waiting.set()
        assert allow_model.wait(4)
        return command("WriteFile", {"path": "port.txt", "content": private})

    stack.model.responses = [late_answer]
    request = {**run_body(session), "instruction": private}
    owner.post("/runs", json=request).raise_for_status()
    assert model_waiting.wait(3)
    try:
        assert owner.delete(f"/sessions/{session}").status_code == 200
        assert owner.post("/runs", json=request).status_code == 409
        assert not stack.harness.protected.records
    finally:
        allow_model.set()
    events = owner.get("/runs/ext-run/events").json()["events"]
    assert [event["type"] for event in events] == ["started", "result"]
    assert events[-1]["result"]["status"] == "cancelled"
    assert not stack.engine.commands


def test_sandbox_lifespan_detaches_mounted_sessions(stack):
    owner = stack.clients["owner"]
    _, session = provision(owner)
    assert session in stack.helper.bound
    # FastAPI lifecycle callbacks run on an event loop even when defined with def.
    with TestClient(stack.sandbox.app):
        assert session not in stack.helper.bound
        assert stack.sandbox.mounted_journal.get("session:" + session)["status"] == "released"

    _, another = provision(owner, "shutdown-fixture")
    assert another in stack.helper.bound
    # Run the shutdown callback on the actual ASGI event loop with a live binding.
    with TestClient(stack.sandbox.app):
        _, last = provision(owner, "shutdown-fixture-live")
        assert last in stack.helper.bound
    assert last not in stack.helper.bound
    assert stack.sandbox.mounted_journal.get("session:" + last)["status"] == "released"
