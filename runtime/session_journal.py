import hashlib
import json
import math
import time

from fastapi import HTTPException

from runtime.common import Journal


TOOLS = {"ReadFile", "WriteFile", "RunCommand"}
STATUSES = {"ready", "released", "running", "success", "error", "cancelled"}
DECISIONS = {"applied", "conflict", "proposal", "unknown", "cancelled", "unchanged"}
ERRORS = {
    "command_error", "harness_restarted", "invalid_tool", "model_error", "mount_lost",
    "sandbox_error", "sandbox_restarted", "step_limit", "timeout", "transport_error",
    "write_conflict", "write_outcome_unknown", "write_unverified",
}


def clean(value, fields):
    return {field: value[field] for field in fields if field in value}


def project(record):
    result = clean(record, ("version", "session_id", "request_id", "operation_id"))
    if type(record.get("exit_code")) is int:
        result["exit_code"] = record["exit_code"]
    if type(record.get("truncated")) is bool:
        result["truncated"] = record["truncated"]
    for field, allowed in (("tool", TOOLS), ("status", STATUSES), ("error", ERRORS),
                           ("model_mode", {"mock", "real"})):
        if isinstance(record.get(field), str) and record[field] in allowed:
            result[field] = record[field]
    duration = record.get("duration_seconds")
    if type(duration) in {int, float} and math.isfinite(duration) and duration >= 0:
        result["duration_seconds"] = duration
    return result


def project_event(kind, data):
    if kind == "started":
        return clean(data, ("session_id", "request_id", "operation_id"))
    if kind == "model":
        usage = data.get("usage") or {}
        counters = ("prompt_tokens", "completion_tokens", "total_tokens")
        return {
            **clean(data, ("step",)),
            **(clean(data, ("mode",)) if data.get("mode") in ("mock", "real") else {}),
            "usage": {key: usage[key] for key in counters if isinstance(usage, dict)
                      and type(usage.get(key)) is int and usage[key] >= 0},
        }
    if kind == "tool_call":
        return {**clean(data, ("operation_id",)),
                **(clean(data, ("tool",)) if data.get("tool") in TOOLS else {})}
    if kind == "tool_result":
        return {**clean(data, ("operation_id",)), "result": project(data["result"])}
    if kind == "write_decision":
        decision = data.get("decision", {})
        return {
            **clean(data, ("operation_id",)),
            "decision": (clean(decision, ("status",))
                         if isinstance(decision.get("status"), str) and decision["status"] in DECISIONS else {}),
        }
    return {}


class SessionJournal(Journal):
    """Journal technical rows on disk and keep active content in process memory."""

    def __init__(self, name):
        super().__init__(name)
        self.records = {}
        self.details = {}
        self.fingerprints = {}
        self.started = {}
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS scopes (key TEXT PRIMARY KEY, session TEXT, trace TEXT)")
            self.db.execute("CREATE INDEX IF NOT EXISTS scopes_session ON scopes(session)")
            self.db.execute("CREATE INDEX IF NOT EXISTS scopes_trace ON scopes(trace)")
            self.db.execute("CREATE INDEX IF NOT EXISTS events_run_seq ON events(run,seq)")

    def reserve(self, key, payload, value):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()
        with self.lock:
            if key in self.records:
                if self.fingerprints[key] != fingerprint:
                    raise HTTPException(409, "identifier already used with different payload")
                return False, self.records[key].copy()
            if self.db.execute("SELECT 1 FROM records WHERE key=?", (key,)).fetchone():
                raise HTTPException(409, "identifier already reserved")
            with self.db:
                self.db.execute("INSERT INTO records VALUES (?,NULL,?)", (key, json.dumps(project(value))))
                self.db.execute(
                    "INSERT INTO scopes VALUES (?,?,?)", (key, value["session_id"], key.removeprefix("op:")),
                )
            self.records[key] = value.copy()
            self.fingerprints[key] = fingerprint
            self.started[key] = time.monotonic()
            return True, value

    def get(self, key):
        with self.lock:
            if key in self.records:
                return self.records[key].copy()
            return super().get(key)

    def put(self, key, value):
        with self.lock:
            super().put(key, project(value))
            if key in self.records:
                self.records[key] = value.copy()

    def write_event(self, trace, kind, full, safe):
        value = {"version": "0", "trace_id": trace, "type": kind, "time": time.time()}
        cursor = self.db.execute(
            "INSERT INTO events(run,value) VALUES (?,?)", (trace, json.dumps({**safe, **value})),
        )
        return {**full, **value, "seq": cursor.lastrowid}

    def event(self, run, kind, **data):
        with self.lock:
            record = self.records.get(run) or self.records.get("op:" + run)
            if record is None or record["status"] != "running":
                return
            with self.db:
                event = self.write_event(run, kind, data, project_event(kind, data))
            self.details.setdefault(run, {})[event["seq"]] = event

    def complete(self, key, run, record):
        with self.lock:
            current = self.get(key)
            if current["status"] != "running":
                return current
            if key in self.started:
                record.setdefault("duration_seconds", time.monotonic() - self.started[key])
            else:
                first = self.db.execute(
                    "SELECT value FROM events WHERE run=? ORDER BY seq LIMIT 1", (run,),
                ).fetchone()
                if first:
                    record.setdefault("duration_seconds", max(0, time.time() - json.loads(first[0])["time"]))
            safe = project(record)
            with self.db:
                self.db.execute("UPDATE records SET value=? WHERE key=?", (json.dumps(safe), key))
                event = self.write_event(run, "result", {"result": record}, {"result": safe})
            if key in self.records:
                self.records[key] = record.copy()
                self.details.setdefault(run, {})[event["seq"]] = event
            return record

    def events(self, run, after=0):
        with self.lock:
            overlays = self.details.get(run, {})
            return [overlays.get(event["seq"], event) for event in super().events(run, after)]

    def technical_history(self, session):
        with self.lock:
            rows = self.db.execute(
                """SELECT events.seq,events.value FROM events
                   JOIN scopes ON events.run=scopes.trace
                   WHERE scopes.session=? ORDER BY events.seq""",
                (session,),
            ).fetchall()
        return [{"seq": seq, **json.loads(value)} for seq, value in rows]

    def session_records(self, session):
        with self.lock:
            keys = self.db.execute("SELECT key FROM scopes WHERE session=?", (session,)).fetchall()
            return [(key, self.get(key)) for (key,) in keys]

    def drop_session(self, session):
        with self.lock:
            for key, trace in self.db.execute("SELECT key,trace FROM scopes WHERE session=?", (session,)):
                self.records.pop(key, None)
                self.fingerprints.pop(key, None)
                self.started.pop(key, None)
                self.details.pop(trace, None)
