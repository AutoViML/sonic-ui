from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any, Callable

from config import Settings
from schemas import ToolSpec
from tools.builtins import filesystem_read, filesystem_write, http_get, shell_exec


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    mode: str
    timeout_seconds: int
    handler: Callable[[dict[str, Any], Settings, dict[str, Any]], Any] | None = None


class ToolRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._builtins = self._build_builtin_tools()

    def resolve_tools(self, request_tools: list[ToolSpec]) -> dict[str, ToolDefinition]:
        resolved: dict[str, ToolDefinition] = {}
        for spec in request_tools:
            builtin = self._builtins.get(spec.name)
            handler = builtin.handler if builtin else None
            resolved[spec.name] = ToolDefinition(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
                mode=spec.mode,
                timeout_seconds=spec.timeout_seconds,
                handler=handler,
            )
        return resolved

    async def execute_server_tool(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        if tool.mode != "server":
            raise ValueError(f"Tool '{tool.name}' is not server-executed")

        if tool.handler is None:
            raise ValueError(f"Tool '{tool.name}' has no server handler")

        if not self._is_tool_allowed(tool.name):
            raise ValueError(f"Tool '{tool.name}' is not allowlisted")

        return tool.handler(arguments, self.settings, context or {})

    def _is_tool_allowed(self, name: str) -> bool:
        if not self.settings.tool_allowlist:
            return False
        return name in self.settings.tool_allowlist

    def _build_builtin_tools(self) -> dict[str, ToolDefinition]:
        return {
            "calc": ToolDefinition(
                name="calc",
                description="Evaluate arithmetic expressions",
                parameters={
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string"},
                    },
                    "required": ["expression"],
                },
                mode="client",
                timeout_seconds=30,
                handler=None,
            ),
            "filesystem_read": ToolDefinition(
                name="filesystem_read",
                description="Read file contents",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
                mode="server",
                timeout_seconds=30,
                handler=lambda args, settings, ctx: filesystem_read.execute(
                    args,
                    filesystem_root=ctx.get("current_dir") or settings.filesystem_root,
                ),
            ),
            "filesystem_write": ToolDefinition(
                name="filesystem_write",
                description="Write file contents",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                mode="server",
                timeout_seconds=30,
                handler=lambda args, settings, ctx: filesystem_write.execute(
                    args,
                    filesystem_root=ctx.get("current_dir") or settings.filesystem_root,
                ),
            ),
            "shell_exec": ToolDefinition(
                name="shell_exec",
                description="Execute shell command",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                    },
                    "required": ["command"],
                },
                mode="server",
                timeout_seconds=30,
                handler=self._shell_exec_handler,
            ),
            "http_get": ToolDefinition(
                name="http_get",
                description="HTTP GET request",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                    },
                    "required": ["url"],
                },
                mode="server",
                timeout_seconds=30,
                handler=self._http_get_handler,
            ),
        }

    @staticmethod
    def evaluate_calc_expression(expression: str) -> str:
        allowed_ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Mod: operator.mod,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos,
        }

        def _eval(node: ast.AST) -> float:
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_ops:
                return allowed_ops[type(node.op)](_eval(node.operand))
            if isinstance(node, ast.BinOp) and type(node.op) in allowed_ops:
                return allowed_ops[type(node.op)](_eval(node.left), _eval(node.right))
            raise ValueError("Unsupported calc expression")

        tree = ast.parse(expression, mode="eval")
        value = _eval(tree)
        if int(value) == value:
            return str(int(value))
        return str(value)

    def _shell_exec_handler(self, arguments: dict[str, Any], settings: Settings, context: dict[str, Any]) -> str:
        if not settings.enable_shell_exec:
            raise ValueError("shell_exec is disabled")
        
        # Prioritize cwd from arguments if present, then context, else settings
        cwd = arguments.get("cwd") or context.get("current_dir") or settings.filesystem_root
        return shell_exec.execute(arguments, timeout_seconds=30, cwd=cwd)

    def _http_get_handler(self, arguments: dict[str, Any], settings: Settings, context: dict[str, Any]) -> str:
        if not settings.enable_http_get:
            raise ValueError("http_get is disabled")
        return http_get.execute(arguments, timeout_seconds=15)
