import asyncio
import base64
import hashlib
import io
import json
import os
import re
import socket
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import docker
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from runtime.streaming import stream_events

from runtime.common import Journal, Operation, Session, check_version
from runtime.mount import MountClient, MountError
from runtime.session_journal import SessionJournal

mounted_journal = SessionJournal("sandbox-mounted")
mount_client = MountClient.from_env()
mounted_sessions = {}
MOUNT_ROOT = PurePosixPath(os.getenv("MOUNT_ROOT", "/var/lib/itmo-mount"))


@dataclass
class MountedSandbox:
    path: str
    generation: str
    readonly: bool
    container: object = None
    operation: str | None = None
    closing: bool = False
    close_lock: threading.Lock = field(default_factory=threading.Lock)


def mount_call(method, *args):
    return asyncio.run(method(*args))


app = FastAPI(title="Harness sandbox protocol", version="0")
journal = Journal("sandbox")
engine = docker.from_env(timeout=15)
containers = {}
guard = threading.RLock()
active = set()
IMAGE = os.environ.get("SANDBOX_IMAGE", "itmo-sandbox:0.1")
LABEL = "itmo.checkpoint.sandbox"
NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "itmo-checkpoint")


@app.on_event("startup")
async def recover_at_startup():
    await asyncio.to_thread(recover)


def recover():
    # A reserved operation is never executed again after an uncertain crash.
    for container in engine.containers.list(all=True, filters={"label": LABEL + "=" + NAMESPACE}):
        container.remove(force=True)
    for key, record in mounted_journal.all():
        if key.startswith("session:"):
            if record["status"] not in {"released"}:
                try:
                    mount_call(mount_client.detach, record["session_id"], None)
                except MountError:
                    continue
                mounted_journal.put(key, {**record, "status": "released"})
        elif record.get("status") == "running":
            mounted_journal.complete(key, key[3:], {
                **record, "status": "error", "error": "sandbox_restarted",
            })
    for key, record in journal.all():
        if key.startswith("session:") and record.get("status") in ("ready", "running"):
            journal.put(key, {"version": "0", "session_id": key[8:], "status": "released"})
            continue
        if record.get("status") == "running":
            record.update(status="error", error="sandbox_error", detail="manager restarted; outcome unknown")
            if key.startswith("op:"):
                journal.complete(key, key[3:], record)
            else:
                journal.put(key, record)


@app.on_event("shutdown")
async def shutdown_at_exit():
    await asyncio.to_thread(shutdown)


def shutdown():
    for session_id in list(mounted_sessions):
        try:
            close_mounted(session_id)
        except MountError:
            pass
    for container in list(containers.values()):
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass


@app.get("/health")
def health():
    engine.ping()
    engine.images.get(IMAGE)
    return {"status": "ready", "version": "0"}


def create_container(session_id, mounted=None):
    tmpfs = {"/tmp": "rw,noexec,nosuid,nodev,size=16m,uid=1000,gid=1000,mode=1777"}
    mounts = []
    if mounted is None:
        tmpfs["/work"] = "rw,noexec,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=0700"
    else:
        mounts.append(docker.types.Mount(target="/work", source=mounted.path, type="bind",
                                         read_only=mounted.readonly, propagation="rprivate"))
    return engine.containers.run(
        IMAGE, ["sleep", "infinity"], detach=True, user="1000:1000",
        name=NAMESPACE + ("-mounted-" if mounted else "-sb-") + session_id,
        labels={LABEL: NAMESPACE},
        network_mode="none", read_only=True, cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"], pids_limit=64,
        mem_limit="256m", memswap_limit="256m", nano_cpus=500_000_000,
        tmpfs=tmpfs, mounts=mounts, log_config={"type": "none"},
        working_dir="/work", environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
    )


