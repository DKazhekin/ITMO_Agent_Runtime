"""Exercise the desktop and CLI session controller with a reconnecting HTTP transport."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from client.mounted import MountedSession


def test_owner_retry_cursor_resume_and_close(tmp_path, monkeypatch):
    from client.mount import MacMount

    calls = []
    attempts = dict(create=0, submit=0, poll=0)
    descriptor = {"generation": "a" * 32, "reverse_port": 45678, "host_key": "test", "readonly": False}
    draft = tmp_path / "draft"
    draft.write_bytes(b"alternative")

    async def open_mount(*args, **kwargs):
        return SimpleNamespace(descriptor=descriptor, aclose=lambda: asyncio.sleep(0),
                               proposal_path=lambda value: draft if value == "proposal" else None)

    class Transport:
        def request(self, method, path, body=None, *, session_key):
            calls.append((method, path, body, session_key))
            if method == "POST" and path in {"/sessions", "/runs"}:
                stage = "create" if path == "/sessions" else "submit"
                attempts[stage] += 1
                if attempts[stage] == 1:
                    raise httpx.ReadError("reply lost")
                return {"status": "ready"} if stage == "create" else {"request_id": "remote-run"}
            if "/events?" in path:
                attempts["poll"] += 1
                if attempts["poll"] == 2:
                    raise httpx.ConnectError("temporary disconnect")
                first = {"seq": 1, "type": "started"}
                return {"events": [first] if attempts["poll"] == 1 else [
                    first, {"seq": 2, "type": "result", "result": {"status": "success"}}]}
            return {"status": "cancelled" if path.endswith("/cancel") else "released"}

    monkeypatch.setattr(MacMount, "open", open_mount)
    settings = SimpleNamespace(ssh_host="local", ssh_port=22, ssh_user=None, ssh_known_hosts=tmp_path,
                               ssh_client_key=None, server_mount_key=tmp_path)
    with MountedSession.open(Transport(), tmp_path, settings=settings) as session:
        run = session.submit("read the port")
        result, events = session.follow(run)
        assert run == "remote-run" and result["status"] == "success"
        assert [row["seq"] for row in events] == [1, 2]
        assert session.cancel(run)["status"] == "cancelled"
        target = tmp_path / "saved"
        assert session.save_proposal("proposal", target).read_bytes() == b"alternative"
        with pytest.raises(FileExistsError):
            session.save_proposal("proposal", target)
    assert calls[0] == calls[1] and calls[2] == calls[3]
    assert [call[1] for call in calls if "/events?" in call[1]] == [
        "/runs/remote-run/events?after=0", "/runs/remote-run/events?after=1", "/runs/remote-run/events?after=1"]
    assert calls[-1][0] == "DELETE" and session._closed
    assert len({call[3] for call in calls}) == 1 and len(calls[-1][3]) >= 32
