# Pytest configuration and shared fixtures for Phase 8a real database testing.
# Configures the application to connect strictly to the isolated TEST_DATABASE_URL on Neon,
# enforces safety guards, manages database truncation, and provides authentication and test helpers.

import hashlib
import hmac
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import jwt
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import pytest
from fastapi.testclient import TestClient

# Resolve project root and locate .env file
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
ENV_PATH = PROJECT_ROOT / ".env"

# Read environment variables directly from .env before importing application modules
env_vars = {}
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip().strip("'\"")

main_db_url = env_vars.get("DATABASE_URL")
test_db_url = env_vars.get("TEST_DATABASE_URL")

# Safety Guard Condition 1: Check presence and inequality
if not test_db_url:
    pytest.exit("Safety error: TEST_DATABASE_URL is missing in .env", returncode=1)

if test_db_url == main_db_url:
    pytest.exit("Safety error: TEST_DATABASE_URL must differ from DATABASE_URL", returncode=1)

test_db_name = urlparse(test_db_url).path.lstrip("/")
if "test" not in test_db_name.lower():
    pytest.exit("Safety error: TEST_DATABASE_URL database name must contain 'test'", returncode=1)

# Configure environment variable so app settings instantiate with TEST_DATABASE_URL
os.environ["DATABASE_URL"] = test_db_url

from app.core.config import get_settings
get_settings.cache_clear()
settings = get_settings()

import app.core.database as db
from app.main import app
from app.services import razorpay_service

# Re-initialize the application database connection pool to point to TEST_DATABASE_URL
if not db.pool.closed:
    db.pool.close()

db.pool = ConnectionPool(
    conninfo=test_db_url,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row},
)


# Verifies that the app connection pool connects to the test database at session start and keeps it open throughout the session.
# Aborts the entire test run immediately if the connected database is not the test database.
@pytest.fixture(scope="session", autouse=True)
def manage_app_pool_session_lifecycle():
    if db.pool.closed:
        db.open_pool()
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database();")
            row = cur.fetchone()
            current_db = row["current_database"] if isinstance(row, dict) else row[0]
            if current_db != test_db_name:
                pytest.exit("Safety error: App pool is not connected to the expected test database", returncode=1)

    # Protect pool from being closed prematurely between tests by TestClient lifespan exits
    original_db_close_pool = db.close_pool
    import app.main
    original_app_close_pool = app.main.close_pool
    db.close_pool = lambda: None
    app.main.close_pool = lambda: None

    yield

    # Cleanly close pool when test session finishes
    db.close_pool = original_db_close_pool
    app.main.close_pool = original_app_close_pool
    if not db.pool.closed:
        db.close_pool()


# Truncates all application tables before each test execution to ensure total test isolation.
# Uses a direct, independent connection to TEST_DATABASE_URL, never using the application pool.
@pytest.fixture(autouse=True)
def truncate_tables_before_each_test():
    with psycopg.connect(test_db_url, autocommit=True) as direct_conn:
        with direct_conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE webhook_events, order_items, orders, products CASCADE;")
    yield


# Creates and yields a FastAPI TestClient instance for issuing HTTP requests against the application.
@pytest.fixture
def client():
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


# Seeds catalog products in the test database for tests: Pen (2000 paise, stock 5) and Notebook (15000 paise, stock 3).
@pytest.fixture
def products():
    pen_name = f"Pen-{uuid.uuid4().hex[:8]}"
    notebook_name = f"Notebook-{uuid.uuid4().hex[:8]}"
    pen_price = 2000
    pen_stock = 5
    notebook_price = 15000
    notebook_stock = 3

    with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as direct_conn:
        with direct_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO products (name, price_paise, stock)
                VALUES (%s, %s, %s)
                RETURNING id, name, price_paise, stock;
                """,
                (pen_name, pen_price, pen_stock),
            )
            pen_row = cur.fetchone()

            cur.execute(
                """
                INSERT INTO products (name, price_paise, stock)
                VALUES (%s, %s, %s)
                RETURNING id, name, price_paise, stock;
                """,
                (notebook_name, notebook_price, notebook_stock),
            )
            notebook_row = cur.fetchone()

    return {
        "pen": pen_row,
        "notebook": notebook_row,
    }


# Generates JWT authentication tokens and headers for two distinct users using the claim the app expects.
@pytest.fixture
def user_tokens():
    user1_id = 1
    user2_id = 2

    token1 = jwt.encode(
        {"userId": user1_id},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    token2 = jwt.encode(
        {"userId": user2_id},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )

    return {
        "user1": {
            "id": user1_id,
            "token": token1,
            "headers": {
                "Authorization": f"Bearer {token1}",
                "Content-Type": "application/json",
            },
        },
        "user2": {
            "id": user2_id,
            "token": token2,
            "headers": {
                "Authorization": f"Bearer {token2}",
                "Content-Type": "application/json",
            },
        },
    }


# Queries the current stock for a given product directly from the test database.
@pytest.fixture
def get_stock():
    def _get_stock(product_id: uuid.UUID) -> int:
        with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as direct_conn:
            with direct_conn.cursor() as cur:
                cur.execute("SELECT stock FROM products WHERE id = %s;", (product_id,))
                row = cur.fetchone()
                return row["stock"] if row else None
    return _get_stock


# Queries an order row by its unique identifier directly from the test database.
@pytest.fixture
def get_order_row():
    def _get_order_row(order_id: uuid.UUID | str) -> dict | None:
        with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as direct_conn:
            with direct_conn.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE id = %s;", (order_id,))
                return cur.fetchone()
    return _get_order_row


# Queries all line items associated with an order directly from the test database.
@pytest.fixture
def get_order_items():
    def _get_order_items(order_id: uuid.UUID | str) -> list[dict]:
        with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as direct_conn:
            with direct_conn.cursor() as cur:
                cur.execute("SELECT * FROM order_items WHERE order_id = %s;", (order_id,))
                return cur.fetchall()
    return _get_order_items


# Counts the total number of records in the webhook_events table directly on the test database.
@pytest.fixture
def count_webhook_events():
    def _count_webhook_events() -> int:
        with psycopg.connect(test_db_url, autocommit=True) as direct_conn:
            with direct_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM webhook_events;")
                return cur.fetchone()[0]
    return _count_webhook_events


# Computes an HMAC-SHA256 signature using RAZORPAY_WEBHOOK_SECRET over the exact raw payload bytes.
@pytest.fixture
def sign_webhook():
    def _sign_webhook(raw_bytes: bytes) -> str:
        return hmac.new(
            key=settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
            msg=raw_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()
    return _sign_webhook


# Mocks the external Razorpay SDK order creation call to avoid external network requests during tests.
@pytest.fixture(autouse=True)
def mock_razorpay_order_sdk(monkeypatch):
    call_log = []

    def fake_create_razorpay_order(total_paise: int, receipt: str) -> str:
        rzp_id = f"order_{uuid.uuid4().hex[:14]}"
        call_log.append({"total_paise": total_paise, "receipt": receipt, "razorpay_order_id": rzp_id})
        return rzp_id

    monkeypatch.setattr(razorpay_service, "create_razorpay_order", fake_create_razorpay_order)
    return call_log
