# ITMO Agent Runtime

A client selects a folder on a Mac and sends a task to a Linux server. The model can read files, propose edits and run commands in a network-isolated container. The desktop application and the terminal client use the same workspace session.

**Checkpoint 2** covers 35 single-function tasks across nine groups. Open the [six offline slides](docs/checkpoint-2/slides.html) in a browser and use the arrow keys to present. The [speaker notes and 30 course questions](docs/checkpoint-2/questions.md), [demonstration instructions](docs/checkpoint-2/workspace.md) and [recorded results](evidence/checkpoint-2/README.md) accompany the slides. The first checkpoint evaluated a different 15-task set and recorded [15/15 with DeepSeek](evidence/baf/real/20261106T125440Z-e6a11cdd/summary.json).

## Architecture

- [Gateway](runtime/gateway.py): loopback HTTP endpoint, bearer authentication and forwarding.
- [Harness](runtime/harness.py): model/tool loop, context, cancellation and bounded execution.
- [Sandbox manager](runtime/sandbox.py): container lifecycle, command execution and artifact collection.
- [Model proxy](runtime/proxy.py): deterministic replay or a remote HTTPS model, with credentials isolated from the agent.
- [Desktop application](client/desktop.py) and [terminal client](client/cli.py) share [session ownership, recovery and proposal export](client/mounted.py).
- [Protocols](contracts/README.md): request identities, errors and resumable event cursors.

Tool execution uses a non-root container with no network, no capabilities, a read-only root filesystem and bounded CPU, memory, process count and temporary storage. The manager has Docker socket access and is a trusted administrative service. This deployment targets a trusted local development environment; containers share the Linux host's kernel.

A mounted workspace uses an authenticated reverse SSH connection to a private file service on the Mac. A trusted Linux helper mounts it with SSHFS. Each mounted operation gets a fresh, isolated action container. The manager removes that container before the Mac publishes a confirmed edit. A synthetic fixture can also be supplied as a snapshot. [Mounted workspace protocol](docs/checkpoint-2/workspace.md) · [Architecture decision](docs/adr/0003-mounting.md).

## Requirements

Use a Linux Docker Engine with Compose, cgroup v2 limits and access to `/var/run/docker.sock`. On Apple Silicon, the tested server runs in a Colima Ubuntu 24.04.4 `aarch64` virtual machine with 4 CPUs and 6 GiB RAM. See the [tested environment](docs/reference/environment.md).

Initial image and dependency preparation requires network access. Python dependencies are fixed in [uv.lock](uv.lock) and the hash-checked [requirements.lock](requirements.lock); base images are pinned in the Dockerfiles. The host-side client requires Python 3.12 and `uv`:

```sh
uv sync --frozen
```

## Start the server

From the repository root:

```sh
cp .env.example .env
docker compose up --build
```

In another terminal:

```sh
docker compose ps
curl --fail http://127.0.0.1:8080/health
```

The default is `MODEL_MODE=mock`, with a local demonstration token and gateway bound to `127.0.0.1:8080`. If `CLIENT_TOKEN` changes in `.env`, set the same value in the client environment; the client does not load `.env` automatically. The one-shot `sandbox-image` service exits after preparing its image; the four server services expose readiness checks.

If only the standalone Compose command is installed, replace `docker compose` with `docker-compose`, or use `sh scripts/compose.sh`. Remote access requires an authenticated SSH tunnel or a configured TLS endpoint. The language model can run remotely even when the runtime server is local.

## Run a file task

With the mock server ready:

```sh
DEMO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/itmo-demo.XXXXXX")
.venv/bin/python -m client.cli \
  --fixture baf/B1-read/B1.1-service-port/fixture \
  --output "$DEMO_DIR" \
  "Read the service port from config/app.yaml and write only the number to answer.txt."
.venv/bin/python baf/B1-read/B1.1-service-port/check.py "$DEMO_DIR"
cat "$DEMO_DIR/answer.txt"
```

Expected content: `8137`. The independent checker must report `"pass": true`; this original course checker uses a JSON verdict even when its exit code is zero. The mock proxy deterministically requests `ReadFile` and `WriteFile`, so this demonstrates the execution path rather than model quality.

To save an attempt with its events and artifacts:

```sh
.venv/bin/python -m baf.run --mode mock --task B1.1
```

Without an explicit task, mock mode runs one simple example for each of the nine functions. The paid evaluation uses a fresh mounted synthetic directory for every task. [Task definitions and checking](docs/baf.md).

## Verify

After preparing images and dependencies, run all offline checks with:

```sh
sh scripts/check.sh
```

The command uses existing images without builds or pulls, forces mock mode, clears provider credentials and removes the model proxy's external network. It runs infrastructure and client tests, validates the 35 task checkers and executes nine simple examples. GitHub Actions runs the same command on pushes and pull requests. The recorded [Mac](evidence/verification/checkpoint-final-mac.txt) and [Linux](evidence/verification/checkpoint-final-linux.txt) runs each passed 244 tests and 48 task-package cases, with no skips and two FastAPI startup-event deprecation warnings. Linux verification used a clean source directory and empty project volumes in a prepared virtual machine, not a newly installed operating system.

The task package can also be checked without a server:

```sh
.venv/bin/python baf/validate_tasks.py
```

Its [recorded output](baf/checker-validation.json) is marked `task_package_validation_not_model_run`. The separate mounting experiment has [preparation and repeat instructions](prototypes/mounting/README.md), a [17-check result](evidence/mounting/attempt-3/result.json) and two retained failed attempts.

## Use a real model

Real mode requires a configured HTTPS endpoint, a model credential, exact model identity, model metadata and a persistent spending limit. Follow the [model configuration guide](docs/reference/model.md). The client, harness and proxy check the expected mode and model before provider calls; a default mock request cannot silently become a paid run.

The recorded first-checkpoint evaluation used DeepSeek V4.1 Flash 1017 in `high` reasoning mode through DeepInfra FP8. It passed all file checks and B5 command-execution checks. Total provider cost across the recorded attempts was **$0.018821183**. This is one successful full run after a 12/15 attempt, not a repeated-run reliability estimate. [Evidence and limitations](evidence/README.md) preserve the attempt history and distinguish recorded source hashes from subsequent repository changes.

## Stop the server

```sh
docker compose down
```

This preserves named state volumes, including the paid-call ledger. `docker compose down -v` also deletes the ledger; reserve it for disposable test stacks or a reconciled reset. Restart and isolated cleanup results are available in [restart.json](evidence/verification/restart.json) and [checkpoint-final-cleanup.json](evidence/verification/checkpoint-final-cleanup.json).

## Documentation

- [Architecture](docs/architecture.md) and [model/tool loop](docs/harness.md).
- [Checkpoint 2 demonstration](docs/checkpoint-2/workspace.md), [slides](docs/checkpoint-2/slides.html), [questions](docs/checkpoint-2/questions.md) and [results](evidence/checkpoint-2/README.md).
- [Checkpoint 1 demonstration and questions](docs/checkpoint-1.md).
- [Pinned course requirements](docs/reference/course.md).
- [Comparison of three open-source agent systems](docs/reference/systems.md).
- [Mounting alternatives and limitations](docs/reference/mounting.md).
- [Architecture decisions](docs/adr/) and [protocol schemas](contracts/README.md).

The first checkpoint course source is pinned to [`b01ddf7b89e5c603985e3d5b5af958bebc7b1972`](https://github.com/deeppavlov/agentic-course-itmo/tree/b01ddf7b89e5c603985e3d5b5af958bebc7b1972/projects). Evaluation data uses synthetic course fixtures; new run output is written to the local `results/` directory.
