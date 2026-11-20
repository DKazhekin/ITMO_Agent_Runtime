"""Own the local file service and the matching remote workspace session."""

import asyncio
import json
import os
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class MountSettings:
    ssh_host: str
    ssh_known_hosts: Path
    server_mount_key: Path
    ssh_user: str | None = None
    ssh_port: int = 22
    ssh_client_key: Path | None = None

    @classmethod
    def from_env(cls):
        return cls.from_values(os.environ)

    @classmethod
    def from_file(cls, path):
        return cls.from_values(json.loads(Path(path).read_text()))

    @classmethod
    def from_values(cls, values):
        if not hasattr(values, "get"):
            raise ValueError("Invalid mount connection settings")
        required = ("MOUNT_SSH_HOST", "MOUNT_SSH_KNOWN_HOSTS", "MOUNT_SERVER_PUBLIC_KEY")
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise ValueError("Configure the mount connection: " + ", ".join(missing))
        key = values.get("MOUNT_SSH_CLIENT_KEY")
        return cls(values["MOUNT_SSH_HOST"], Path(values["MOUNT_SSH_KNOWN_HOSTS"]).expanduser(),
                   Path(values["MOUNT_SERVER_PUBLIC_KEY"]).expanduser(), values.get("MOUNT_SSH_USER"),
                   int(values.get("MOUNT_SSH_PORT", "22")), Path(key).expanduser() if key else None)


class MountedSession:
    def __init__(self, client):
        self.client = client
        self.session_id = uuid.uuid4().hex
        self._key = secrets.token_urlsafe(32)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._mount = None
        self._released = False
        self._closed = False

    @classmethod
    def open(cls, client, folder, *, readonly=False, coordinated=False, settings=None):
        from client.mount import MacMount

        settings = settings or MountSettings.from_env()
        folder = Path(folder).expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("Select a directory")
        session = cls(client)
        session._thread.start()
        try:
            session._mount = session._wait(MacMount.open(
                folder, session.session_id, ssh_host=settings.ssh_host, ssh_user=settings.ssh_user,
                ssh_port=settings.ssh_port, ssh_known_hosts=settings.ssh_known_hosts,
                ssh_client_key=settings.ssh_client_key, server_mount_key=settings.server_mount_key,
                readonly=readonly, coordinated=coordinated))
            body = {"session_id": session.session_id, "mount": session._mount.descriptor}
            try:
                state = session._request("POST", "/sessions", body)
            except httpx.TransportError:
                # Repeat the same creation with the same credentials after an ambiguous reply.
                state = session._request("POST", "/sessions", body)
            if state.get("status") != "ready":
                raise RuntimeError("Workspace provisioning failed: " + str(state.get("status")))
            return session
        except BaseException:
            try:
                if session._mount is not None:
                    session._request("DELETE", f"/sessions/{session.session_id}")
            except httpx.HTTPError:
                pass
            finally:
                session._stop_local()
            raise

    def _wait(self, awaitable):
        return asyncio.run_coroutine_threadsafe(awaitable, self._loop).result(timeout=90)

    def _request(self, method, path, body=None):
        return self.client.request(method, path, body, session_key=self._key)

    def submit(self, instruction, *, timeout=240, expected_mode="mock", expected_model=None, request_id=None):
        run_id = request_id or uuid.uuid4().hex
        body = {"version": "0", "request_id": run_id, "session_id": self.session_id,
                "instruction": instruction, "timeout_seconds": timeout, "expected_mode": expected_mode,
                "expected_model": expected_model}
        try:
            state = self._request("POST", "/runs", body)
        except httpx.TransportError:
            state = self._request("POST", "/runs", body)
        return state["request_id"]

    def events(self, run_id, after=0):
        return self._request("GET", f"/runs/{run_id}/events?after={int(after)}")["events"]

    def cancel(self, run_id):
        return self._request("POST", f"/runs/{run_id}/cancel", {})

    def status(self, run_id):
        return self._request("GET", f"/runs/{run_id}")

    def follow(self, run_id, *, after=0, timeout=240, on_event=None):
        deadline = time.monotonic() + timeout + 15
        trace = []
        while time.monotonic() < deadline:
            try:
                for event in self.events(run_id, after):
                    if event["seq"] <= after:
                        continue
                    after = event["seq"]
                    trace.append(event)
                    if on_event:
                        on_event(event)
                    if event["type"] == "result":
                        return event["result"], trace
            except httpx.TransportError:
                time.sleep(.3)
                continue
            time.sleep(.1)
        try:
            self.cancel(run_id)
        finally:
            raise TimeoutError("Timed out waiting for the run")

    def execute(self, instruction, *, timeout=240, on_event=None, expected_mode="mock", expected_model=None):
        self.client.preflight(expected_mode, expected_model)
        run_id = self.submit(instruction, timeout=timeout, expected_mode=expected_mode, expected_model=expected_model)
        return self.follow(run_id, timeout=timeout, on_event=on_event)

    def history(self):
        response = self.client.http.get(f"/sessions/{self.session_id}/history",
                                        headers={"X-Session-Key": self._key})
        response.raise_for_status()
        return response.text

    def save_proposal(self, proposal_id, destination):
        source = self._mount.proposal_path(proposal_id)
        if source is None or self._closed:
            raise ValueError("This proposal is no longer available")
        target = Path(destination)
        with source.open("rb") as input_file, target.open("xb") as output_file:
            try:
                shutil.copyfileobj(input_file, output_file)
            except OSError:
                target.unlink(missing_ok=True)
                raise
        return target

    def _stop_local(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._mount is not None:
                self._wait(self._mount.aclose())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()

    def close(self):
        try:
            if not self._released:
                self._request("DELETE", f"/sessions/{self.session_id}")
                self._released = True
        finally:
            self._stop_local()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
