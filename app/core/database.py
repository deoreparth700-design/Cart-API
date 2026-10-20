from contextlib import contextmanager
from typing import Generator
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings

# Global connection pool initialized with DATABASE_URL from settings.
# check=ConnectionPool.check_connection verifies connections are alive before handing them out.
# max_idle=60.0 retires idle connections before Neon's proxy drops quiet SSL sockets.
# row_factory=dict_row ensures all query results are accessible as key-value dictionaries.
pool = ConnectionPool(
    conninfo=settings.DATABASE_URL,
    min_size=1,
    max_size=10,
    open=False,
    check=ConnectionPool.check_connection,
    max_idle=60.0,
    kwargs={"row_factory": dict_row},
)


# Validates database configuration and opens the connection pool during application startup.
# Establishes connections to Neon PostgreSQL so incoming requests have fast DB access.
def open_pool():
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in environment or .env file.")
    if pool.closed:
        pool.open(wait=True)


# Closes all open connections in the pool during application shutdown.
# Gracefully releases all socket connections held by the application.
def close_pool():
    if not pool.closed:
        pool.close()


# Yields a database connection inside a managed single transaction.
# Automatically commits changes if the block completes successfully, or rolls back on any exception.
@contextmanager
def transaction() -> Generator[psycopg.Connection, None, None]:
    if pool.closed:
        open_pool()
    with pool.connection() as conn:
        with conn.transaction():
            yield conn
