from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from persistence import SQLitePersistence


@dataclass(slots=True)
class ResponseState:
    response_id: str
    thread_id: str
    parent_response_id: str | None
    model: str
    status: str
    created_at: int
    completed_at: int | None
    assistant_output: str
    last_error: str | None
    messages: list[dict[str, str]] = field(default_factory=list)
    next_message_seq: int = 1


class StateStore:
    def __init__(
        self,
        persistence: SQLitePersistence,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.persistence = persistence
        self.on_event = on_event
        self._lock = asyncio.Lock()
        self._responses: dict[str, ResponseState] = {}

    async def initialize(self) -> int:
        await self.persistence.connect()
        loaded = await self.persistence.load_state()

        responses = loaded.get("responses", {})
        async with self._lock:
            for response_id, row in responses.items():
                messages = [dict(m) for m in row.get("messages", [])]
                self._responses[response_id] = ResponseState(
                    response_id=response_id,
                    thread_id=row["thread_id"],
                    parent_response_id=row.get("parent_response_id"),
                    model=row["model"],
                    status=row["status"],
                    created_at=row["created_at"],
                    completed_at=row.get("completed_at"),
                    assistant_output=row.get("assistant_output") or "",
                    last_error=row.get("last_error"),
                    messages=messages,
                    next_message_seq=len(messages) + 1,
                )
        return len(self._responses)

    async def close(self) -> None:
        await self.persistence.close()

    async def resolve_previous_response(
        self,
        previous_response_id: str,
    ) -> tuple[str | None, list[dict[str, str]], bool]:
        async with self._lock:
            record = self._responses.get(previous_response_id)
            if record is None:
                return None, [], False
            messages = [dict(m) for m in record.messages]
            return record.thread_id, messages, True

    async def start_response(
        self,
        response_id: str,
        thread_id: str,
        parent_response_id: str | None,
        model: str,
        base_messages: list[dict[str, str]],
        user_input: str,
    ) -> ResponseState:
        now = _epoch_ms()
        full_messages = [dict(m) for m in base_messages] + [
            {"role": "user", "content": user_input}
        ]

        state = ResponseState(
            response_id=response_id,
            thread_id=thread_id,
            parent_response_id=parent_response_id,
            model=model,
            status="in_progress",
            created_at=now,
            completed_at=None,
            assistant_output="",
            last_error=None,
            messages=full_messages,
            next_message_seq=len(full_messages) + 1,
        )

        async with self._lock:
            self._responses[response_id] = state

        await self.persistence.insert_thread(thread_id=thread_id, created_at=now)
        await self.persistence.insert_response(
            response_id=response_id,
            thread_id=thread_id,
            parent_response_id=parent_response_id,
            model=model,
            status="in_progress",
            created_at=now,
        )
        for idx, message in enumerate(full_messages, start=1):
            await self.persistence.insert_message(
                response_id=response_id,
                thread_id=thread_id,
                step_id=None,
                seq=idx,
                role=message["role"],
                content=message["content"],
                created_at=now,
            )
        return state

    async def append_message(
        self,
        response_id: str,
        role: str,
        content: str,
        step_id: str | None,
    ) -> None:
        now = _epoch_ms()
        async with self._lock:
            response = self._responses[response_id]
            response.messages.append({"role": role, "content": content})
            seq = response.next_message_seq
            response.next_message_seq += 1
            thread_id = response.thread_id

        await self.persistence.insert_message(
            response_id=response_id,
            thread_id=thread_id,
            step_id=step_id,
            seq=seq,
            role=role,
            content=content,
            created_at=now,
        )

    async def start_step(
        self,
        response_id: str,
        step_id: str,
        step_index: int,
    ) -> None:
        now = _epoch_ms()
        async with self._lock:
            response = self._responses[response_id]
            thread_id = response.thread_id

        await self.persistence.insert_step(
            step_id=step_id,
            response_id=response_id,
            thread_id=thread_id,
            step_index=step_index,
            status="in_progress",
            created_at=now,
        )

    async def complete_step(
        self,
        step_id: str,
        *,
        status: str,
        last_error: str | None = None,
    ) -> None:
        await self.persistence.update_step(
            step_id=step_id,
            status=status,
            last_error=last_error,
            completed_at=_epoch_ms(),
        )

    async def create_tool_call(
        self,
        tool_call_id: str,
        response_id: str,
        step_id: str,
        name: str,
        arguments: dict,
        mode: str,
    ) -> None:
        async with self._lock:
            response = self._responses[response_id]
            thread_id = response.thread_id

        await self.persistence.insert_tool_call(
            tool_call_id=tool_call_id,
            response_id=response_id,
            thread_id=thread_id,
            step_id=step_id,
            name=name,
            arguments=arguments,
            mode=mode,
            status="in_progress",
            created_at=_epoch_ms(),
        )

    async def complete_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str,
        last_error: str | None = None,
    ) -> None:
        await self.persistence.update_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            last_error=last_error,
            completed_at=_epoch_ms(),
        )

    async def add_tool_result(
        self,
        tool_call_id: str,
        response_id: str,
        output: object,
        is_error: bool,
    ) -> None:
        async with self._lock:
            response = self._responses[response_id]
            thread_id = response.thread_id
        await self.persistence.insert_tool_result(
            tool_call_id=tool_call_id,
            response_id=response_id,
            thread_id=thread_id,
            output=output,
            is_error=is_error,
            created_at=_epoch_ms(),
        )

    async def set_response_status(
        self,
        response_id: str,
        *,
        status: str,
        assistant_output: str,
        last_error: str | None,
    ) -> None:
        completed_at = _epoch_ms() if status in {"completed", "cancelled", "failed"} else None
        async with self._lock:
            response = self._responses[response_id]
            response.status = status
            response.assistant_output = assistant_output
            response.last_error = last_error
            response.completed_at = completed_at

        await self.persistence.update_response(
            response_id=response_id,
            status=status,
            assistant_output=assistant_output,
            last_error=last_error,
            completed_at=completed_at,
        )

    async def get_response(self, response_id: str) -> ResponseState | None:
        async with self._lock:
            response = self._responses.get(response_id)
            if response is None:
                return None
            return ResponseState(
                response_id=response.response_id,
                thread_id=response.thread_id,
                parent_response_id=response.parent_response_id,
                model=response.model,
                status=response.status,
                created_at=response.created_at,
                completed_at=response.completed_at,
                assistant_output=response.assistant_output,
                last_error=response.last_error,
                messages=[dict(m) for m in response.messages],
                next_message_seq=response.next_message_seq,
            )


def _epoch_ms() -> int:
    return int(time.time() * 1000)
