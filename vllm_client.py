from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import httpx


@dataclass(slots=True)
class VLLMBackendError(Exception):
    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


class VLLMClient:
    def __init__(
        self,
        base_url: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        # Reuse one client so keep-alive/concurrency can benefit vLLM batching.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=256, max_connections=1024),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[str, None]:
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        url = f"{self.base_url}/v1/chat/completions"

        try:
            async with self._client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    snippet = (await response.aread())[:512].decode(
                        "utf-8", errors="ignore"
                    )
                    raise VLLMBackendError(
                        f"Backend returned status {response.status_code}: {snippet}",
                        status_code=response.status_code,
                    )

                async for data in _iter_sse_data(response):
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError
                    if data == "[DONE]":
                        return

                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    fragment = _extract_fragment(payload)
                    if fragment:
                        yield fragment
        except httpx.TimeoutException as exc:
            raise VLLMBackendError(f"Backend timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise VLLMBackendError(f"Backend request failed: {exc}") from exc


async def _iter_sse_data(response: httpx.Response) -> AsyncGenerator[str, None]:
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while True:
            frame_end = _find_frame_end(buffer)
            if frame_end < 0:
                break
            frame = buffer[:frame_end]
            buffer = buffer[frame_end:]
            if buffer.startswith("\r\n\r\n"):
                buffer = buffer[4:]
            elif buffer.startswith("\n\n"):
                buffer = buffer[2:]

            data_lines: list[str] = []
            for line in frame.splitlines():
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                yield "\n".join(data_lines)


def _find_frame_end(buffer: str) -> int:
    idx = buffer.find("\n\n")
    idx_crlf = buffer.find("\r\n\r\n")
    if idx == -1:
        return idx_crlf
    if idx_crlf == -1:
        return idx
    return min(idx, idx_crlf)


def _extract_fragment(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""

    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content

    text = first.get("text")
    if isinstance(text, str):
        return text

    return ""
