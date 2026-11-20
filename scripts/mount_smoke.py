"""Exercise the trusted mount helper with a disposable synthetic folder on macOS."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import uuid

from client.mount import MacMount


CHECKS = []
SESSION = "mount-smoke-" + uuid.uuid4().hex[:16]


def record(name, actual, expected):
    CHECKS.append(dict(name=name, actual=actual, expected=expected, passed=actual == expected))
    print(f"{name}: {actual!r}", flush=True)
    assert actual == expected, (name, actual, expected)


async def guest(*args, expect=0):
    result = await asyncio.to_thread(
        subprocess.run, ["/usr/bin/ssh", "-F", "/dev/null", "-o", "IdentitiesOnly=yes",
                         "-o", "StrictHostKeyChecking=yes", "-o",
                         "UserKnownHostsFile=" + os.environ["MOUNT_SSH_KNOWN_HOSTS"],
                         "-i", os.environ["MOUNT_SSH_CLIENT_KEY"], "-p", os.environ["MOUNT_SSH_PORT"],
                         os.environ["MOUNT_SSH_USER"] + "@" + os.environ["MOUNT_SSH_HOST"],
                         shlex.join(args)],
        text=True, capture_output=True, timeout=100,
    )
    assert result.returncode == expect, (result.returncode, result.stderr, result.stdout)
    return result.stdout


async def main(args):
    with tempfile.TemporaryDirectory(prefix="itmo-product-mount-", dir=args.workspace_parent) as raw:
        folder = Path(raw) / "shared"
        folder.mkdir()
        (folder / "original").write_text("8000")
        (folder / ".env").write_text("synthetic-protected")
        mount = await MacMount.open(
            folder, SESSION, ssh_host=os.environ["MOUNT_SSH_HOST"],
            ssh_user=os.environ["MOUNT_SSH_USER"], ssh_port=int(os.environ["MOUNT_SSH_PORT"]),
            ssh_known_hosts=os.environ["MOUNT_SSH_KNOWN_HOSTS"],
            ssh_client_key=os.environ["MOUNT_SSH_CLIENT_KEY"],
            server_mount_key=os.environ["MOUNT_SERVER_PUBLIC_KEY"],
            coordinated=args.coordinated, readonly=args.case == "read-only",
        )
        attached = False

        async def rpc(action, **fields):
            call = {"action": action, "session_id": SESSION, **fields}
            if action != "attach":
                call["generation"] = mount.generation
            cmd = "import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect('/run/itmo-mount/helper.sock');s.sendall(sys.argv[1].encode()+b'\\n');print(s.makefile().readline().strip())"
            value = json.loads(await guest("sudo", "python3", "-c", cmd, json.dumps(call)))
            assert "result" in value, value
            return value["result"]

        try:
            data = await rpc("attach", descriptor=mount.descriptor)
            attached = True
            path = Path(data["path"])
            record("real_mount", (await guest("sudo", "findmnt", "-n", "-M", str(path), "-o", "FSTYPE")).strip(), "fuse.sshfs")
            if args.case == "read-only":
                mount_options = await guest("sudo", "findmnt", "-n", "-M", str(path), "-o", "OPTIONS")
                record("kernel_read_only", data["readonly"] and "ro" in mount_options.strip().split(","), True)
                await rpc("begin", run_id="run", operation_id="read-only", kind="RunCommand")
                record("read_only_original_visible",
                       (await guest("sudo", "cat", str(path / "original"))).strip(), "8000")
                await guest("sudo", "touch", str(path / "rejected"), expect=1)
                record("read_only_new_file_absent", (folder / "rejected").exists(), False)
                record("read_only_close", (await rpc("finish", operation_id="read-only", success=True))["status"],
                       "unchanged")
                return
            if args.case == "disconnect":
                await rpc("begin", run_id="run", operation_id="disconnect", kind="RunCommand")
                writer = asyncio.create_task(guest("sudo", "python3", "-c",
                    "import sys,time; f=open(sys.argv[1],'wb',buffering=0);f.write(b'8');time.sleep(4);f.close()",
                    str(path / "original"), expect=1))
                try:
                    async with asyncio.timeout(3):
                        while not mount.state.current.edits.get("original"):
                            await asyncio.sleep(0.02)
                        draft = mount.state.current.edits["original"].draft
                        while draft.stat().st_size != 1:
                            await asyncio.sleep(0.02)
                    record("partial_bytes_staged", draft.read_text(), "8")
                    mount.transport.close()
                    await mount.transport.wait_closed()
                    record("known_original_preserved_at_disconnect", (folder / "original").read_text(), "8000")
                    outcome = await rpc("finish", operation_id="disconnect", success=True)
                    record("disconnect_reports_unknown", outcome["status"], "unknown")
                    record("closed_mount_is_unavailable", await rpc("check"), False)
                    try:
                        await rpc("attach", descriptor=mount.descriptor)
                    except AssertionError:
                        refused = True
                    else:
                        refused = False
                    record("same_generation_is_not_reattached", refused, True)
                finally:
                    await writer
                return
            record("record_version_read", await rpc("begin", run_id="run", operation_id="read", kind="ReadFile"), True)
            record("linux_reads_mac", (await guest("sudo", "cat", str(path / "original"))).strip(), "8000")
            record("read_confirmed", (await rpc("finish", operation_id="read", success=True))["status"], "unchanged")
            await guest("sudo", "cat", str(path / ".env"), expect=1)
            content = b"\x00\xffcontent\x80"
            expected = {"path": "output", "length": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            record("begin_binary", await rpc("begin", run_id="run", operation_id="binary", kind="WriteFile", expected=expected), True)
            await guest("sudo", "python3", "-c", "import sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))", str(path / "output"), content.hex())
            record("before_confirmation", (folder / "output").exists(), False)
            reply = await rpc("finish", operation_id="binary", success=True)
            record("binary_published", reply["status"], "applied")
            record("binary_content", (folder / "output").read_bytes().hex(), content.hex())
            record("retry_idempotent", await rpc("query", operation_id="binary"), reply)
            await rpc("begin", run_id="run", operation_id="rename", kind="RunCommand")
            await guest("sudo", "python3", "-c",
                        "import pathlib,sys,os; p=pathlib.Path(sys.argv[1]); (p/'sub').mkdir(); (p/'sub/temp').write_text('nested'); os.replace(p/'sub/temp',p/'sub/final')", str(path))
            record("rename_applied", (await rpc("finish", operation_id="rename", success=True))["status"], "applied")
            record("renamed_content", (folder / "sub/final").read_text(), "nested")
            await rpc("begin", run_id="run", operation_id="edit", kind="RunCommand")
            await guest("sudo", "python3", "-c",
                        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text('8080')", str(path / "original"))
            result = await rpc("finish", operation_id="edit", success=True)
            if args.coordinated:
                record("coordinated_real_edit", result["status"], "applied")
                record("coordinated_original", (folder / "original").read_text(), "8080")
            else:
                record("unverified_writer_proposal", result["status"], "proposal")
                record("proposal_checksum", mount.proposal_path(result["outcomes"][0]["proposal_id"]).read_text(), "8080")
                record("unverified_original", (folder / "original").read_text(), "8000")
            await rpc("begin", run_id="run", operation_id="offsets", kind="RunCommand")
            await guest("sudo", "python3", "-c",
                        "import sys; f=open(sys.argv[1],'wb');f.write(b'abcdef');f.close();f=open(sys.argv[1],'r+b',buffering=0);f.seek(4);f.write(b'XX');f.seek(1);f.write(b'Q');f.truncate(5);f.close();f=open(sys.argv[1],'ab');f.write(b'Y');f.close()", str(path / "offsets"))
            record("non_sequential_apply", (await rpc("finish", operation_id="offsets", success=True))["status"], "applied")
            record("non_sequential_binary_result", (folder / "offsets").read_bytes().hex(), b"aQcdXY".hex())

            await rpc("begin", run_id="run", operation_id="temporary-delete", kind="RunCommand")
            await guest("sudo", "python3", "-c",
                        "import pathlib,sys; p=pathlib.Path(sys.argv[1]);p.write_bytes(b'temporary');p.unlink()", str(path / "removed"))
            record("staged_create_and_delete", (await rpc("finish", operation_id="temporary-delete", success=True))["status"], "unchanged")
            record("removed_file_not_published", (folder / "removed").exists(), False)

            (folder / "original").write_text("9000")
            first = await rpc("changes", run_id="run")
            record("client_detects_local_change", [item["path"] for item in first], ["original"])
            record("client_replays_change_without_read", await rpc("changes", run_id="run"), first)
            await rpc("begin", run_id="run", operation_id="conflict", kind="RunCommand")
            await guest("sudo", "python3", "-c",
                        "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('8222')", str(path / "original"))
            result = await rpc("finish", operation_id="conflict", success=True)
            record("original_conflict", result["status"], "conflict")
            record("original_preserved", (folder / "original").read_text(), "9000")
            if args.coordinated:
                (folder / "move-source").write_bytes(b"move bytes")
                (folder / "move-target").write_bytes(b"old target")
                for item in ("move-source", "move-target"):
                    await rpc("begin", run_id="run", operation_id=item, kind="ReadFile")
                    await guest("sudo", "cat", str(path / item))
                    await rpc("finish", operation_id=item, success=True)
                await rpc("begin", run_id="run", operation_id="move", kind="RunCommand")
                await guest("sudo", "mv", "-f", str(path / "move-source"), str(path / "move-target"))
                record("coordinated_existing_rename", (await rpc("finish", operation_id="move", success=True))["status"], "applied")
                record("coordinated_source_removed", (folder / "move-source").exists(), False)
                record("coordinated_destination_bytes", (folder / "move-target").read_bytes().hex(), b"move bytes".hex())
                await rpc("begin", run_id="run", operation_id="delete-existing", kind="RunCommand")
                await guest("sudo", "rm", str(path / "move-target"))
                record("coordinated_delete_applied", (await rpc("finish", operation_id="delete-existing", success=True))["status"], "applied")
                record("coordinated_delete_visible", (folder / "move-target").exists(), False)
        finally:
            if attached:
                record("detach", await rpc("detach"), True)
            await mount.aclose()
            record("client_cleanup", mount.stage, None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--coordinated", action="store_true")
    parser.add_argument("--case", choices=("files", "read-only", "disconnect"), default="files")
    parser.add_argument("--workspace-parent", type=Path, default=Path("/private/tmp"))
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    finally:
        args.report.write_text(json.dumps(CHECKS, indent=2) + "\n")
