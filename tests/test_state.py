from __future__ import annotations

import asyncio
from pathlib import Path

from persistence import SQLitePersistence
from state_store import StateStore


def test_persistence_reload_and_previous_lookup(tmp_path: Path) -> None:
    asyncio.run(_run_persistence_reload_and_previous_lookup(tmp_path))


async def _run_persistence_reload_and_previous_lookup(tmp_path: Path) -> None:
    db_path = tmp_path / "phase2_state.db"

    store1 = StateStore(SQLitePersistence(str(db_path)))
    loaded = await store1.initialize()
    assert loaded == 0

    await store1.start_response(
        response_id="resp_1",
        thread_id="thread_1",
        parent_response_id=None,
        model="mitko",
        base_messages=[],
        user_input="hello",
    )
    await store1.append_message("resp_1", role="assistant", content="world", step_id="step_1")
    await store1.set_response_status(
        "resp_1",
        status="completed",
        assistant_output="world",
        last_error=None,
    )
    await store1.close()

    store2 = StateStore(SQLitePersistence(str(db_path)))
    loaded = await store2.initialize()
    assert loaded == 1

    thread_id, messages, found = await store2.resolve_previous_response("resp_1")
    assert found
    assert thread_id == "thread_1"
    assert [m["role"] for m in messages] == ["user", "assistant"]

    response = await store2.get_response("resp_1")
    assert response is not None
    assert response.status == "completed"
    assert response.assistant_output == "world"
    await store2.close()
