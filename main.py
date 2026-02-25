from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import os
import subprocess

from agent_loop import ActiveResponseSession
from config import Settings
from persistence import SQLitePersistence
from schemas import ProtocolError, parse_response_cancel, parse_response_create, parse_tool_result_submit
from state_store import StateStore
from tools.registry import ToolRegistry
from vllm_client import VLLMClient

logger = logging.getLogger("sonic")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def log_event(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


class RateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit_per_minute = max(1, limit_per_minute)
        self._events: dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        window_start = now - 60.0
        bucket = self._events[key]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= self.limit_per_minute:
            return False
        bucket.append(now)
        return True


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Sonic WS Gateway", version="0.2.0")

    app.state.settings = settings or Settings.from_env()
    app.state.store = None
    app.state.vllm_client = None
    app.state.tool_registry = None
    app.state.rate_limiter = None

    @app.on_event("startup")
    async def startup() -> None:
        cfg: Settings = app.state.settings

        persistence = SQLitePersistence(cfg.state_db_path)
        store = StateStore(
            persistence=persistence,
            on_event=lambda event, payload: log_event(event, **payload),
        )
        loaded = await store.initialize()

        app.state.store = store
        app.state.vllm_client = VLLMClient(
            base_url=cfg.vllm_url,
            connect_timeout=cfg.backend_connect_timeout,
            read_timeout=cfg.backend_read_timeout,
        )
        app.state.tool_registry = ToolRegistry(cfg)
        app.state.rate_limiter = RateLimiter(cfg.rate_limit_per_minute)

        log_event("persistence_load", count=loaded)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        store: StateStore = app.state.store
        vllm: VLLMClient = app.state.vllm_client
        if store:
            await store.close()
        if vllm:
            await vllm.close()

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/")
    async def root_ui() -> FileResponse:
        """Serve the Sonic WebSocket chat UI."""
        ui_path = Path(__file__).parent / "ui" / "index.html"
        return FileResponse(ui_path, media_type="text/html")

    # ── OpenAI-compatible REST API (for Cline, Continue, etc.) ──────────

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        """Return available models in OpenAI list format."""
        cfg: Settings = app.state.settings
        models = []
        for model_id in sorted(cfg.allowed_models):
            models.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "sonic",
                }
            )
        return JSONResponse({"object": "list", "data": models})

    @app.get("/v1/config")
    async def get_config() -> JSONResponse:
        """Return basic server config for the UI."""
        cfg: Settings = app.state.settings
        return JSONResponse({
            "default_model": cfg.model_name,
            "allowed_models": sorted(list(cfg.allowed_models))
        })

    @app.get("/v1/files")
    async def list_files(path: str = "."):
        """Simple file explorer endpoint."""
        try:
            root = Path(path).resolve()
            
            items = []
            for item in sorted(os.listdir(root)):
                if item.startswith('.'): continue
                full_path = root / item
                items.append({
                    "name": item,
                    "is_dir": full_path.is_dir(),
                    "path": str(full_path)
                })
            return JSONResponse({"path": str(root), "items": items})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/v1/terminal")
    async def terminal_exec(request: Request):
        """Simple terminal execution endpoint."""
        try:
            body = await request.json()
            command = body.get("command")
            cwd = body.get("cwd", ".")
            
            if not command:
                return JSONResponse({"error": "Command is required"}, status_code=400)
            
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=cwd
            )
            return JSONResponse({
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "cwd": os.getcwd()
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request):
        """OpenAI-compatible HTTP proxy — forwards to the backend."""
        cfg: Settings = app.state.settings
        vllm: VLLMClient = app.state.vllm_client

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status_code=400,
            )

        model = body.get("model") or cfg.model_name
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        temperature = body.get("temperature")
        top_p = body.get("top_p")
        max_tokens = body.get("max_tokens")

        if not messages:
            return JSONResponse(
                {"error": {"message": "messages is required", "type": "invalid_request_error"}},
                status_code=400,
            )

        if stream:
            # Stream back as SSE — proxy each fragment from the backend.
            async def _sse_generator():
                resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
                created = int(time.time())
                try:
                    async for fragment in vllm.stream_chat(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens,
                    ):
                        chunk = {
                            "id": resp_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": fragment},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    # Send final chunk with finish_reason
                    final_chunk = {
                        "id": resp_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    error_chunk = {
                        "error": {
                            "message": str(exc),
                            "type": "server_error",
                        }
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                _sse_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-streaming: collect all fragments and return a complete response.
        full_text = ""
        try:
            async for fragment in vllm.stream_chat(
                model=model,
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            ):
                full_text += fragment
        except Exception as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "server_error"}},
                status_code=502,
            )

        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_text,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": -1,
                    "completion_tokens": -1,
                    "total_tokens": -1,
                },
            }
        )

    @app.websocket("/v1/responses")
    async def responses_socket(websocket: WebSocket) -> None:
        cfg: Settings = app.state.settings
        store: StateStore = app.state.store
        vllm: VLLMClient = app.state.vllm_client
        tool_registry: ToolRegistry = app.state.tool_registry
        rate_limiter: RateLimiter = app.state.rate_limiter

        conn_id = uuid.uuid4().hex
        client_ip = websocket.client.host if websocket.client else "unknown"

        if not _authorized(websocket, cfg):
            await websocket.close(code=1008, reason="Unauthorized")
            return

        await websocket.accept()
        log_event("connection_opened", conn_id=conn_id, client_ip=client_ip)

        send_lock = asyncio.Lock()
        active_session: ActiveResponseSession | None = None
        active_task: asyncio.Task[None] | None = None

        async def send_event(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def send_error(
            code: str,
            message: str,
            *,
            response_id: str | None = None,
            thread_id: str | None = None,
            step_id: str | None = None,
        ) -> None:
            event: dict[str, Any] = {
                "type": "error",
                "error": {
                    "code": code,
                    "message": message,
                },
            }
            if response_id is not None:
                event["response_id"] = response_id
            if thread_id is not None:
                event["thread_id"] = thread_id
            if step_id is not None:
                event["step_id"] = step_id
            await send_event(event)

        def on_task_done(task: asyncio.Task[None]) -> None:
            nonlocal active_session, active_task
            active_session = None
            active_task = None
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            log_event("task_error", conn_id=conn_id, message=str(exc))

        try:
            while True:
                raw = await websocket.receive_text()

                if not rate_limiter.allow(client_ip):
                    await send_error("rate_limited", "Rate limit exceeded")
                    continue

                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await send_error("invalid_request", "Invalid JSON payload")
                    continue

                event_type = payload.get("type")
                if event_type == "response.create":
                    if active_task is not None and not active_task.done():
                        await send_error("busy", "A response is already in progress on this connection")
                        continue

                    try:
                        request = parse_response_create(payload, cfg)
                    except ProtocolError as exc:
                        await send_error(exc.code, exc.message)
                        continue

                    session_ref: dict[str, ActiveResponseSession] = {}
                    session = ActiveResponseSession(
                        conn_id=conn_id,
                        request=request,
                        settings=cfg,
                        store=store,
                        vllm_client=vllm,
                        tool_registry=tool_registry,
                        emit=lambda typ, body, step_id=None: _emit_response_event(
                            websocket=websocket,
                            send_lock=send_lock,
                            session=session_ref["session"],
                            event_type=typ,
                            body=body,
                            step_id=step_id,
                        ),
                        log_event=lambda event, fields: log_event(event, **fields),
                    )
                    session_ref["session"] = session
                    active_session = session
                    active_task = asyncio.create_task(session.run())
                    active_task.add_done_callback(on_task_done)
                    continue

                if event_type == "tool_result.submit":
                    if active_session is None:
                        await send_error("invalid_request", "No active response waiting for tool result")
                        continue

                    try:
                        submit = parse_tool_result_submit(payload)
                    except ProtocolError as exc:
                        await send_error(
                            exc.code,
                            exc.message,
                            response_id=active_session.response_id,
                            thread_id=active_session.thread_id,
                        )
                        continue

                    accepted = await active_session.submit_tool_result(submit)
                    if not accepted:
                        await send_error(
                            "invalid_request",
                            "tool_call_id does not match pending tool call",
                            response_id=active_session.response_id,
                            thread_id=active_session.thread_id,
                        )
                    continue

                if event_type == "response.cancel":
                    if active_session is None:
                        await send_error("invalid_request", "No active response to cancel")
                        continue

                    try:
                        cancel_response_id = parse_response_cancel(payload)
                    except ProtocolError as exc:
                        await send_error(exc.code, exc.message)
                        continue

                    if not active_session.matches_response(cancel_response_id):
                        await send_error(
                            "invalid_request",
                            "response_id does not match active response",
                            response_id=active_session.response_id,
                            thread_id=active_session.thread_id,
                        )
                        continue

                    active_session.cancel()
                    continue

                await send_error("invalid_request", f"Unsupported event type: {event_type}")

        except WebSocketDisconnect:
            if active_session is not None:
                active_session.cancel()
            if active_task is not None and not active_task.done():
                active_task.cancel()
                try:
                    await active_task
                except asyncio.CancelledError:
                    pass
            log_event("connection_closed", conn_id=conn_id)

    return app


async def _emit_response_event(
    *,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    session: ActiveResponseSession,
    event_type: str,
    body: dict[str, Any],
    step_id: str | None,
) -> None:
    payload: dict[str, Any] = {
        "type": event_type,
        "response_id": session.response_id,
        "thread_id": session.thread_id,
    }
    if step_id is not None:
        payload["step_id"] = step_id
    payload.update(body)
    async with send_lock:
        await websocket.send_json(payload)


def _authorized(websocket: WebSocket, settings: Settings) -> bool:
    if not settings.require_api_key:
        return True
    if not settings.api_key:
        return False

    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        return token == settings.api_key

    return websocket.query_params.get("token") == settings.api_key


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = Settings.from_env()
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.port, reload=False)
