#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

import websockets


def _safe_calc(expression: str) -> str:
    # Demo-only lightweight arithmetic evaluator.
    allowed = set("0123456789+-*/(). %")
    if not expression or any(ch not in allowed for ch in expression):
        raise ValueError("Unsupported expression")
    value = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
    return str(value)


async def run_client(
    url: str,
    model: str,
    first_prompt: str,
    second_prompt: str,
    agentic: bool,
) -> None:
    async with websockets.connect(url) as ws:
        if agentic:
            first_response_id = await _send_agentic_request(ws, model, first_prompt)
            await _send_agentic_request(
                ws,
                model,
                second_prompt,
                previous_response_id=first_response_id,
            )
        else:
            first_response_id = await _send_plain_request(ws, model, first_prompt)
            inferred_limit = _infer_word_limit(second_prompt)
            response_format = _word_limit_response_format(inferred_limit) if inferred_limit else None
            await _send_plain_request(
                ws,
                model,
                second_prompt,
                previous_response_id=first_response_id,
                response_format=response_format,
            )


async def _send_plain_request(
    ws: websockets.WebSocketClientProtocol,
    model: str,
    prompt: str,
    previous_response_id: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> str:
    request: dict[str, Any] = {
        "type": "response.create",
        "response": {
            "model": model,
            "input": prompt,
        },
    }
    if previous_response_id:
        request["response"]["previous_response_id"] = previous_response_id
    if response_format is not None:
        request["response"]["response_format"] = response_format

    await ws.send(json.dumps(request))

    response_id: str | None = None
    deltas: list[str] = []
    structured_mode = response_format is not None
    print(f"\nRequest: {prompt}")
    while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
        event_type = event.get("type")
        if event_type == "response.created":
            response_id = event["response"]["id"]
            print(f"response.created id={response_id} thread={event.get('thread_id')}")
        elif event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            deltas.append(delta)
            if not structured_mode:
                print(delta, end="", flush=True)
        elif event_type == "response.step.created":
            print(f"\nstep.created id={event.get('step_id')}")
        elif event_type == "response.step.completed":
            print(f"\nstep.completed id={event.get('step_id')} status={event['step']['status']}")
        elif event_type == "response.tool_call.created":
            tool_call = event.get("tool_call", {})
            tool_call_id = tool_call.get("id")
            print(
                f"\nwarning: unexpected tool call in plain mode: "
                f"{tool_call.get('name')} id={tool_call_id}"
            )
            if tool_call_id:
                await ws.send(
                    json.dumps(
                        {
                            "type": "tool_result.submit",
                            "tool_call_id": tool_call_id,
                            "output": "Tool unavailable in plain mode client",
                            "is_error": True,
                        }
                    )
                )
                print(f"tool_result.submit sent for {tool_call_id} (plain-mode fallback)")
        elif event_type == "response.tool_result.waiting":
            tool_call = event.get("tool_call", {})
            print(f"\ntool_result.waiting id={tool_call.get('id')}")
        elif event_type == "response.tool_result.received":
            tool_call = event.get("tool_call", {})
            print(f"\ntool_result.received id={tool_call.get('id')}")
        elif event_type == "response.tool_call.completed":
            tool_call = event.get("tool_call", {})
            print(f"\ntool_call.completed id={tool_call.get('id')}")
        elif event_type == "response.completed":
            if structured_mode:
                _print_structured_word_summary("".join(deltas), response_format)
            print(f"\nresponse.completed status={event['response']['status']}")
            break
        elif event_type == "error":
            raise RuntimeError(f"Gateway error: {event['error']}")

    if not response_id:
        raise RuntimeError("No response_id received")
    return response_id


async def _send_agentic_request(
    ws: websockets.WebSocketClientProtocol,
    model: str,
    prompt: str,
    previous_response_id: str | None = None,
) -> str:
    request: dict[str, Any] = {
        "type": "response.create",
        "response": {
            "model": model,
            "input": prompt,
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
    }
    if previous_response_id:
        request["response"]["previous_response_id"] = previous_response_id

    await ws.send(json.dumps(request))

    response_id: str | None = None
    print(f"\nAgentic request: {prompt}")
    while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
        event_type = event.get("type")

        if event_type == "response.created":
            response_id = event["response"]["id"]
            print(f"response.created id={response_id} thread={event.get('thread_id')}")
        elif event_type == "response.step.created":
            print(f"step.created id={event.get('step_id')}")
        elif event_type == "response.output_text.delta":
            print(event.get("delta", ""), end="", flush=True)
        elif event_type == "response.tool_call.created":
            tool_call = event["tool_call"]
            tool_call_id = tool_call["id"]
            name = tool_call["name"]
            arguments = tool_call.get("arguments", {})
            print(f"\ntool_call.created id={tool_call_id} name={name} args={arguments}")

            output: Any
            is_error = False
            try:
                if name == "calc":
                    expr = arguments.get("expression", "")
                    output = _safe_calc(expr)
                else:
                    raise ValueError(f"Unsupported demo tool: {name}")
            except Exception as exc:  # pylint: disable=broad-except
                output = str(exc)
                is_error = True

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
            print(f"tool_result.submit sent for {tool_call_id}")
        elif event_type == "response.tool_result.waiting":
            tool_call = event["tool_call"]
            print(
                f"tool_result.waiting id={tool_call['id']} timeout={tool_call['timeout_seconds']}"
            )
        elif event_type == "response.tool_result.received":
            print(f"tool_result.received id={event['tool_call']['id']}")
        elif event_type == "response.tool_call.completed":
            print(f"tool_call.completed id={event['tool_call']['id']}")
        elif event_type == "response.step.completed":
            print(f"step.completed id={event.get('step_id')} status={event['step']['status']}")
        elif event_type == "response.completed":
            print(f"response.completed status={event['response']['status']}")
            break
        elif event_type == "error":
            raise RuntimeError(f"Gateway error: {event['error']}")

    if not response_id:
        raise RuntimeError("No response_id received")
    return response_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Syncra WS test client")
    parser.add_argument(
        "--url",
        default="ws://localhost:9000/v1/responses",
        help="WebSocket URL",
    )
    parser.add_argument("--model", default="mitko", help="Model name")
    parser.add_argument(
        "--first",
        default="Explain MicroShift in 2 sentences.",
        help="First prompt",
    )
    parser.add_argument(
        "--second",
        default="Now summarize that in 5 words.",
        help="Follow-up prompt",
    )
    parser.add_argument(
        "--agentic",
        action="store_true",
        help="Enable agentic mode with tool-call streaming",
    )
    return parser.parse_args()


def _infer_word_limit(prompt: str) -> int | None:
    match = re.search(r"\bin\s+(\d+)\s+words?\b", prompt, flags=re.IGNORECASE)
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    return value


def _word_limit_response_format(word_count: int) -> dict[str, Any]:
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


def _print_structured_word_summary(raw: str, response_format: dict[str, Any] | None) -> None:
    if response_format is None:
        return
    try:
        parsed = json.loads(raw)
        words = parsed.get("words")
        if isinstance(words, list) and all(isinstance(w, str) for w in words):
            print(" ".join(words))
            return
    except json.JSONDecodeError:
        pass
    print(raw)


def main() -> None:
    args = parse_args()
    asyncio.run(
        run_client(
            url=args.url,
            model=args.model,
            first_prompt=args.first,
            second_prompt=args.second,
            agentic=args.agentic,
        )
    )


if __name__ == "__main__":
    main()
