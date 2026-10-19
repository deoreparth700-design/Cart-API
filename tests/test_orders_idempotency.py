import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_current_user
from app.main import app
from app.services import order_service
from app.schemas.order import OrderCreate


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


def make_order():
    product_id = uuid.uuid4()
    return {
        "id": uuid.uuid4(),
        "user_id": TEST_USER_ID,
        "status": "created",
        "total_paise": 100,
        "items": [
            {
                "id": uuid.uuid4(),
                "product_id": product_id,
                "quantity": 1,
                "unit_price_paise": 100,
            }
        ],
    }


def test_invalid_idempotency_keys_return_400(client, monkeypatch):
    def unexpected_service_call(*args, **kwargs):
        pytest.fail("Order service must not run for an invalid key")

    monkeypatch.setattr(
        order_service, "create_order", unexpected_service_call
    )

    test_cases = [
        {},                             # Missing header
        {"Idempotency-Key": ""},        # Empty header
        {"Idempotency-Key": "   "},     # Whitespace only
        {"Idempotency-Key": "a" * 256}, # Too long
    ]

    for headers in test_cases:
        response = client.post(
            "/api/orders",
            json={
                "items": [
                    {
                        "product_id": str(uuid.uuid4()),
                        "quantity": 1,
                    }
                ]
            },
            headers=headers,
        )
        assert response.status_code == 400, (
            f"Expected 400 for headers {headers!r}; "
            f"got {response.status_code}: {response.text}"
        )


def test_new_order_returns_201(client, monkeypatch):
    order = make_order()

    monkeypatch.setattr(
        order_service,
        "create_order",
        lambda user_id, order_data, idempotency_key: (order, True),
    )

    response = client.post(
        "/api/orders",
        json={
            "items": [
                {
                    "product_id": str(order["items"][0]["product_id"]),
                    "quantity": 1,
                }
            ]
        },
        headers={"Idempotency-Key": "automated-test-new"},
    )

    assert response.status_code == 201
    assert response.json()["id"] == str(order["id"])


def test_duplicate_order_returns_200(client, monkeypatch):
    order = make_order()

    monkeypatch.setattr(
        order_service,
        "create_order",
        lambda user_id, order_data, idempotency_key: (order, False),
    )

    response = client.post(
        "/api/orders",
        json={
            "items": [
                {
                    "product_id": str(order["items"][0]["product_id"]),
                    "quantity": 1,
                }
            ]
        },
        headers={"Idempotency-Key": "automated-test-duplicate"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(order["id"])


def test_existing_key_bypasses_stock_decrement(monkeypatch):
    existing_order = make_order()

    @contextmanager
    def fake_transaction():
        # Supply a placeholder connection; no database is opened.
        yield object()

    monkeypatch.setattr(
        order_service, "transaction", fake_transaction
    )
    monkeypatch.setattr(
        order_service.order_repo,
        "get_order_by_idempotency_key",
        lambda conn, user_id, key: existing_order,
    )

    def unexpected_stock_decrement(*args, **kwargs):
        pytest.fail("Duplicate request must not decrement stock")

    monkeypatch.setattr(
        order_service.product_repo,
        "decrement_product_stock",
        unexpected_stock_decrement,
    )

    request = OrderCreate(
        items=[
            {
                "product_id": uuid.uuid4(),
                "quantity": 1,
            }
        ]
    )

    result, is_created = order_service.create_order(
        TEST_USER_ID, request, "existing-test-key"
    )

    assert result == existing_order
    assert is_created is False


from contextlib import contextmanager
from types import SimpleNamespace

from psycopg.errors import UniqueViolation


class FakeUniqueViolation(UniqueViolation):
    def __init__(self, constraint_name):
        super().__init__("duplicate key value violates unique constraint")
        self._test_constraint_name = constraint_name

    @property
    def diag(self):
        return SimpleNamespace(
            constraint_name=self._test_constraint_name
        )


@contextmanager
def fake_transaction():
    # Never connects to the actual database.
    yield object()


def test_unique_idempotency_conflict_returns_existing_order(monkeypatch):
    existing_order = make_order()
    lookup_calls = 0
    decrement_calls = 0

    def lookup_order(conn, user_id, key):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return existing_order

    def decrement_stock(conn, product_id, quantity):
        nonlocal decrement_calls
        decrement_calls += 1
        return 100

    def duplicate_insert(*args, **kwargs):
        raise FakeUniqueViolation("unique_user_idempotency")

    monkeypatch.setattr(order_service, "transaction", fake_transaction)
    monkeypatch.setattr(
        order_service.order_repo,
        "get_order_by_idempotency_key",
        lookup_order,
    )
    monkeypatch.setattr(
        order_service.product_repo,
        "decrement_product_stock",
        decrement_stock,
    )
    monkeypatch.setattr(
        order_service.order_repo, "create_order", duplicate_insert
    )

    request = OrderCreate(
        items=[
            {
                "product_id": uuid.uuid4(),
                "quantity": 1,
            }
        ]
    )

    result, is_created = order_service.create_order(
        TEST_USER_ID, request, "race-recovery-test"
    )

    assert result == existing_order
    assert is_created is False
    assert lookup_calls == 2
    assert decrement_calls == 1


def test_unrelated_unique_violation_is_reraised(monkeypatch):
    lookup_calls = 0

    def lookup_order(conn, user_id, key):
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    def decrement_stock(conn, product_id, quantity):
        return 100

    def duplicate_insert(*args, **kwargs):
        raise FakeUniqueViolation("orders_pkey")

    monkeypatch.setattr(order_service, "transaction", fake_transaction)
    monkeypatch.setattr(
        order_service.order_repo,
        "get_order_by_idempotency_key",
        lookup_order,
    )
    monkeypatch.setattr(
        order_service.product_repo,
        "decrement_product_stock",
        decrement_stock,
    )
    monkeypatch.setattr(
        order_service.order_repo, "create_order", duplicate_insert
    )

    request = OrderCreate(
        items=[
            {
                "product_id": uuid.uuid4(),
                "quantity": 1,
            }
        ]
    )

    with pytest.raises(FakeUniqueViolation):
        order_service.create_order(
            TEST_USER_ID, request, "unrelated-conflict-test"
        )

    # Only the initial lookup occurs; unrelated conflicts aren't treated
    # as successful idempotency replays.
    assert lookup_calls == 1
