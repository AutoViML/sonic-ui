from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agent_loop import ActiveResponseSession
from config import Settings
from schemas import ResponseCreateRequest, ResponseFormat, ToolResultSubmit, ToolSpec
from state_store import StateStore
from persistence import SQLitePersistence
from tools.registry import ToolRegistry


class FakeAgenticVLLMClient:
    def __init__(self) -> None:
        self.cancelled = False

    async def stream_chat(
        self,
        model,
        messages,
        temperature=None,
        top_p=None,
        max_tokens=None,
        cancel_event=None,
    ):
        _ = model, temperature, top_p, max_tokens

        latest_user = ""
        saw_tool = False
        last_tool_content = ""
        saw_repair_prompt = False
        for msg in messages:
            if msg["role"] == "user":
                latest_user = msg["content"]
                if "Fix to match schema" in msg["content"]:
                    saw_repair_prompt = True
            if msg["role"] == "tool":
                saw_tool = True
                last_tool_content = msg["content"]

        system_text = messages[0]["content"] if messages else ""
        if "Return ONLY valid JSON" in system_text:
            if saw_repair_prompt:
                text = '{"summary":"ok","keywords":["a","b"]}'
            else:
                text = "invalid json"
        elif "Calculate" in latest_user and not saw_tool:
            text = '{"tool_call":{"name":"calc","arguments":{"expression":"12*7"}}}'
        elif saw_tool:
            text = f"Tool said: {last_tool_content}"
        else:
            text = f"Echo: {latest_user}"

        try:
            for chunk in text.split(" "):
                if cancel_event is not None and cancel_event.is_set():
                    self.cancelled = True
                    raise asyncio.CancelledError
                await asyncio.sleep(0.001)
                yield chunk + " "
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class SlowVLLMClient:
    def __init__(self) -> None:
        self.cancelled = False

    async def stream_chat(
        self,
        model,
        messages,
        temperature=None,
        top_p=None,
        max_tokens=None,
        cancel_event=None,
    ):
        _ = model, messages, temperature, top_p, max_tokens
        try:
            for token in ["one", "two", "three", "four", "five"]:
                if cancel_event is not None and cancel_event.is_set():
                    self.cancelled = True
                    raise asyncio.CancelledError
                await asyncio.sleep(0.01)
                yield token + " "
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class ToolJsonOnlyVLLMClient:
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
        text = '{"tool_call":{"name":"calc","arguments":{}}}'
        for chunk in text.split(" "):
            yield chunk + " "


def test_agentic_tool_loop() -> None:
    asyncio.run(_run_agentic_tool_loop())


async def _run_agentic_tool_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "state.db")
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=db_path,
            max_steps=8,
            max_tool_calls=16,
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        events: list[dict] = []
        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def emit(event_type: str, body: dict, step_id: str | None = None) -> None:
            payload = {
                "type": event_type,
                "response_id": session.response_id,
                "thread_id": session.thread_id,
            }
            if step_id:
                payload["step_id"] = step_id
            payload.update(body)
            events.append(payload)
            await queue.put(payload)

        session = ActiveResponseSession(
            conn_id="conn-test",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Calculate 12*7 using tool calc",
                previous_response_id=None,
                tools=[
                    ToolSpec(
                        name="calc",
                        description="calc",
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
            vllm_client=FakeAgenticVLLMClient(),
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
                        output="84",
                        is_error=False,
                    )
                )
                assert accepted
            if event["type"] == "response.completed":
                assert event["response"]["status"] == "completed"
                break

        await task

        seen = {event["type"] for event in events}
        assert "response.tool_call.created" in seen
        assert "response.tool_result.waiting" in seen
        assert "response.tool_result.received" in seen
        assert "response.step.created" in seen
        assert "response.step.completed" in seen

        await store.close()


def test_plain_mode_does_not_enter_tool_wait() -> None:
    asyncio.run(_run_plain_mode_does_not_enter_tool_wait())


async def _run_plain_mode_does_not_enter_tool_wait() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "state.db")
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=db_path,
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        events: list[dict] = []

        async def emit(event_type: str, body: dict, step_id: str | None = None) -> None:
            payload = {
                "type": event_type,
                "response_id": session.response_id,
                "thread_id": session.thread_id,
            }
            if step_id:
                payload["step_id"] = step_id
            payload.update(body)
            events.append(payload)

        session = ActiveResponseSession(
            conn_id="conn-plain",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Summarize in 5 words.",
                previous_response_id=None,
                tools=[],
                response_format=None,
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=ToolJsonOnlyVLLMClient(),
            tool_registry=ToolRegistry(settings),
            emit=emit,
            log_event=lambda _event, _fields: None,
        )
        await session.run()

        assert any(
            e["type"] == "response.completed" and e["response"]["status"] == "completed"
            for e in events
        )
        assert not any(e["type"] == "response.tool_result.waiting" for e in events)
        await store.close()


