"""Stripe simulator routes: API handlers, fake checkout UI, ops."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qsl

from litestar import Request
from litestar import get
from litestar import post
from litestar.enums import MediaType
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.exceptions import NotFoundException
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Response

from getpaid_stripe.simulator.transitions import REFUND_TRANSITIONS
from getpaid_stripe.simulator.webhooks import CHARGE_PREFIX
from getpaid_stripe.simulator.webhooks import INTENT_PREFIX
from getpaid_stripe.simulator.webhooks import REFUND_PREFIX
from getpaid_stripe.simulator.webhooks import SESSION_PREFIX
from getpaid_stripe.simulator.webhooks import charge_object
from getpaid_stripe.simulator.webhooks import deliver_events
from getpaid_stripe.simulator.webhooks import payment_intent_object
from getpaid_stripe.simulator.webhooks import refund_object
from getpaid_stripe.simulator.webhooks import review_object
from getpaid_stripe.simulator.webhooks import session_object


logger = logging.getLogger(__name__)
URL_ENCODED_BODY = Body(media_type=RequestEncodingType.URL_ENCODED)


def _provider_config(request: Request[Any, Any, Any]) -> dict[str, Any]:
    return dict(request.app.state.provider_configs["stripe"])


def _stripe_error(
    status_code: int, message: str, code: str | None = None
) -> Response[object]:
    error: dict[str, Any] = {
        "type": "invalid_request_error",
        "message": message,
    }
    if code:
        error["code"] = code
    return Response(
        content={"error": error},
        status_code=status_code,
        media_type=MediaType.JSON,
    )


def _json(content: object, status_code: int = 200) -> Response[object]:
    return Response(
        content=content, status_code=status_code, media_type=MediaType.JSON
    )


async def _form(request: Request[Any, Any, Any]) -> dict[str, str]:
    """Parse Stripe's url-encoded bracket-notation request body flat."""
    body = await request.body()
    return dict(parse_qsl(body.decode("utf-8")))


def _get_order(
    request: Request[Any, Any, Any], order_id: str
) -> dict[str, Any] | None:
    order = request.app.state.storage.get_order(order_id)
    if order is None or order.get("provider") != "stripe":
        return None
    return order


def _order_by_prefixed_id(
    request: Request[Any, Any, Any], prefixed_id: str, prefix: str
) -> dict[str, Any] | None:
    if not prefixed_id.startswith(prefix):
        return None
    return _get_order(request, prefixed_id.removeprefix(prefix))


