# Sonic

Sonic is a WebSocket Responses gateway for vLLM (`/v1/chat/completions`) with Phase 2 agentic behavior.

Implemented in this repo:
- Multi-step agent loop (`response.step.*` events)
- Tool call protocol via strict JSON (`{"tool_call":...}`)
- Client tool loop (`tool_result.submit`) and optional server tools
- Structured output mode (`response_format.type=json_schema`) with validation/retry
- Durable SQLite state for threads, responses, steps, messages, tool calls/results
- `response.cancel` support

Repository:
- GitHub: `https://github.com/mitkox/sonic`
- License: MIT

## Project Layout

- `main.py`: FastAPI + WebSocket session handling
- `agent_loop.py`: core multi-step agent execution loop
- `state_store.py`: in-memory response state backed by persistence
- `persistence.py`: SQLite schema and persistence operations
- `schemas.py`: protocol validation/parsing and system prompt contract
- `tools/registry.py`: tool registry + server tool execution policies
- `tools/builtins/`: built-in server tools scaffolding
- `vllm_client.py`: streamed backend SSE parsing
- `scripts/test_ws_client.py`: plain and agentic streaming test client
- `scripts/demo_tool_client.py`: tool-loop demo
- `scripts/demo_structured_output.py`: structured-output demo

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run Gateway

```bash
source venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 9000
```

Health check:

```bash
curl http://localhost:9000/healthz
```

## Key Environment Variables

- `VLLM_URL` (default: `http://localhost:8000`)
- `MODEL_NAME` (default: `mitko`)
- `ALLOWED_MODELS` (default: `MODEL_NAME`)
- `PORT` (default: `9000`)
- `STATE_DB_PATH` (default: `./sonic_state.db`)
- `MAX_STEPS` (default: `8`)
- `MAX_TOOL_CALLS` (default: `16`)
- `TOOL_WAIT_TIMEOUT_SECONDS` (default: `120`)
- `BACKEND_CONNECT_TIMEOUT` / `BACKEND_READ_TIMEOUT`
- `DEFAULT_TEMPERATURE` (default: `0.2`)
- `DEFAULT_TOP_P` (default: `0.9`)
- `MAX_INPUT_BYTES` (default: `262144`)
- `RATE_LIMIT_PER_MINUTE` (default: `120`)
- `REQUIRE_API_KEY` / `API_KEY`
- `TOOL_ALLOWLIST` (comma-separated, required for server tool execution)
- `ENABLE_SHELL_EXEC` (default: `false`)
- `ENABLE_HTTP_GET` (default: `false`)
- `FILESYSTEM_ROOT` (default: `.`)

Performance notes:
- Gateway now reuses persistent HTTP connections to vLLM (better batching/throughput).
- For lowest latency, keep `STATE_DB_PATH` on fast local disk (or tmpfs if durability is not needed).
- Keep `DEFAULT_TEMPERATURE<=0.2` and `DEFAULT_TOP_P<=0.9` for stable tool behavior on Qwen3-Coder-Next.

## Test Clients

Plain streaming and continuation:

```bash
python scripts/test_ws_client.py --model mitko
```

Agentic tool streaming (auto-responds to `calc` tool calls):

```bash
python scripts/test_ws_client.py --agentic --first "Calculate 12*7 using tool calc"
```

Dedicated demos:

```bash
python scripts/demo_tool_client.py
python scripts/demo_structured_output.py
python scripts/showcase_sonic.py --url ws://localhost:9000/v1/responses --model mitko --concurrent-clients 8
scripts/run_showcase.sh
```

`showcase_sonic.py` runs a full value demo:
- stateful continuation + exact 5-word summary
- agentic tool loop
- structured JSON generation
- streaming profile with TTFT/token-rate stats
- cancellation
- concurrent request throughput (useful when vLLM batching is enabled)

`run_showcase.sh` is the all-in-one entrypoint:
- runs the full automated test suite (`pytest` over all tests)
- then runs the live showcase scorecard
- then runs live trace demos for `agentic tool calling` and `structured output`

Optional env vars for `run_showcase.sh`:
- `SONIC_CONCURRENCY=16` (live concurrency level)
- `SONIC_RUN_TESTS=0` (skip pytest phase)
- `SONIC_RUN_LIVE=0` (tests-only mode)
- `SONIC_RUN_DEMO_TRACES=0` (skip trace demos)
- `SONIC_PYTEST_ARGS="-q -k gateway"` (custom pytest args)

## WebSocket Events

Inbound:
- `response.create`
- `tool_result.submit`
- `response.cancel`

Outbound:
- `response.created`
- `response.in_progress`
- `response.output_text.delta`
- `response.step.created`
- `response.step.completed`
- `response.tool_call.created`
- `response.tool_call.completed`
- `response.tool_result.waiting`
- `response.tool_result.received`
- `response.completed`
- `error`

Each response-scoped event includes `response_id` and `thread_id`; step events include `step_id`.

## Run Tests

```bash
source venv/bin/activate
pytest -q
pytest -q tests/test_acceptance_wow.py
scripts/run_showcase.sh
```

CI:
- GitHub Actions runs `pytest -q` on pushes/PRs.

## systemd

Service file: `systemd/codex-ws-gateway.service`

Example env file (`/etc/codex-ws-gateway.env`):

```bash
VLLM_URL=http://localhost:8000
MODEL_NAME=mitko
PORT=9000
STATE_DB_PATH=/var/lib/sonic/state.db
MAX_STEPS=8
MAX_TOOL_CALLS=16
TOOL_WAIT_TIMEOUT_SECONDS=120
REQUIRE_API_KEY=false
```

## Contributing

See `CONTRIBUTING.md` for development workflow and PR expectations.
