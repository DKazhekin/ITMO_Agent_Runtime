"""Normalize acknowledged SFTP mutations without retaining their contents."""

import hashlib
import struct


def digest(data):
    return hashlib.sha256(data).hexdigest()


def uint32(data):
    return struct.unpack_from(">I", data)[0]


def sftp_string(data):
    size = uint32(data)
    if size > len(data) - 4:
        raise ValueError("Truncated SFTP string")
    return data[4:4 + size], data[4 + size:]


def remote_name(value):
    path = value.decode("utf-8", errors="strict")
    if path in ("/", "/.", ".", ""):
        return ""
    return path.removeprefix("/")
