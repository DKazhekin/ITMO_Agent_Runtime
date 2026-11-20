#!/bin/sh
set -eu
export CLIENT_TOKEN=local-checkpoint-token
export MODEL_MODE=mock MODEL_API_KEY= MODEL_BASE_URL= MODEL_NAME=
export RUNTIME_URL="http://127.0.0.1:${GATEWAY_PORT:-8080}"
python=${CHECK_PYTHON:-python3}
if [ -z "${CHECK_PYTHON:-}" ] && [ -x .venv/bin/python ]; then
  python=.venv/bin/python
fi
sh scripts/compose.sh -f compose.yaml -f compose.offline.yaml up -d --wait --no-build --pull never
sh scripts/compose.sh -f compose.yaml -f compose.offline.yaml --profile test run --rm --no-deps tests
"$python" -m baf.validate_tasks
"$python" -m baf.run --mode mock --workspace snapshot --output "${CHECK_OUTPUT:-results/offline}"
