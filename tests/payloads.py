"""Hand-written minimal Stripe webhook payloads for tests.

Deliberately independent of the simulator's payload builders: these
encode the spec's expectations (envelope + exactly the fields the
processor reads), so a simulator bug cannot silently self-validate.
"""

from typing import Any


PAYMENT_ID = "test-payment-123"


def event(
    event_type: str,
    obj: dict[str, Any],
    event_id: str = "evt_test_0001",
    created: int = 1767000000,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "api_version": "2025-06-30",
        "created": created,
        "type": event_type,
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "data": {"object": obj},
    }


def checkout_session(
    session_id: str = "cs_test_a1b2c3",
    payment_intent: str | None = "pi_test_0001",
    status: str = "complete",
    payment_status: str = "paid",
    payment_id: str = PAYMENT_ID,
    **overrides: Any,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": session_id,
        "object": "checkout.session",
        "mode": "payment",
        "client_reference_id": payment_id,
        "metadata": {"payment_id": payment_id},
        "payment_intent": payment_intent,
        "status": status,
        "payment_status": payment_status,
        "currency": "pln",
        "amount_total": 10000,
    }
    obj.update(overrides)
    return obj


def payment_intent(
    intent_id: str = "pi_test_0001",
    status: str = "succeeded",
    amount: int = 10000,
    currency: str = "pln",
    amount_received: int = 0,
    amount_capturable: int = 0,
    capture_method: str = "automatic",
    payment_id: str = PAYMENT_ID,
    cancellation_reason: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": intent_id,
        "object": "payment_intent",
        "amount": amount,
        "amount_received": amount_received,
        "amount_capturable": amount_capturable,
        "currency": currency,
        "capture_method": capture_method,
        "status": status,
        "metadata": {"payment_id": payment_id},
        "cancellation_reason": cancellation_reason,
    }
    obj.update(overrides)
    return obj


def refund(
    refund_id: str = "re_test_0001",
    status: str = "succeeded",
    amount: int = 10000,
    currency: str = "pln",
    payment_intent: str = "pi_test_0001",
    payment_id: str = PAYMENT_ID,
    reason: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": refund_id,
        "object": "refund",
        "amount": amount,
        "currency": currency,
        "payment_intent": payment_intent,
        "status": status,
        "reason": reason,
        "metadata": {"payment_id": payment_id},
    }
    obj.update(overrides)
    return obj


def review(
    review_id: str = "prv_test_0001",
    payment_intent: str = "pi_test_0001",
    is_open: bool = True,
    closed_reason: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": review_id,
        "object": "review",
        "payment_intent": payment_intent,
        "open": is_open,
        "reason": "rule" if is_open else closed_reason,
        "closed_reason": closed_reason,
    }
    obj.update(overrides)
    return obj
