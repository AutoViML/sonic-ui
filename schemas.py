from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from config import Settings


TOOL_SYSTEM_PROMPT = """You are an autonomous AI coding agent.

TOOLS
You may call tools when needed.

When calling a tool:
- Respond with ONLY valid JSON.
- No explanations.
- Format exactly:

{
  \"tool_call\": {
    \"name\": \"<tool_name>\",
    \"arguments\": { \"<arg_name>\": \"<value>\", ... }
  }
}

CRITICAL RULES:
- ALWAYS include ALL required arguments for every tool call. Never emit an empty arguments object {}.
- Do NOT call a tool unless you have values for all required arguments.
- Do NOT include markdown.
- Do NOT include text before or after JSON.
- After receiving a tool result, IMMEDIATELY continue with the next tool call or produce the final answer. Do NOT stop and wait. Do NOT output explanatory text between tool calls.
- Only produce plain text when you have completed ALL steps and are ready to give the final answer.

When you receive [TOOL RESULT]:
- Do NOT acknowledge or summarize the result.
- Immediately emit the next tool_call JSON or write the final answer.

When you receive [TOOL ERROR]:
- Do NOT give up or stop.
- Diagnose what went wrong (wrong path, missing tool, network issue, etc).
- Immediately try a different approach (different command, fallback data, alternative method).
- Continue working toward the goal autonomously.

If no tool is needed:
- Respond normally with plain text.
"""

PLAIN_SYSTEM_PROMPT = """You are an AI coding assistant.
Respond clearly and directly in plain text unless the user explicitly asks for JSON output.
Do not emit tool_call JSON unless tools were explicitly provided by the caller.
"""


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(slots=True)
class ResponseFormat:
    type: str
    schema: dict[str, Any]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    mode: str = "client"
    timeout_seconds: int = 120


@dataclass(slots=True)
class ResponseCreateRequest:
    model: str
    input_text: str
    previous_response_id: str | None
    tools: list[ToolSpec]
    response_format: ResponseFormat | None
    temperature: float
    top_p: float
    max_tokens: int | None
    current_dir: str | None = None


@dataclass(slots=True)
class ToolResultSubmit:
    tool_call_id: str
    output: Any
    is_error: bool


def parse_response_create(payload: dict[str, Any], settings: Settings) -> ResponseCreateRequest:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise ProtocolError("invalid_request", "Missing or invalid response object")

    model = response.get("model") or settings.model_name
    if model not in settings.allowed_models:
        raise ProtocolError("invalid_request", f"Model '{model}' is not allowed")

    input_value = response.get("input")
    if isinstance(input_value, str):
        input_text = input_value.strip()
    elif isinstance(input_value, list):
        input_text = _flatten_input_items(input_value)
    else:
        input_text = ""
    if not input_text:
        raise ProtocolError("invalid_request", "response.input must be non-empty")

    if len(input_text.encode("utf-8")) > settings.max_input_bytes:
        raise ProtocolError(
            "invalid_request",
            f"response.input exceeds {settings.max_input_bytes} bytes",
        )

    previous_response_id = response.get("previous_response_id")
    if previous_response_id is not None and not isinstance(previous_response_id, str):
        raise ProtocolError("invalid_request", "previous_response_id must be a string")

    tools = _parse_tools(response.get("tools"))
    response_format = _parse_response_format(response.get("response_format"))

    temperature = _parse_float_field(
        response.get("temperature"),
        fallback=settings.default_temperature,
    )
    top_p = _parse_float_field(response.get("top_p"), fallback=settings.default_top_p)
    max_tokens = response.get("max_tokens")
    if max_tokens is not None and not isinstance(max_tokens, int):
        raise ProtocolError("invalid_request", "max_tokens must be an integer")

    return ResponseCreateRequest(
        model=model,
        input_text=input_text,
        previous_response_id=previous_response_id,
        tools=tools,
        response_format=response_format,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        current_dir=response.get("current_dir")
    )


def parse_tool_result_submit(payload: dict[str, Any]) -> ToolResultSubmit:
    tool_call_id = payload.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        raise ProtocolError("invalid_request", "tool_call_id is required")

    if "output" not in payload:
        raise ProtocolError("invalid_request", "output is required")

    is_error = payload.get("is_error", False)
    if not isinstance(is_error, bool):
        raise ProtocolError("invalid_request", "is_error must be a boolean")

    return ToolResultSubmit(
        tool_call_id=tool_call_id,
        output=payload["output"],
        is_error=is_error,
    )


