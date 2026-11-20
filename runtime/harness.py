import json
import os
import threading
import time

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from runtime.common import Journal, Run, Session, call, check_version
from runtime.session_journal import SessionJournal
from runtime.session_store import SESSION_KEY_HEADER, SessionStore

app = FastAPI(title="Agent harness", version="0")
journal = Journal("harness")
protected = SessionJournal("harness-mounted")
owners = SessionStore()
SANDBOX = os.getenv("SANDBOX_URL", "http://sandbox-manager:8000")
MODEL = os.getenv("MODEL_URL", "http://llm-proxy:8000")
guard = threading.RLock()
busy = {}
cancelled = set()
active_operations = set()
SESSION_KEY = Header(default=None, alias=SESSION_KEY_HEADER)


TOOLS = [{"type": "function", "function": {"name": name, "description": description,
    "parameters": {"type": "object", "properties": {p: {"type": "string"} for p in fields},
                   "required": fields, "additionalProperties": False}}}
    for name, description, fields in [
        ("ReadFile", "Read a UTF-8 file relative to /work.", ["path"]),
        ("WriteFile", "Write UTF-8 text to a file relative to /work.", ["path", "content"]),
        ("RunCommand", "Execute a command in /work. The result separately provides exit_code, stdout and stderr. Run existing programs directly without output merging or trailing status-print commands.", ["command"])]]


def finish(store, run_id, status, **detail):
    with guard:
        record = store.get(run_id)
        if record["status"] != "running":
            return record
        record.update(status=status, **detail)
        store.complete(run_id, run_id, record)
        busy.pop((store, record["session_id"]), None)
        return record


def sandbox_release(session, *, mounted=False, cancel=False):
    collection = "mounted-sessions" if mounted else "sessions"
    path = f"/{collection}/{session}"
    return (
        call(SANDBOX, path + "/cancel", {}) if cancel
        else call(SANDBOX, path, method="DELETE")
    )


@app.on_event("startup")
def recover():
    for store in (journal, protected):
        for key, record in store.all():
            if record.get("status") == "running":
                try:
                    sandbox_release(record["session_id"], mounted=store is protected, cancel=True)
                except (HTTPException, httpx.HTTPError, OSError):
                    pass
                finish(store, key, "error", error="harness_restarted")
    for session, _ in owners.owned_sessions():
        owners.set_status(session, "closing")
        try:
            sandbox_release(session, mounted=True)
        except (HTTPException, httpx.HTTPError, OSError):
            continue
        owners.set_status(session, "closed")


@app.get("/health")
def health():
    call(SANDBOX, "/health")
    model = call(MODEL, "/health")
    return {"status": "ready", "version": "0", "model_mode": model["mode"], "model": model.get("model")}


@app.post("/sessions")
def provision(request: Session, key: str | None = SESSION_KEY):
    check_version(request)
    with guard:
        descriptor = request.mount
        if descriptor is not None and owners.session(request.session_id, key) is None:
            try:
                call(SANDBOX, f"/sessions/{request.session_id}")
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            else:
                raise HTTPException(409, "session already reserved")
        identifier, state = owners.claim_session(
            request.session_id, key, descriptor.model_dump_json() if descriptor is not None else None,
        )
        if descriptor is None:
            return call(SANDBOX, "/sessions", request.model_dump(exclude_none=True))
        if state == "ready":
            return {"version": "0", "session_id": identifier, "status": state}
        payload = {**request.model_dump(exclude_none=True), "session_id": identifier,
                   "client_session_id": request.session_id}
        result = call(SANDBOX, "/mounted-sessions", payload)
        if result.get("status") != "ready":
            raise HTTPException(409, "mounted session not ready")
        owners.set_status(identifier, "ready")
        return result


