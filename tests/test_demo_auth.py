# Real database tests for the /api/demo-token endpoint and demo authentication security.
# Verifies feature toggle behavior, fixed user ID enforcement, token expiration, query parameter immunity,
# and database-level user isolation on order retrieval.

from datetime import datetime, timezone
import uuid
import jwt
import psycopg
from psycopg.rows import dict_row
import pytest

from app.core.config import settings
from tests.conftest import test_db_url

FIXED_DEMO_USER_ID = 999999


# Helper to insert test orders with specific user IDs directly into the test database.
def insert_test_order(conn_url: str, user_id: int, total_paise: int = 5000) -> dict:
    with psycopg.connect(conn_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (user_id, status, total_paise, razorpay_order_id, idempotency_key)
                VALUES (%s, 'created', %s, %s, %s)
                RETURNING id, user_id, status, total_paise, razorpay_order_id;
                """,
                (
                    user_id,
                    total_paise,
                    f"order_{uuid.uuid4().hex[:14]}",
                    f"key-{uuid.uuid4().hex[:8]}",
                ),
            )
            return cur.fetchone()


# Verifies that /api/demo-token returns HTTP 404 when DEMO_MODE is disabled.
def test_demo_token_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    response = client.get("/api/demo-token")
    assert response.status_code == 404
    assert response.json()["detail"] == "Demo mode is disabled"


# Verifies that /api/demo-token returns HTTP 200 with fixed demo user ID and valid <= 30m expiry when enabled.
def test_demo_token_enabled_returns_token_with_fixed_user_id_and_expiry(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    response = client.get("/api/demo-token")
    assert response.status_code == 200
    data = response.json()
    assert "token" in data

    payload = jwt.decode(
        data["token"],
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )

    assert payload["userId"] == FIXED_DEMO_USER_ID
    assert "exp" in payload
    assert "iat" in payload

    now_timestamp = datetime.now(timezone.utc).timestamp()
    time_remaining_seconds = payload["exp"] - now_timestamp
    # Token must have a positive life and expire in 30 minutes (1800s) or less
    assert 0 < time_remaining_seconds <= 1800


# Verifies that passing query parameters like ?user_id=1 or ?userId=1 does NOT override the user ID.
def test_demo_token_ignores_query_parameters(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    tampered_queries = [
        "?user_id=1",
        "?userId=1",
        "?user_id=2&exp=9999999999",
        "?userId=42",
    ]

    for query in tampered_queries:
        response = client.get(f"/api/demo-token{query}")
        assert response.status_code == 200
        payload = jwt.decode(
            response.json()["token"],
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        assert payload["userId"] == FIXED_DEMO_USER_ID


# Verifies that demo tokens can retrieve the demo user's orders but receive 404 on orders belonging to other users.
def test_demo_token_order_access_isolation(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)

    # Obtain demo token
    token_res = client.get("/api/demo-token")
    assert token_res.status_code == 200
    demo_token = token_res.json()["token"]
    auth_headers = {"Authorization": f"Bearer {demo_token}"}

    # Insert an order for the demo user and an order for a real user (user_id=1) in the test database
    demo_order = insert_test_order(test_db_url, user_id=FIXED_DEMO_USER_ID, total_paise=4500)
    other_order = insert_test_order(test_db_url, user_id=1, total_paise=9000)

    # Demo user accessing their own order should succeed with 200 OK
    res_own = client.get(f"/api/orders/{demo_order['id']}", headers=auth_headers)
    assert res_own.status_code == 200
    assert res_own.json()["id"] == str(demo_order["id"])
    assert res_own.json()["user_id"] == FIXED_DEMO_USER_ID

    # Demo user attempting to access another user's order should receive 404 Not Found
    res_other = client.get(f"/api/orders/{other_order['id']}", headers=auth_headers)
    assert res_other.status_code == 404
    assert res_other.json()["detail"] == "Order not found"
