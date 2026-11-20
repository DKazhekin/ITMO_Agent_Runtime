"""Manager client for the trusted mount service on the Docker host."""

import asyncio
import json
import os


DEFAULT_SOCKET = "/run/itmo-mount/helper.sock"
MAX_MESSAGE = 1_048_576


class MountError(RuntimeError):
    """The requested mount operation was rejected or could not be confirmed."""


class MountClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("MOUNT_HELPER_SOCKET", DEFAULT_SOCKET))

    async def _call(self, action, **payload):
        try:
            async with asyncio.timeout(90):
                reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=MAX_MESSAGE)
                try:
                    writer.write(json.dumps({"action": action, **payload}).encode() + b"\n")
                    await writer.drain()
                    response = json.loads(await reader.readline())
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, ValueError, TimeoutError) as error:
            raise MountError("Mount service is unavailable") from error
        if "error" in response:
            raise MountError(response["error"])
        return response["result"]

    async def attach(self, session_id, descriptor):
        return await self._call("attach", session_id=session_id, descriptor=descriptor)

    async def check(self, session_id, generation):
        return await self._call("check", session_id=session_id, generation=generation)

    async def begin(self, session_id, generation, run_id, operation_id, kind, expected=None):
        return await self._call("begin", session_id=session_id, generation=generation,
                                run_id=run_id, operation_id=operation_id, kind=kind, expected=expected)

    async def finish(self, session_id, generation, operation_id, success):
        return await self._call("finish", session_id=session_id, generation=generation,
                                operation_id=operation_id, success=success)

    async def cancel(self, session_id, generation, operation_id):
        return await self._call("cancel", session_id=session_id, generation=generation,
                                operation_id=operation_id)

    async def query(self, session_id, generation, operation_id):
        return await self._call("query", session_id=session_id, generation=generation,
                                operation_id=operation_id)

    async def changes(self, session_id, generation, run_id):
        return await self._call("changes", session_id=session_id, generation=generation, run_id=run_id)

    async def detach(self, session_id, generation):
        return await self._call("detach", session_id=session_id, generation=generation)
