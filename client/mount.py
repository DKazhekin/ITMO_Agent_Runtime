"""Mount a selected folder through an authenticated reverse SSH connection."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Lock
import uuid

from client.mount_files import Workspace
from client.mount_state import MountState
from client.mount_watch import WatchHints, start_watcher


_coordinator_lock = Lock()
_coordinator_cache = None


def workspace_coordinator():
    """Build the macOS helper once per client process in a private short path."""
    global _coordinator_cache

    if sys.platform != "darwin":
        raise ValueError("Coordinated workspaces require macOS")
    with _coordinator_lock:
        if _coordinator_cache is None:
            temporary = tempfile.TemporaryDirectory(prefix="itmo-coord-", dir="/private/tmp")
            root = Path(temporary.name)
            try:
                result = subprocess.run(
                    ["swiftc", "-module-cache-path", str(root / "cache"),
                     str(Path(__file__).with_name("mount_coordinate.swift")),
                     "-o", str(root / "coordinator")],
                    capture_output=True, text=True, timeout=90, check=False,
                )
                if result.returncode != 0:
                    diagnostic = result.stderr.strip()[-2000:] or "no compiler diagnostic"
                    raise RuntimeError(f"Workspace coordinator build failed: {diagnostic}")
            except BaseException:
                temporary.cleanup()
                raise
            _coordinator_cache = temporary
        return Path(_coordinator_cache.name) / "coordinator"


class MacMount:
    @classmethod
    async def open(cls, folder, session_id, *, ssh_host, ssh_known_hosts, server_mount_key,
                   ssh_user=None, ssh_port=22, ssh_client_key=None, readonly=False,
                   coordinated=False, quota_bytes=64 * 1024 * 1024, watch=True):
        import asyncssh

        from client.mount_sftp import MountSFTP

        self = cls()
        root = Path(folder).resolve(strict=True)
        self.generation = uuid.uuid4().hex
        self.session_id = session_id
        self.workspace = Workspace(root)
        self.stage = None
        self.forwarder = None
        self.listener = None
        self.transport = None
        self.tracker = None
        self.watcher = None
        self.connections = set()
        self.publishers = set()
        try:
            self.stage = Path(tempfile.mkdtemp(prefix=".itmo-mount-", dir=root.parent))
            if root.stat().st_dev != self.stage.stat().st_dev:
                raise ValueError("Workspace staging must be on the same filesystem")
            coordinator = await asyncio.to_thread(workspace_coordinator) if coordinated else None
            hints = WatchHints(root) if watch else None
            self.state = MountState(self.workspace, self.stage, readonly=readonly,
                                    coordinator=coordinator, quota_bytes=quota_bytes, hints=hints)
            if hints is not None:
                try:
                    self.watcher = start_watcher(root, hints)
                except (ImportError, OSError, RuntimeError):
                    # A missed save hint never disables digest verification.
                    self.watcher = None
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            allowed_key = asyncssh.read_public_key(Path(server_mount_key))

            class KeyOnly(asyncssh.SSHServer):
                def connection_made(server, connection):
                    self.connections.add(connection)
                    server.connection = connection

                def connection_lost(server, error):
                    self.connections.discard(server.connection)

                def public_key_auth_supported(server):
                    return True

                def validate_public_key(server, user, key):
                    return user == "itmo-mount" and key == allowed_key

            async def control(process):
                if process.command != "itmo-mount-control":
                    process.exit(126)
                    return
                try:
                    raw = await asyncio.wait_for(process.stdin.readline(), 10)
                    if not raw or len(raw) > 1_048_576:
                        raise ValueError("Invalid control request")
                    request = json.loads(raw)
                    result = await self.control(request)
                    process.stdout.write(json.dumps({"result": result}) + "\n")
                except (ValueError, OSError, KeyError, TypeError):
                    process.stdout.write(json.dumps({"error": "Mount control request rejected"}) + "\n")
                process.exit(0)

            self.listener = await asyncssh.create_server(
                KeyOnly, "127.0.0.1", 0, server_host_keys=[host_key],
                sftp_factory=lambda channel: MountSFTP(channel, self.state),
                process_factory=control, allow_scp=False, agent_forwarding=False, sftp_version=3,
            )
            kwargs = {} if ssh_client_key is None else {"client_keys": [str(ssh_client_key)]}
            self.transport = await asyncssh.connect(
                ssh_host, port=ssh_port, username=ssh_user, known_hosts=str(ssh_known_hosts),
                keepalive_interval=2, keepalive_count_max=2, **kwargs,
            )
            self.forwarder = await self.transport.forward_remote_port(
                "127.0.0.1", 0, "127.0.0.1", self.listener.get_port(),
            )
            self.descriptor = {
                "generation": self.generation,
                "reverse_port": self.forwarder.get_port(),
                "host_key": host_key.export_public_key("openssh").decode().strip(),
                "readonly": readonly,
            }
            self.tracker = asyncio.create_task(self.track_connection())
            return self
        except BaseException:
            await self.aclose()
            raise

    async def control(self, request):
        if request["session_id"] != self.session_id or request["generation"] != self.generation:
            raise ValueError("Mount session mismatch")
        action = request["action"]
        if action == "hello":
            return {"readonly": self.state.readonly, "connected": self.state.connected}
        if action == "begin":
            self.state.begin(request["run_id"], request["operation_id"], request["kind"],
                             request.get("expected"))
            return True
        if action == "finish":
            task = asyncio.create_task(asyncio.to_thread(
                self.state.finish, request["operation_id"], request["success"], request["transcript"],
            ))
            self.publishers.add(task)
            task.add_done_callback(self.publishers.discard)
            return await asyncio.shield(task)
        if action == "query":
            return self.state.query(request["operation_id"])
        if action == "changes":
            if not self.state.connected:
                raise ValueError("Mount is disconnected")
            return await asyncio.to_thread(self.state.changes, request["run_id"])
        if action == "cancel":
            return await asyncio.to_thread(self.state.cancel, request["operation_id"])
        raise ValueError("Unknown mount action")

    async def track_connection(self):
        await self.transport.wait_closed()
        await asyncio.to_thread(self.state.disconnect)

    def proposal_path(self, operation_id):
        return self.state.proposals.get(operation_id)

    async def aclose(self):
        if hasattr(self, "state"):
            await asyncio.to_thread(self.state.disconnect)
        if self.watcher is not None:
            observer, self.watcher = self.watcher, None
            observer.stop()
            await asyncio.to_thread(observer.join, 2)
        connections = tuple(self.connections)
        for connection in connections:
            connection.close()
        for item in (self.forwarder, self.listener, self.transport):
            if item is not None:
                item.close()
        for item in (self.forwarder, self.listener, self.transport, *connections):
            if item is not None:
                await item.wait_closed()
        if self.tracker is not None and self.tracker is not asyncio.current_task():
            await self.tracker
        if self.publishers:
            await asyncio.gather(*tuple(self.publishers), return_exceptions=True)
        if self.workspace is not None:
            self.workspace.close()
            self.workspace = None
        if self.stage is not None:
            shutil.rmtree(self.stage)
            self.stage = None