async def _emit(
    request: Request[Any, Any, Any],
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    await deliver_events(
        events,
        _provider_config(request),
        request.app.state.webhook_transport,
    )


def _transition(
    request: Request[Any, Any, Any], order_id: str, new_status: str
) -> dict[str, Any]:
    return request.app.state.state_machine.transition(order_id, new_status)


# --- Stripe API surface -------------------------------------------------


@post("/v1/checkout/sessions")
async def create_checkout_session(
    request: Request[Any, Any, Any],
) -> Response[object]:
    form = await _form(request)
    required = (
        "mode",
        "success_url",
        "cancel_url",
        "line_items[0][price_data][currency]",
        "line_items[0][price_data][unit_amount]",
    )
    for field in required:
        if field not in form:
            return _stripe_error(
                400, f"Missing required param: {field}.",
                code="parameter_missing",
            )
    if form["mode"] != "payment":
        return _stripe_error(
            400, "The stripe simulator supports mode=payment only."
        )

    metadata = {
        key.removeprefix("metadata[").removesuffix("]"): value
        for key, value in form.items()
        if key.startswith("metadata[")
    }
    order_data: dict[str, Any] = {
        "provider": "stripe",
        "status": "open",
        "amount": int(form["line_items[0][price_data][unit_amount]"]),
        "currency": form["line_items[0][price_data][currency]"],
        "description": form.get(
            "line_items[0][price_data][product_data][name]", ""
        ),
        "metadata": metadata,
        "client_reference_id": form.get("client_reference_id"),
        "capture_method": form.get(
            "payment_intent_data[capture_method]", "automatic"
        ),
        "success_url": form["success_url"],
        "cancel_url": form["cancel_url"],
        "expires_at": form.get("expires_at"),
        "amount_received": 0,
        "amount_capturable": 0,
        "has_payment_intent": False,
        "cancellation_reason": None,
    }
    order_id = request.app.state.storage.create_order(
        order_data, provider="stripe"
    )
    request.app.state.storage.update_order(
        order_id,
        session_id=f"{SESSION_PREFIX}{order_id}",
        pi_id=f"{INTENT_PREFIX}{order_id}",
        charge_id=f"{CHARGE_PREFIX}{order_id}",
    )
    order = _get_order(request, order_id)
    assert order is not None

    host = request.headers.get("host", "localhost")
    session = session_object(order)
    session["url"] = f"http://{host}/sim/stripe/authorize/{order_id}"
    # Stripe always sets expires_at (24 h default)
    session["expires_at"] = (
        int(order["expires_at"])
        if order.get("expires_at")
        else int(time.time()) + 24 * 3600
    )
    return _json(session)


@get("/v1/checkout/sessions/{session_id:str}")
async def retrieve_checkout_session(
    request: Request[Any, Any, Any], session_id: str
) -> Response[object]:
    order = _order_by_prefixed_id(request, session_id, SESSION_PREFIX)
    if order is None:
        return _stripe_error(
            404, f"No such checkout.session: '{session_id}'",
            code="resource_missing",
        )
    return _json(session_object(order))


@post("/v1/checkout/sessions/{session_id:str}/expire")
async def expire_checkout_session(
    request: Request[Any, Any, Any], session_id: str
) -> Response[object]:
    order = _order_by_prefixed_id(request, session_id, SESSION_PREFIX)
    if order is None:
        return _stripe_error(
            404, f"No such checkout.session: '{session_id}'",
            code="resource_missing",
        )
    if order.get("status") not in ("open", "declined"):
        return _stripe_error(
            400,
            "Only sessions with an open status can be expired.",
        )
    order = _transition(request, order["id"], "expired")
    await _emit(
        request, [("checkout.session.expired", session_object(order))]
    )
    return _json(session_object(order))


@get("/v1/payment_intents/{intent_id:str}")
async def retrieve_payment_intent(
    request: Request[Any, Any, Any], intent_id: str
) -> Response[object]:
    order = _order_by_prefixed_id(request, intent_id, INTENT_PREFIX)
    if order is None:
        return _stripe_error(
            404, f"No such payment_intent: '{intent_id}'",
            code="resource_missing",
        )
    return _json(payment_intent_object(order))


@post("/v1/payment_intents/{intent_id:str}/capture")
async def capture_payment_intent(
    request: Request[Any, Any, Any], intent_id: str
) -> Response[object]:
    order = _order_by_prefixed_id(request, intent_id, INTENT_PREFIX)
    if order is None:
        return _stripe_error(
            404, f"No such payment_intent: '{intent_id}'",
            code="resource_missing",
        )
    if order.get("status") != "requires_capture":
        return _stripe_error(
            400,
            "PaymentIntent could not be captured because it has a "
            f"status of {order.get('status')}.",
        )

    form = await _form(request)
    capturable = int(order.get("amount_capturable") or 0)
    amount_to_capture = int(form.get("amount_to_capture", capturable))
    if amount_to_capture > capturable or amount_to_capture <= 0:
        return _stripe_error(
            400,
            f"Amount to capture ({amount_to_capture}) must be between 1 "
            f"and amount_capturable ({capturable}).",
        )

    request.app.state.storage.update_order(
        order["id"],
        amount_received=amount_to_capture,
        # Stripe auto-releases the uncaptured remainder.
        amount_capturable=0,
    )
    order = _transition(request, order["id"], "succeeded")
    await _emit(
        request,
        [
            # ignore-list traffic first: proves unordered robustness
            ("charge.captured", charge_object(order)),
            ("payment_intent.succeeded", payment_intent_object(order)),
        ],
    )
    return _json(payment_intent_object(order))


@post("/v1/payment_intents/{intent_id:str}/cancel")
async def cancel_payment_intent(
    request: Request[Any, Any, Any], intent_id: str
) -> Response[object]:
    order = _order_by_prefixed_id(request, intent_id, INTENT_PREFIX)
    if order is None:
        return _stripe_error(
            404, f"No such payment_intent: '{intent_id}'",
            code="resource_missing",
        )
    if order.get("status") not in ("requires_capture", "processing"):
        return _stripe_error(
            400,
            "PaymentIntent could not be canceled because it has a "
            f"status of {order.get('status')}.",
        )

    form = await _form(request)
    request.app.state.storage.update_order(
        order["id"],
        cancellation_reason=form.get(
            "cancellation_reason", "requested_by_customer"
        ),
        amount_capturable=0,
    )
    order = _transition(request, order["id"], "canceled")
    await _emit(
        request,
        [("payment_intent.canceled", payment_intent_object(order))],
    )
    return _json(payment_intent_object(order))


@post("/v1/refunds")
async def create_refund(
    request: Request[Any, Any, Any],
) -> Response[object]:
    form = await _form(request)
    intent_id = form.get("payment_intent", "")
    order = _order_by_prefixed_id(request, intent_id, INTENT_PREFIX)
    if order is None:
        return _stripe_error(
            404, f"No such payment_intent: '{intent_id}'",
            code="resource_missing",
        )
    if order.get("status") != "succeeded":
        # an uncaptured intent is canceled, never refunded (SPEC §9)
        return _stripe_error(
            400,
            "This PaymentIntent does not have a successful charge to "
            "refund.",
            code="charge_not_captured",
        )

    received = int(order.get("amount_received") or 0)
    already_refunded = sum(
        int(refund.get("amount") or 0)
        for refund in request.app.state.storage.get_refunds(order["id"])
        if refund.get("status") in ("pending", "requires_action", "succeeded")
    )
    amount = int(form.get("amount", received - already_refunded))
    if amount <= 0 or amount > received - already_refunded:
        return _stripe_error(
            400,
            "Refund amount exceeds the remaining, unrefunded amount "
            "of the charge.",
            code="charge_already_refunded",
        )

    metadata = {
        key.removeprefix("metadata[").removesuffix("]"): value
        for key, value in form.items()
        if key.startswith("metadata[")
    }
    initial_status = (
        "requires_action"
        if order.get("refund_mode") == "requires_action"
        else "pending"
    )
    refund_id = request.app.state.storage.create_refund(
        order["id"],
        {
            "amount": amount,
            "status": initial_status,
            "reason": None,
            "metadata": metadata,
        },
    )
    refund = request.app.state.storage.get_refund(refund_id)
    assert refund is not None
    response_body = refund_object(refund, order)
    events = [("refund.created", refund_object(refund, order))]

    if initial_status == "pending":
        # Card-style refunds settle promptly; late failure is driven
        # via the ops endpoint against the succeeded refund.
        request.app.state.storage.update_refund(
            refund_id, status="succeeded"
        )
        refund = request.app.state.storage.get_refund(refund_id)
        assert refund is not None
        events.append(("charge.refunded", charge_object(order)))
        events.append(("refund.updated", refund_object(refund, order)))

    await _emit(request, events)
    return _json(response_body)


@post("/v1/refunds/{refund_id:str}/cancel")
async def cancel_refund(
    request: Request[Any, Any, Any], refund_id: str
) -> Response[object]:
    if not refund_id.startswith(REFUND_PREFIX):
        return _stripe_error(
            404, f"No such refund: '{refund_id}'", code="resource_missing"
        )
    storage_id = refund_id.removeprefix(REFUND_PREFIX)
    refund = request.app.state.storage.get_refund(storage_id)
    if refund is None:
        return _stripe_error(
            404, f"No such refund: '{refund_id}'", code="resource_missing"
        )
    if refund.get("status") != "requires_action":
        # Card refunds are never requires_action: API cancel always
        # fails for them (Dashboard-only), SPEC §9.
        return _stripe_error(
            400,
            f"Refunds in a {refund.get('status')} status cannot be "
            "canceled.",
        )

    request.app.state.storage.update_refund(storage_id, status="canceled")
    refund = request.app.state.storage.get_refund(storage_id)
    assert refund is not None
    order = _get_order(request, str(refund["order_id"]))
    assert order is not None
    await _emit(
        request, [("refund.updated", refund_object(refund, order))]
    )
    return _json(refund_object(refund, order))


# --- fake hosted checkout UI --------------------------------------------


AUTHORIZE_PAGE = """<!doctype html>
<html>
<head><title>Stripe simulator checkout</title></head>
<body>
  <h1>Stripe simulator — fake hosted checkout</h1>
  <p>Order <code>{order_id}</code>: <strong>{amount} {currency}</strong>
     (capture: {capture_method})</p>
  <form method="post">
    <button name="action" value="pay">Pay</button>
    <button name="action" value="pay_delayed">Pay (delayed method)</button>
    <button name="action" value="decline">Decline</button>
    <button name="action" value="abandon">Abandon</button>
  </form>
</body>
</html>
"""


@get("/sim/stripe/authorize/{order_id:str}")
async def stripe_authorize_get(
    request: Request[Any, Any, Any], order_id: str
) -> Response[str]:
    order = _get_order(request, order_id)
    if order is None:
        raise NotFoundException("Payment not found")
    if order.get("status") not in ("open", "declined"):
        raise HTTPException(
            status_code=400, detail="Payment already processed"
        )
    return Response(
        content=AUTHORIZE_PAGE.format(
            order_id=order_id,
            amount=order.get("amount"),
            currency=str(order.get("currency", "")).upper(),
            capture_method=order.get("capture_method", "automatic"),
        ),
        media_type=MediaType.HTML,
    )


def _buyer_redirect(order: dict[str, Any], url_key: str) -> Redirect:
    url = str(order.get(url_key) or "/sim/dashboard")
    return Redirect(
        path=url.replace("{CHECKOUT_SESSION_ID}", str(order["session_id"]))
    )


@post("/sim/stripe/authorize/{order_id:str}")
async def stripe_authorize_post(
    request: Request[Any, Any, Any],
    order_id: str,
    data: dict[str, str] = URL_ENCODED_BODY,
) -> Redirect:
    order = _get_order(request, order_id)
    if order is None:
        raise NotFoundException("Payment not found")
    if order.get("status") not in ("open", "declined"):
        raise HTTPException(
            status_code=400, detail="Payment already processed"
        )

    action = data.get("action")
    storage = request.app.state.storage

    if action == "abandon":
        # No status change and no events: the session just sits there
        # until it expires (checkout.session.expired is the only
        # abandonment signal the processor will ever get).
        return _buyer_redirect(order, "cancel_url")

    if action == "decline":
        storage.update_order(order_id, has_payment_intent=True)
        order = _transition(request, order_id, "declined")
        await _emit(
            request,
            [
                (
                    "payment_intent.payment_failed",
                    payment_intent_object(order),
                )
            ],
        )
        return _buyer_redirect(order, "cancel_url")

    if action == "pay_delayed":
        storage.update_order(order_id, has_payment_intent=True)
        order = _transition(request, order_id, "processing")
        await _emit(
            request,
            [
                ("checkout.session.completed", session_object(order)),
                # ignore-list traffic (SPEC §7)
                (
                    "payment_intent.processing",
                    payment_intent_object(order),
                ),
            ],
        )
        return _buyer_redirect(order, "success_url")

    if action == "pay":
        amount = int(order.get("amount") or 0)
        if order.get("capture_method") == "manual":
            # TODO(SPEC §12): charge.succeeded timing at authorization
            # is unverified; the simulator emits no charge event here.
            storage.update_order(
                order_id,
                has_payment_intent=True,
                amount_capturable=amount,
            )
            order = _transition(request, order_id, "requires_capture")
            await _emit(
                request,
                [
                    ("checkout.session.completed", session_object(order)),
                    (
                        "payment_intent.amount_capturable_updated",
                        payment_intent_object(order),
                    ),
                ],
            )
        else:
            storage.update_order(
                order_id,
                has_payment_intent=True,
                amount_received=amount,
            )
            order = _transition(request, order_id, "succeeded")
            await _emit(
                request,
                [
                    # ignore-list traffic first: both success events
                    # arrive with distinct evt_ ids (SPEC §7)
                    ("charge.succeeded", charge_object(order)),
                    ("checkout.session.completed", session_object(order)),
                    (
                        "payment_intent.succeeded",
                        payment_intent_object(order),
                    ),
                ],
            )
        return _buyer_redirect(order, "success_url")

    raise HTTPException(status_code=400, detail="Invalid action")


# --- ops endpoint: transitions with no natural actor ---------------------


@post("/sim/stripe/ops/{order_id:str}")
async def stripe_ops(
    request: Request[Any, Any, Any],
    order_id: str,
) -> Response[object]:
    order = _get_order(request, order_id)
    if order is None:
        raise NotFoundException("Payment not found")

    payload = await request.json()
    action = payload.get("action") if isinstance(payload, dict) else None
    storage = request.app.state.storage

    if action == "expire_session":
        # time-travel: nobody waits 24 h for a session to expire
        order = _transition(request, order_id, "expired")
        await _emit(
            request,
            [("checkout.session.expired", session_object(order))],
        )
    elif action == "expire_auth":
        # time-travel across the ~7-day authorization window
        storage.update_order(
            order_id, cancellation_reason="automatic", amount_capturable=0
        )
        order = _transition(request, order_id, "canceled")
        await _emit(
            request,
            [("payment_intent.canceled", payment_intent_object(order))],
        )
    elif action == "fail_delayed":
        order = _transition(request, order_id, "declined")
        await _emit(
            request,
            [
                # ignore-list traffic (SPEC §7)
                (
                    "checkout.session.async_payment_failed",
                    session_object(order),
                ),
                (
                    "payment_intent.payment_failed",
                    payment_intent_object(order),
                ),
            ],
        )
    elif action == "settle_delayed":
        storage.update_order(
            order_id, amount_received=int(order.get("amount") or 0)
        )
        order = _transition(request, order_id, "succeeded")
        await _emit(
            request,
            [
                # ignore-list traffic (SPEC §7)
                (
                    "checkout.session.async_payment_succeeded",
                    session_object(order),
                ),
                ("payment_intent.succeeded", payment_intent_object(order)),
            ],
        )
    elif action == "fail_refund":
        refund_id = str(payload.get("refund_id", "")).removeprefix(
            REFUND_PREFIX
        )
        refund = storage.get_refund(refund_id)
        if refund is None or str(refund.get("order_id")) != order_id:
            raise NotFoundException("Refund not found")
        if "failed" not in REFUND_TRANSITIONS.get(
            str(refund.get("status")), set()
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Refund is {refund.get('status')}; cannot fail",
            )
        storage.update_refund(refund_id, status="failed")
        refund = storage.get_refund(refund_id)
        assert refund is not None
        await _emit(
            request, [("refund.failed", refund_object(refund, order))]
        )
    elif action == "set_refund_mode":
        storage.update_order(
            order_id, refund_mode=str(payload.get("mode", ""))
        )
    elif action == "open_review":
        await _emit(
            request,
            [("review.opened", review_object(order, is_open=True))],
        )
    elif action == "close_review":
        closed_reason = str(payload.get("closed_reason", "approved"))
        await _emit(
            request,
            [
                (
                    "review.closed",
                    review_object(
                        order, is_open=False, closed_reason=closed_reason
                    ),
                )
            ],
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    return _json({"ok": True, "order_id": order_id, "action": action})
