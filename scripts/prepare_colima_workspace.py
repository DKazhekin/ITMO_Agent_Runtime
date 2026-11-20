"""Install the trusted mount helper in a dedicated, already prepared Colima VM."""

import argparse
import io
import json
import re
import shlex
import subprocess
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.profile):
        parser.error("Invalid Colima profile")
    vm = ["colima", "ssh", "--profile", args.profile, "--"]

    def guest(*command, data=None):
        return subprocess.run([*vm, *command], input=data, stdout=subprocess.PIPE, check=True).stdout

    root = Path(__file__).resolve().parents[1]
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w:gz") as archive:
        for path in [root / "runtime/__init__.py", *sorted((root / "runtime").glob("mount*.py"))]:
            archive.add(path, arcname=str(path.relative_to(root)))
    guest("sudo", "install", "-d", "-m", "0755", "/opt/itmo-runtime", "/run/itmo-mount")
    guest("sudo", "install", "-d", "-m", "0700", "/etc/itmo-mount")
    guest("sudo", "tar", "-xzf", "-", "-C", "/opt/itmo-runtime", data=package.getvalue())
    guest("sudo", "sh", "-c", "test -f /etc/itmo-mount/id_ed25519 || "
          "ssh-keygen -q -t ed25519 -N '' -f /etc/itmo-mount/id_ed25519")
    host_pub = guest("cat", "/etc/ssh/ssh_host_ed25519_key.pub").decode().strip()
    mount_pub = guest("sudo", "cat", "/etc/itmo-mount/id_ed25519.pub")
    unit = """[Unit]
Description=Workspace mount helper for the local Docker daemon
After=network.target

[Service]
WorkingDirectory=/opt/itmo-runtime
RuntimeDirectory=itmo-mount
RuntimeDirectoryPreserve=restart
ExecStart=/usr/bin/python3 -m runtime.mount_host --socket /run/itmo-mount/helper.sock --root /var/lib/itmo-mount --identity /etc/itmo-mount/id_ed25519 --sshfs /tmp/itmo-sshfs-build/build/sshfs

[Install]
WantedBy=multi-user.target
"""
    guest("sudo", "tee", "/etc/systemd/system/itmo-workspace-mount.service", data=unit.encode())
    guest("sudo", "systemctl", "daemon-reload")
    guest("sudo", "systemctl", "start", "itmo-workspace-mount")
    guest("systemctl", "is-active", "itmo-workspace-mount")

    machine = "colima" if args.profile == "default" else "colima-" + args.profile
    config = Path.home() / ".colima/_lima" / machine / "ssh.config"
    entries = {}
    for line in config.read_text().splitlines():
        fields = shlex.split(line, comments=True)
        if len(fields) == 2:
            entries[fields[0].lower()] = fields[1]
    host, port = entries.get("hostname", "127.0.0.1"), int(entries["port"])
    directory = args.output.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    known = directory / "known_hosts"
    public = directory / "mount_host.pub"
    known.write_text(f"[{host}]:{port} {host_pub}\n")
    public.write_bytes(mount_pub)
    values = {"MOUNT_SSH_HOST": host, "MOUNT_SSH_USER": entries["user"], "MOUNT_SSH_PORT": str(port),
              "MOUNT_SSH_CLIENT_KEY": entries["identityfile"], "MOUNT_SSH_KNOWN_HOSTS": str(known),
              "MOUNT_SERVER_PUBLIC_KEY": str(public)}
    (directory / "mount.env").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in values.items()))
    (directory / "connection.json").write_text(json.dumps(values, indent=2) + "\n")
    print(f"Mount helper started. Client connection settings: {directory / 'mount.env'}")


if __name__ == "__main__":
    main()
