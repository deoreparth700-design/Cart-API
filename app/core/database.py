from contextlib import contextmanager
from typing import Generator
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings

# Global connection pool initialized with DATABASE_URL from settings.
# row_factory=dict_row ensures all query results are accessible as key-value dictionaries.
pool = ConnectionPool(
    conninfo=settings.DATABASE_URL,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row},
)


# Opens the connection pool during application startup.
# Establishes initial connections to Neon PostgreSQL so incoming requests have fast DB access.
def open_pool():
    if settings.DATABASE_URL and pool.closed:
        pool.open()


# Closes all open connections in the pool during application shutdown.
# Gracefully releases all socket connections held by the application.
def close_pool():
    if not pool.closed:
        pool.close()


# Yields a database connection inside a managed single transaction.
# Automatically commits changes if the block completes successfully, or rolls back on any exception.
@contextmanager
def transaction() -> Generator[psycopg.Connection, None, None]:
    if pool.closed and settings.DATABASE_URL:
        pool.open()
    with pool.connection() as conn:
        with conn.transaction():
            yield conn
