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

SONIC_RUN_TESTS="${SONIC_RUN_TESTS:-1}"
SONIC_RUN_LIVE="${SONIC_RUN_LIVE:-1}"
SONIC_RUN_DEMO_TRACES="${SONIC_RUN_DEMO_TRACES:-1}"
SONIC_PYTEST_ARGS="${SONIC_PYTEST_ARGS:--q}"

if [[ "$SONIC_RUN_TESTS" == "1" ]]; then
  echo "[1/2] Running full automated test suite..."
  # Runs all tests in ./tests so showcase reflects total quality, not only demos.
  pytest ${SONIC_PYTEST_ARGS}
fi

if [[ "$SONIC_RUN_LIVE" == "1" ]]; then
  echo "[2/3] Running live Sonic showcase scorecard..."
  python scripts/showcase_sonic.py \
    --url "${SONIC_WS_URL:-ws://localhost:9000/v1/responses}" \
    --model "${SONIC_MODEL:-mitko}" \
    --concurrent-clients "${SONIC_CONCURRENCY:-8}"
fi

if [[ "$SONIC_RUN_DEMO_TRACES" == "1" ]]; then
  echo "[3/3] Running trace demos (agentic + structured)..."
  echo
  echo "=== Agentic Tool Calling Trace ==="
  python scripts/demo_tool_client.py
  echo
  echo "=== Structured Output Trace ==="
  python scripts/demo_structured_output.py
fi

echo "Showcase run complete."
