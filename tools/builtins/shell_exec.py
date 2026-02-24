from __future__ import annotations

import subprocess


def execute(arguments: dict, timeout_seconds: int = 30) -> str:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("shell_exec requires 'command'")

    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"Command failed: {result.stderr.strip()}")
    return result.stdout
