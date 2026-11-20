"""Measure mounted and fixture operations on an already-running local deployment."""

import argparse
import base64
import hashlib
import json
import math
import os
import platform
import shlex
import statistics
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import docker

from client.cli import Client
from client.mounted import MountedSession


SMALL_COUNT = 16
SMALL_BYTES = 1024
LARGE_BYTES = 1_048_576
WRITTEN_BYTES = 16_384
OUTPUT_BYTES = b"R" * WRITTEN_BYTES
SERVICES = ("gateway", "harness", "sandbox-manager")
COURSE = "https://github.com/deeppavlov/agentic-course-itmo/blob/818740966b0d5b08fc9e46c3f6e449c824367ba9/projects/"
REMOTE_REQUEST = """
import json, sys, time, urllib.request
method, route, data = json.load(sys.stdin)
url = 'http://sandbox-manager:8000' + route
body = None if data is None else json.dumps(data).encode()
request = urllib.request.Request(url, data=body, method=method, headers={'Content-Type': 'application/json'})
started = time.perf_counter_ns()
with urllib.request.urlopen(request, timeout=120) as response:
    result = json.load(response)
print(json.dumps({'milliseconds': (time.perf_counter_ns() - started) / 1e6, 'result': result}))
"""
HOST_SAMPLE = """
import hashlib, json, pathlib, subprocess
pid = subprocess.check_output(['systemctl', 'show', '--property=MainPID', '--value',
                               'itmo-workspace-mount'], text=True).strip()
assert pid not in ('0', '')

def rss(path):
    lines = pathlib.Path(path, 'status').read_text().splitlines()
    return 1024 * int(next(line.split()[1] for line in lines if line.startswith('VmRSS:')))

fs = []
for proc in pathlib.Path('/proc').iterdir():
    if proc.name.isdecimal():
        try:
            if (proc / 'comm').read_text().strip() == 'sshfs':
                fs.append(rss(proc))
        except (OSError, StopIteration):
            continue
code = pathlib.Path('/opt/itmo-runtime/runtime')
digest = hashlib.sha256()
for path in sorted(code.glob('mount*.py')):
    digest.update(path.name.encode() + b'\\0' + path.read_bytes() + b'\\0')
print(json.dumps({'helper_rss_bytes': rss('/proc/' + pid), 'sshfs_processes': len(fs),
                  'sshfs_rss_bytes': sum(fs), 'helper_source_sha256': digest.hexdigest()}))
"""
FILE_WORKLOAD = f"""
import json, os, pathlib, time
root = pathlib.Path('/work')
small = sorted((root / 'small').glob('*.txt'))
assert len(small) == {SMALL_COUNT}

def measured(action):
    start = time.perf_counter_ns()
    result = action()
    return (time.perf_counter_ns() - start) / 1e6, result

stat, stat_size = measured(lambda: (root / 'big.bin').stat().st_size)
walk, entries = measured(lambda: sum(
    (pathlib.Path(directory) / item).stat().st_size
    for directory, subdirs, files in os.walk(root)
    for item in files
))
small_reads, small_sizes = measured(lambda: sum(len(path.read_bytes()) for path in small))
large_read, large_size = measured(lambda: len((root / 'big.bin').read_bytes()))
write, written = measured(lambda: (root / 'created.bin').write_bytes(b'R' * {WRITTEN_BYTES}))
assert stat_size == large_size == {LARGE_BYTES} and written == {WRITTEN_BYTES}
assert small_sizes == {SMALL_COUNT * SMALL_BYTES} and entries == small_sizes + large_size
memory = int(pathlib.Path('/sys/fs/cgroup/memory.current').read_text())
print(json.dumps({{
    'stat_ms': stat, 'walk_ms': walk, 'read_small_batch_ms': small_reads,
    'read_large_ms': large_read, 'write_in_container_ms': write, 'tool_container_memory_bytes': memory,
}}))
"""


def command(*args, **options):
    return subprocess.run(args, capture_output=True, text=True, check=True, **options).stdout.strip()


