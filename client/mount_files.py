"""Descriptor-relative access to the selected folder and isolated write drafts."""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import PurePosixPath
import stat


PROTECTED = {".git", ".env", ".ssh", ".aws", ".codex", ".agents", ".ds_store"}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def path_parts(path):
    if not path or path.startswith("/") or "\x00" in path:
        raise PermissionError(errno.EACCES, "Unsafe workspace path")
    parts = path.split("/")
    if any(part in ("", ".", "..") or part.casefold() in PROTECTED
           or part.casefold().startswith((".env.", ".itmo-mount-"))
           for part in parts):
        raise PermissionError(errno.EACCES, "Protected workspace path")
    return parts


class Workspace:
    def __init__(self, root):
        self.root = root
        self.root_fd = os.open(root, DIR_FLAGS)
        self.device = os.fstat(self.root_fd).st_dev

    def close(self):
        os.close(self.root_fd)

    @contextmanager
    def parent(self, path):
        *ancestors, name = path_parts(path)
        fd = os.dup(self.root_fd)
        try:
            for ancestor in ancestors:
                opened = self.open_directory(ancestor, fd)
                os.close(fd)
                fd = opened
            yield fd, name
        finally:
            os.close(fd)

    @contextmanager
    def directory(self, path):
        if not path:
            fd = os.dup(self.root_fd)
        else:
            with self.parent(path) as (parent, name):
                fd = self.open_directory(name, parent)
        try:
            yield fd
        finally:
            os.close(fd)

    def open_directory(self, name, parent):
        descriptor = os.open(name, DIR_FLAGS, dir_fd=parent)
        if os.fstat(descriptor).st_dev == self.device:
            return descriptor
        os.close(descriptor)
        raise PermissionError(errno.EACCES, "Nested mount is outside the workspace device")

    def open(self, path):
        with self.parent(path) as (parent, name):
            descriptor = os.open(name, FILE_FLAGS, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != self.device:
            os.close(descriptor)
            raise PermissionError(errno.EACCES, "Unsupported workspace file")
        return os.fdopen(descriptor, "rb")

    def stat(self, path):
        if not path:
            return os.fstat(self.root_fd)
        with self.parent(path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_dev == self.device and (stat.S_ISDIR(info.st_mode)
                                          or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
            return info
        raise PermissionError(errno.EACCES, "Unsupported workspace file")

    def listing(self, path):
        with self.directory(path) as descriptor:
            names = []
            for name in os.listdir(descriptor):
                child = str(PurePosixPath(path, name))
                try:
                    self.stat(child)
                except (PermissionError, FileNotFoundError):
                    continue
                names.append(name)
            return names

    def version(self, path):
        try:
            with self.open(path) as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        except FileNotFoundError:
            return "missing"

    def publish_new(self, path, draft):
        with self.parent(path) as (parent, name):
            os.link(draft, name, dst_dir_fd=parent, follow_symlinks=False)

    def create_directory(self, path):
        with self.parent(path) as (parent, name):
            os.mkdir(name, mode=0o700, dir_fd=parent)
