#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from typing import Any

import websockets


@dataclass(slots=True)
class ResponseRun:
    response_id: str | None
    thread_id: str | None
    status: str | None
    text: str
    events: list[dict[str, Any]]
    duration_ms: int
    error: dict[str, Any] | None
    first_delta_ms: int | None
    delta_count: int
    stream_tokens_estimate: int
    stream_tokens_per_sec: float
    avg_inter_delta_ms: float | None


@dataclass(slots=True)
class ScenarioResult:
    name: str
    ok: bool
    duration_ms: int
    summary: str


def _calc_tool(arguments: dict[str, Any]) -> tuple[Any, bool]:
    expression = arguments.get("expression", "")
    if not isinstance(expression, str):
        return "invalid expression", True

    allowed = set("0123456789+-*/(). %")
    if not expression or any(ch not in allowed for ch in expression):
        return f"unsupported expression: {expression}", True

    try:
        value = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
    except Exception as exc:  # pylint: disable=broad-except
        return str(exc), True
    return str(value), False


async def _send_and_collect(
    ws: websockets.WebSocketClientProtocol,
    request: dict[str, Any],
    *,
    auto_tool: bool = False,
    cancel_on_first_delta: bool = False,
    timeout_seconds: int = 180,
) -> ResponseRun:
    await ws.send(json.dumps(request))

    started = time.perf_counter()
    events: list[dict[str, Any]] = []
    response_id: str | None = None
    thread_id: str | None = None
    status: str | None = None
    error: dict[str, Any] | None = None
    text_parts: list[str] = []
    step_text_parts: dict[str, list[str]] = {}
    ordered_steps: list[str] = []
    delta_times: list[float] = []

    cancelled = False
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout_seconds)
        event = json.loads(raw)
        events.append(event)

        event_type = event.get("type")
        if event_type == "response.created":
            response = event.get("response", {})
            response_id = response.get("id")
            thread_id = event.get("thread_id")
        elif event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if isinstance(delta, str):
                text_parts.append(delta)
                delta_times.append(time.perf_counter())
                step_id = event.get("step_id")
                if isinstance(step_id, str):
                    if step_id not in step_text_parts:
                        step_text_parts[step_id] = []
                        ordered_steps.append(step_id)
                    step_text_parts[step_id].append(delta)
            if cancel_on_first_delta and not cancelled and response_id:
                cancelled = True
                await ws.send(
                    json.dumps({"type": "response.cancel", "response_id": response_id})
                )
        elif event_type == "response.tool_call.created" and auto_tool:
            tool_call = event.get("tool_call", {})
            tool_call_id = tool_call.get("id")
            name = tool_call.get("name")
            args = tool_call.get("arguments", {})

            if name == "calc":
                output, is_error = _calc_tool(args if isinstance(args, dict) else {})
            else:
                output, is_error = (f"unsupported tool: {name}", True)

            if tool_call_id:
                await ws.send(
                    json.dumps(
                        {
                            "type": "tool_result.submit",
                            "tool_call_id": tool_call_id,
                            "output": output,
                            "is_error": is_error,
                        }
                    )
                )
        elif event_type == "error":
            error = event.get("error")
        elif event_type == "response.completed":
            response = event.get("response", {})
            status = response.get("status")
            if not response_id:
                response_id = response.get("id")
            if not thread_id:
                thread_id = event.get("thread_id")
            break

    final_text = "".join(text_parts).strip()
    if ordered_steps:
        last_step = ordered_steps[-1]
        final_text = "".join(step_text_parts.get(last_step, [])).strip() or final_text

    first_delta_ms: int | None = None
    avg_inter_delta_ms: float | None = None
    if delta_times:
        first_delta_ms = int((delta_times[0] - started) * 1000)
        if len(delta_times) > 1:
            gaps = [b - a for a, b in zip(delta_times, delta_times[1:])]
            avg_inter_delta_ms = round((sum(gaps) / len(gaps)) * 1000, 2)
    tokens_est = len(final_text.split())
    tokens_per_sec = 0.0
    elapsed_s = max((time.perf_counter() - started), 1e-9)
    if tokens_est > 0:
        tokens_per_sec = round(tokens_est / elapsed_s, 2)

    return ResponseRun(
        response_id=response_id,
        thread_id=thread_id,
        status=status,
        text=final_text,
        events=events,
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=error,
        first_delta_ms=first_delta_ms,
        delta_count=len(delta_times),
        stream_tokens_estimate=tokens_est,
        stream_tokens_per_sec=tokens_per_sec,
        avg_inter_delta_ms=avg_inter_delta_ms,
    )


