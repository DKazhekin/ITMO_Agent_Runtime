import asyncio
import hashlib
import os
from pathlib import Path
import struct

import pytest

from client.mount_files import Workspace
from client.mount_state import MountState
from runtime.mount_host import Host, MountError, label, public_key
from runtime.mount_observer import Observer, write_fd


@pytest.fixture
def mounted_state(tmp_path):
    folder = tmp_path / "chosen"
    folder.mkdir()
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    workspace = Workspace(folder)
    state = MountState(workspace, drafts, readonly=False, coordinator=None, quota_bytes=1000)
    try:
        yield workspace, state
    finally:
        workspace.close()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_host_rejects_external_paths_and_keys():
    for invalid in ("../root", "ssh\noption", "/tmp/other", "a" * 101):
        with pytest.raises(MountError):
            label(invalid)
    with pytest.raises(MountError):
        public_key("ssh-ed25519 junk\n@cert-authority")
    helper = Host(Path("/var/lib/itmo-mount"), Path("/identity"), Path("/sshfs"))
    with pytest.raises(MountError):
        helper.resolve("valid-session", "0" * 32)


def test_workspace_paths_confined_and_service_entries_are_hidden(mounted_state, tmp_path):
    workspace, _ = mounted_state
    external = tmp_path / "outside"
    external.write_bytes(b"secret")
    (workspace.root / "alias").symlink_to(external)
    os.link(external, workspace.root / "hardlink")
    (workspace.root / ".git").mkdir()
    (workspace.root / "nested").mkdir()
    (workspace.root / "nested/.env.local").write_text("secret")
    for path in ("../outside", "/etc/passwd", "nested/.env.local", ".git", "hardlink"):
        with pytest.raises(PermissionError):
            workspace.open(path)
    with pytest.raises(OSError):
        workspace.open("alias")
    assert workspace.listing("") == ["nested"]
    assert workspace.listing("nested") == []
    assert external.read_bytes() == b"secret"


def test_expected_binary_write_applies_once(mounted_state):
    workspace, state = mounted_state
    data = b"\x00\xff\xc0\x80data"
    state.begin("run", "op", "WriteFile", {"path": "new", "length": len(data), "sha256": digest(data)})
    edit = state.write_edit("new")
    edit.draft.write_bytes(data)
    transcript = [state.write_record("new", 0, data)]
    state.current.received = transcript
    reply = state.finish("op", True, transcript)
    assert reply["status"] == "applied"
    applied = workspace.root / "new"
    inode = applied.stat().st_ino
    assert applied.read_bytes() == data
    assert state.finish("op", True, transcript) == reply
    assert applied.stat().st_ino == inode


def test_held_name_is_preserved_and_unknown_editor_gets_proposal(mounted_state):
    workspace, state = mounted_state
    state.begin("run", "first", "RunCommand", None)
    edit = state.write_edit("port", truncate=True)
    edit.draft.write_text("8080")
    original = workspace.root / "port"
    original.write_text("9000")
    conflict = state.finish("first", True, [])
    assert conflict["status"] == "conflict"
    assert state.proposals[conflict["outcomes"][0]["proposal_id"]].read_text() == "8080"
    assert original.read_text() == "9000"
    state.begin("other", "second", "RunCommand", None)
    state.record_read("port", digest(b"9000"))
    state.write_edit("port", truncate=True).draft.write_text("8111")
    result = state.finish("second", True, [])
    assert result["status"] == "proposal"
    assert state.proposals[result["outcomes"][0]["proposal_id"]].read_text() == "8111"
    assert original.read_text() == "9000"


def test_versions_are_deduplicated_and_missing_is_detected(mounted_state):
    workspace, state = mounted_state
    path = workspace.root / "port"
    path.write_text("8000")
    state.begin("run", "read", "ReadFile", None)
    state.record_read("port", workspace.version("port"))
    assert state.finish("read", True, [])["status"] == "unchanged"
    path.write_text("9000")
    assert state.changes("run") == [{"path": "port", "version": digest(b"9000"), "seq": 1}]
    assert state.changes("run") == [{"path": "port", "version": digest(b"9000"), "seq": 1}]
    path.unlink()
    assert state.changes("run") == [{"path": "port", "version": "missing", "seq": 2}]


def test_incomplete_wire_proposal_never_applies(mounted_state):
    workspace, state = mounted_state
    state.begin("run", "op", "RunCommand", None)
    state.write_edit("file").draft.write_bytes(b"x")
    state.current.received = [["open", "file", 26]]
    assert state.finish("op", True, [])["status"] == "unknown"
    assert not (workspace.root / "file").exists()


