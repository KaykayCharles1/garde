import asyncio
from types import SimpleNamespace

from garde.db.base import Base
from garde.db.session import create_engine
from garde.runtime import (
    BACKGROUND_KEY,
    schema_is_current,
    start_background_tasks,
    stop_background_tasks,
)


async def test_schema_check_detects_an_unmigrated_database(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'x.db').as_posix()}"
    assert await schema_is_current(url) is False
    engine = create_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    assert await schema_is_current(url) is True


async def test_background_tasks_start_and_are_cancelled_cleanly():
    app = SimpleNamespace(bot_data={})
    started = asyncio.Event()
    cancelled = {"flag": False}

    async def worker():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise

    await start_background_tasks(app, {"worker": worker})
    await asyncio.wait_for(started.wait(), 1)
    await stop_background_tasks(app)
    assert cancelled["flag"] is True
    assert BACKGROUND_KEY not in app.bot_data


async def test_a_dying_task_is_logged_loudly_and_the_others_keep_running(caplog):
    app = SimpleNamespace(bot_data={})

    async def dies():
        raise RuntimeError("boom")

    async def lives():
        await asyncio.sleep(3600)

    await start_background_tasks(app, {"dies": dies, "lives": lives})
    await asyncio.sleep(0.05)
    assert any("background_task_died" in r.getMessage() for r in caplog.records)
    assert not app.bot_data[BACKGROUND_KEY][1].done()
    await stop_background_tasks(app)