class Deployment:
    def __init__(self, context, project, colima_profile=None):
        self.colima_profile = colima_profile
        self.cli = ("docker", "--context", context)
        endpoint = command(*self.cli, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", context)
        self.engine = docker.DockerClient(base_url=endpoint)
        self.containers = {}
        for service in SERVICES:
            matches = self.engine.containers.list(filters={"label": [
                f"com.docker.compose.project={project}", f"com.docker.compose.service={service}",
            ]})
            if len(matches) != 1:
                raise RuntimeError(f"Expected one running {service} in the selected project")
            self.containers[service] = matches[0]

    def request(self, method, route, body=None):
        return json.loads(command(
            *self.cli, "exec", "-i", self.containers["gateway"].id, "python", "-c", REMOTE_REQUEST,
            input=json.dumps((method, route, body)),
        ))

    def memory(self):
        result = {}
        for name, container in self.containers.items():
            memory = container.stats(stream=False, one_shot=True)["memory_stats"]
            recorded = memory["stats"]
            inactive = recorded.get("inactive_file", recorded.get("total_inactive_file", 0))
            result[name] = max(0, memory["usage"] - inactive)
        return result

    def host_memory(self):
        if not self.colima_profile:
            return {}
        sample = json.loads(command("colima", "ssh", "--profile", self.colima_profile,
                                    "--", "python3", "-c", HOST_SAMPLE))
        sample["client_rss_bytes"] = 1024 * int(command("ps", "-o", "rss=", "-p", str(os.getpid())))
        return sample

    def runtime_digest(self):
        code = """
import hashlib, pathlib
root = pathlib.Path('runtime')
data = hashlib.sha256()
for path in sorted(root.glob('*.py')):
    data.update(path.name.encode() + b'\\0' + path.read_bytes() + b'\\0')
print(data.hexdigest())
"""
        return {name: command(*self.cli, "exec", container.id, "python", "-c", code)
                for name, container in self.containers.items()}

    def execute(self, session, shell, *, mounted):
        run = uuid.uuid4().hex
        payload = {"session_id": session, "operation_id": run + "-0-0", "tool": "RunCommand",
                   "arguments": {"command": shell}, "timeout_seconds": 45}
        if mounted:
            payload["run_id"] = run
        response = self.request("POST", "/mounted-exec" if mounted else "/exec", payload)
        result = response["result"]
        if result["status"] != "success":
            raise RuntimeError("Operation failed: " + json.dumps({
                "status": result["status"], "error": result.get("error"), "exit_code": result.get("exit_code"),
                "fixture_stderr": result.get("stderr", ""),
            }))
        return response["milliseconds"], result


def make_fixture(folder):
    small = folder / "small"
    small.mkdir()
    for index in range(SMALL_COUNT):
        (small / f"{index:02}.txt").write_bytes(b"S" * SMALL_BYTES)
    (folder / "big.bin").write_bytes(b"L" * LARGE_BYTES)


def observe_mounted(deployment, client, folder):
    before = deployment.memory()
    host_before = deployment.host_memory()
    started = time.perf_counter_ns()
    session = MountedSession.open(client, folder)
    opened_ms = (time.perf_counter_ns() - started) / 1e6
    try:
        # Resolve the opaque server identifier via an idempotent repeat outside the timing.
        request = {"session_id": session.session_id, "mount": session._mount.descriptor}
        server_id = session._request("POST", "/sessions", request)["session_id"]
        during = deployment.memory()
        host_open = deployment.host_memory()
        if host_open and host_open["helper_source_sha256"] != host_before["helper_source_sha256"]:
            raise RuntimeError("Host mount service changed during the sample.")
        noop_ms, no_op = deployment.execute(server_id, "true", mounted=True)
        fs_ms, operation = deployment.execute(
            server_id, shlex.join(("python", "-I", "-S", "-c", FILE_WORKLOAD)), mounted=True,
        )
        verified = (folder / "created.bin").read_bytes() == OUTPUT_BYTES
        assert verified and any(result["status"] == "applied" for result in operation["write_decisions"])
        return {"open_ms": opened_ms, "noop_exec_ms": noop_ms,
                "noop_manager_ms": 1000 * no_op["duration_seconds"], "workload_exec_ms": fs_ms,
                **json.loads(operation["stdout"]),
                "idle_control_before_bytes": before, "idle_control_open_bytes": during,
                "idle_control_delta_bytes": {service: during[service] - before[service] for service in SERVICES},
                "host_before": host_before, "host_open": host_open,
                "host_delta": {name: host_open[name] - host_before[name] for name in host_open
                               if isinstance(host_open[name], int)},
                "write_verified_on_mac": verified}
    finally:
        session.close()


def observe_fixture(deployment, folder):
    fixture = uuid.uuid4().hex
    files = {file.relative_to(folder).as_posix(): base64.b64encode(file.read_bytes()).decode()
             for file in folder.rglob("*") if file.is_file()}
    before = deployment.memory()
    response = deployment.request("POST", "/sessions", {"session_id": fixture, "files": files})
    if response["result"]["status"] != "ready":
        raise RuntimeError("Fixture session provisioning failed")
    try:
        during = deployment.memory()
        noop_ms, no_op = deployment.execute(fixture, "true", mounted=False)
        fs_ms, operation = deployment.execute(
            fixture, shlex.join(("python", "-I", "-S", "-c", FILE_WORKLOAD)), mounted=False,
        )
        return {"open_ms": response["milliseconds"], "noop_exec_ms": noop_ms,
                "noop_manager_ms": 1000 * no_op["duration_seconds"], "workload_exec_ms": fs_ms,
                **json.loads(operation["stdout"]),
                "idle_control_before_bytes": before, "idle_control_open_bytes": during,
                "idle_control_delta_bytes": {service: during[service] - before[service] for service in SERVICES}}
    finally:
        deployment.request("DELETE", f"/sessions/{fixture}")


def percentiles(values):
    return {"median": statistics.median(values), "p95": sorted(values)[math.ceil(.95 * len(values)) - 1]}


def summarize(samples):
    return {
        **{key: percentiles([sample[key] for sample in samples])
           for key, value in samples[0].items() if type(value) in (int, float)},
        "idle_control_delta_bytes": {service: percentiles(
            [sample["idle_control_delta_bytes"][service] for sample in samples],
        ) for service in SERVICES},
        **({"host_delta": {key: percentiles([sample["host_delta"][key] for sample in samples])
                           for key in samples[0]["host_delta"]}}
           if samples[0].get("host_delta") else {}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--colima-profile", help="Include Linux helper and Mac client resident memory")
    parser.add_argument("--url")
    parser.add_argument("--background", default="No separate workload recorded.")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Resume a partial result from unchanged code")
    parser.add_argument("--previous", type=Path, help="Keep a previous deployment measurement separately")
    args = parser.parse_args()
    if args.repetitions < 10:
        parser.error("Use at least ten repetitions.")
    deployment = Deployment(args.docker_context, args.project, args.colima_profile)
    host = deployment.engine.info()
    helper = deployment.host_memory()
    samples = {"mounted": [], "fixture": []}
    measurements = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "client_os": platform.platform(),
        "docker_host": {key: host[key] for key in ("NCPU", "MemTotal", "KernelVersion", "ServerVersion")},
        "mount_helper_sha256": helper.get("helper_source_sha256"),
        "server_runtime_sha256": deployment.runtime_digest(),
        "client_source_sha256": hashlib.sha256(b"".join(
            path.read_bytes() for path in sorted(Path("client").glob("*.py"))
        )).hexdigest(),
        "reference": [COURSE + "A-agent-runtime.md#L57", COURSE + "A-agent-runtime.md#L76",
                      COURSE + "checkpoint-2-questions.md#L84"],
        "small_files": {"count": SMALL_COUNT, "bytes_each": SMALL_BYTES},
        "large_file_bytes": LARGE_BYTES,
        "written_file_bytes": WRITTEN_BYTES,
        "method": {
            "environment": "Running local services and built images; serial independent sessions, no model call.",
            "background": args.background,
            "open_ms": "Mounted: Mac file service, SSH reverse tunnel and HTTP provision. Fixture: only manager HTTP provision with payload prepared.",
            "noop_exec_ms": "A true command round trip measured inside the control network; includes acknowledgement and mounted container lifecycle.",
            "noop_manager_ms": "Duration recorded by the sandbox manager for the same true command.",
            "workload_exec_ms": "The complete file workload round trip inside the control network.",
            "file_operations": "Times measured inside the work container; mounted write timer ends after staging, and receipt/actual publication checked separately.",
            "memory": "Container cgroup memory.current during the workload; idle service deltas use memory usage less inactive_file. Optional host deltas use RSS for the mount service, SSHFS processes and this Mac client process, independently. These values are not summed. Linux ssh and the Docker daemon are excluded.",
            "percentiles": "Arithmetic median; p95 is empirical nearest rank.",
            "parallelism": "Not measured.",
        },
        "samples": samples,
        "failures": [],
    }
    if args.previous:
        measurements["previous_deployment"] = json.loads(args.previous.read_text())
    if args.resume:
        previous = json.loads(args.output.read_text())
        for field in ("server_runtime_sha256", "client_source_sha256", "client_os", "docker_host",
                      "mount_helper_sha256"):
            if previous.get(field) != measurements[field]:
                parser.error(f"Cannot combine runs with different {field}.")
        measurements = previous
        samples = measurements["samples"]
        measurements.setdefault("failures", [])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    client = Client(args.url)
    try:
        while len(samples["mounted"]) < args.repetitions:
            if len(measurements["failures"]) > args.repetitions:
                break
            pair = {}
            try:
                for mode, observe in (("mounted", lambda root: observe_mounted(deployment, client, root)),
                                      ("fixture", lambda root: observe_fixture(deployment, root))):
                    with tempfile.TemporaryDirectory(prefix="measure-agent-runtime-") as directory:
                        root = Path(directory)
                        make_fixture(root)
                        pair[mode] = observe(root)
                for mode, sample in pair.items():
                    samples[mode].append(sample)
                print(f"completed pairs {len(samples['mounted'])}/{args.repetitions}", flush=True)
            except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
                detail = error.stderr[-600:] if isinstance(error, subprocess.CalledProcessError) else str(error)
                measurements["failures"].append({
                    "before_pair": len(samples["mounted"]) + 1, "mode": mode,
                    "type": type(error).__name__, "detail": detail,
                })
                print(f"{mode} attempt failed: {detail}", flush=True)
            if samples["mounted"]:
                measurements["summary"] = {mode: summarize(data) for mode, data in samples.items()}
            temp = args.output.with_suffix(args.output.suffix + ".tmp")
            temp.write_text(json.dumps(measurements, indent=2) + "\n")
            temp.replace(args.output)
        if len(samples["mounted"]) != args.repetitions:
            raise RuntimeError("Too many failed attempts to complete the measurement.")
    finally:
        client.http.close()
        deployment.engine.close()


if __name__ == "__main__":
    main()
