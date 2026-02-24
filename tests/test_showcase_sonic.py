from __future__ import annotations

import asyncio
import json

from scripts.showcase_sonic import _send_and_collect


class FakeWebSocket:
    def __init__(self, events: list[dict], delay_s: float = 0.001) -> None:
        self._events = [json.dumps(event) for event in events]
        self._delay_s = delay_s
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        await asyncio.sleep(self._delay_s)
        if not self._events:
            raise RuntimeError("No events left")
        return self._events.pop(0)


def test_send_and_collect_stream_metrics() -> None:
    asyncio.run(_run_send_and_collect_stream_metrics())


async def _run_send_and_collect_stream_metrics() -> None:
    ws = FakeWebSocket(
        [
            {
                "type": "response.created",
                "thread_id": "thread_1",
                "response": {"id": "resp_1"},
            },
            {
                "type": "response.output_text.delta",
                "step_id": "step_1",
                "delta": "hello ",
            },
            {
                "type": "response.output_text.delta",
                "step_id": "step_1",
                "delta": "world",
            },
            {
                "type": "response.completed",
                "thread_id": "thread_1",
                "response": {"id": "resp_1", "status": "completed"},
            },
        ]
    )

    run = await _send_and_collect(
        ws,
        {"type": "response.create", "response": {"model": "mitko", "input": "x"}},
    )

    assert run.status == "completed"
    assert run.text == "hello world"
    assert run.delta_count == 2
    assert run.first_delta_ms is not None
    assert run.first_delta_ms >= 0
    assert run.avg_inter_delta_ms is not None
    assert run.stream_tokens_estimate == 2
    assert run.stream_tokens_per_sec > 0