@app.post("/sessions")
def provision(request: Session):
    check_version(request)
    if request.mount is not None:
        raise HTTPException(422, "mounted session requires the mounted protocol")
    for path, content in request.files.items():
        if path.startswith("/") or any(p in ("", ".", "..", ".git") for p in path.split("/")):
            raise HTTPException(422, "unsafe file path")
        try:
            base64.b64decode(content, validate=True)
        except ValueError:
            raise HTTPException(422, "invalid base64")
    if sum(len(v) for v in request.files.values()) > 8_000_000:
        raise HTTPException(413, "fixture too large")
    with guard:
        fresh, record = journal.reserve("session:" + request.session_id, request.model_dump(exclude_none=True),
                                        {"version": "0", "status": "running", "session_id": request.session_id})
        if not fresh:
            return record
        container = create_container(request.session_id)
        containers[request.session_id] = container
        try:
            blob = io.BytesIO()
            with tarfile.open(fileobj=blob, mode="w") as archive:
                for path, value in request.files.items():
                    data = base64.b64decode(value)
                    info = tarfile.TarInfo(path)
                    info.size, info.uid, info.gid, info.mode = len(data), 1000, 1000, 0o600
                    archive.addfile(info, io.BytesIO(data))
            setup = engine.api.exec_create(container.id, ["/usr/local/bin/python", "-I", "-S", "-c", "import sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/work',filter='data')"], stdin=True, user="1000:1000")
            channel = engine.api.exec_start(setup["Id"], socket=True)
            channel._sock.settimeout(10)
            channel._sock.sendall(blob.getvalue())
            channel._sock.shutdown(socket.SHUT_WR)
            while channel.read(65536):
                pass
            channel.close()
            if engine.api.exec_inspect(setup["Id"])["ExitCode"] != 0:
                raise RuntimeError("fixture setup failed")
        except Exception:
            container.remove(force=True)
            containers.pop(request.session_id, None)
            journal.put("session:" + request.session_id, {"version": "0", "session_id": request.session_id, "status": "error", "error": "sandbox_error"})
            raise
        record["status"] = "ready"
        journal.put("session:" + request.session_id, record)
        return record


@app.get("/sessions/{session_id}")
def legacy_session_status(session_id: str):
    return journal.get("session:" + session_id)


class MountedProvision(Session):
    client_session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


@app.post("/mounted-sessions")
def provision_mounted(request: MountedProvision):
    check_version(request)
    if not re.fullmatch(r"[a-f0-9]{32}", request.session_id) or request.mount is None:
        raise HTTPException(422, "invalid mount descriptor")
    session_key = "session:" + request.session_id
    with guard:
        fresh, record = mounted_journal.reserve(session_key, request.model_dump(),
            {"version": "0", "session_id": request.session_id, "status": "running"})
        if not fresh and record["status"] != "error":
            return record
        try:
            descriptor = {**request.mount.model_dump(exclude={"version"}),
                          "client_session_id": request.client_session_id}
            attached = mount_call(mount_client.attach, request.session_id, descriptor)
            mount_path = PurePosixPath(attached["path"])
            if (not mount_path.is_relative_to(MOUNT_ROOT) or mount_path == MOUNT_ROOT
                    or ".." in mount_path.parts or attached["generation"] != request.mount.generation
                    or attached["readonly"] is not request.mount.readonly):
                raise MountError("mount service returned an invalid binding")
            mounted_sessions[request.session_id] = MountedSandbox(
                str(mount_path), attached["generation"], attached["readonly"],
            )
        except (MountError, KeyError, TypeError):
            try:
                mount_call(mount_client.detach, request.session_id, request.mount.generation)
            except MountError:
                pass
            mounted_journal.put(session_key, {**record, "status": "error", "error": "mount_lost"})
            raise HTTPException(503, "mounted folder unavailable")
        record["status"] = "ready"
        mounted_journal.put(session_key, record)
        return record


def remove_container(container):
    if container:
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass


def close_mounted(session_id):
    with guard:
        mounted = mounted_sessions.get(session_id)
        if mounted:
            mounted.closing = True
    if not mounted:
        return close_mount_binding(session_id, None)
    with mounted.close_lock:
        return close_mount_binding(session_id, mounted)


