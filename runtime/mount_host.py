"""Trusted Linux service that provisions mounts on the Docker daemon host."""

import argparse
import asyncio
import base64
import fcntl
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import time
import uuid

from runtime.mount import MAX_MESSAGE, MountError


IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
GENERATION = re.compile(r"[0-9a-f]{32}\Z")
PROJECT = Path(__file__).resolve().parent.parent


def label(value, pattern=IDENTIFIER):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MountError("Invalid mount identifier")
    return value


def public_key(value):
    try:
        algorithm, encoded, *_ = value.split()
        decoded = base64.b64decode(encoded, validate=True)
    except (AttributeError, ValueError) as error:
        raise MountError("Invalid SFTP host key") from error
    if algorithm != "ssh-ed25519" or len(decoded) != 51 or not decoded.startswith(b"\x00\x00\x00\x0bssh-ed25519"):
        raise MountError("Invalid SFTP host key")
    return f"{algorithm} {encoded}"


@dataclass
class Attached:
    session: str
    generation: str
    readonly: bool
    port: int
    state: Path
    path: Path
    process: asyncio.subprocess.Process | None = None
    active: str | None = None
    broken: bool = False
    kernel_mount_id: str | None = None
    client_session_id: str | None = None

    def view(self):
        return {"path": str(self.path), "generation": self.generation, "readonly": self.readonly}


async def command(argv, stdin=None, timeout=8):
    async with asyncio.timeout(timeout):
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await process.communicate(stdin)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        return process.returncode, output


