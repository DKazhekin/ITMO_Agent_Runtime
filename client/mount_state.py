"""Staged workspace transactions and file publication on the client."""

from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from threading import RLock
import uuid

from client.mount_files import path_parts
from runtime.mount_wire import digest


@dataclass
class Edit:
    path: str
    baseline: str
    draft: Path | None
    kind: str
    permissions: int = 0o600
    proposal_only: bool = False


@dataclass
class Transaction:
    run: str
    identifier: str
    kind: str
    expected: dict | None
    directory: Path
    edits: dict = field(default_factory=dict)
    received: list = field(default_factory=list)
    renames: dict = field(default_factory=dict)
    handles: int = 0
    unstable: bool = False
    finishing: bool = False
    state: str = "pending"
    result: dict | None = None


class MountState:
    def __init__(self, workspace, staging, *, readonly, coordinator, quota_bytes, hints=None):
        self.workspace = workspace
        self.staging = staging
        self.readonly = readonly
        self.coordinator = coordinator
        self.quota_bytes = quota_bytes
        self.hints = hints
        self.current = None
        self.operations = {}
        self.baselines = {}
        self.blocked = set()
        self.change_events = {}
        self.seq = 0
        self.lock = RLock()
        self.connected = True
        self.proposals = {}

    def begin(self, run_id, operation_id, kind, expected):
        if self.current is not None or operation_id in self.operations or not self.connected:
            raise ValueError("Mount generation or operation unavailable")
        if len(self.operations) >= 4096:
            raise ValueError("Mount operation quota exceeded")
        directory = self.staging / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        (directory / "state").write_text("pending")
        op = Transaction(run_id, operation_id, kind, expected, directory)
        self.current = op
        self.operations[operation_id] = op

    def mutable(self):
        op = self.current
        if self.readonly or not self.connected or op is None or op.finishing or op.state != "pending":
            raise PermissionError(errno.EACCES, "No writable workspace operation")
        if len(op.received) >= 4096:
            op.unstable = True
            raise OSError(errno.ENOSPC, "Workspace mutation quota exceeded")
        return op

    def check_quota(self, extra=0):
        retained = set(self.proposals.values())
        if self.current is not None:
            retained.update(item for item in self.current.directory.iterdir()
                            if item.name not in ("state", "lock"))
        size = sum(item.stat().st_size for item in retained if item.is_file())
        if size + extra > self.quota_bytes:
            raise OSError(errno.ENOSPC, "Workspace draft quota exceeded")

    def version(self, path):
        return self.workspace.version(path)

    def record_read(self, path, observed):
        op = self.current
        if op is not None and path not in op.edits:
            key = op.run, path
            if key not in self.baselines and len(self.baselines) >= 4096:
                raise OSError(errno.ENOSPC, "Workspace read tracking quota exceeded")
            if key not in self.baselines or op.kind == "ReadFile":
                self.baselines[key] = observed
                self.change_events.pop(key, None)

    def write_edit(self, path, *, truncate=False):
        op = self.mutable()
        existing = op.edits.get(path)
        if existing is not None:
            if existing.kind == "write":
                return existing
            if existing.kind != "delete":
                raise IsADirectoryError(path)
        exists = True
        try:
            info = self.workspace.stat(path)
            if not stat.S_ISREG(info.st_mode):
                raise IsADirectoryError(path)
            if not info.st_mode & 0o222:
                raise PermissionError(errno.EACCES, "File is read-only")
            baseline = self.baselines.get((op.run, path), "unknown")
            mode = stat.S_IMODE(info.st_mode) & 0o777
        except FileNotFoundError:
            exists = False
            baseline, mode = self.baselines.get((op.run, path), "missing"), 0o600
        draft = op.directory / uuid.uuid4().hex
        with draft.open("xb") as destination:
            if not truncate and exists:
                with self.workspace.open(path) as source:
                    self.check_quota(os.fstat(source.fileno()).st_size)
                    shutil.copyfileobj(source, destination)
        edit = Edit(path, baseline, draft, "write", mode)
        op.edits[path] = edit
        return edit

    def edit_stat(self, path):
        if self.current is not None:
            edit = self.current.edits.get(path)
            if edit:
                if edit.kind == "delete":
                    raise FileNotFoundError(path)
                if edit.draft:
                    return edit.draft.stat()
        return self.workspace.stat(path)

    def rename(self, source, target):
        op = self.mutable()
        path_parts(target)
        if source == target:
            return
        original = op.edits.get(source)
        if original is None:
            self.edit_stat(source)
            original = self.write_edit(source)
        if original.kind != "write":
            raise PermissionError(errno.EACCES, "Only regular file renames are supported")
        can_rename_original = target not in op.edits and not original.proposal_only
        baseline = self.version(target)
        baseline = self.baselines.get((op.run, target), "missing" if baseline == "missing" else "unknown")
        op.edits[target] = Edit(target, baseline, original.draft, "write", original.permissions,
                                proposal_only=original.baseline != "missing")
        if original.baseline == "missing":
            del op.edits[source]
        else:
            op.edits[source] = Edit(source, original.baseline, None, "delete", proposal_only=True)
            if can_rename_original:
                op.renames[target] = source

    def mkdir(self, path):
        op = self.mutable()
        try:
            self.edit_stat(path)
        except FileNotFoundError:
            path_parts(path)
        else:
            raise FileExistsError(path)
        draft = op.directory / uuid.uuid4().hex
        draft.mkdir(mode=0o700)
        op.edits[path] = Edit(path, "missing", draft, "mkdir")

    def remove(self, path):
        op = self.mutable()
        prior = op.edits.get(path)
        if prior and prior.baseline == "missing":
            del op.edits[path]
        else:
            self.workspace.stat(path)
            baseline = self.baselines.get((op.run, path), "unknown")
            op.edits[path] = Edit(path, baseline, None, "delete")

    def changes(self, run_id):
        found = []
        with self.lock:
            baselines = tuple(item for item in self.baselines.items() if item[0][0] == run_id)
        if self.hints is not None:
            baselines = self.hints.prioritize(baselines)
        for (run, path), baseline in baselines:
            try:
                version = self.version(path)
            except OSError:
                version = "unavailable"
            key = run, path
            with self.lock:
                if self.baselines.get(key) != baseline:
                    continue
                if version == baseline:
                    self.change_events.pop(key, None)
                    continue
                event = self.change_events.get(key)
                if event is None or event["version"] != version:
                    self.seq += 1
                    event = {"path": path, "version": version, "seq": self.seq}
                    self.change_events[key] = event
                found.append(event.copy())
        return found

    def _transition(self, op, state):
        with (op.directory / "lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            (op.directory / "state").write_text(state)
            op.state = state

    def cancel(self, operation_id):
        with self.lock:
            op = self.operations.get(operation_id)
            if op is None:
                return {"status": "unknown", "outcomes": []}
            if op.result is not None:
                return op.result
            self._transition(op, "cancelled")
            if not op.finishing:
                return self._complete(op, "cancelled", [])
            return {"status": "unknown", "outcomes": []}

    def disconnect(self):
        with self.lock:
            self.connected = False
            if self.current is not None:
                self.cancel(self.current.identifier)

    def query(self, operation_id):
        op = self.operations.get(operation_id)
        return op.result if op is not None and op.result is not None else {"status": "unknown", "outcomes": []}

    def _complete(self, op, status, outcomes):
        op.result = {"status": status, "outcomes": outcomes}
        if self.current is op:
            self.current = None
        for draft in op.directory.iterdir():
            if draft.name not in ("state", "lock") and draft not in self.proposals.values():
                if draft.is_dir():
                    draft.rmdir()
                else:
                    draft.unlink(missing_ok=True)
        return op.result

    def _proposal(self, op, edit, status):
        event = {"path": edit.path, "operation_id": op.identifier, "status": status}
        if edit.draft and edit.draft.is_file():
            proposal = uuid.uuid4().hex
            self.proposals[proposal] = edit.draft
            event["proposal_id"] = proposal
        if status == "conflict":
            self.blocked.add((op.run, edit.path))
        return event

    def _publish(self, op, edit):
        with self.lock:
            if op.state != "pending" or not self.connected:
                return self._proposal(op, edit, "cancelled")
            if (op.run, edit.path) in self.blocked:
                return self._proposal(op, edit, "conflict")
        if edit.proposal_only:
            return self._proposal(op, edit, "proposal")
        if edit.kind == "mkdir":
            try:
                with self.lock:
                    if op.state != "pending" or not self.connected:
                        return self._proposal(op, edit, "cancelled")
                    self.workspace.create_directory(edit.path)
            except FileExistsError:
                return self._proposal(op, edit, "conflict")
            except OSError:
                return self._proposal(op, edit, "unknown")
            return {"path": edit.path, "operation_id": op.identifier, "status": "applied"}
        current = self.version(edit.path)
        if edit.baseline not in ("unknown", current):
            return self._proposal(op, edit, "conflict")
        published_version = "missing"
        if edit.draft:
            edit.draft.chmod(edit.permissions)
            with edit.draft.open("rb") as payload:
                published_version = hashlib.file_digest(payload, "sha256").hexdigest()
        try:
            if edit.kind == "write" and edit.baseline == "missing":
                with self.lock:
                    if op.state != "pending" or not self.connected:
                        return self._proposal(op, edit, "cancelled")
                    self.workspace.publish_new(edit.path, edit.draft)
                    edit.draft.unlink()
            elif edit.baseline == "unknown" or self.coordinator is None:
                return self._proposal(op, edit, "proposal")
            else:
                with self.workspace.parent(edit.path) as (parent, name):
                    command = [str(self.coordinator), str(self.workspace.root / edit.path),
                               str(parent), name, edit.baseline, str(op.directory / "state"),
                               str(op.directory / "lock"), edit.kind, str(edit.draft or "")]
                    result = subprocess.run(command, pass_fds=(parent,), check=False, timeout=40)
                if result.returncode == 3:
                    return self._proposal(op, edit, "conflict")
                if result.returncode == 4:
                    return self._proposal(op, edit, "cancelled")
                if result.returncode != 0:
                    return self._proposal(op, edit, "unknown")
        except FileExistsError:
            return self._proposal(op, edit, "conflict")
        except (OSError, subprocess.TimeoutExpired):
            return self._proposal(op, edit, "unknown")
        with self.lock:
            self.baselines[op.run, edit.path] = published_version
            self.change_events.pop((op.run, edit.path), None)
        return {"path": edit.path, "operation_id": op.identifier, "status": "applied"}

    def _publish_rename(self, op, source, target):
        def outcomes(status):
            return [self._proposal(op, source, status), self._proposal(op, target, status)]

        with self.lock:
            if op.state != "pending" or not self.connected:
                return outcomes("cancelled")
            if any((op.run, edit.path) in self.blocked for edit in (source, target)):
                return outcomes("conflict")
        if (self.coordinator is None or source.baseline == "unknown" or target.baseline == "unknown"
                or target.draft is None):
            return outcomes("proposal")
        try:
            with target.draft.open("rb") as draft:
                if hashlib.file_digest(draft, "sha256").hexdigest() != source.baseline:
                    return outcomes("proposal")
            with self.workspace.parent(source.path) as (origin, origin_name):
                with self.workspace.parent(target.path) as (destination, destination_name):
                    command = [str(self.coordinator), str(self.workspace.root / source.path),
                               str(origin), origin_name, source.baseline, str(op.directory / "state"),
                               str(op.directory / "lock"), "rename",
                               str(self.workspace.root / target.path), str(destination), destination_name,
                               target.baseline]
                    result = subprocess.run(command, pass_fds=(origin, destination), check=False, timeout=40)
        except (OSError, subprocess.TimeoutExpired):
            return outcomes("unknown")
        if result.returncode != 0:
            return outcomes({3: "conflict", 4: "cancelled"}.get(result.returncode, "unknown"))
        with self.lock:
            for edit, version in ((source, "missing"), (target, source.baseline)):
                key = op.run, edit.path
                self.baselines[key] = version
                self.change_events.pop(key, None)
        return [{"path": edit.path, "operation_id": op.identifier, "status": "applied"}
                for edit in (source, target)]

    def finish(self, operation_id, success, transcript):
        with self.lock:
            op = self.operations.get(operation_id)
            if op is None:
                return {"status": "unknown", "outcomes": []}
            if op.result is not None:
                return op.result
            if op.finishing:
                return {"status": "unknown", "outcomes": []}
            if op.handles:
                return self._complete(op, "unknown", [])
            op.finishing = True
            if not self.connected or op.state != "pending" or not success:
                return self._complete(op, "cancelled", [])
            if op.unstable or op.received != transcript:
                outcomes = [self._proposal(op, edit, "unknown") for edit in op.edits.values()]
                return self._complete(op, "unknown", outcomes)
            if op.expected is not None:
                expected = op.expected
                target = op.edits.get(expected["path"])
                if (target is None or target.draft is None or len(op.edits) != 1
                        or target.draft.stat().st_size != expected["length"]):
                    return self._complete(op, "unknown", [])
                with target.draft.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != expected["sha256"]:
                        return self._complete(op, "unknown", [])
        renames = {target: source for target, source in op.renames.items()
                   if target in op.edits and op.edits[target].kind == "write"
                   and source in op.edits and op.edits[source].kind == "delete"}
        outcomes = []
        sources = set(renames.values())
        for path, edit in op.edits.items():
            if path in renames:
                outcomes.extend(self._publish_rename(op, op.edits[renames[path]], edit))
            elif path not in sources:
                outcomes.append(self._publish(op, edit))
        for status in ("unknown", "conflict", "proposal", "cancelled", "applied"):
            if any(outcome["status"] == status for outcome in outcomes):
                break
        else:
            status = "unchanged"
        with self.lock:
            return self._complete(op, status, outcomes)

    @staticmethod
    def write_record(path, offset, data):
        return ["write", path, offset, len(data), digest(data)]