def _word_limit_schema(word_count: int) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "words": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": word_count,
                    "maxItems": word_count,
                }
            },
            "required": ["words"],
            "additionalProperties": False,
        },
    }


async def scenario_stateful_memory(url: str, model: str) -> ScenarioResult:
    name = "Stateful Memory + Exact 5-Word"
    start = time.perf_counter()

    async with websockets.connect(url) as ws:
        first = await _send_and_collect(
            ws,
            {
                "type": "response.create",
                "response": {
                    "model": model,
                    "input": "Explain MicroShift in 2 sentences.",
                },
            },
        )
        if not first.response_id:
            return ScenarioResult(name, False, int((time.perf_counter() - start) * 1000), "No response_id")

        second = await _send_and_collect(
            ws,
            {
                "type": "response.create",
                "response": {
                    "model": model,
                    "input": "Now summarize that in 5 words.",
                    "previous_response_id": first.response_id,
                    "response_format": _word_limit_schema(5),
                },
            },
        )

    if second.status != "completed":
        return ScenarioResult(
            name,
            False,
            int((time.perf_counter() - start) * 1000),
            f"status={second.status} error={second.error}",
        )

    try:
        parsed = json.loads(second.text)
        words = parsed.get("words")
        ok_words = isinstance(words, list) and len(words) == 5 and all(isinstance(w, str) for w in words)
    except json.JSONDecodeError:
        ok_words = False
        words = None

    same_thread = first.thread_id is not None and first.thread_id == second.thread_id
    ok = same_thread and ok_words
    summary = (
        f"thread_reused={same_thread}, five_words={ok_words}, words={words}, "
        f"latency_ms={second.duration_ms}, ttft_ms={second.first_delta_ms}, "
        f"tok_s={second.stream_tokens_per_sec}"
    )
    return ScenarioResult(name, ok, int((time.perf_counter() - start) * 1000), summary)


async def scenario_agentic_tool(url: str, model: str) -> ScenarioResult:
    name = "Agentic Tool Loop"
    start = time.perf_counter()

    async with websockets.connect(url) as ws:
        run = await _send_and_collect(
            ws,
            {
                "type": "response.create",
                "response": {
                    "model": model,
                    "input": "Calculate 12*7 using tool calc and then explain briefly.",
                    "tools": [
                        {
                            "name": "calc",
                            "description": "Evaluate arithmetic expression",
                            "parameters": {
                                "type": "object",
                                "properties": {"expression": {"type": "string"}},
                                "required": ["expression"],
                            },
                            "mode": "client",
                            "timeout_seconds": 30,
                        }
                    ],
                },
            },
            auto_tool=True,
        )

    event_types = {event.get("type") for event in run.events}
    ok = (
        run.status == "completed"
        and "response.tool_call.created" in event_types
        and "response.tool_result.received" in event_types
    )
    summary = (
        f"status={run.status}, tool_events={sorted(t for t in event_types if t and 'tool' in t)}, "
        f"latency_ms={run.duration_ms}, ttft_ms={run.first_delta_ms}, tok_s={run.stream_tokens_per_sec}"
    )
    return ScenarioResult(name, ok, int((time.perf_counter() - start) * 1000), summary)


async def scenario_structured_output(url: str, model: str) -> ScenarioResult:
    name = "Structured JSON Output"
    start = time.perf_counter()

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
            },
        },
        "required": ["summary", "keywords"],
        "additionalProperties": False,
    }

    async with websockets.connect(url) as ws:
        run = await _send_and_collect(
            ws,
            {
                "type": "response.create",
                "response": {
                    "model": model,
                    "input": "Summarize Kubernetes in one sentence and 3 keywords.",
                    "response_format": {
                        "type": "json_schema",
                        "schema": schema,
                    },
                },
            },
        )

    ok_json = False
    try:
        parsed = json.loads(run.text)
        ok_json = isinstance(parsed, dict) and isinstance(parsed.get("keywords"), list)
    except json.JSONDecodeError:
        ok_json = False

    ok = run.status == "completed" and ok_json
    summary = (
        f"status={run.status}, json_valid={ok_json}, latency_ms={run.duration_ms}, "
        f"ttft_ms={run.first_delta_ms}, tok_s={run.stream_tokens_per_sec}"
    )
    return ScenarioResult(name, ok, int((time.perf_counter() - start) * 1000), summary)


async def scenario_cancel(url: str, model: str) -> ScenarioResult:
    name = "Cancellation"
    start = time.perf_counter()

    async with websockets.connect(url) as ws:
        run = await _send_and_collect(
            ws,
            {
                "type": "response.create",
                "response": {
                    "model": model,
                    "input": "Write a very long explanation of Linux namespaces with many details.",
                },
            },
            cancel_on_first_delta=True,
        )

    ok = run.status == "cancelled"
    summary = (
        f"status={run.status}, latency_ms={run.duration_ms}, "
        f"ttft_ms={run.first_delta_ms}, deltas={run.delta_count}"
    )
    return ScenarioResult(name, ok, int((time.perf_counter() - start) * 1000), summary)


