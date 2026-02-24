from __future__ import annotations

from pathlib import Path


def execute(arguments: dict, filesystem_root: str) -> str:
    path = arguments.get("path")
    content = arguments.get("content")

    if not isinstance(path, str) or not path.strip():
        raise ValueError("filesystem_write requires 'path'")
    if not isinstance(content, str):
        raise ValueError("filesystem_write requires string 'content'")

    root = Path(filesystem_root).resolve()
    target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("Path is outside filesystem allowlist root")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return "ok"
