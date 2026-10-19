# Real database tests for order transactions, inventory consistency, and item merging.
# Verifies ACID transaction semantics against isolated Neon PostgreSQL database.

import concurrent.futures
import threading
import uuid
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row

from app.main import app


# Tests that purchasing 2 notebooks and 1 pen returns 201, computes total paise correctly, and deducts inventory.
def test_valid_order_computes_total_and_reduces_stock(client, products, user_tokens, get_stock, get_order_row, get_order_items):
    user = user_tokens["user1"]
    pen = products["pen"]
    notebook = products["notebook"]

    payload = {
        "items": [
            {"product_id": str(notebook["id"]), "quantity": 2},
            {"product_id": str(pen["id"]), "quantity": 1},
        ]
    }
    headers = {**user["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"}

    response = client.post("/api/orders", json=payload, headers=headers)

    assert response.status_code == 201
    body = response.json()
    expected_total = (2 * 15000) + (1 * 2000)  # 32000 paise
    assert body["total_paise"] == expected_total
    assert body["status"] == "created"

    order_id = body["id"]
    db_order = get_order_row(order_id)
    assert db_order is not None
    assert db_order["total_paise"] == expected_total

    # Verify inventory was decremented correctly in the real database
    assert get_stock(notebook["id"]) == 3 - 2  # 1 notebook remaining
    assert get_stock(pen["id"]) == 5 - 1       # 4 pens remaining

    # Verify order item records
    items = get_order_items(order_id)
    assert len(items) == 2


# Tests that requesting more items than available in stock triggers 409 and rolls back all stock deductions.
def test_insufficient_stock_rolls_back_atomically(client, products, user_tokens, get_stock):
    from tests.conftest import test_db_url
    user = user_tokens["user1"]
    pen = products["pen"]
    notebook = products["notebook"]

    # Request 1 pen (available: 5) and 10 notebooks (available: 3)
    payload = {
        "items": [
            {"product_id": str(pen["id"]), "quantity": 1},
            {"product_id": str(notebook["id"]), "quantity": 10},
        ]
    }
    headers = {**user["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"}

    response = client.post("/api/orders", json=payload, headers=headers)

    assert response.status_code == 409
    assert "insufficient stock" in response.json()["detail"].lower()

    # Verify pen stock was NOT decremented and remained unchanged
    assert get_stock(pen["id"]) == 5
    assert get_stock(notebook["id"]) == 3

    # Verify no order row was committed to the database
    with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM orders;")
            assert cur.fetchone()["count"] == 0


# Tests that client-provided amount fields in request bodies are ignored and priced strictly from catalog.
def test_client_provided_amount_in_body_is_ignored(client, products, user_tokens):
    user = user_tokens["user1"]
    pen = products["pen"]

    # Client maliciously attempts to set price to 1 paise
    payload = {
        "items": [{"product_id": str(pen["id"]), "quantity": 1}],
        "amount": 1,
    }
    headers = {**user["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"}

    response = client.post("/api/orders", json=payload, headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["total_paise"] == 2000, "Order total must be calculated from catalog price (2000), ignoring client 'amount'"


# Tests that user B requesting user A's order receives a 404 Not Found error.
def test_user_cannot_access_another_users_order(client, products, user_tokens):
    user1 = user_tokens["user1"]
    user2 = user_tokens["user2"]
    pen = products["pen"]

    # User 1 creates an order
    create_payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}
    create_res = client.post(
        "/api/orders",
        json=create_payload,
        headers={**user1["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"},
    )
    assert create_res.status_code == 201
    order_id = create_res.json()["id"]

    # User 2 attempts to fetch User 1's order
    get_res_user2 = client.get(f"/api/orders/{order_id}", headers=user2["headers"])
    assert get_res_user2.status_code == 404

    # User 1 fetches their own order successfully
    get_res_user1 = client.get(f"/api/orders/{order_id}", headers=user1["headers"])
    assert get_res_user1.status_code == 200
    assert get_res_user1.json()["id"] == order_id


# Tests that duplicate product IDs within a single order request are merged and summed before processing.
def test_duplicate_product_ids_are_merged(client, products, user_tokens, get_stock, get_order_items):
    user = user_tokens["user1"]
    pen = products["pen"]

    # Supply duplicate items for the same product ID (quantity 1 + quantity 2 = 3)
    payload = {
        "items": [
            {"product_id": str(pen["id"]), "quantity": 1},
            {"product_id": str(pen["id"]), "quantity": 2},
        ]
    }
    headers = {**user["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"}

    response = client.post("/api/orders", json=payload, headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["total_paise"] == 3 * 2000  # 6000 paise

    # Verify inventory was decremented by 3
    assert get_stock(pen["id"]) == 5 - 3  # 2 pens remaining

    # Verify line items in the database contain exactly 1 merged record
    items = get_order_items(body["id"])
    assert len(items) == 1
    assert items[0]["quantity"] == 3


# Tests concurrent race condition where two different users order the last remaining item in stock simultaneously.
# Verifies that exactly one user receives 201 Created and the other receives a clean 409 Conflict, leaving stock at 0.
def test_concurrent_race_two_users_last_item_in_stock(products, user_tokens, get_stock):
    from tests.conftest import test_db_url
    pen = products["pen"]
    user1 = user_tokens["user1"]
    user2 = user_tokens["user2"]

    # Explicitly set pen stock to exactly 1 in the test database
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE products SET stock = 1 WHERE id = %s;", (pen["id"],))

    assert get_stock(pen["id"]) == 1

    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}
    headers1 = {**user1["headers"], "Idempotency-Key": f"race-u1-{uuid.uuid4().hex[:10]}"}
    headers2 = {**user2["headers"], "Idempotency-Key": f"race-u2-{uuid.uuid4().hex[:10]}"}

    barrier = threading.Barrier(2)

    def order_worker(headers):
        thread_client = TestClient(app)
        try:
            barrier.wait()
            response = thread_client.post("/api/orders", json=payload, headers=headers)
            return response.status_code, response.json()
        finally:
            thread_client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(order_worker, headers1)
        f2 = executor.submit(order_worker, headers2)
        status1, body1 = f1.result()
        status2, body2 = f2.result()

    statuses = {status1, status2}
    assert statuses == {201, 409}, f"Expected one 201 and one 409, but received {status1} and {status2}"

    # Verify the 409 response is a clean HTTP 409 rather than a 500 or DB exception
    res_409_body = body1 if status1 == 409 else body2
    assert "insufficient stock" in res_409_body.get("detail", "").lower()

    # Verify inventory was decremented to exactly 0
    assert get_stock(pen["id"]) == 0

    # Verify exactly one order row was committed in the database
    with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM orders;")
            assert cur.fetchone()["count"] == 1