async def scenario_concurrency(url: str, model: str, clients: int) -> ScenarioResult:
    name = "Concurrency/Batched Throughput"
    start = time.perf_counter()

    async def one_client(index: int) -> ResponseRun:
        async with websockets.connect(url) as ws:
            return await _send_and_collect(
                ws,
                {
                    "type": "response.create",
                    "response": {
                        "model": model,
                        "input": f"Reply with one short sentence about Syncra. request={index}",
                    },
                },
                timeout_seconds=120,
            )

    runs = await asyncio.gather(*(one_client(i + 1) for i in range(clients)))
    latencies = [run.duration_ms for run in runs]
    ttfts = [run.first_delta_ms for run in runs if run.first_delta_ms is not None]
    tok_rates = [run.stream_tokens_per_sec for run in runs if run.stream_tokens_per_sec > 0]
    success = [run for run in runs if run.status == "completed"]

    ok = len(success) == clients
    total_ms = int((time.perf_counter() - start) * 1000)
    throughput = (len(success) / (total_ms / 1000.0)) if total_ms > 0 else 0.0
    p50 = int(statistics.median(latencies)) if latencies else 0
    p95 = int(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)]) if latencies else 0
    p50_ttft = int(statistics.median(ttfts)) if ttfts else 0
    p95_ttft = int(sorted(ttfts)[max(0, int(0.95 * len(ttfts)) - 1)]) if ttfts else 0
    avg_tok_rate = round(statistics.mean(tok_rates), 2) if tok_rates else 0.0

    summary = (
        f"ok={len(success)}/{clients}, p50={p50}ms, p95={p95}ms, "
        f"p50_ttft={p50_ttft}ms, p95_ttft={p95_ttft}ms, "
        f"avg_tok_s={avg_tok_rate}, throughput={throughput:.2f} req/s"
    )
    return ScenarioResult(name, ok, total_ms, summary)


async def scenario_streaming_profile(url: str, model: str) -> ScenarioResult:
    name = "Streaming Profile (Single Long Response)"
    start = time.perf_counter()

    async with websockets.connect(url) as ws:
        run = await _send_and_collect(
            ws,
            {
                "type": "response.create",
                "response": {
                    "model": model,
                    "input": (
                        "Write 120 words about why WebSocket token streaming improves "
                        "user perceived latency in coding assistants."
                    ),
                    "max_tokens": 256,
                },
            },
            timeout_seconds=180,
        )

    ok = run.status == "completed" and run.delta_count > 5 and run.first_delta_ms is not None
    summary = (
        f"status={run.status}, deltas={run.delta_count}, ttft_ms={run.first_delta_ms}, "
        f"avg_gap_ms={run.avg_inter_delta_ms}, tok_s={run.stream_tokens_per_sec}"
    )
    return ScenarioResult(name, ok, int((time.perf_counter() - start) * 1000), summary)


async def run_showcase(url: str, model: str, concurrent_clients: int) -> list[ScenarioResult]:
    scenarios = [
        scenario_stateful_memory(url, model),
        scenario_agentic_tool(url, model),
        scenario_structured_output(url, model),
        scenario_streaming_profile(url, model),
        scenario_cancel(url, model),
        scenario_concurrency(url, model, max(1, concurrent_clients)),
    ]

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            result = await scenario
        except Exception as exc:  # pylint: disable=broad-except
            result = ScenarioResult(
                name="Unknown Scenario",
                ok=False,
                duration_ms=0,
                summary=f"exception={exc}",
            )
        results.append(result)
        mark = "PASS" if result.ok else "FAIL"
        print(f"[{mark}] {result.name} ({result.duration_ms} ms)")
        print(f"  {result.summary}")

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a full Syncra value showcase")
    parser.add_argument("--url", default="ws://localhost:9000/v1/responses")
    parser.add_argument("--model", default="mitko")
    parser.add_argument("--concurrent-clients", type=int, default=6)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.perf_counter()
    results = asyncio.run(
        run_showcase(
            url=args.url,
            model=args.model,
            concurrent_clients=args.concurrent_clients,
        )
    )

    passed = sum(1 for result in results if result.ok)
    total = len(results)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print("\n=== Syncra Showcase Scorecard ===")
    print(f"Passed: {passed}/{total}")
    print(f"Total runtime: {elapsed_ms} ms")
    if passed != total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
