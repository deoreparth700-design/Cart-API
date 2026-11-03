# Integration tests for abandoned order stock release and 15-minute expiration window.
# Verifies that expired 'created' orders are marked 'failed' and release stock,
# fresh orders and paid orders are untouched, and double-release does not occur under concurrency.

import uuid
import psycopg
from psycopg.rows import dict_row
import pytest

from app.core.database import transaction
from app.repositories import order_repo
from tests.conftest import test_db_url


# Helper to age an order in the test database without sleeping
def age_order(order_id: uuid.UUID | str, minutes_ago: int = 20):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders
                SET created_at = NOW() - (%s || ' minutes')::interval
                WHERE id = %s;
                """,
                (minutes_ago, order_id),
            )


# 1. Expired order releases stock and transitions to 'failed'
def test_expired_order_releases_stock(client, products, user_tokens, get_stock, get_order_row):
    user = user_tokens["user1"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    # Place an order for 2 pens (stock decrements by 2)
    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 2}]}
    headers = {**user["headers"], "Idempotency-Key": f"key-exp-1-{uuid.uuid4().hex[:8]}"}
    res = client.post("/api/orders", json=payload, headers=headers)
    assert res.status_code == 201
    order_id = res.json()["id"]
    assert get_stock(pen["id"]) == initial_stock - 2
    assert get_order_row(order_id)["status"] == "created"

    # Age the order by 20 minutes (exceeding 15 min expiration window)
    age_order(order_id, minutes_ago=20)

    # Call release_expired_orders
    with transaction() as conn:
        released = order_repo.release_expired_orders(conn)
        assert uuid.UUID(order_id) in released

    # Verify order is marked 'failed' and stock is restored
    assert get_order_row(order_id)["status"] == "failed"
    assert get_stock(pen["id"]) == initial_stock


# 2. Fresh order (younger than 15 min) is untouched
def test_fresh_order_is_untouched(client, products, user_tokens, get_stock, get_order_row):
    user = user_tokens["user1"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}
    headers = {**user["headers"], "Idempotency-Key": f"key-fresh-{uuid.uuid4().hex[:8]}"}
    res = client.post("/api/orders", json=payload, headers=headers)
    assert res.status_code == 201
    order_id = res.json()["id"]
    assert get_stock(pen["id"]) == initial_stock - 1

    # Order is 5 minutes old (fresh, not expired)
    age_order(order_id, minutes_ago=5)

    with transaction() as conn:
        released = order_repo.release_expired_orders(conn)
        assert uuid.UUID(order_id) not in released

    assert get_order_row(order_id)["status"] == "created"
    assert get_stock(pen["id"]) == initial_stock - 1


# 3. Releasing twice does not restore stock twice
def test_releasing_twice_does_not_double_restore_stock(client, products, user_tokens, get_stock, get_order_row):
    user = user_tokens["user1"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 2}]}
    headers = {**user["headers"], "Idempotency-Key": f"key-twice-{uuid.uuid4().hex[:8]}"}
    res = client.post("/api/orders", json=payload, headers=headers)
    assert res.status_code == 201
    order_id = res.json()["id"]

    age_order(order_id, minutes_ago=25)

    # First release
    with transaction() as conn:
        released1 = order_repo.release_expired_orders(conn)
        assert uuid.UUID(order_id) in released1
    assert get_stock(pen["id"]) == initial_stock

    # Second release on same connection or new transaction
    with transaction() as conn:
        released2 = order_repo.release_expired_orders(conn)
        assert uuid.UUID(order_id) not in released2

    # Stock must NOT be incremented again
    assert get_stock(pen["id"]) == initial_stock
    assert get_order_row(order_id)["status"] == "failed"


# 4. Paid order is never released
def test_paid_order_is_never_released(client, products, user_tokens, get_stock, get_order_row):
    user = user_tokens["user1"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}
    headers = {**user["headers"], "Idempotency-Key": f"key-paid-{uuid.uuid4().hex[:8]}"}
    res = client.post("/api/orders", json=payload, headers=headers)
    assert res.status_code == 201
    order_id = res.json()["id"]

    # Mark order as paid
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = 'paid' WHERE id = %s;", (order_id,))

    # Age the paid order past 15 minutes
    age_order(order_id, minutes_ago=30)

    with transaction() as conn:
        released = order_repo.release_expired_orders(conn)
        assert uuid.UUID(order_id) not in released

    assert get_order_row(order_id)["status"] == "paid"
    assert get_stock(pen["id"]) == initial_stock - 1


# 5. Integration: create_order automatically releases abandoned stock to fulfill new orders
def test_create_order_automatically_releases_abandoned_stock(client, products, user_tokens, get_stock):
    user1 = user_tokens["user1"]
    user2 = user_tokens["user2"]
    pen = products["pen"]

    # Set pen stock to exactly 1
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE products SET stock = 1 WHERE id = %s;", (pen["id"],))

    # User 1 orders the last pen
    res1 = client.post(
        "/api/orders",
        json={"items": [{"product_id": str(pen["id"]), "quantity": 1}]},
        headers={**user1["headers"], "Idempotency-Key": f"key-u1-{uuid.uuid4().hex[:8]}"},
    )
    assert res1.status_code == 201
    order1_id = res1.json()["id"]
    assert get_stock(pen["id"]) == 0

    # User 2 tries to order immediately -> 409 Conflict (Insufficient stock)
    res2_fail = client.post(
        "/api/orders",
        json={"items": [{"product_id": str(pen["id"]), "quantity": 1}]},
        headers={**user2["headers"], "Idempotency-Key": f"key-u2-fail-{uuid.uuid4().hex[:8]}"},
    )
    assert res2_fail.status_code == 409

    # User 1 abandons checkout (age order past 15 min)
    age_order(order1_id, minutes_ago=16)

    # User 2 orders again -> create_order releases User 1's expired order and succeeds with 201!
    res2_success = client.post(
        "/api/orders",
        json={"items": [{"product_id": str(pen["id"]), "quantity": 1}]},
        headers={**user2["headers"], "Idempotency-Key": f"key-u2-succ-{uuid.uuid4().hex[:8]}"},
    )
    assert res2_success.status_code == 201
    assert get_stock(pen["id"]) == 0
