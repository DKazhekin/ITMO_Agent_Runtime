"""Exercise submitted Python inside a networkless, read-only container."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import uuid

from check_support import guarded_file

IMAGE = "itmo-sandbox:0.1"
RUNNER = """
from copy import deepcopy
import json
import sys
import types

payload = json.load(sys.stdin)
module = types.ModuleType("submission")
module.__file__ = "submission.py"
sys.modules[module.__name__] = module
source = compile(payload["source"], module.__file__, "exec")
exec(source, module.__dict__)
answers = []
for function, arguments in payload["requests"]:
    original = deepcopy(arguments) if payload["preserve_arguments"] else None
    try:
        result = getattr(module, function)(*arguments)
    except Exception as exc:
        answers.append({"exception": type(exc).__name__})
    else:
        if original is not None and original != arguments:
            answers.append({"arguments_mutated": True})
        elif isinstance(result, tuple) or (
            isinstance(result, list) and any(isinstance(item, tuple) for item in result)
        ):
            answers.append({"invalid_type": "tuple"})
        else:
            answers.append({"value": result})
print(json.dumps(answers, allow_nan=False))
"""


def check_code(work: Path, path: str, cases: list, *, preserve_arguments: bool = False) -> None:
    candidate = guarded_file(work, path)
    name = f"baf-check-{uuid.uuid4().hex}"
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                              "XDG_RUNTIME_DIR", "SYSTEMROOT"}}
    command = ["docker", "run", "--pull=never", "--interactive", "--rm", "--name", name,
               "--network", "none", "--ipc", "none", "--read-only", "--user", "1000:1000",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "16", "--memory", "64m", "--memory-swap", "64m", "--cpus", "0.5",
               "--ulimit", "fsize=1048576:1048576", "--ulimit", "nofile=64:64", "--log-driver", "none",
               IMAGE, "python", "-I", "-B", "-c", RUNNER]
    requests = [[function, arguments] for function, arguments, _ in cases]
    payload = {"source": candidate.read_text(encoding="utf-8"), "requests": requests,
               "preserve_arguments": preserve_arguments}
    output = bytearray()
    try:
        with tempfile.TemporaryFile() as stdin, selectors.DefaultSelector() as selector:
            stdin.write(json.dumps(payload).encode())
            stdin.seek(0)
            with subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=environment) as process:
                try:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + 12
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise subprocess.TimeoutExpired(command, 12)
                        chunk = os.read(process.stdout.fileno(), 8192)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > 65536:
                            raise ValueError("Isolated code output exceeds 64 KiB")
                    exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except (ValueError, subprocess.TimeoutExpired) as exc:
                    process.kill()
                    subprocess.run(["docker", "rm", "-f", name], env=environment,
                                   timeout=4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if isinstance(exc, subprocess.TimeoutExpired):
                        raise ValueError("Isolated code check timed out") from None
                    raise
    except OSError as exc:
        raise ValueError(f"Isolated code checker unavailable: {exc}") from None
    if exit_code == 125:
        raise ValueError("Isolated code checker unavailable: Docker or local checker image is missing")
    if exit_code or not output:
        raise ValueError("Submitted code did not complete in the isolated checker")
    try:
        actual = json.loads(output)
    except ValueError:
        raise ValueError("Submitted code produced invalid check output") from None
    expected = [answer for _, _, answer in cases]
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("Submitted code failed one or more hidden cases")
