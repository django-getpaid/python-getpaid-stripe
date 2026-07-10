"""Stripe webhook signing for the simulator plugin.

Computes a genuine ``Stripe-Signature`` header —
``t=<ts>,v1=HMAC-SHA256("<ts>.<payload>", secret)`` — so the processor
under test runs its real ``stripe.Webhook.construct_event`` path.
"""

import hashlib
import hmac
import time


def compute_signature(payload: str, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.{payload}"
    return hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def stripe_signature_header(
    payload: bytes | str,
    secret: str,
    timestamp: int | None = None,
) -> str:
    """Build the value of a ``Stripe-Signature`` header."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if timestamp is None:
        timestamp = int(time.time())
    signature = compute_signature(payload, secret, timestamp)
    return f"t={timestamp},v1={signature}"


def sign_webhook(
    body: bytes,
    secret: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Return the webhook delivery headers for a payload."""
    return {
        "Stripe-Signature": stripe_signature_header(
            body, secret, timestamp
        ),
    }
