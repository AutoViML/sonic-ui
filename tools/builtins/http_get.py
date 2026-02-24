from __future__ import annotations

import httpx


def execute(arguments: dict, timeout_seconds: int = 15) -> str:
    url = arguments.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("http_get requires 'url'")

    response = httpx.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.text
