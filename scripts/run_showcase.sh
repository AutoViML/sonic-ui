#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x "venv/bin/python" ]]; then
  echo "Missing venv at $ROOT_DIR/venv"
  echo "Create it first: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

source venv/bin/activate

SYNCRA_RUN_TESTS="${SYNCRA_RUN_TESTS:-1}"
SYNCRA_RUN_LIVE="${SYNCRA_RUN_LIVE:-1}"
SYNCRA_RUN_DEMO_TRACES="${SYNCRA_RUN_DEMO_TRACES:-1}"
SYNCRA_PYTEST_ARGS="${SYNCRA_PYTEST_ARGS:--q}"

if [[ "$SYNCRA_RUN_TESTS" == "1" ]]; then
  echo "[1/2] Running full automated test suite..."
  # Runs all tests in ./tests so showcase reflects total quality, not only demos.
  pytest ${SYNCRA_PYTEST_ARGS}
fi

if [[ "$SYNCRA_RUN_LIVE" == "1" ]]; then
  echo "[2/3] Running live Syncra showcase scorecard..."
  python scripts/showcase_syncra.py \
    --url "${SYNCRA_WS_URL:-ws://localhost:9000/v1/responses}" \
    --model "${SYNCRA_MODEL:-mitko}" \
    --concurrent-clients "${SYNCRA_CONCURRENCY:-8}"
fi

if [[ "$SYNCRA_RUN_DEMO_TRACES" == "1" ]]; then
  echo "[3/3] Running trace demos (agentic + structured)..."
  echo
  echo "=== Agentic Tool Calling Trace ==="
  python scripts/demo_tool_client.py
  echo
  echo "=== Structured Output Trace ==="
  python scripts/demo_structured_output.py
fi

echo "Showcase run complete."
