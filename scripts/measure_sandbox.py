"""Measure cold container creation, warm exec and idle memory on a running offline stack."""

import argparse
import json
import math
import statistics
import subprocess
from pathlib import Path

from scripts.measure_mounted import COURSE


REMOTE = """
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

with tempfile.TemporaryDirectory(prefix='measure-sandbox-') as state:
    os.environ['STATE_DIR'] = state
    os.environ['SANDBOX_NAMESPACE'] = 'measure-' + uuid.uuid4().hex[:12]
    from runtime import sandbox

    samples = []
    os_release = sandbox.engine.info()
    for _ in range(REPETITIONS):
        name = uuid.uuid4().hex
        started = time.perf_counter_ns()
        container = sandbox.create_container(name)
        cold_ms = (time.perf_counter_ns() - started) / 1e6
        try:
            started = time.perf_counter_ns()
            execution = sandbox.engine.api.exec_create(
                container.id, ['/bin/true'], workdir='/work', user='1000:1000',
            )
            output = sandbox.engine.api.exec_start(execution['Id'])
            exit_code = sandbox.engine.api.exec_inspect(execution['Id'])['ExitCode']
            warm_exec_ms = (time.perf_counter_ns() - started) / 1e6
            assert exit_code == 0 and output == b''
            memory = container.stats(stream=False, one_shot=True)['memory_stats']
            counters = memory['stats']
            cache = counters.get('inactive_file', counters.get('total_inactive_file', 0))
            samples.append({
                'cold_create_and_start_ms': cold_ms,
                'started_container_true_ms': warm_exec_ms,
                'idle_cgroup_usage_bytes': memory['usage'],
                'idle_cgroup_usage_minus_inactive_file_bytes': max(0, memory['usage'] - cache),
            })
        finally:
            sandbox.remove_container(container)
    assert not sandbox.engine.containers.list(all=True, filters={
        'label': sandbox.LABEL + '=' + sandbox.NAMESPACE,
    })
    print(json.dumps({
        'measured_at_utc': datetime.now(timezone.utc).isoformat(),
        'sandbox_image_id': sandbox.engine.images.get(sandbox.IMAGE).id,
        'sandbox_source_sha256': hashlib.sha256(Path(sandbox.__file__).read_bytes()).hexdigest(),
        'docker_host': {key: os_release[key] for key in ('ServerVersion', 'KernelVersion', 'NCPU', 'MemTotal')},
        'samples': samples,
        'remaining_test_containers': 0,
    }))
"""


def describe(values):
    return {
        "median": statistics.median(values),
        "p95": sorted(values)[math.ceil(0.95 * len(values)) - 1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--background", required=True, help="Describe other work sharing the VM")
    args = parser.parse_args()
    if args.repetitions < 10:
        parser.error("Use at least ten repetitions.")

    docker = ["docker", "--context", args.docker_context]
    manager = subprocess.check_output(
        [*docker, "ps", "--filter", f"label=com.docker.compose.project={args.project}",
         "--filter", "label=com.docker.compose.service=sandbox-manager", "--format", "{{.ID}}"], text=True,
    ).splitlines()
    if len(manager) != 1:
        parser.error("Expected one running sandbox manager in the selected project.")
    command = [*docker, "exec", manager[0], "python", "-c", f"REPETITIONS = {args.repetitions}\n" + REMOTE]
    output = subprocess.run(command, text=True, stdout=subprocess.PIPE, check=True)
    evidence = json.loads(output.stdout)
    samples = evidence["samples"]
    assert len(samples) == args.repetitions and evidence["remaining_test_containers"] == 0
    evidence["reference"] = [COURSE + "A-agent-runtime.md#L76", COURSE + "checkpoint-2-questions.md#L84"]
    evidence["method"] = {
        "container_start": "Fresh container and namespace, built image already local. Timer covers runtime.sandbox.create_container, not image build or pull.",
        "exec": "One /bin/true per started container; timer covers Docker exec creation, start and exit inspection. No gateway, model or fixture transfer.",
        "memory": "One Docker cgroup reading per idle container after true returned. Cache-adjusted sample subtracts inactive_file. VM, manager, daemon, host mount helper and parallel capacity are outside this measurement.",
        "background": args.background,
        "percentiles": "Arithmetic median; p95 uses empirical nearest rank.",
    }
    evidence["summary"] = {name: describe([sample[name] for sample in samples]) for name in samples[0]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps(evidence["summary"], indent=2))


if __name__ == "__main__":
    main()
