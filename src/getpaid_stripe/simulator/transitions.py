"""Stripe simulator state transitions.

One merged lifecycle per order, following the real object lifecycles
(SPEC §10): the Checkout Session is ``open`` until paid or expired;
the PaymentIntent takes over from there.

- ``open``: session open, no successful payment attempt yet
- ``declined``: attempt failed; session stays open for retry
- ``processing``: delayed payment method submitted, funds not cleared
- ``requires_capture``: manual-capture authorization placed (the lock)
- ``succeeded`` / ``canceled`` / ``expired``: terminal
"""

STRIPE_TRANSITIONS: dict[str, set[str]] = {
    "open": {
        "declined",
        "processing",
        "requires_capture",
        "succeeded",
        "expired",
    },
    "declined": {
        "processing",
        "requires_capture",
        "succeeded",
        "expired",
    },
    "processing": {"succeeded", "canceled"},
    "requires_capture": {"succeeded", "canceled"},
    "succeeded": set(),
    "canceled": set(),
    "expired": set(),
}

#: Refund lifecycle (tracked on refund records, not the state machine).
REFUND_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"succeeded", "failed", "canceled"},
    "requires_action": {"succeeded", "canceled"},
    # Late failure after success is real Stripe behavior.
    "succeeded": {"failed"},
    "failed": set(),
    "canceled": set(),
}
