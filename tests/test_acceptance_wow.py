from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from agent_loop import ActiveResponseSession
from config import Settings
from persistence import SQLitePersistence
from schemas import ResponseCreateRequest, ResponseFormat, ToolResultSubmit, ToolSpec
from state_store import StateStore
from tools.registry import ToolRegistry


def test_multi_tool_multi_step_flow() -> None:
    asyncio.run(_run_multi_tool_multi_step_flow())


def test_schema_failure_path() -> None:
    asyncio.run(_run_schema_failure_path())


def test_max_steps_guardrail() -> None:
    asyncio.run(_run_max_steps_guardrail())


class TwoToolCallsBackend:
    async def stream_chat(
        self,
        model,
        messages,
        temperature=None,
        top_p=None,
        max_tokens=None,
        cancel_event=None,
    ):
        _ = model, temperature, top_p, max_tokens, cancel_event
        tool_messages = [msg for msg in messages if msg["role"] == "tool"]
        if len(tool_messages) == 0:
            text = '{"tool_call":{"name":"calc","arguments":{"expression":"12*7"}}}'
        elif len(tool_messages) == 1:
            text = '{"tool_call":{"name":"calc","arguments":{"expression":"(84+16)/2"}}}'
        else:
            text = "Final answer uses both tool results."

        for chunk in text.split(" "):
            yield chunk + " "


class InvalidStructuredBackend:
    async def stream_chat(
        self,
        model,
        messages,
        temperature=None,
        top_p=None,
        max_tokens=None,
        cancel_event=None,
    ):
        _ = model, messages, temperature, top_p, max_tokens, cancel_event
        text = "not valid json"
        for chunk in text.split(" "):
            yield chunk + " "


class EndlessToolBackend:
    async def stream_chat(
        self,
        model,
        messages,
        temperature=None,
        top_p=None,
        max_tokens=None,
        cancel_event=None,
    ):
        _ = model, messages, temperature, top_p, max_tokens, cancel_event
        text = '{"tool_call":{"name":"calc","arguments":{"expression":"1+1"}}}'
        for chunk in text.split(" "):
            yield chunk + " "


async def _run_multi_tool_multi_step_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=str(Path(tmp) / "state.db"),
            max_steps=8,
            max_tool_calls=16,
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        events: list[dict[str, Any]] = []
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def emit(event_type: str, body: dict[str, Any], step_id: str | None = None) -> None:
            payload = {"type": event_type}
            if step_id is not None:
                payload["step_id"] = step_id
            payload.update(body)
            events.append(payload)
            await queue.put(payload)

        session = ActiveResponseSession(
            conn_id="wow-multi-tool",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Use calc twice and then summarize.",
                previous_response_id=None,
                tools=[
                    ToolSpec(
                        name="calc",
                        description="calculator",
                        parameters={"type": "object"},
                        mode="client",
                        timeout_seconds=10,
                    )
                ],
                response_format=None,
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=TwoToolCallsBackend(),
            tool_registry=ToolRegistry(settings),
            emit=emit,
            log_event=lambda _event, _fields: None,
        )

        task = asyncio.create_task(session.run())

        while True:
            event = await queue.get()
            if event["type"] == "response.tool_call.created":
                tool_call = event["tool_call"]
                expression = tool_call.get("arguments", {}).get("expression", "")
                output = "84" if expression == "12*7" else "50"
                accepted = await session.submit_tool_result(
                    ToolResultSubmit(
                        tool_call_id=tool_call["id"],
                        output=output,
                        is_error=False,
                    )
                )
                assert accepted
            if event["type"] == "response.completed":
                assert event["response"]["status"] == "completed"
                break

        await task

        tool_calls = [e for e in events if e["type"] == "response.tool_call.created"]
        steps = [e for e in events if e["type"] == "response.step.created"]
        assert len(tool_calls) == 2
        assert len(steps) >= 3

        await store.close()


async def _run_schema_failure_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=str(Path(tmp) / "state.db"),
            max_steps=8,
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        events: list[dict[str, Any]] = []

        async def emit(event_type: str, body: dict[str, Any], step_id: str | None = None) -> None:
            payload = {"type": event_type}
            if step_id is not None:
                payload["step_id"] = step_id
            payload.update(body)
            events.append(payload)

        session = ActiveResponseSession(
            conn_id="wow-schema-fail",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Return structured data",
                previous_response_id=None,
                tools=[],
                response_format=ResponseFormat(
                    type="json_schema",
                    schema={
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
                    },
                ),
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=InvalidStructuredBackend(),
            tool_registry=ToolRegistry(settings),
            emit=emit,
            log_event=lambda _event, _fields: None,
        )

        await session.run()

        errors = [e for e in events if e["type"] == "error"]
        assert errors
        assert errors[-1]["error"]["code"] == "schema_validation_failed"
        assert any(
            e["type"] == "response.completed" and e["response"]["status"] == "failed"
            for e in events
        )

        await store.close()


async def _run_max_steps_guardrail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=str(Path(tmp) / "state.db"),
            max_steps=2,
            max_tool_calls=16,
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        events: list[dict[str, Any]] = []
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def emit(event_type: str, body: dict[str, Any], step_id: str | None = None) -> None:
            payload = {"type": event_type}
            if step_id is not None:
                payload["step_id"] = step_id
            payload.update(body)
            events.append(payload)
            await queue.put(payload)

        session = ActiveResponseSession(
            conn_id="wow-max-steps",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Keep using tools forever",
                previous_response_id=None,
                tools=[
                    ToolSpec(
                        name="calc",
                        description="calculator",
                        parameters={"type": "object"},
                        mode="client",
                        timeout_seconds=10,
                    )
                ],
                response_format=None,
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=EndlessToolBackend(),
            tool_registry=ToolRegistry(settings),
            emit=emit,
            log_event=lambda _event, _fields: None,
        )

        task = asyncio.create_task(session.run())

        while True:
            event = await queue.get()
            if event["type"] == "response.tool_call.created":
                accepted = await session.submit_tool_result(
                    ToolResultSubmit(
                        tool_call_id=event["tool_call"]["id"],
                        output="2",
                        is_error=False,
                    )
                )
                assert accepted
            if event["type"] == "response.completed":
                break

        await task

        errors = [e for e in events if e["type"] == "error"]
        assert errors
        assert errors[-1]["error"]["code"] == "max_steps_exceeded"
        assert any(
            e["type"] == "response.completed" and e["response"]["status"] == "failed"
            for e in events
        )

        await store.close()