class Host:
    def __init__(self, root, identity, sshfs):
        self.root = root
        self.identity = identity
        self.sshfs = sshfs
        self.mounts = {}
        self.attaching = set()

    @staticmethod
    def ssh_options(mount):
        return [
            "BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
            "NoHostAuthenticationForLocalhost=no", "GlobalKnownHostsFile=/dev/null",
            f"UserKnownHostsFile={mount.state / 'known_hosts'}", "ConnectTimeout=3",
            "ServerAliveInterval=2", "ServerAliveCountMax=2", "ClearAllForwardings=yes",
        ]

    async def control(self, mount, action, **payload):
        argv = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", str(self.identity),
                "-p", str(mount.port)]
        for option in self.ssh_options(mount):
            argv += ["-o", option]
        argv += ["itmo-mount@127.0.0.1", "itmo-mount-control"]
        request = dict(action=action, session_id=mount.client_session_id or mount.session,
                       generation=mount.generation, **payload)
        code, output = await command(argv, (json.dumps(request) + "\n").encode(), timeout=65)
        if code != 0 or len(output) > MAX_MESSAGE:
            raise MountError("Client mount control channel unavailable")
        reply = json.loads(output)
        if "result" not in reply:
            raise MountError("Client rejected mount control")
        return reply["result"]

    async def mount_id(self, mount):
        code, output = await command(["findmnt", "-n", "-M", str(mount.path), "-o", "ID,FSTYPE,TARGET"])
        if not code:
            parts = output.decode().strip().split(maxsplit=2)
            if len(parts) == 3 and parts[1:] == ["fuse.sshfs", str(mount.path)]:
                return parts[0]
        return None

    async def present(self, mount):
        if (mount.broken or mount.process is None or mount.process.returncode is not None
                or await self.mount_id(mount) != mount.kernel_mount_id):
            mount.broken = True
            return False
        return True

    def resolve(self, session_id, generation=None):
        key = label(session_id)
        mount = self.mounts.get(key)
        if mount is None or (generation is not None and mount.generation != label(generation, GENERATION)):
            raise MountError("Mount generation is unavailable")
        return mount

    async def attach(self, session_id, descriptor):
        session = label(session_id)
        generation = label(descriptor["generation"], GENERATION)
        client_session = label(descriptor.get("client_session_id", session))
        port = descriptor["reverse_port"]
        readonly = descriptor["readonly"]
        if (not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535
                or not isinstance(readonly, bool)):
            raise MountError("Invalid mount descriptor")
        key = public_key(descriptor["host_key"])
        if session in self.mounts:
            mount = self.resolve(session, generation)
            if (mount.client_session_id != client_session or mount.readonly != readonly or mount.port != port
                    or (mount.state / "known_hosts").read_text() != f"[127.0.0.1]:{port} {key}\n"
                    or not await self.present(mount)):
                raise MountError("Mount cannot be reattached implicitly")
            return mount.view()
        if session in self.attaching:
            raise MountError("Mount is being provisioned")
        self.attaching.add(session)
        root = self.root / ("generation-" + uuid.uuid4().hex)
        root.mkdir(mode=0o711)
        state = root / "control"
        state.mkdir(mode=0o700)
        path = root / "work"
        path.mkdir(mode=0o700)
        mount = Attached(session, generation, readonly, port, state, path, client_session_id=client_session)
        try:
            (state / "known_hosts").write_text(f"[127.0.0.1]:{port} {key}\n")
            response = await self.control(mount, "hello")
            if response != {"readonly": readonly, "connected": True}:
                raise MountError("Client mount session or mode mismatch")
            wrapper = state / "ssh-observer"
            argv = ["/usr/bin/python3", "-m", "runtime.mount_observer", str(state),
                    "-F", "/dev/null", "-o", "ClearAllForwardings=yes"]
            wrapper.write_text("#!/bin/sh\ncd " + shlex.quote(str(PROJECT))
                               + "\nexec " + shlex.join(argv) + ' "$@"\n')
            wrapper.chmod(0o700)
            opts = [
                f"port={port}", f"IdentityFile={self.identity}", f"ssh_command={wrapper}",
                "allow_other", "default_permissions", "uid=1000", "gid=1000", "direct_io",
                "dir_cache=no", "attr_timeout=0", "entry_timeout=0", "negative_timeout=0",
                "sshfs_sync", "contain_symlinks", "nodev", "nosuid", "noexec",
                *(option for option in self.ssh_options(mount) if not option.startswith("ClearAllForwardings=")),
            ]
            if readonly:
                opts.append("ro")
            mount.process = await asyncio.create_subprocess_exec(
                str(self.sshfs), "itmo-mount@127.0.0.1:/", str(path), "-f", "-o", ",".join(opts),
                stdout=asyncio.subprocess.DEVNULL, stderr=None,
            )
            async with asyncio.timeout(12):
                while mount.kernel_mount_id is None:
                    mount.kernel_mount_id = await self.mount_id(mount)
                    if mount.process.returncode is not None:
                        raise MountError("SSHFS exited during mount")
                    await asyncio.sleep(0.05)
            self.mounts[session] = mount
            return mount.view()
        except BaseException:
            await self.close_mount(mount)
            raise
        finally:
            self.attaching.discard(session)

    async def close_mount(self, mount):
        mount.broken = True
        if await self.mount_id(mount):
            await command(["fusermount3", "-u", str(mount.path)])
            if await self.mount_id(mount):
                await command(["fusermount3", "-uz", str(mount.path)])
        if mount.process is not None and mount.process.returncode is None:
            mount.process.terminate()
            try:
                await asyncio.wait_for(mount.process.wait(), 5)
            except TimeoutError:
                mount.process.kill()
                await mount.process.wait()
        if await self.mount_id(mount) is not None:
            raise MountError("Mount cleanup could not be confirmed")
        shutil.rmtree(mount.state.parent)

    async def begin(self, mount, run_id, operation_id, kind, expected=None):
        operation = label(operation_id)
        if not await self.present(mount) or mount.active:
            raise MountError("Mount is occupied or lost")
        if kind not in ("ReadFile", "WriteFile", "RunCommand"):
            raise MountError("Invalid mounted operation kind")
        if (mount.state / (operation + ".wire")).exists():
            raise MountError("Mount operation was already started")
        # Claim the lane before yielding to remote authentication.
        mount.active = operation
        try:
            result = await self.control(mount, "begin", run_id=label(run_id),
                                        operation_id=operation, kind=kind, expected=expected)
            if result is not True or mount.active != operation:
                raise MountError("Client did not accept the mounted operation")
            (mount.state / "active").write_text(operation)
            return True
        except BaseException:
            if mount.active == operation:
                mount.active = None
            raise

    async def barrier(self, mount, operation):
        path = mount.state / (operation + ".wire")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and await self.present(mount):
            name = mount.path / (".itmo-mount-barrier-" + uuid.uuid4().hex)
            await command(["stat", "--", str(name)], timeout=3)
            try:
                if path.stat().st_size > 900_000:
                    raise MountError("SFTP operation transcript exceeded its limit")
                payload = path.read_text()
            except FileNotFoundError:
                continue
            records = [json.loads(line) for line in payload.splitlines()]
            if any(record.get("disconnected") for record in records):
                break
            if records and records[-1].get("barrier") and records[-1]["open_handles"] == 0:
                return [record["event"] for record in records if "event" in record]
            await asyncio.sleep(0.05)
        raise MountError("SFTP completion could not be confirmed")

    async def finish(self, mount, operation_id, success):
        operation = label(operation_id)
        if mount.active != operation:
            return await self.control(mount, "query", operation_id=operation)
        try:
            if not success:
                return await self.control(mount, "cancel", operation_id=operation)
            transcript = await self.barrier(mount, operation)
            return await self.control(mount, "finish", operation_id=operation, success=True,
                                      transcript=transcript)
        except (MountError, OSError, TimeoutError, ValueError):
            mount.broken = True
            await self.best_effort_cancel(mount, operation)
            return {"status": "unknown", "outcomes": []}
        finally:
            self.retire(mount, operation)

    @staticmethod
    def retire(mount, operation):
        if mount.active == operation:
            (mount.state / "active").unlink(missing_ok=True)
            mount.active = None
        (mount.state / (operation + ".wire")).unlink(missing_ok=True)

    async def best_effort_cancel(self, mount, operation):
        try:
            return await self.control(mount, "cancel", operation_id=operation)
        except (MountError, OSError, TimeoutError, ValueError):
            return {"status": "unknown", "outcomes": []}

    async def dispatch(self, payload):
        request = dict(payload)
        action = request.pop("action")
        session = request.pop("session_id")
        if action == "attach":
            return await self.attach(session, **request)
        generation = request.pop("generation")
        if action == "detach" and session not in self.mounts:
            return False
        if generation is None and action != "detach":
            raise MountError("Mount generation is required")
        mount = self.resolve(session, generation)
        if action == "check":
            return await self.present(mount)
        if action == "begin":
            return await self.begin(mount, **request)
        if action == "finish":
            return await self.finish(mount, **request)
        if action == "cancel":
            operation = label(request["operation_id"])
            result = await self.best_effort_cancel(mount, operation)
            self.retire(mount, operation)
            return result
        if action in ("query", "changes"):
            return await self.control(mount, action, **request)
        if action == "detach":
            await self.close_mount(mount)
            del self.mounts[mount.session]
            return True
        raise MountError("Unknown mount service action")

    async def request(self, reader, writer):
        try:
            async with asyncio.timeout(88):
                payload = json.loads(await reader.readline())
                response = {"result": await self.dispatch(payload)}
        except (MountError, OSError, TimeoutError, ValueError, KeyError, TypeError) as error:
            response = {"error": str(error) if isinstance(error, MountError) else "Mount operation failed"}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def serve(args):
    root = args.root.resolve()
    root.mkdir(mode=0o711, parents=True, exist_ok=True)
    args.socket.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with (root / ".helper.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await serve_locked(args, root)


async def serve_locked(args, root):
    host = Host(root, args.identity.resolve(strict=True), args.sshfs.resolve(strict=True))
    version, text = await command([str(host.sshfs), "--version"])
    if version != 0 or b"SSHFS version 3.7.6" not in text:
        raise SystemExit("Mount host requires SSHFS 3.7.6")
    for orphan in root.glob("generation-*"):
        if orphan.is_dir() and not orphan.is_symlink():
            mount = Attached("", "", False, 0, orphan / "control", orphan / "work")
            await host.close_mount(mount)
    server = await asyncio.start_unix_server(host.request, path=args.socket, limit=MAX_MESSAGE)
    args.socket.chmod(0o600)
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
    async with server:
        await stopping.wait()
    for mount in list(host.mounts.values()):
        await host.close_mount(mount)
    args.socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--sshfs", type=Path, required=True)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
