import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from pathlib import Path

from fastapi import HTTPException


SESSION_KEY_HEADER = "X-Session-Key"


class SessionStore:
    """Resolve client-supplied resource names without persisting them."""

    def __init__(self):
        root = Path(os.environ.get("STATE_DIR", "/state"))
        root.mkdir(parents=True, exist_ok=True)
        secret = root / "session-identities.key"
        if not secret.exists():
            temporary = root / (".session-identities-" + secrets.token_hex(16))
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb",
            ) as file:
                file.write(secrets.token_bytes(32))
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, secret)
            except FileExistsError:
                pass
            finally:
                temporary.unlink()
        self.secret = secret.read_bytes()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "session-owners.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                alias TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, owner TEXT,
                fingerprint TEXT, status TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
                alias TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, session_alias TEXT NOT NULL)""")

    def digest(self, kind, *values):
        payload = "\0".join((kind, *values)).encode()
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def session(self, external, key):
        alias = self.digest("session", external)
        with self.lock:
            row = self.db.execute(
                """SELECT id,owner,status,alias FROM sessions
                   WHERE alias=? OR (id=? AND owner IS NOT NULL) ORDER BY (id=?) DESC LIMIT 1""",
                (alias, external, external),
            ).fetchone()
        if row is None:
            return None
        identifier, owner, status, alias = row
        if owner is not None and (not key or not hmac.compare_digest(owner, self.digest("owner", alias, key))):
            raise HTTPException(403, "invalid session key")
        return identifier, owner is not None, status

    def claim_session(self, external, key, descriptor):
        alias = self.digest("session", external)
        mounted = descriptor is not None
        with self.lock, self.db:
            existing = self.session(external, key)
            if existing:
                identifier, was_mounted, status = existing
                fingerprint = self.db.execute(
                    "SELECT fingerprint FROM sessions WHERE id=?", (identifier,),
                ).fetchone()[0]
                if (was_mounted != mounted or status in {"closed", "closing", "halted"}
                        or mounted and fingerprint != self.digest("mount", descriptor)):
                    raise HTTPException(409, "session already reserved")
                return identifier, status
            if mounted and (not key or len(key) < 32 or len(key) > 256):
                raise HTTPException(403, "invalid session key")
            identifier = secrets.token_hex(16) if mounted else alias
            owner = self.digest("owner", alias, key) if mounted else None
            fingerprint = self.digest("mount", descriptor) if mounted else None
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?)",
                (alias, identifier, owner, fingerprint, "opening"),
            )
            return identifier, "opening"

    def set_status(self, identifier, status):
        with self.lock, self.db:
            self.db.execute("UPDATE sessions SET status=? WHERE id=?", (status, identifier))

    def owned_sessions(self):
        with self.lock:
            return list(self.db.execute(
                "SELECT id,status FROM sessions WHERE owner IS NOT NULL AND status!='closed'",
            ))

    def run(self, external, key):
        alias = self.digest("run", external)
        with self.lock:
            row = self.db.execute(
                """SELECT runs.id, sessions.id, sessions.owner, sessions.status, sessions.alias
                   FROM runs JOIN sessions ON runs.session_alias=sessions.alias
                   WHERE runs.alias=? OR runs.id=? ORDER BY (runs.id=?) DESC LIMIT 1""",
                (alias, external, external),
            ).fetchone()
        if row is None:
            return None
        run, session, owner, status, session_alias = row
        if not key or not hmac.compare_digest(owner, self.digest("owner", session_alias, key)):
            raise HTTPException(403, "invalid session key")
        return run, session, status

    def claim_run(self, external, session_external, key):
        run_alias = self.digest("run", external)
        with self.lock, self.db:
            session_id = self.session(session_external, key)[0]
            existing = self.run(external, key)
            if existing:
                if existing[1] != session_id:
                    raise HTTPException(409, "request identifier already used")
                return existing[0]
            identifier = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO runs SELECT ?,?,alias FROM sessions WHERE id=?",
                (run_alias, identifier, session_id),
            )
            return identifier
