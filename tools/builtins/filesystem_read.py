from __future__ import annotations

from pathlib import Path


def execute(arguments: dict, filesystem_root: str) -> str:
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("filesystem_read requires 'path'")

    root = Path(filesystem_root).resolve()
    target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("Path is outside filesystem allowlist root")

    return target.read_text(encoding="utf-8")
