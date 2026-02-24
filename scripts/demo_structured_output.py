#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def run(url: str, model: str, prompt: str) -> None:
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["summary", "keywords"],
        "additionalProperties": False,
    }

    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "model": model,
                        "input": prompt,
                        "response_format": {
                            "type": "json_schema",
                            "schema": schema,
                        },
                    },
                }
            )
        )

        print("Structured output stream:")
        while True:
            event = json.loads(await ws.recv())
            et = event["type"]
            if et == "response.output_text.delta":
                print(event["delta"], end="", flush=True)
            elif et == "response.completed":
                print(f"\ncompleted status={event['response']['status']}")
                break
            elif et == "error":
                print(f"\nerror={event['error']}")
                break


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://localhost:9000/v1/responses")
    parser.add_argument("--model", default="mitko")
    parser.add_argument(
        "--prompt",
        default="Summarize Kubernetes in one sentence and provide 3 keywords.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args.url, args.model, args.prompt))


if __name__ == "__main__":
    main()