def close_mount_binding(session_id, mounted):
    try:
        try:
            record = mounted_journal.get("session:" + session_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return
            raise
        if record["status"] == "released":
            return
        if mounted and mounted.operation:
            try:
                mount_call(mount_client.cancel, session_id, mounted.generation, mounted.operation)
            except MountError:
                pass
        if mounted:
            remove_container(mounted.container)
        mount_call(mount_client.detach, session_id, mounted.generation if mounted else None)
        with guard:
            mounted_sessions.pop(session_id, None)
            mounted_journal.put("session:" + session_id, {
                "version": "0", "session_id": session_id, "status": "released",
            })
    finally:
        with guard:
            for key, record in mounted_journal.session_records(session_id):
                if key.startswith("op:") and record["status"] == "running":
                    mounted_journal.complete(key, key[3:], {
                        **record, "status": "error", "error": "write_outcome_unknown",
                    })
            mounted_journal.drop_session(session_id)


@app.delete("/mounted-sessions/{session_id}")
def release_mounted(session_id: str):
    try:
        close_mounted(session_id)
    except MountError:
        raise HTTPException(503, "mount release could not be confirmed")
    return {"version": "0", "status": "released"}


@app.post("/mounted-sessions/{session_id}/cancel")
def cancel_mounted(session_id: str):
    return release_mounted(session_id)


@app.get("/mounted-sessions/{session_id}/history")
def mounted_history(session_id: str):
    return {"events": mounted_journal.technical_history(session_id)}


@app.post("/mounted-sessions/{session_id}/changes")
def mounted_changes(session_id: str, data: dict):
    with guard:
        mounted = mounted_sessions.get(session_id)
        if not mounted or mounted.closing:
            raise HTTPException(409, "mounted session unavailable")
    try:
        if not mount_call(mount_client.check, session_id, mounted.generation):
            raise MountError("mount unavailable")
        changes = mount_call(mount_client.changes, session_id, mounted.generation, data["run_id"])
    except MountError:
        raise HTTPException(503, "mount unavailable")
    return {"changes": changes}


def get_container(session_id):
    with guard:
        if session_id not in containers:
            raise HTTPException(404, "sandbox absent or released")
        return containers[session_id]


def destroy(session_id):
    with guard:
        container = containers.get(session_id)
    if container:
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        with guard:
            if containers.get(session_id) is container:
                containers.pop(session_id)
    try:
        journal.put("session:" + session_id, {"version": "0", "session_id": session_id, "status": "released"})
    except Exception:
        pass


@app.delete("/sessions/{session_id}")
def release(session_id: str):
    destroy(session_id)
    return {"version": "0", "status": "released"}


@app.post("/sessions/{session_id}/cancel")
def cancel(session_id: str):
    destroy(session_id)
    return {"version": "0", "status": "cancelled"}


FILE_TOOL = '''import base64,json,pathlib,sys
a=json.loads(sys.argv[1]); p=(pathlib.Path('/work')/a['path']).resolve()
if not p.is_relative_to('/work') or '.git' in p.parts: raise ValueError('path outside allowed workdir')
if sys.argv[2]=='ReadFile':
 if p.stat().st_size>131072: raise ValueError('file too large')
 print(p.read_text())
else:
 p.parent.mkdir(parents=True,exist_ok=True); p.write_text(a['content']); print('written')
'''


@app.post("/exec")
def execute(request: Operation):
    if request.run_id is not None:
        raise HTTPException(422, "invalid fixture operation")
    return execute_operation(request)


@app.post("/mounted-exec")
def execute_mounted(request: Operation):
    if (request.run_id is None
            or not re.fullmatch(r"[a-f0-9]{32}", request.session_id)
            or not re.fullmatch(re.escape(request.run_id) + r"-[0-9]+-[0-9]+", request.operation_id)):
        raise HTTPException(422, "invalid mounted operation")
    return execute_operation(request, mounted=True)


def execute_operation(request, mounted=False):
    check_version(request)
    allowed = {"RunCommand": {"command"}, "ReadFile": {"path"}, "WriteFile": {"path", "content"}}
    if set(request.arguments) != allowed[request.tool] or not all(isinstance(x, str) for x in request.arguments.values()):
        raise HTTPException(422, "invalid tool arguments")
    if sum(len(x) for x in request.arguments.values()) > 131072:
        raise HTTPException(413, "arguments too large")
    started = time.monotonic()
    session = request.session_id
    operation = request.operation_id
    key = "op:" + operation
    store = mounted_journal if mounted else journal
    identity = (mounted, session)
    binding = None
    with guard:
        if mounted:
            binding = mounted_sessions.get(session)
            if not binding or binding.closing:
                raise HTTPException(409, "mounted session unavailable")
        record = {"version": "0", "operation_id": operation, "status": "running"}
        if binding:
            record.update(session_id=session, request_id=request.run_id, tool=request.tool)
        fresh, record = store.reserve(key, request.model_dump(exclude_none=True), record)
        if not fresh:
            return record
        if identity in active:
            record.update(status="error", error="sandbox_error", detail="session busy")
            store.complete(key, operation, record)
            return record
        active.add(identity)
        if binding:
            binding.operation = operation
    store.event(operation, "started", session_id=session)
    if binding:
        store.event(operation, "tool_call", operation_id=operation, tool=request.tool)
    expired = threading.Event()
    timer = None
    begun = False
    container = None

    def expire():
        expired.set()
        if binding:
            try:
                close_mounted(session)
            except MountError:
                pass
        else:
            destroy(session)

    try:
        if binding:
            generation = binding.generation
            if not mount_call(mount_client.check, session, generation):
                raise MountError("mount unavailable")
            expected = None
            if request.tool == "WriteFile":
                if binding.readonly:
                    record.update(status="error", error="invalid_tool")
                    return record
                content = request.arguments["content"].encode()
                expected = {"path": request.arguments["path"], "length": len(content),
                            "sha256": hashlib.sha256(content).hexdigest()}
            mount_call(mount_client.begin, session, generation, request.run_id,
                       operation, request.tool, expected)
            begun = True
            with guard:
                if binding.closing:
                    raise MountError("mounted session closing")
                container = binding.container = create_container(session, binding)
        else:
            container = get_container(session)
        timer = threading.Timer(request.timeout_seconds, expire)
        timer.start()
        cmd = ["/bin/sh", "-c", request.arguments["command"]] if request.tool == "RunCommand" else [
            "/usr/local/bin/python", "-I", "-S", "-c", FILE_TOOL, json.dumps(request.arguments), request.tool]
        execution = engine.api.exec_create(container.id, cmd, workdir="/work", user="1000:1000")
        chunks, size = [], 0
        for chunk in engine.api.exec_start(execution["Id"], stream=True, demux=True):
            for index, data in enumerate(chunk):
                if data:
                    if size < 131072:
                        chunks.append((index, data[:131072-size]))
                    size += len(data)
                    if size > 2_000_000:
                        if binding:
                            remove_container(container)
                        else:
                            destroy(session)
                        raise RuntimeError("output limit exceeded")
        exit_code = engine.api.exec_inspect(execution["Id"])["ExitCode"]
        record.update(status="success" if exit_code == 0 else "error", exit_code=exit_code,
                      stdout=b"".join(d for i, d in chunks if i == 0).decode(errors="replace"),
                      stderr=b"".join(d for i, d in chunks if i == 1).decode(errors="replace"),
                      truncated=size > 131072)
        if exit_code != 0:
            record["error"] = "command_error"
        if binding:
            remove_container(container)
            with guard:
                binding.container = None
                if binding.closing or expired.is_set():
                    raise MountError("operation cancelled")
            try:
                receipt = mount_call(mount_client.finish, session, generation, operation, exit_code == 0)
            except MountError:
                receipt = mount_call(mount_client.query, session, generation, operation)
            if not isinstance(receipt, dict) or receipt.get("status") not in {
                "applied", "conflict", "proposal", "unknown", "cancelled", "unchanged",
            } or not isinstance(receipt.get("outcomes"), list):
                raise MountError("missing operation receipt")
            for effect in receipt["outcomes"]:
                store.event(operation, "write_decision", operation_id=operation, decision=effect)
            record["write_decisions"] = receipt["outcomes"]
            errors = {"unknown": "write_outcome_unknown", "conflict": "write_conflict",
                      "proposal": "write_unverified"}
            if (receipt["status"] == "cancelled" and exit_code == 0
                    or request.tool == "WriteFile" and exit_code == 0 and receipt["status"] == "unchanged"):
                record.update(status="error", error="write_outcome_unknown")
            elif error := errors.get(receipt["status"]):
                record.update(status="error", error=error)
    except Exception as exc:
        if begun:
            try:
                mount_call(mount_client.cancel, session, binding.generation, operation)
            except MountError:
                pass
        error = ("write_outcome_unknown" if begun else "mount_lost") if binding else "sandbox_error"
        record.update(status="error", error="timeout" if expired.is_set() else error, detail=type(exc).__name__)
    finally:
        if timer:
            timer.cancel()
            timer.join(timeout=20)
        if expired.is_set():
            record.update(status="error", error="timeout")
        if binding:
            try:
                remove_container(container)
            except docker.errors.DockerException:
                record.update(status="error", error="sandbox_error")
        with guard:
            if binding:
                binding.container = None
                binding.operation = None
            active.discard(identity)
            record["duration_seconds"] = time.monotonic() - started
            if binding:
                record = store.complete(key, operation, record)
            else:
                store.complete(key, operation, record)
    return record


@app.get("/operations/{operation_id}/events")
def events(operation_id: str, after: int = 0):
    return {"version": "0", "events": journal.events(operation_id, after)}


@app.get("/sessions/{session_id}/files")
def collect(session_id: str):
    container = get_container(session_id)
    script = """import os,pathlib,json,base64,stat
files={}; total=0
for root,dirs,names in os.walk('/work',followlinks=False):
 dirs[:]=[d for d in dirs if not pathlib.Path(root,d).is_symlink()]
 for name in names:
  p=pathlib.Path(root,name)
  if p.is_symlink(): continue
  fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  try:
   info=os.fstat(fd)
   if not stat.S_ISREG(info.st_mode): continue
   if info.st_size>4*1024*1024 or total+info.st_size>8*1024*1024 or len(files)>=4096: raise SystemExit(3)
   with os.fdopen(os.dup(fd),'rb') as stream: data=stream.read(min(4*1024*1024,8*1024*1024-total)+1)
   if len(data)>4*1024*1024 or total+len(data)>8*1024*1024: raise SystemExit(3)
   total+=len(data)
   files[str(p.relative_to('/work'))]=base64.b64encode(data).decode()
  finally: os.close(fd)
print(json.dumps(files))
"""
    timer = threading.Timer(15, lambda: destroy(session_id))
    timer.start()
    try:
        result = container.exec_run(["/usr/local/bin/python", "-I", "-S", "-c", script], user="1000:1000")
    finally:
        timer.cancel()
    if result.exit_code == 3:
        raise HTTPException(413, "collection limit: 4 MiB per file, 8 MiB total, 4096 files")
    if result.exit_code != 0:
        raise HTTPException(500, "file collection failed")
    return {"version": "0", "files": json.loads(result.output)}


@app.get("/operations/{operation_id}/stream")
def stream(operation_id: str, request: Request, after: int = 0):
    status = journal.get("op:" + operation_id)["status"]
    try:
        cursor = int(request.headers.get("last-event-id", after))
    except ValueError:
        raise HTTPException(422, "invalid event cursor")
    return StreamingResponse(stream_events(lambda value: journal.events(operation_id, value), cursor,
                                           terminal=status != "running"), media_type="text/event-stream")
