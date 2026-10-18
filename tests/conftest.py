import pytest_asyncio

from garde.db import models  # noqa: F401
from garde.db.base import Base
from garde.db.session import create_engine, create_session_factory


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    # A real SQLite file in a temp folder, so constraint behavior is real.
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s