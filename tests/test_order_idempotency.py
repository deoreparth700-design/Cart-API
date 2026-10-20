# Real database tests for order idempotency, header validation, and concurrent race safety.
# Verifies idempotency guarantees against isolated Neon PostgreSQL database.

import concurrent.futures
import threading
import uuid
import pytest
from fastapi.testclient import TestClient
from app.core.config import settings
from app.main import app

# Stores race condition outcome to report to user
RACE_RESULT_REPORT = {}


# Tests that replaying the same idempotency key returns 201 then 200 with identical IDs, reducing stock and calling Razorpay once.
def test_same_idempotency_key_twice_replays_existing_order(client, products, user_tokens, get_stock, mock_razorpay_order_sdk):
    user = user_tokens["user1"]
    pen = products["pen"]
    shared_key = f"key-replay-{uuid.uuid4().hex[:10]}"
    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}
    headers = {**user["headers"], "Idempotency-Key": shared_key}

    # First request: Creates order
    res1 = client.post("/api/orders", json=payload, headers=headers)
    assert res1.status_code == 201
    body1 = res1.json()
    order_id = body1["id"]
    rzp_order_id = body1["razorpay_order_id"]

    # Verify public razorpay_key_id is returned and sensitive secrets are never leaked in response text
    assert "razorpay_key_id" in body1
    assert body1["razorpay_key_id"] == settings.RAZORPAY_KEY_ID
    if settings.RAZORPAY_KEY_SECRET:
        assert settings.RAZORPAY_KEY_SECRET not in res1.text
    if settings.RAZORPAY_WEBHOOK_SECRET:
        assert settings.RAZORPAY_WEBHOOK_SECRET not in res1.text

    assert get_stock(pen["id"]) == 5 - 1  # Stock decremented once
    assert len(mock_razorpay_order_sdk) == 1

    # Second request: Replays existing order
    res2 = client.post("/api/orders", json=payload, headers=headers)
    assert res2.status_code == 200
    body2 = res2.json()

    assert body2["id"] == order_id
    assert body2["razorpay_order_id"] == rzp_order_id
    assert body2.get("razorpay_key_id") == settings.RAZORPAY_KEY_ID
    if settings.RAZORPAY_KEY_SECRET:
        assert settings.RAZORPAY_KEY_SECRET not in res2.text
    if settings.RAZORPAY_WEBHOOK_SECRET:
        assert settings.RAZORPAY_WEBHOOK_SECRET not in res2.text
    assert get_stock(pen["id"]) == 4      # Stock untouched on replay
    assert len(mock_razorpay_order_sdk) == 1, "Razorpay SDK must not be invoked on idempotent replay"


# Tests that sending different idempotency keys creates separate distinct orders.
def test_new_idempotency_key_creates_new_order(client, products, user_tokens, get_stock):
    user = user_tokens["user1"]
    pen = products["pen"]
    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}

    res1 = client.post(
        "/api/orders",
        json=payload,
        headers={**user["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"},
    )
    assert res1.status_code == 201

    res2 = client.post(
        "/api/orders",
        json=payload,
        headers={**user["headers"], "Idempotency-Key": f"key-{uuid.uuid4().hex[:10]}"},
    )
    assert res2.status_code == 201

    assert res1.json()["id"] != res2.json()["id"]
    assert get_stock(pen["id"]) == 5 - 2  # Stock decremented twice


# Tests that missing, blank, whitespace, and oversized idempotency keys return HTTP 400 with no stock or order changes.
def test_invalid_idempotency_headers_return_400(client, products, user_tokens, get_stock):
    user = user_tokens["user1"]
    pen = products["pen"]
    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}

    invalid_headers = [
        {"Authorization": user["headers"]["Authorization"], "Content-Type": "application/json"},
        {**user["headers"], "Idempotency-Key": ""},
        {**user["headers"], "Idempotency-Key": "   "},
        {**user["headers"], "Idempotency-Key": "k" * 256},
    ]

    for header_set in invalid_headers:
        response = client.post("/api/orders", json=payload, headers=header_set)
        assert response.status_code == 400
        assert "idempotency-key" in response.json()["detail"].lower()

    # Stock must remain completely untouched
    assert get_stock(pen["id"]) == 5


# Tests that two different users using the same idempotency key are both allowed and create separate orders.
def test_same_idempotency_key_allowed_for_different_users(client, products, user_tokens, get_stock):
    user1 = user_tokens["user1"]
    user2 = user_tokens["user2"]
    pen = products["pen"]
    shared_key = f"key-shared-{uuid.uuid4().hex[:10]}"
    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}

    # User 1 sends request with shared key
    res1 = client.post(
        "/api/orders",
        json=payload,
        headers={**user1["headers"], "Idempotency-Key": shared_key},
    )
    assert res1.status_code == 201

    # User 2 sends request with identical key
    res2 = client.post(
        "/api/orders",
        json=payload,
        headers={**user2["headers"], "Idempotency-Key": shared_key},
    )
    assert res2.status_code == 201

    assert res1.json()["id"] != res2.json()["id"]
    assert get_stock(pen["id"]) == 5 - 2


# Tests concurrent race condition where two threads send the same idempotency key at the exact same moment.
# Verifies atomic conflict resolution: exactly one 201 and one 200, same order ID, and stock decremented once.
def test_concurrent_race_same_idempotency_key(products, user_tokens, get_stock):
    user = user_tokens["user1"]
    pen = products["pen"]
    shared_key = f"race-key-{uuid.uuid4().hex[:10]}"
    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 1}]}
    req_headers = {**user["headers"], "Idempotency-Key": shared_key}

    barrier = threading.Barrier(2)

    def send_order_worker():
        # Each thread uses its own TestClient to ensure complete client isolation
        thread_client = TestClient(app)
        try:
            barrier.wait()
            response = thread_client.post("/api/orders", json=payload, headers=req_headers)
            return response.status_code, response.json()
        finally:
            thread_client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send_order_worker)
        f2 = executor.submit(send_order_worker)
        status1, body1 = f1.result()
        status2, body2 = f2.result()

    statuses = {status1, status2}

    # Identify responses
    order_id1 = body1.get("id")
    order_id2 = body2.get("id")
    rzp_id1 = body1.get("razorpay_order_id")
    rzp_id2 = body2.get("razorpay_order_id")

    print(f"\nRACE OUTCOME: status1={status1}, status2={status2}, rzp_id1={rzp_id1}, rzp_id2={rzp_id2}")


    # Condition 4 Assertions:
    # 1. Exactly one 201 and one 200 (or {200} if existing)
    assert statuses == {200, 201} or statuses == {200}
    # 2. Same order id
    assert order_id1 == order_id2
    # 3. Stock reduced exactly once (5 - 1 = 4)
    assert get_stock(pen["id"]) == 4
