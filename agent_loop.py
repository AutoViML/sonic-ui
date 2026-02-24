from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from jsonschema import ValidationError, validate

from config import Settings
from schemas import (
    ResponseCreateRequest,
    ToolResultSubmit,
    build_system_message,
    parse_tool_call_json,
)
from state_store import StateStore
from tools.registry import ToolDefinition, ToolRegistry
from vllm_client import VLLMBackendError, VLLMClient

EmitFn = Callable[[str, dict[str, Any], str | None], Awaitable[None]]
LogFn = Callable[[str, dict[str, Any]], None]


REPAIR_PROMPT = "Your previous output was invalid. Fix to match schema."


@dataclass(slots=True)
class AgentRunContext:
    conn_id: str
    response_id: str
    thread_id: str


class ActiveResponseSession:
    def __init__(
        self,
        *,
        conn_id: str,
        request: ResponseCreateRequest,
        settings: Settings,
        store: StateStore,
        vllm_client: VLLMClient,
        tool_registry: ToolRegistry,
        emit: EmitFn,
        log_event: LogFn,
    ) -> None:
        self.request = request
        self.settings = settings
        self.store = store
        self.vllm_client = vllm_client
        self.tool_registry = tool_registry
        self.emit = emit
        self.log_event = log_event

        self.cancel_event = asyncio.Event()

        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.thread_id = f"thread_{uuid.uuid4().hex}"
        self.conn_id = conn_id

        self._pending_tool_call_id: str | None = None
        self._pending_tool_result: asyncio.Future[ToolResultSubmit] | None = None
        self._pending_lock = asyncio.Lock()

    def matches_response(self, response_id: str | None) -> bool:
        if response_id is None:
            return True
        return response_id == self.response_id

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._pending_tool_result is not None and not self._pending_tool_result.done():
            self._pending_tool_result.cancel()

    async def submit_tool_result(self, submit: ToolResultSubmit) -> bool:
        async with self._pending_lock:
            if (
                self._pending_tool_call_id != submit.tool_call_id
                or self._pending_tool_result is None
                or self._pending_tool_result.done()
            ):
                return False
            self._pending_tool_result.set_result(submit)
            return True

    async def run(self) -> None:
        try:
            await self._run_inner()
        except asyncio.CancelledError:
            await self._finalize_cancelled(None)
            raise
        except Exception as exc:  # pylint: disable=broad-except
            self.log_event(
                "session_internal_error",
                {
                    "conn_id": self.conn_id,
                    "response_id": self.response_id,
                    "thread_id": self.thread_id,
                    "message": str(exc),
                },
            )
            await self._emit_terminal_failure_no_store(str(exc))

    async def _run_inner(self) -> None:
        base_messages: list[dict[str, str]] = []
        previous_id = self.request.previous_response_id
        if previous_id:
            previous_thread_id, messages, found = await self.store.resolve_previous_response(
                previous_id
            )
            if found and previous_thread_id:
                self.thread_id = previous_thread_id
                base_messages = messages
            else:
                self.log_event(
                    "previous_response_not_found",
                    {
                        "conn_id": self.conn_id,
                        "response_id": self.response_id,
                        "previous_response_id": previous_id,
                    },
                )

        await self.store.start_response(
            response_id=self.response_id,
            thread_id=self.thread_id,
            parent_response_id=previous_id,
            model=self.request.model,
            base_messages=base_messages,
            user_input=self.request.input_text,
        )

        await self.emit(
            "response.created",
            {
                "response": {
                    "id": self.response_id,
                    "thread_id": self.thread_id,
                    "model": self.request.model,
                    "status": "created",
                }
            },
        )
        await self.emit(
            "response.in_progress",
            {
                "response": {
                    "id": self.response_id,
                    "thread_id": self.thread_id,
                    "status": "in_progress",
                }
            },
        )

        tools = self.tool_registry.resolve_tools(self.request.tools)
        step_count = 0
        tool_call_count = 0
        schema_attempts = 0

        while step_count < self.settings.max_steps:
            if self.cancel_event.is_set():
                await self._finalize_cancelled(None)
                return

            step_count += 1
            step_id = f"step_{uuid.uuid4().hex}"

            await self.store.start_step(self.response_id, step_id, step_count)
            await self.emit(
                "response.step.created",
                {
                    "step": {
                        "id": step_id,
                        "index": step_count,
                        "status": "in_progress",
                    }
                },
                step_id=step_id,
            )

            snapshot = await self.store.get_response(self.response_id)
            if snapshot is None:
                await self._fail_response(
                    code="internal_error",
                    message="Response state vanished",
                    step_id=step_id,
                )
                return

            system_message = build_system_message(
                tools=list(tools.values()),
                response_format=self.request.response_format,
            )
            llm_messages = [{"role": "system", "content": system_message}] + snapshot.messages

            output_text = ""
            try:
                async for fragment in self.vllm_client.stream_chat(
                    model=self.request.model,
                    messages=llm_messages,
                    temperature=self.request.temperature,
                    top_p=self.request.top_p,
                    max_tokens=self.request.max_tokens,
                    cancel_event=self.cancel_event,
                ):
                    output_text += fragment
                    await self.emit(
                        "response.output_text.delta",
                        {"delta": fragment},
                        step_id=step_id,
                    )
            except asyncio.CancelledError:
                await self._finalize_cancelled(step_id)
                return
            except VLLMBackendError as exc:
                await self._fail_response(
                    code="backend_error",
                    message=str(exc),
                    step_id=step_id,
                )
                return
            except Exception as exc:  # pylint: disable=broad-except
                await self._fail_response(
                    code="internal_error",
                    message=str(exc),
                    step_id=step_id,
                )
                return

            tool_call = parse_tool_call_json(output_text)
            if tool_call and tool_call.name in tools:
                if tool_call_count >= self.settings.max_tool_calls:
                    await self._fail_response(
                        code="max_tool_calls_exceeded",
                        message="Maximum tool calls reached",
                        step_id=step_id,
                    )
                    return

                tool_call_count += 1
                tool_def = tools[tool_call.name]
                tool_call_id = f"tool_{uuid.uuid4().hex}"

                await self.store.append_message(
                    self.response_id,
                    role="assistant",
                    content=json.dumps(
                        {"tool_call": {"name": tool_call.name, "arguments": tool_call.arguments}},
                        separators=(",", ":"),
                    ),
                    step_id=step_id,
                )
                await self.store.create_tool_call(
                    tool_call_id=tool_call_id,
                    response_id=self.response_id,
                    step_id=step_id,
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                    mode=tool_def.mode,
                )

                await self.emit(
                    "response.tool_call.created",
                    {
                        "tool_call": {
                            "id": tool_call_id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                            "mode": tool_def.mode,
                        }
                    },
                    step_id=step_id,
                )

                if tool_def.mode == "server":
                    ok = await self._execute_server_tool(
                        step_id=step_id,
                        tool_call_id=tool_call_id,
                        tool_def=tool_def,
                        arguments=tool_call.arguments,
                    )
                    if ok:
                        await self.store.complete_step(step_id, status="completed")
                        await self.emit(
                            "response.step.completed",
                            {"step": {"id": step_id, "index": step_count, "status": "completed"}},
                            step_id=step_id,
                        )
                        continue
                    return

                await self.store.complete_step(step_id, status="waiting_tool")
                await self.emit(
                    "response.step.completed",
                    {"step": {"id": step_id, "index": step_count, "status": "waiting_tool"}},
                    step_id=step_id,
                )
                await self.emit(
                    "response.tool_result.waiting",
                    {
                        "tool_call": {
                            "id": tool_call_id,
                            "name": tool_call.name,
                            "timeout_seconds": min(
                                tool_def.timeout_seconds,
                                self.settings.tool_wait_timeout_seconds,
                            ),
                        }
                    },
                    step_id=step_id,
                )

                submit = await self._wait_for_tool_result(
                    tool_call_id,
                    timeout_seconds=min(
                        tool_def.timeout_seconds,
                        self.settings.tool_wait_timeout_seconds,
                    ),
                )
                if submit is None:
                    if self.cancel_event.is_set():
                        await self._finalize_cancelled(step_id)
                        return
                    await self.store.complete_tool_call(
                        tool_call_id,
                        status="failed",
                        last_error="tool result timeout",
                    )
                    await self._fail_response(
                        code="tool_timeout",
                        message="Timed out waiting for tool result",
                        step_id=step_id,
                    )
                    return

                await self.store.add_tool_result(
                    tool_call_id=tool_call_id,
                    response_id=self.response_id,
                    output=submit.output,
                    is_error=submit.is_error,
                )
                await self.store.complete_tool_call(
                    tool_call_id,
                    status="completed",
                    last_error=None,
                )

                await self.emit(
                    "response.tool_result.received",
                    {
                        "tool_call": {
                            "id": tool_call_id,
                            "is_error": submit.is_error,
                        }
                    },
                    step_id=step_id,
                )
                await self.emit(
                    "response.tool_call.completed",
                    {
                        "tool_call": {
                            "id": tool_call_id,
                            "status": "completed",
                        }
                    },
                    step_id=step_id,
                )

                tool_content = submit.output
                if not isinstance(tool_content, str):
                    tool_content = json.dumps(tool_content, separators=(",", ":"))

                await self.store.append_message(
                    self.response_id,
                    role="tool",
                    content=tool_content,
                    step_id=step_id,
                )
                continue

            await self.store.append_message(
                self.response_id,
                role="assistant",
                content=output_text,
                step_id=step_id,
            )

            if self.request.response_format is not None:
                if self._validate_structured_output(
                    output_text,
                    self.request.response_format.schema,
                ):
                    await self.store.complete_step(step_id, status="completed")
                    await self.emit(
                        "response.step.completed",
                        {"step": {"id": step_id, "index": step_count, "status": "completed"}},
                        step_id=step_id,
                    )
                    await self._finalize_completed(output_text)
                    return

                schema_attempts += 1
                if schema_attempts <= 2:
                    await self.store.append_message(
                        self.response_id,
                        role="user",
                        content=REPAIR_PROMPT,
                        step_id=step_id,
                    )
                    await self.store.complete_step(step_id, status="schema_retry")
                    await self.emit(
                        "response.step.completed",
                        {
                            "step": {
                                "id": step_id,
                                "index": step_count,
                                "status": "schema_retry",
                            }
                        },
                        step_id=step_id,
                    )
                    continue

                await self._fail_response(
                    code="schema_validation_failed",
                    message="Model output did not match requested schema",
                    step_id=step_id,
                )
                return

            await self.store.complete_step(step_id, status="completed")
            await self.emit(
                "response.step.completed",
                {"step": {"id": step_id, "index": step_count, "status": "completed"}},
                step_id=step_id,
            )
            await self._finalize_completed(output_text)
            return

        await self._fail_response(
            code="max_steps_exceeded",
            message="Maximum step count reached",
            step_id=None,
        )

    async def _execute_server_tool(
        self,
        *,
        step_id: str,
        tool_call_id: str,
        tool_def: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        await self.emit(
            "response.tool_result.waiting",
            {
                "tool_call": {
                    "id": tool_call_id,
                    "name": tool_def.name,
                    "timeout_seconds": tool_def.timeout_seconds,
                }
            },
            step_id=step_id,
        )
        try:
            result = await asyncio.wait_for(
                self.tool_registry.execute_server_tool(tool_def, arguments),
                timeout=tool_def.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self.store.complete_tool_call(
                tool_call_id,
                status="failed",
                last_error="server tool timeout",
            )
            await self._fail_response(
                code="tool_timeout",
                message=f"Tool '{tool_def.name}' timed out",
                step_id=step_id,
            )
            return False
        except Exception as exc:  # pylint: disable=broad-except
            await self.store.complete_tool_call(
                tool_call_id,
                status="failed",
                last_error=str(exc),
            )
            await self._fail_response(
                code="tool_error",
                message=f"Tool '{tool_def.name}' failed: {exc}",
                step_id=step_id,
            )
            return False

        await self.store.add_tool_result(
            tool_call_id=tool_call_id,
            response_id=self.response_id,
            output=result,
            is_error=False,
        )
        await self.store.complete_tool_call(
            tool_call_id,
            status="completed",
            last_error=None,
        )
        await self.emit(
            "response.tool_result.received",
            {
                "tool_call": {
                    "id": tool_call_id,
                    "is_error": False,
                }
            },
            step_id=step_id,
        )
        await self.emit(
            "response.tool_call.completed",
            {
                "tool_call": {
                    "id": tool_call_id,
                    "status": "completed",
                }
            },
            step_id=step_id,
        )

        tool_content = result
        if not isinstance(tool_content, str):
            tool_content = json.dumps(tool_content, separators=(",", ":"))

        await self.store.append_message(
            self.response_id,
            role="tool",
            content=tool_content,
            step_id=step_id,
        )
        return True

    async def _wait_for_tool_result(
        self,
        tool_call_id: str,
        timeout_seconds: int,
    ) -> ToolResultSubmit | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolResultSubmit] = loop.create_future()

        async with self._pending_lock:
            self._pending_tool_call_id = tool_call_id
            self._pending_tool_result = future

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            async with self._pending_lock:
                self._pending_tool_call_id = None
                self._pending_tool_result = None

    @staticmethod
    def _validate_structured_output(output_text: str, schema: dict[str, Any]) -> bool:
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            return False

        try:
            validate(instance=parsed, schema=schema)
        except ValidationError:
            return False
        return True

    async def _finalize_completed(self, assistant_output: str) -> None:
        await self.store.set_response_status(
            self.response_id,
            status="completed",
            assistant_output=assistant_output,
            last_error=None,
        )
        await self.emit(
            "response.completed",
            {
                "response": {
                    "id": self.response_id,
                    "thread_id": self.thread_id,
                    "status": "completed",
                }
            },
        )

    async def _finalize_cancelled(self, step_id: str | None) -> None:
        await self.store.set_response_status(
            self.response_id,
            status="cancelled",
            assistant_output="",
            last_error="cancelled",
        )
        await self.emit(
            "response.completed",
            {
                "response": {
                    "id": self.response_id,
                    "thread_id": self.thread_id,
                    "status": "cancelled",
                }
            },
            step_id=step_id,
        )

    async def _fail_response(self, *, code: str, message: str, step_id: str | None) -> None:
        await self.store.set_response_status(
            self.response_id,
            status="failed",
            assistant_output="",
            last_error=message,
        )
        if step_id:
            await self.store.complete_step(step_id, status="failed", last_error=message)

        await self.emit(
            "error",
            {
                "error": {
                    "code": code,
                    "message": message,
                }
            },
            step_id=step_id,
        )
        await self.emit(
            "response.completed",
            {
                "response": {
                    "id": self.response_id,
                    "thread_id": self.thread_id,
                    "status": "failed",
                }
            },
            step_id=step_id,
        )

    async def _emit_terminal_failure_no_store(self, message: str) -> None:
        # Last-resort path: avoid hanging clients when persistence/state updates fail.
        try:
            await self.emit(
                "error",
                {
                    "error": {
                        "code": "internal_error",
                        "message": message,
                    }
                },
                step_id=None,
            )
        except Exception:  # pylint: disable=broad-except
            pass

        try:
            await self.emit(
                "response.completed",
                {
                    "response": {
                        "id": self.response_id,
                        "thread_id": self.thread_id,
                        "status": "failed",
                    }
                },
                step_id=None,
            )
        except Exception:  # pylint: disable=broad-except
            pass
