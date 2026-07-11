from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from agno.db.in_memory import InMemoryDb
from agno.db.sqlite import SqliteDb

from config import Settings


@lru_cache(maxsize=8)
def build_agno_db(db_url: str, sqlite_file: str):
    """Build the Agno persistence backend for agents and workflows."""

    if db_url:
        return SqliteDb(db_url=db_url)
    if sqlite_file:
        path = Path(sqlite_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteDb(db_file=str(path))
    return InMemoryDb()


def get_agno_db(settings: Settings):
    """Return a cached Agno DB based on runtime settings."""

    return build_agno_db(settings.agno_db_url, settings.agno_sqlite_file)
