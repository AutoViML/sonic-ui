#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from test_ws_client import run_client


async def main_async() -> None:
    await run_client(
        url="ws://localhost:9000/v1/responses",
        model="mitko",
        first_prompt="Calculate 12*7 using tool calc.",
        second_prompt="Now compute (84+16)/2 using tool calc.",
        agentic=True,
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
