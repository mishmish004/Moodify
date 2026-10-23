"""SQLite storage: one file, stdlib sqlite3, tables defined in schema.sql."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.core.config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Off by default and per connection; without it ON DELETE CASCADE does nothing
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Create the file and tables if they're missing. Safe to run on every startup."""
    db_path = Path(db_path or get_settings().DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        # WAL is stored in the file itself, so setting it once here covers every later connection
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_PATH.read_text())
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error, and always closes."""
    conn = _connect(Path(db_path or get_settings().DB_PATH))
    try:
        with conn:
            yield conn
    finally:
        conn.close()
