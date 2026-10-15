import pytest_asyncio

from garde.db import models  # noqa: F401
from garde.db.base import Base
from garde.db.session import create_engine, create_session_factory


@pytest_asyncio.fixture
async def session(tmp_path):
    # A real SQLite file in a temp folder, so foreign-key and constraint behavior is real.
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()