"""Stripe webhook payload building and delivery for the simulator.

Payloads are deliberately minimal — the v1 event envelope plus exactly
the fields the processor reads. The simulator test-suite round-trips
every payload through stripe-python's typed classes so SDK upgrades
flag drift in CI (SPEC §10).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

from getpaid_stripe.simulator.signing import sign_webhook


if TYPE_CHECKING:
    from getpaid_simulator.core.webhooks import WebhookTransport


def _as_int(value: Any) -> int:
    """Undo SimulatorStorage's stringification of integer amounts."""
    return int(value or 0)


def _session_status(order: dict[str, Any]) -> str:
    status = str(order.get("status", "open"))
    if status in ("open", "declined"):
        return "open"
    if status == "expired":
        return "expired"
    return "complete"


def _payment_intent_status(order: dict[str, Any]) -> str:
    status = str(order.get("status", "open"))
    if status in ("open", "declined"):
        return "requires_payment_method"
    return status


def session_object(order: dict[str, Any]) -> dict[str, Any]:
    has_intent = bool(order.get("has_payment_intent"))
    status = _session_status(order)
    return {
        "id": order["session_id"],
        "object": "checkout.session",
        "mode": "payment",
        "status": status,
        "payment_status": (
            "paid" if order.get("status") == "succeeded" else "unpaid"
        ),
        "payment_intent": order["pi_id"] if has_intent else None,
        "client_reference_id": order.get("client_reference_id"),
        "metadata": dict(order.get("metadata") or {}),
        "currency": order.get("currency"),
        "amount_total": _as_int(order.get("amount")),
    }


def payment_intent_object(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order["pi_id"],
        "object": "payment_intent",
        "status": _payment_intent_status(order),
        "amount": _as_int(order.get("amount")),
        "amount_received": _as_int(order.get("amount_received")),
        "amount_capturable": _as_int(order.get("amount_capturable")),
        "currency": order.get("currency"),
        "capture_method": order.get("capture_method", "automatic"),
        "metadata": dict(order.get("metadata") or {}),
        "cancellation_reason": order.get("cancellation_reason"),
    }


def refund_object(
    refund: dict[str, Any], order: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": f"re_sim_{refund['id']}",
        "object": "refund",
        "status": refund.get("status", "pending"),
        "amount": _as_int(refund.get("amount")),
        "currency": order.get("currency"),
        "payment_intent": order["pi_id"],
        "reason": refund.get("reason"),
        "metadata": dict(refund.get("metadata") or {}),
    }


def charge_object(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order["charge_id"],
        "object": "charge",
        "amount": _as_int(order.get("amount")),
        "currency": order.get("currency"),
        "payment_intent": order["pi_id"],
        "metadata": dict(order.get("metadata") or {}),
    }


def review_object(
    order: dict[str, Any],
    *,
    is_open: bool,
    closed_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"prv_sim_{order['id']}",
        "object": "review",
        "payment_intent": order["pi_id"],
        "open": is_open,
        "reason": "rule" if is_open else closed_reason,
        "closed_reason": closed_reason,
    }


def build_event(
    event_type: str,
    obj: dict[str, Any],
    *,
    created: int | None = None,
) -> dict[str, Any]:
    return {
        "id": f"evt_sim_{uuid4().hex[:24]}",
        "object": "event",
        "api_version": "2025-06-30",
        "created": created if created is not None else int(time.time()),
        "type": event_type,
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "data": {"object": obj},
    }


async def deliver_events(
    events: list[tuple[str, dict[str, Any]]],
    provider_config: dict[str, Any],
    transport: WebhookTransport,
) -> list[bool | None]:
    """Sign and deliver a batch of ``(event_type, object)`` pairs."""
    notify_url = provider_config.get("notify_url")
    results: list[bool | None] = []
    for event_type, obj in events:
        if not notify_url:
            results.append(None)
            continue
        payload = build_event(event_type, obj)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **sign_webhook(
                body, str(provider_config["webhook_secret"])
            ),
        }
        results.append(
            await transport.deliver(
                url=str(notify_url), body=body, headers=headers
            )
        )
    return results
