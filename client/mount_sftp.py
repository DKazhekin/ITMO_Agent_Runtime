"""Restricted SFTP view of the selected folder and its current draft."""

import asyncio
import errno
import hashlib
import io
import os
import posixpath

import asyncssh

from client.mount_files import path_parts
from runtime.mount_wire import remote_name


class MountSFTP(asyncssh.SFTPServer):
    def __init__(self, channel, state):
        super().__init__(channel)
        self.state = state
        self.handles = {}
        self.snapshots = {}
        self.snapshot_bytes = 0

    def realpath(self, path):
        name = remote_name(path)
        if name:
            path_parts(name)
        return os.fsencode("/" + name)

    def stat(self, path):
        return self.state.edit_stat(remote_name(path))

    lstat = stat

    async def scandir(self, path):
        root = remote_name(path)
        operation = self.state.current
        staged = operation.edits.get(root) if operation else None
        names = set() if staged and staged.kind == "mkdir" else set(self.state.workspace.listing(root))
        if operation is not None:
            for path, edit in operation.edits.items():
                if posixpath.dirname(path) != root:
                    continue
                name = posixpath.basename(path)
                if edit.kind == "delete":
                    names.discard(name)
                else:
                    names.add(name)
        for name in sorted(names):
            attrs = self.state.edit_stat(posixpath.join(root, name))
            yield asyncssh.SFTPName(os.fsencode(name), attrs=asyncssh.SFTPAttrs.from_local(attrs))

    def open(self, path, flags, attrs):
        name = remote_name(path)
        path_parts(name)
        if len(self.handles) + len(self.snapshots) >= 128:
            raise OSError(errno.EMFILE, "Workspace open handle quota exceeded")
        operation = self.state.current
        edit = operation.edits.get(name) if operation else None
        if flags & (asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_TRUNC):
            op = self.state.mutable()
            if flags & asyncssh.FXF_EXCL:
                try:
                    self.state.edit_stat(name)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(errno.EEXIST, "Workspace path already exists")
            elif not flags & asyncssh.FXF_CREAT:
                self.state.edit_stat(name)
            edit = self.state.write_edit(name, truncate=bool(flags & asyncssh.FXF_TRUNC))
            if flags & asyncssh.FXF_TRUNC:
                edit.draft.write_bytes(b"")
            handle = edit.draft.open("r+b", buffering=0)
            op.handles += 1
            op.received.append(["open", name, flags])
            self.handles[handle] = (op, name, flags)
            return handle
        if edit and edit.kind == "delete":
            raise FileNotFoundError(errno.ENOENT, "Workspace file has been removed")
        if edit and edit.draft:
            handle = edit.draft.open("rb")
        else:
            handle = self.state.workspace.open(name)
            if operation is not None:
                # Capture and serve a single snapshot. A digest of a separate read is not a read version.
                available = self.state.quota_bytes - self.snapshot_bytes
                with handle:
                    data = handle.read(available + 1)
                if len(data) > available:
                    raise OSError(errno.EFBIG, "Workspace read exceeds quota")
                self.state.record_read(name, hashlib.sha256(data).hexdigest())
                handle = io.BytesIO(data)
                self.snapshots[handle] = len(data)
                self.snapshot_bytes += len(data)
        return handle

    def fstat(self, handle):
        try:
            return super().fstat(handle)
        except OSError:
            return asyncssh.SFTPAttrs(size=len(handle.getbuffer()), permissions=0o100600)

    def write(self, handle, offset, data):
        op, path, flags = self.handles[handle]
        if self.state.mutable() is not op:
            raise PermissionError(errno.EACCES, "Stale workspace descriptor")
        size = os.fstat(handle.fileno()).st_size
        actual_offset = size if flags & asyncssh.FXF_APPEND else offset
        self.state.check_quota(max(0, actual_offset + len(data) - size))
        handle.seek(actual_offset)
        written = handle.write(data)
        if written != len(data):
            op.unstable = True
            raise OSError(errno.EIO, "Partial draft write")
        op.received.append(self.state.write_record(path, offset, data))
        return written

    def close(self, handle):
        binding = self.handles.pop(handle, None)
        self.snapshot_bytes -= self.snapshots.pop(handle, 0)
        try:
            handle.close()
        finally:
            if binding is not None:
                op, path, _ = binding
                op.handles -= 1
                op.received.append(["close", path])

    def mkdir(self, path, attrs):
        name = remote_name(path)
        op = self.state.mutable()
        self.state.mkdir(name)
        op.received.append(["mkdir", name])

    def remove(self, path):
        name = remote_name(path)
        op = self.state.mutable()
        self.state.remove(name)
        op.received.append(["remove", name])

    def rename(self, oldpath, newpath):
        old, new = map(remote_name, (oldpath, newpath))
        op = self.state.mutable()
        self.state.rename(old, new)
        op.received.append(["rename", old, new])

    posix_rename = rename

    def setstat(self, path, attrs):
        self._truncate(remote_name(path), attrs)

    def fsetstat(self, handle, attrs):
        try:
            op, path, _ = self.handles[handle]
        except KeyError as error:
            raise PermissionError(errno.EACCES, "Read-only descriptor") from error
        if op is not self.state.current:
            raise PermissionError(errno.EACCES, "Stale workspace descriptor")
        self._truncate(path, attrs)

    def _truncate(self, path, attrs):
        if attrs.size is None or any(getattr(attrs, name) is not None
                for name in ("permissions", "uid", "gid", "atime", "mtime")):
            raise PermissionError(errno.EACCES, "Only file size can be changed")
        op = self.state.mutable()
        edit = self.state.write_edit(path)
        self.state.check_quota(max(0, attrs.size - edit.draft.stat().st_size))
        with edit.draft.open("r+b") as draft:
            draft.truncate(attrs.size)
        op.received.append(["truncate", path, attrs.size])

    def rejected(self, *args):
        raise PermissionError(errno.EACCES, "Unsupported workspace operation")

    rmdir = symlink = link = readlink = lsetstat = statvfs = fstatvfs = rejected

    async def exit(self):
        # Channel EOF and explicit close share the file callback. The Linux transcript
        # must prove the latter before publication.
        await asyncio.to_thread(self.state.disconnect)
