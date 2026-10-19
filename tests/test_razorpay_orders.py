import uuid
from contextlib import contextmanager
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import get_current_user
from app.main import app
from app.schemas.order import OrderCreate, OrderItemCreate
from app.services import order_service, razorpay_service


TEST_USER_ID = 8888


@pytest.fixture
def client():
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


def test_checkout_test_html_static_endpoint(client):
    response = client.get("/static/checkout_test.html")
    assert response.status_code == 200
    assert "https://checkout.razorpay.com/v1/checkout.js" in response.text
    assert "Razorpay Checkout Test" in response.text


def test_successful_order_creates_razorpay_order_and_includes_public_key(client, monkeypatch):
    mock_order_id = uuid.uuid4()
    mock_item_id = uuid.uuid4()
    mock_product_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_test_12345"

    created_order = {
        "id": mock_order_id,
        "user_id": TEST_USER_ID,
        "status": "created",
        "total_paise": 15000,
        "razorpay_order_id": None,
        "items": [
            {
                "id": mock_item_id,
                "product_id": mock_product_id,
                "quantity": 1,
                "unit_price_paise": 15000,
            }
        ],
    }

    @contextmanager
    def fake_transaction():
        yield object()

    # Pre-check returns None (new order)
    monkeypatch.setattr(order_service, "transaction", fake_transaction)
    monkeypatch.setattr(order_service.order_repo, "get_order_by_idempotency_key", lambda conn, u, k: None)
    monkeypatch.setattr(order_service.product_repo, "decrement_product_stock", lambda conn, p, q: 15000)
    monkeypatch.setattr(order_service.order_repo, "create_order", lambda conn, u, t, k: created_order)
    monkeypatch.setattr(order_service.order_repo, "create_order_items", lambda conn, o, items: created_order["items"])

    rzp_calls = []
    def mock_create_rzp(total_paise, receipt):
        rzp_calls.append({"total_paise": total_paise, "receipt": receipt})
        return mock_rzp_order_id

    saved_rzp_ids = []
    def mock_set_rzp_id(conn, order_id, rzp_id):
        saved_rzp_ids.append((order_id, rzp_id))

    monkeypatch.setattr(razorpay_service, "create_razorpay_order", mock_create_rzp)
    monkeypatch.setattr(order_service.order_repo, "set_razorpay_order_id", mock_set_rzp_id)

    response = client.post(
        "/api/orders",
        json={"items": [{"product_id": str(mock_product_id), "quantity": 1}]},
        headers={"Idempotency-Key": "test-rzp-create-1"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == str(mock_order_id)
    assert body["status"] == "created"
    assert body["total_paise"] == 15000
    assert body["razorpay_order_id"] == mock_rzp_order_id
    assert body["razorpay_key_id"] == settings.RAZORPAY_KEY_ID

    # Security check: verify secrets are never returned
    assert "RAZORPAY_KEY_SECRET" not in body
    assert "key_secret" not in body

    # Verify Razorpay was called with our order ID as receipt
    assert len(rzp_calls) == 1
    assert rzp_calls[0]["total_paise"] == 15000
    assert rzp_calls[0]["receipt"] == str(mock_order_id)
    assert saved_rzp_ids == [(mock_order_id, mock_rzp_order_id)]


def test_razorpay_failure_marks_order_failed_restores_stock_and_returns_502(client, monkeypatch):
    mock_order_id = uuid.uuid4()
    mock_product_id = uuid.uuid4()

    created_order = {
        "id": mock_order_id,
        "user_id": TEST_USER_ID,
        "status": "created",
        "total_paise": 15000,
        "razorpay_order_id": None,
        "items": [],
    }

    @contextmanager
    def fake_transaction():
        yield object()

    monkeypatch.setattr(order_service, "transaction", fake_transaction)
    monkeypatch.setattr(order_service.order_repo, "get_order_by_idempotency_key", lambda conn, u, k: None)
    monkeypatch.setattr(order_service.product_repo, "decrement_product_stock", lambda conn, p, q: 15000)
    monkeypatch.setattr(order_service.order_repo, "create_order", lambda conn, u, t, k: created_order)
    monkeypatch.setattr(order_service.order_repo, "create_order_items", lambda conn, o, items: [])

    # Simulate Razorpay Gateway Failure
    def failing_rzp_order(total_paise, receipt):
        raise RuntimeError("Gateway connection timeout")

    failed_order_ids = []
    def mock_mark_failed(conn, order_id):
        failed_order_ids.append(order_id)

    restored_stock_items = []
    def mock_increment_stock(conn, product_id, quantity):
        restored_stock_items.append((product_id, quantity))

    monkeypatch.setattr(razorpay_service, "create_razorpay_order", failing_rzp_order)
    monkeypatch.setattr(order_service.order_repo, "mark_order_failed", mock_mark_failed)
    monkeypatch.setattr(order_service.product_repo, "increment_product_stock", mock_increment_stock)

    response = client.post(
        "/api/orders",
        json={"items": [{"product_id": str(mock_product_id), "quantity": 2}]},
        headers={"Idempotency-Key": "test-rzp-fail-1"},
    )

    assert response.status_code == 502
    assert "Failed to create Razorpay payment order" in response.json()["detail"]

    # Verify order was marked failed and stock restored
    assert failed_order_ids == [mock_order_id]
    assert restored_stock_items == [(mock_product_id, 2)]


def test_idempotent_replay_returns_same_razorpay_order_without_second_call(client, monkeypatch):
    mock_order_id = uuid.uuid4()
    mock_product_id = uuid.uuid4()
    existing_rzp_order_id = "order_existing_9999"

    existing_order = {
        "id": mock_order_id,
        "user_id": TEST_USER_ID,
        "status": "created",
        "total_paise": 5000,
        "razorpay_order_id": existing_rzp_order_id,
        "items": [
            {
                "id": uuid.uuid4(),
                "product_id": mock_product_id,
                "quantity": 1,
                "unit_price_paise": 5000,
            }
        ],
    }

    @contextmanager
    def fake_transaction():
        yield object()

    monkeypatch.setattr(order_service, "transaction", fake_transaction)
    monkeypatch.setattr(order_service.order_repo, "get_order_by_idempotency_key", lambda conn, u, k: existing_order)

    def unexpected_rzp_call(*args, **kwargs):
        pytest.fail("Idempotent replay must NEVER create a second Razorpay order")

    monkeypatch.setattr(razorpay_service, "create_razorpay_order", unexpected_rzp_call)

    response = client.post(
        "/api/orders",
        json={"items": [{"product_id": str(mock_product_id), "quantity": 1}]},
        headers={"Idempotency-Key": "test-rzp-replay-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(mock_order_id)
    assert body["razorpay_order_id"] == existing_rzp_order_id
    assert body["razorpay_key_id"] == settings.RAZORPAY_KEY_ID