def parse_response_cancel(payload: dict[str, Any]) -> str | None:
    response_id = payload.get("response_id")
    if response_id is not None and not isinstance(response_id, str):
        raise ProtocolError("invalid_request", "response_id must be a string")
    return response_id


@dataclass(slots=True)
class ParsedToolCall:
    name: str
    arguments: dict[str, Any]


def parse_tool_call_json(text: str) -> ParsedToolCall | None:
    stripped = text.strip()
    if not stripped:
        return None

    # Strip markdown code fences if the model wrapped the JSON (e.g. ```json ... ```)
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Drop the opening fence line (```json or ```)
        lines = lines[1:]
        # Drop the closing fence line if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    if not isinstance(value, dict):
        return None

    tool_call = value.get("tool_call")
    if not isinstance(tool_call, dict):
        return None

    name = tool_call.get("name")
    arguments = tool_call.get("arguments", {})

    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(arguments, dict):
        return None

    return ParsedToolCall(name=name.strip(), arguments=arguments)


def build_system_message(
    tools: list[ToolSpec],
    response_format: ResponseFormat | None,
) -> str:
    parts = [TOOL_SYSTEM_PROMPT.rstrip() if tools else PLAIN_SYSTEM_PROMPT.rstrip()]

    if tools:
        tool_lines = ["Available tools:"]
        for spec in tools:
            # Extract required fields from parameters schema so the model knows what is mandatory
            required_fields = spec.parameters.get("required", [])
            props = spec.parameters.get("properties", {})
            arg_descriptions = ", ".join(
                f"{k} ({props[k].get('type', 'string')}{'*' if k in required_fields else ''})"
                for k in props
            )
            required_note = f" [REQUIRED: {', '.join(required_fields)}]" if required_fields else ""
            tool_lines.append(
                f"- {spec.name}: {spec.description} (mode={spec.mode}, timeout={spec.timeout_seconds}s){required_note}"
                + (f"\n  Args: {arg_descriptions}" if arg_descriptions else "")
            )
        parts.append("\n".join(tool_lines))

    if response_format is not None:
        schema_json = json.dumps(response_format.schema, separators=(",", ":"))
        parts.append(
            "Return ONLY valid JSON that matches this schema. "
            "No text. No markdown.\n"
            f"Schema: {schema_json}"
        )

    return "\n\n".join(parts).strip()


def _parse_tools(raw: Any) -> list[ToolSpec]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProtocolError("invalid_request", "tools must be an array")

    parsed: list[ToolSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProtocolError("invalid_request", "tool definitions must be objects")

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("invalid_request", "tool name is required")

        description = item.get("description", "")
        if not isinstance(description, str):
            raise ProtocolError("invalid_request", "tool description must be a string")

        parameters = item.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ProtocolError("invalid_request", "tool parameters must be an object")

        mode = item.get("mode", "client")
        if mode not in {"client", "server"}:
            raise ProtocolError("invalid_request", "tool mode must be 'client' or 'server'")

        timeout_seconds = item.get("timeout_seconds", 120)
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ProtocolError("invalid_request", "timeout_seconds must be a positive integer")

        parsed.append(
            ToolSpec(
                name=name.strip(),
                description=description,
                parameters=parameters,
                mode=mode,
                timeout_seconds=timeout_seconds,
            )
        )
    return parsed


def _parse_response_format(raw: Any) -> ResponseFormat | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError("invalid_request", "response_format must be an object")

    value_type = raw.get("type")
    schema = raw.get("schema")
    if value_type != "json_schema" or not isinstance(schema, dict):
        raise ProtocolError(
            "invalid_request",
            "response_format must be {type:'json_schema', schema:{...}}",
        )

    return ResponseFormat(type="json_schema", schema=schema)


def _parse_float_field(raw: Any, fallback: float) -> float:
    if raw is None:
        return fallback
    if isinstance(raw, (float, int)):
        return float(raw)
    raise ProtocolError("invalid_request", "temperature/top_p must be numeric")


def _flatten_input_items(items: list[Any]) -> str:
    chunks: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            chunks.append(item.strip())
            continue

        if not isinstance(item, dict):
            continue

        if item.get("type") == "input_text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
        elif item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                chunks.append(content.strip())
    return "\n".join(chunks).strip()