def test_response_cancel() -> None:
    asyncio.run(_run_response_cancel())


async def _run_response_cancel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=str(Path(tmp) / "state.db"),
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def emit(event_type: str, body: dict, step_id: str | None = None) -> None:
            payload = {
                "type": event_type,
                "response_id": session.response_id,
                "thread_id": session.thread_id,
            }
            if step_id:
                payload["step_id"] = step_id
            payload.update(body)
            await queue.put(payload)

        backend = SlowVLLMClient()
        session = ActiveResponseSession(
            conn_id="conn-cancel",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Long answer",
                previous_response_id=None,
                tools=[],
                response_format=None,
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=backend,
            tool_registry=ToolRegistry(settings),
            emit=emit,
            log_event=lambda _event, _fields: None,
        )

        task = asyncio.create_task(session.run())

        saw_delta = False
        while True:
            event = await queue.get()
            if event["type"] == "response.output_text.delta" and not saw_delta:
                saw_delta = True
                session.cancel()
            if event["type"] == "response.completed":
                assert event["response"]["status"] == "cancelled"
                break

        await task
        assert backend.cancelled
        await store.close()


def test_structured_output_retry_then_success() -> None:
    asyncio.run(_run_structured_output_retry_then_success())


async def _run_structured_output_retry_then_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=str(Path(tmp) / "state.db"),
            max_steps=8,
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        events: list[dict] = []

        async def emit(event_type: str, body: dict, step_id: str | None = None) -> None:
            payload = {
                "type": event_type,
                "response_id": session.response_id,
                "thread_id": session.thread_id,
            }
            if step_id:
                payload["step_id"] = step_id
            payload.update(body)
            events.append(payload)

        session = ActiveResponseSession(
            conn_id="conn-structured",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Provide structured output",
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
            vllm_client=FakeAgenticVLLMClient(),
            tool_registry=ToolRegistry(settings),
            emit=emit,
            log_event=lambda _event, _fields: None,
        )

        await session.run()

        statuses = [
            e["step"]["status"]
            for e in events
            if e["type"] == "response.step.completed"
        ]
        assert "schema_retry" in statuses
        assert any(
            e["type"] == "response.completed" and e["response"]["status"] == "completed"
            for e in events
        )

        await store.close()


def test_stateful_previous_response_continuation() -> None:
    asyncio.run(_run_stateful_previous_response_continuation())


async def _run_stateful_previous_response_continuation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            model_name="mitko",
            allowed_models=frozenset({"mitko"}),
            state_db_path=str(Path(tmp) / "state.db"),
        )
        store = StateStore(SQLitePersistence(settings.state_db_path))
        await store.initialize()

        async def emit_noop(_event_type: str, _body: dict, step_id: str | None = None) -> None:
            _ = step_id

        first = ActiveResponseSession(
            conn_id="conn-prev-1",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="First turn",
                previous_response_id=None,
                tools=[],
                response_format=None,
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=FakeAgenticVLLMClient(),
            tool_registry=ToolRegistry(settings),
            emit=emit_noop,
            log_event=lambda _event, _fields: None,
        )
        await first.run()

        second = ActiveResponseSession(
            conn_id="conn-prev-2",
            request=ResponseCreateRequest(
                model="mitko",
                input_text="Second turn",
                previous_response_id=first.response_id,
                tools=[],
                response_format=None,
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
            ),
            settings=settings,
            store=store,
            vllm_client=FakeAgenticVLLMClient(),
            tool_registry=ToolRegistry(settings),
            emit=emit_noop,
            log_event=lambda _event, _fields: None,
        )
        await second.run()

        record = await store.get_response(second.response_id)
        assert record is not None
        roles = [m["role"] for m in record.messages]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert record.thread_id == first.thread_id

        await store.close()
