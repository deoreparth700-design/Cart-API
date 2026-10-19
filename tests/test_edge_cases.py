# Mock and unit tests for specific edge cases: static HTML endpoint, malformed JSON bodies,
# unexpected database rollbacks, and non-idempotency unique constraint violation re-raises.

from contextlib import contextmanager
import hashlib
import hmac
import json
from types import SimpleNamespace
import uuid
import pytest
from psycopg.errors import UniqueViolation

from app.core.config import settings
from app.schemas.order import OrderCreate
from app.services import order_service, webhook_service

DUMMY_SECRET = "dummy_secret_for_mock_tests"


# Generates a dummy payment.captured webhook payload dictionary for unit tests.
def make_payment_captured_payload(rzp_order_id: str, rzp_payment_id: str = "pay_test_123", amount: int = 15000) -> dict:
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": rzp_payment_id,
                    "amount": amount,
                    "order_id": rzp_order_id,
                }
            }
        },
    }


# Computes an HMAC-SHA256 signature for unit test payloads using a dummy test secret.
def compute_mock_signature(body_bytes: bytes, secret: str = DUMMY_SECRET) -> str:
    return hmac.new(key=secret.encode("utf-8"), msg=body_bytes, digestmod=hashlib.sha256).hexdigest()


# Mock UniqueViolation exception with simulated PostgreSQL diagnostic constraint names.
class FakeUniqueViolation(UniqueViolation):
    def __init__(self, constraint_name: str):
        super().__init__("duplicate key value violates unique constraint")
        self._test_constraint_name = constraint_name

    @property
    def diag(self):
        return SimpleNamespace(constraint_name=self._test_constraint_name)


# Context manager simulating a database transaction without opening a network connection.
@contextmanager
def mock_transaction():
    yield object()


# Tests that the static checkout demo page is served correctly with Razorpay Checkout JS script included.
def test_checkout_test_html_static_endpoint(client):
    response = client.get("/static/checkout_test.html")
    assert response.status_code == 200
    assert "https://checkout.razorpay.com/v1/checkout.js" in response.text
    assert "Razorpay Checkout Test" in response.text


# Tests that an unexpected UniqueViolation with an unrelated constraint name is re-raised rather than caught as idempotency.
def test_unrelated_unique_violation_is_reraised(monkeypatch):
    lookup_calls = 0

    def mock_lookup(conn, user_id, key):
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    def mock_decrement(conn, product_id, quantity):
        return 2000

    def mock_failing_create(*args, **kwargs):
        raise FakeUniqueViolation("orders_pkey")

    monkeypatch.setattr(order_service, "transaction", mock_transaction)
    monkeypatch.setattr(order_service.order_repo, "get_order_by_idempotency_key", mock_lookup)
    monkeypatch.setattr(order_service.product_repo, "decrement_product_stock", mock_decrement)
    monkeypatch.setattr(order_service.order_repo, "create_order", mock_failing_create)

    request = OrderCreate(
        items=[{"product_id": uuid.uuid4(), "quantity": 1}]
    )

    with pytest.raises(FakeUniqueViolation):
        order_service.create_order(1, request, "unrelated-conflict-test")

    # Unrelated conflicts are not treated as idempotency hits
    assert lookup_calls == 1


# Tests that catastrophic database failure during webhook order update triggers transaction rollback.
def test_database_failure_rolls_back_atomically(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_SECRET)

    payload_bytes = json.dumps(
        make_payment_captured_payload("order_rollback_test", amount=15000)
    ).encode("utf-8")
    sig = compute_mock_signature(payload_bytes, DUMMY_SECRET)

    rolled_back = False

    class FakeConn:
        pass

    @contextmanager
    def failing_tx():
        nonlocal rolled_back
        try:
            yield FakeConn()
        except Exception:
            rolled_back = True
            raise

    monkeypatch.setattr(webhook_service, "transaction", failing_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": uuid.uuid4(), "status": "created", "total_paise": 15000},
    )

    def failing_mark_paid(conn, r_id, p_id):
        raise RuntimeError("Simulated catastrophic database failure")

    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", failing_mark_paid)

    with pytest.raises(RuntimeError, match="Simulated catastrophic database failure"):
        client.post(
            "/api/webhooks/razorpay",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": sig,
                "X-Razorpay-Event-Id": "evt_rollback_1",
            },
        )

    assert rolled_back is True, "Database failure must trigger transaction rollback"


# Tests that missing event IDs and unparseable malformed non-JSON payloads are rejected with HTTP 400.
def test_missing_event_id_and_malformed_payload(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_SECRET)

    # 1. Missing event ID header and body field
    payload_no_eid = json.dumps(make_payment_captured_payload("order_123")).encode("utf-8")
    sig1 = compute_mock_signature(payload_no_eid, DUMMY_SECRET)

    res1 = client.post(
        "/api/webhooks/razorpay",
        content=payload_no_eid,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig1,
        },
    )
    assert res1.status_code == 400
    assert "event id" in res1.json()["detail"].lower()

    # 2. Malformed non-JSON raw body
    malformed_body = b"not a json string at all {{"
    sig2 = compute_mock_signature(malformed_body, DUMMY_SECRET)

    res2 = client.post(
        "/api/webhooks/razorpay",
        content=malformed_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig2,
            "X-Razorpay-Event-Id": "evt_malformed",
        },
    )
    assert res2.status_code == 400
    assert "malformed" in res2.json()["detail"].lower()