def test_temp_rename_and_parent_directory_publish_together(mounted_state):
    workspace, state = mounted_state
    state.begin("run", "op", "RunCommand", None)
    state.mkdir("results")
    state.write_edit("temporary", truncate=True).draft.write_bytes(b"result")
    state.rename("temporary", "results/output")
    assert state.finish("op", True, [])["status"] == "applied"
    assert not (workspace.root / "temporary").exists()
    assert (workspace.root / "results/output").read_bytes() == b"result"


def packet(kind, request, *fields):
    return struct.pack(">BI", kind, request) + b"".join(fields)


def string(data):
    return struct.pack(">I", len(data)) + data


def test_observer_records_actual_offset_and_marks_transport_loss(tmp_path):
    observer = Observer(tmp_path)
    (tmp_path / "active").write_text("op")
    observer.client(packet(3, 1, string(b"/sample"), struct.pack(">I", 26)))
    observer.server(packet(102, 1, string(b"1")))
    observer.client(packet(6, 2, string(b"1"), struct.pack(">Q", 4), string(b"data")))
    observer.server(packet(101, 2, struct.pack(">I", 0)))
    observer.client(packet(17, 3, string(b"/.itmo-mount-barrier-abc")))
    observer.server(packet(101, 3, struct.pack(">I", 3)))
    observer.disconnected()
    text = (tmp_path / "op.wire").read_text()
    assert '["write","sample",4,4,"' in text
    assert '"open_handles":1' in text and '"disconnected":true' in text



def test_observer_preserves_a_frame_through_nonblocking_backpressure():
    read, write = os.pipe()
    os.set_blocking(write, False)
    expected = b"sample-data" * 8000

    def consume():
        received = bytearray()
        while len(received) < len(expected):
            received.extend(os.read(read, 4096))
        return bytes(received)

    async def exercise():
        sender = asyncio.create_task(write_fd(write, expected))
        await asyncio.sleep(0.01)
        assert not sender.done()
        receiver = asyncio.create_task(asyncio.to_thread(consume))
        await asyncio.wait_for(sender, 5)
        assert await receiver == expected

    try:
        asyncio.run(exercise())
    finally:
        os.close(read)
        os.close(write)



def test_deleted_baseline_is_not_recreated(mounted_state):
    workspace, state = mounted_state
    original = workspace.root / "port"
    original.write_bytes(b"8000")
    state.begin("run", "read", "ReadFile", None)
    state.record_read("port", workspace.version("port"))
    state.finish("read", True, [])
    original.unlink()

    state.begin("run", "write", "RunCommand", None)
    state.write_edit("port", truncate=True).draft.write_bytes(b"8080")
    result = state.finish("write", True, [])
    assert result["status"] == "conflict"
    assert not original.exists()
    assert state.proposals[result["outcomes"][0]["proposal_id"]].read_bytes() == b"8080"


def test_user_save_immediately_after_publication_has_its_own_version(mounted_state, monkeypatch):
    workspace, state = mounted_state
    original = workspace.root / "port"
    publish = workspace.publish_new

    def publish_then_user_saves(path, draft):
        publish(path, draft)
        original.write_bytes(b"9000")

    monkeypatch.setattr(workspace, "publish_new", publish_then_user_saves)
    state.begin("run", "write", "RunCommand", None)
    state.write_edit("port", truncate=True).draft.write_bytes(b"8080")
    assert state.finish("write", True, [])["status"] == "applied"
    assert state.baselines["run", "port"] == digest(b"8080")
    assert state.changes("run") == [{"path": "port", "version": digest(b"9000"), "seq": 1}]



def test_save_hints_prioritize_reads_but_missed_events_are_verified(mounted_state, monkeypatch):
    from client.mount_watch import WatchHints

    workspace, state = mounted_state
    for name in ("first", "second"):
        (workspace.root / name).write_text("before")
    state.begin("run", "read", "ReadFile", None)
    for name in ("first", "second"):
        state.record_read(name, workspace.version(name))
    state.finish("read", True, [])
    state.hints = WatchHints(workspace.root, limit=1)
    state.hints.notify(workspace.root / "first", workspace.root / "second")
    state.hints.notify(workspace.root / ".git/config", workspace.root.parent / "outside")
    for name in ("first", "second"):
        (workspace.root / name).write_text("after")
    reads = []
    version = state.version

    def record(path):
        reads.append(path)
        return version(path)

    monkeypatch.setattr(state, "version", record)
    changes = state.changes("run")
    assert reads == ["second", "first"]
    assert {change["path"] for change in changes} == {"first", "second"}
    state.hints = None
    (workspace.root / "first").unlink()
    assert any(change["path"] == "first" and change["version"] == "missing"
               for change in state.changes("run"))
