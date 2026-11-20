"""SSHFS transport observer for supported SFTP v3 filesystem mutations."""

import asyncio
import json
import os
from pathlib import Path
import struct
import sys

from runtime.mount_wire import digest, remote_name, sftp_string, uint32


class Observer:
    def __init__(self, directory):
        self.directory = directory
        self.requests = {}
        self.handles = {}

    def active(self):
        try:
            return (self.directory / "active").read_text().strip()
        except FileNotFoundError:
            return ""

    def log(self, operation, **record):
        if operation:
            with (self.directory / (operation + ".wire")).open("a") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")

    def client(self, packet):
        kind, request, data = packet[0], uint32(packet[1:]), packet[5:]
        operation = self.active()
        if kind == 3:
            path, attrs = sftp_string(data)
            flags = uint32(attrs)
            if flags & 26:
                self.requests[request] = (operation, ["open", remote_name(path), flags])
        elif kind in (4, 6, 10):
            handle, attrs = sftp_string(data)
            binding = self.handles.get(handle)
            if binding is None:
                return
            owner, path = binding
            if kind == 4:
                event = ["close", path]
            elif kind == 6:
                offset = struct.unpack_from(">Q", attrs)[0]
                data, _ = sftp_string(attrs[8:])
                event = ["write", path, offset, len(data), digest(data)]
            else:
                event = self.truncate(path, attrs)
            self.requests[request] = (owner, event, handle)
        elif kind in (9, 13, 14, 15, 18):
            path, attrs = sftp_string(data)
            name = remote_name(path)
            if kind == 9:
                event = self.truncate(name, attrs)
            elif kind == 18:
                target, _ = sftp_string(attrs)
                event = ["rename", name, remote_name(target)]
            else:
                event = [{13: "remove", 14: "mkdir", 15: "rmdir"}[kind], name]
            self.requests[request] = (operation, event)
        elif kind == 200:
            extension, payload = sftp_string(data)
            if extension == b"posix-rename@openssh.com":
                origin, other = sftp_string(payload)
                target, _ = sftp_string(other)
                self.requests[request] = (
                    operation, ["rename", remote_name(origin), remote_name(target)]
                )
        elif kind in (7, 17):
            path, _ = sftp_string(data)
            name = remote_name(path)
            if name.startswith(".itmo-mount-barrier-"):
                self.requests[request] = (operation, ["barrier"])

    @staticmethod
    def truncate(path, attrs):
        flags = uint32(attrs)
        if flags == 1:
            return ["truncate", path, struct.unpack_from(">Q", attrs, 4)[0]]
        return ["unsupported_metadata", path]

    def server(self, packet):
        kind, request, data = packet[0], uint32(packet[1:]), packet[5:]
        pending = self.requests.pop(request, None)
        if pending is None:
            return
        operation, event, *handle = pending
        if event[0] == "barrier":
            self.log(operation, barrier=True,
                     open_handles=sum(owner == operation for owner, _ in self.handles.values()))
            return
        if event[0] == "open" and kind == 102:
            key, _ = sftp_string(data)
            self.handles[key] = (operation, event[1])
            self.log(operation, event=event)
        elif kind == 101 and uint32(data) == 0:
            if event[0] == "close" and handle:
                self.handles.pop(handle[0], None)
            self.log(operation, event=event)
        else:
            self.log(operation, rejected=True)

    def disconnected(self):
        operations = {owner for owner, _ in self.handles.values()}
        operations.add(self.active())
        for operation in operations:
            self.log(operation, disconnected=True)


async def forward(reader, write, observe):
    while True:
        try:
            header = await reader.readexactly(4)
        except asyncio.IncompleteReadError as error:
            if error.partial:
                raise
            return
        length = uint32(header)
        if not 1 <= length <= 1024 * 1024:
            raise ValueError("Invalid SFTP message size")
        packet = await reader.readexactly(length)
        if len(packet) >= 5:
            observe(packet)
        await write(header + packet)


async def write_fd(fd, data):
    """Forward the full frame even when SSHFS gives its child a nonblocking pipe."""
    loop = asyncio.get_running_loop()
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            ready = loop.create_future()

            def writable():
                if not ready.done():
                    ready.set_result(None)

            loop.add_writer(fd, writable)
            try:
                await ready
            finally:
                loop.remove_writer(fd)
        else:
            remaining = remaining[written:]


async def serve(directory, args):
    observer = Observer(directory)
    ssh = await asyncio.create_subprocess_exec(
        "/usr/bin/ssh", *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    source = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(source), sys.stdin.buffer)

    async def to_ssh(data):
        ssh.stdin.write(data)
        await ssh.stdin.drain()

    async def to_sshfs(data):
        await write_fd(sys.stdout.fileno(), data)

    outbound = asyncio.create_task(forward(source, to_ssh, observer.client))
    inbound = asyncio.create_task(forward(ssh.stdout, to_sshfs, observer.server))
    try:
        finished, _ = await asyncio.wait((outbound, inbound), return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            task.result()
    finally:
        for task in (outbound, inbound):
            task.cancel()
        observer.disconnected()
        if ssh.returncode is None:
            ssh.terminate()
            await ssh.wait()


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), sys.argv[2:]))
