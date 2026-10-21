import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from garde.db.session import create_engine

logger = logging.getLogger(__name__)
BACKGROUND_KEY = "background_tasks"


async def schema_is_current(database_url: str) -> bool:
    """False if the database has not been migrated (the newest column is missing)."""
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT gaps_notified FROM chats LIMIT 1"))
        return True
    except OperationalError:
        return False
    finally:
        await engine.dispose()


def _log_task_end(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.critical("background_task_died name=%s", task.get_name(), exc_info=error)
    else:
        logger.critical("background_task_stopped name=%s", task.get_name())


async def start_background_tasks(
    app: Any, runners: dict[str, Callable[[], Awaitable[None]]]
) -> None:
    tasks = []
    for name, runner in runners.items():
        task = asyncio.create_task(runner(), name=name)
        task.add_done_callback(_log_task_end)
        tasks.append(task)
    app.bot_data[BACKGROUND_KEY] = tasks
    logger.info("background_started tasks=%s", ", ".join(runners))


async def stop_background_tasks(app: Any) -> None:
    tasks = app.bot_data.pop(BACKGROUND_KEY, [])
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)