def agent_loop(request, store=None):
    if store is None:
        store = journal
    run_id = request.request_id
    mounted = store is protected
    identity = (store, run_id)
    started = time.monotonic()
    messages = [{"role": "system", "content":
        "Complete the task using tools. Files are in /work. Check command exit codes. "
        "Do not invent missing data. Run Python via python, scripts via sh. "
        "RunCommand already returns the exit code, stdout and stderr separately. "
        "When executing an existing program or test suite, run it directly in its own tool call. "
        "Do not combine that invocation with ls, cat, echo, postprocessing or output redirection. "
        "Use separate tool calls for inspection and subsequent file creation; preserve the program exit code. "
        "When the requested files have been written, return a final answer without tool calls."},
        {"role": "user", "content": request.instruction}]
    observed_changes = set()
    unapplied_write = None

    def complete(status, **data):
        return finish(store, run_id, status, duration_seconds=time.monotonic() - started, **data)

    def deadline():
        with guard:
            if store.get(run_id)["status"] != "running":
                return
            cancelled.add(identity)
            error = "write_outcome_unknown" if identity in active_operations else "timeout"
            if mounted:
                owners.set_status(request.session_id, "halted")
        try:
            sandbox_release(request.session_id, mounted=mounted, cancel=True)
        except (HTTPException, httpx.HTTPError, OSError):
            pass
        finally:
            complete("error", error=error)

    def check_changes():
        nonlocal observed_changes
        if not mounted:
            return []
        try:
            changes = call(SANDBOX, f"/mounted-sessions/{request.session_id}/changes",
                           {"run_id": run_id}, timeout=10)["changes"]
        except (HTTPException, httpx.HTTPError, OSError):
            with guard:
                if identity not in cancelled:
                    owners.set_status(request.session_id, "halted")
                    complete("error", error="mount_lost")
            return None
        current = {(change["path"], change["version"]) for change in changes}
        new = [change for change in changes if (change["path"], change["version"]) not in observed_changes]
        observed_changes = current
        for change in new:
            store.event(run_id, "file_changed", path=change["path"], file_version=change["version"])
        return new

    def notify(changes):
        if changes:
            payload = json.dumps(changes, ensure_ascii=False)
            messages.append({"role": "user", "content":
                "The client saved files during this task. Read their current versions before acting. "
                f"Changed files and version identifiers: {payload}"})

    timer = threading.Timer(request.timeout_seconds, deadline)
    timer.start()
    try:
        for step in range(request.max_steps):
            if identity in cancelled:
                return
            changes = check_changes()
            if changes is None:
                return
            notify(changes)
            response = call(MODEL, "/completion", {"messages": messages, "tools": TOOLS,
                "expected_mode": request.expected_mode, "expected_model": request.expected_model})
            if identity in cancelled:
                return
            message = response["message"]
            store.event(run_id, "model", step=step, usage=response.get("usage", {}),
                        model=response.get("model"), mode=response.get("mode"))
            if response.get("error"):
                complete("error", error="model_error", detail=response["error"])
                return
            changes = check_changes()
            if changes is None:
                return
            if changes:
                notify(changes)
                continue
            calls = message.get("tool_calls") or []
            if not calls:
                if unapplied_write:
                    complete("error", error=unapplied_write, detail=message.get("content") or "Write not applied")
                else:
                    complete("success", answer=message.get("content") or "", model_mode=response.get("mode"))
                return
            if len(calls) > 16:
                raise ValueError("too many tool calls")
            messages.append(message)
            interrupted = False
            for index, tool in enumerate(calls):
                if identity in cancelled:
                    return
                changes = check_changes() if index else []
                if changes is None:
                    return
                if changes:
                    pending = calls[index:]
                    for skipped in pending:
                        messages.append({"role": "tool", "tool_call_id": skipped["id"],
                                         "content": "Skipped because the client changed a file."})
                    notify(changes)
                    interrupted = True
                    break
                name = tool["function"]["name"]
                args = json.loads(tool["function"]["arguments"])
                op = f"{run_id}-{step}-{index}"
                store.event(run_id, "tool_call", operation_id=op, tool=name, arguments=args)
                operation = {"version": "0", "session_id": request.session_id,
                             "operation_id": op, "tool": name, "arguments": args, "timeout_seconds": 20}
                if mounted:
                    operation["run_id"] = run_id
                with guard:
                    if identity in cancelled:
                        return
                    if mounted and name in ("RunCommand", "WriteFile"):
                        active_operations.add(identity)
                try:
                    result = call(SANDBOX, "/mounted-exec" if mounted else "/exec", operation)
                except (HTTPException, httpx.HTTPError, OSError) as exc:
                    if isinstance(exc, HTTPException) and exc.status_code in (400, 413, 422):
                        result = {"status": "error", "error": "invalid_tool"}
                    elif mounted:
                        with guard:
                            if identity not in cancelled:
                                complete("error", error="write_outcome_unknown")
                        return
                    else:
                        raise
                with guard:
                    if identity in cancelled:
                        return
                    store.event(run_id, "tool_result", operation_id=op, result=result)
                    for decision in result.get("write_decisions", []):
                        store.event(run_id, "write_decision", operation_id=op, decision=decision)
                    active_operations.discard(identity)
                error = result.get("error")
                if error in {"timeout", "sandbox_error", "mount_lost", "write_outcome_unknown"}:
                    complete("error", error=error)
                    return
                messages.append({"role": "tool", "tool_call_id": tool["id"], "content": json.dumps(result)})
                if error in {"write_conflict", "write_unverified"}:
                    unapplied_write = error
                    for skipped in calls[index + 1:]:
                        messages.append({"role": "tool", "tool_call_id": skipped["id"],
                                         "content": "Skipped after a write was not applied."})
                    messages.append({"role": "user", "content":
                        "The Mac kept the current version of the file. Read it before reporting the "
                        "result. The requested write was not applied. Do not repeat it."})
                    interrupted = True
                    break
            if interrupted:
                continue
        complete("error", error="step_limit")
    except Exception as exc:
        complete("error", error="transport_error" if isinstance(exc, (HTTPException, OSError, httpx.HTTPError)) else "model_error",
                 detail=type(exc).__name__)
    finally:
        timer.cancel()
        if timer is not threading.current_thread():
            timer.join()
        with guard:
            cancelled.discard(identity)
            active_operations.discard(identity)


def lookup(store, run_id):
    try:
        return store.get(run_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return None


@app.post("/runs")
def run(request: Run, key: str | None = SESSION_KEY):
    check_version(request)
    if os.getenv("HARNESS", "sequential") != "sequential":
        raise HTTPException(503, "unsupported harness")
    with guard:
        identity = owners.session(request.session_id, key)
        mounted_run = owners.run(request.request_id, key)
        store = journal
        run_id, session_id = request.request_id, request.session_id
        if identity and identity[1]:
            session_id, _, state = identity
            if state != "ready":
                raise HTTPException(409, "session is not open")
            if (mounted_run and mounted_run[1] != session_id) or lookup(journal, run_id):
                raise HTTPException(409, "request identifier already used")
            store = protected
            existing = lookup(store, mounted_run[0]) if mounted_run else None
        else:
            if mounted_run:
                raise HTTPException(409, "request identifier already used")
            existing = lookup(store, run_id)
        if existing is None:
            model = call(MODEL, "/health", timeout=5)
            if model.get("mode") != request.expected_mode:
                raise HTTPException(409, "configured model mode differs from requested mode")
            if request.expected_mode == "real" and (not request.expected_model or model.get("model") != request.expected_model):
                raise HTTPException(409, "configured model differs from requested model")
            if (store, session_id) in busy:
                raise HTTPException(409, "one active run per session")
        if store is protected:
            run_id = owners.claim_run(request.request_id, request.session_id, key)
        internal = request.model_copy(update={"request_id": run_id, "session_id": session_id})
        fresh, record = store.reserve(run_id, internal.model_dump(),
            {"version": "0", "request_id": run_id, "session_id": session_id, "status": "running"})
        if fresh:
            busy[(store, session_id)] = run_id
            store.event(run_id, "started", session_id=session_id)
            threading.Thread(target=agent_loop, args=(internal, store), daemon=True).start()
        return record


def run_scope(run_id, key):
    mounted = owners.run(run_id, key)
    if mounted:
        return protected, mounted[0], mounted[2]
    return journal, run_id, None


@app.get("/runs/{run_id}")
def status(run_id: str, key: str | None = SESSION_KEY):
    with guard:
        store, run_id, _ = run_scope(run_id, key)
        return store.get(run_id)


@app.get("/runs/{run_id}/events")
def events(run_id: str, after: int = 0, key: str | None = SESSION_KEY):
    with guard:
        store, run_id, _ = run_scope(run_id, key)
        store.get(run_id)
        return {"version": "0", "events": store.events(run_id, after)}


@app.post("/runs/{run_id}/cancel")
def cancel(run_id: str, key: str | None = SESSION_KEY):
    with guard:
        store, run_id, state = run_scope(run_id, key)
        if store is protected and state != "ready":
            raise HTTPException(409, "session is not open")
        record = store.get(run_id)
        if record["status"] != "running":
            return record
        identity = (store, run_id)
        cancelled.add(identity)
        detail = {"error": "write_outcome_unknown"} if identity in active_operations else {}
        if store is protected:
            owners.set_status(record["session_id"], "halted")
    try:
        sandbox_release(record["session_id"], mounted=store is protected, cancel=True)
    finally:
        finish(store, run_id, "cancelled", **detail)
    return store.get(run_id)


@app.get("/sessions/{session_id}/files")
def collect(session_id: str, key: str | None = SESSION_KEY):
    with guard:
        if (identity := owners.session(session_id, key)) and identity[1]:
            raise HTTPException(409, "mounted files stay on the client")
    return call(SANDBOX, f"/sessions/{session_id}/files")


@app.get("/sessions/{session_id}/history")
def history(session_id: str, key: str | None = SESSION_KEY):
    with guard:
        identity = owners.session(session_id, key)
        if not identity or not identity[1]:
            raise HTTPException(404, "unknown mounted session")
        identifier = identity[0]
        events = [{"source": "harness", **event} for event in protected.technical_history(identifier)]
    events.extend({"source": "sandbox", **event} for event in call(
        SANDBOX, f"/mounted-sessions/{identifier}/history",
    )["events"])
    events.sort(key=lambda event: (event["time"], event["source"], event["seq"]))
    return StreamingResponse((json.dumps(event) + "\n" for event in events),
                             media_type="application/x-ndjson")


@app.delete("/sessions/{session_id}")
def release(session_id: str, key: str | None = SESSION_KEY):
    with guard:
        identity = owners.session(session_id, key)
        mounted = bool(identity and identity[1])
        store = protected if mounted else journal
        if mounted:
            session_id, _, state = identity
            if state == "closed":
                return {"version": "0", "status": "released"}
            owners.set_status(session_id, "closing")
        run_id = busy.get((store, session_id))
        if run_id:
            identity = (store, run_id)
            cancelled.add(identity)
            if mounted:
                detail = {"error": "write_outcome_unknown"} if identity in active_operations else {}
                finish(store, run_id, "cancelled", **detail)
        if mounted:
            protected.drop_session(session_id)
    try:
        result = sandbox_release(session_id, mounted=mounted)
    finally:
        if run_id and not mounted:
            finish(store, run_id, "cancelled")
    if mounted:
        owners.set_status(session_id, "closed")
    return result
