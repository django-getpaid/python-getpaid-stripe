"""End-to-end simulator scenario walks (SPEC §11.9, §13.2).

The real StripeProcessor talks to the simulator app through the
stripe SDK (httpx ASGI transport); every webhook the simulator emits
is verified and mapped by the same processor, and the resulting
PaymentUpdate sequences are asserted.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import PaymentEvent
from getpaid_simulator.core.state import PaymentStateMachine
from getpaid_simulator.core.storage import SimulatorStorage
from litestar import Litestar
from litestar.datastructures import State
from stripe import HTTPXClient

from getpaid_stripe.processor import StripeProcessor
from getpaid_stripe.simulator import get_plugin

from .conftest import make_mock_payment


NOTIFY_URL = "http://merchant.example/payments/callback"
WEBHOOK_SECRET = "whsec_sim_scenario_secret"


class CapturingTransport:
    """WebhookTransport stand-in that records deliveries."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    async def deliver(
        self, *, url: str, body: bytes, headers: dict[str, str]
    ) -> bool:
        self.captured.append(
            {"url": url, "body": body, "headers": dict(headers)}
        )
        return True


class Harness:
    def __init__(self, **config_overrides: Any) -> None:
        plugin = get_plugin()
        storage = SimulatorStorage()
        state_machine = PaymentStateMachine(storage)
        state_machine.register_provider("stripe", plugin.transitions)
        self.transport = CapturingTransport()
        provider_config = {
            "api_key": "sk_test_sim_stripe_key",
            "webhook_secret": WEBHOOK_SECRET,
            "notify_url": NOTIFY_URL,
        }
        self.app = Litestar(
            route_handlers=[*plugin.api_handlers, *plugin.ui_handlers],
            state=State(
                {
                    "storage": storage,
                    "state_machine": state_machine,
                    "provider_configs": {"stripe": provider_config},
                    "webhook_transport": self.transport,
                }
            ),
        )
        self.browser = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://sim",
        )

        sdk_http_client = HTTPXClient()
        sdk_http_client._client_async = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app)
        )
        self.payment = make_mock_payment(amount=Decimal("100.00"))
        self.processor = StripeProcessor(
            self.payment,
            {
                "api_key": "sk_test_sim_stripe_key",
                "webhook_secret": WEBHOOK_SECRET,
                "success_url": "http://merchant.example/ok/{payment_id}",
                "cancel_url": "http://merchant.example/no/{payment_id}",
                "api_base": "http://sim",
                "http_client": sdk_http_client,
                **config_overrides,
            },
        )

    async def prepare(self, **kwargs: Any) -> str:
        """prepare_transaction; returns the simulator order id."""
        result = await self.processor.prepare_transaction(**kwargs)
        assert result.external_id is not None
        self.payment.external_id = result.external_id
        self.authorize_url = result.redirect_url
        return result.external_id.removeprefix("cs_sim_")

    async def buyer(self, action: str) -> httpx.Response:
        response = await self.browser.post(
            self.authorize_url, data={"action": action}
        )
        assert response.status_code < 400, response.text
        return response

    async def ops(self, order_id: str, action: str, **extra: Any) -> None:
        response = await self.browser.post(
            f"/sim/stripe/ops/{order_id}",
            json={"action": action, **extra},
        )
        assert response.status_code < 400, response.text

    async def drain_updates(self) -> list[Any]:
        """Verify + map every captured webhook, like an adapter would.

        Applies external_id promotion back onto the payment and
        returns the non-None PaymentUpdates in delivery order.
        """
        updates = []
        deliveries, self.transport.captured = self.transport.captured, []
        for delivery in deliveries:
            assert delivery["url"] == NOTIFY_URL
            body = delivery["body"]
            data = json.loads(body)
            await self.processor.verify_callback(
                data, delivery["headers"], raw_body=body
            )
            update = await self.processor.handle_callback(data, {})
            if update is None:
                continue
            if update.external_id:
                self.payment.external_id = update.external_id
            updates.append(update)
        return updates


@pytest.fixture
async def harness():
    h = Harness()
    yield h
    await h.browser.aclose()


def events_of(updates) -> list[Any]:
    return [update.payment_event for update in updates]


# --- one-off payment walks ---------------------------------------------


async def test_pay_now_success(harness):
    await harness.prepare()
    assert harness.payment.external_id.startswith("cs_sim_")

    await harness.buyer("pay")
    updates = await harness.drain_updates()

    # charge.succeeded was emitted too and ignored
    assert events_of(updates) == [None, PaymentEvent.PAYMENT_CAPTURED]
    assert updates[-1].paid_amount == Decimal("100.00")
    # cs_… → pi_… promotion happened
    assert harness.payment.external_id.startswith("pi_sim_")
    # distinct evt ids for core's dedup
    ids = [update.provider_event_id for update in updates]
    assert len(set(ids)) == len(ids)


async def test_decline(harness):
    await harness.prepare()
    await harness.buyer("decline")
    updates = await harness.drain_updates()

    assert events_of(updates) == [PaymentEvent.FAILED]


async def test_abandon_then_expiry(harness):
    order_id = await harness.prepare()
    await harness.buyer("abandon")
    assert await harness.drain_updates() == []  # nothing fires yet

    await harness.ops(order_id, "expire_session")
    updates = await harness.drain_updates()

    assert events_of(updates) == [PaymentEvent.FAILED]


async def test_delayed_payment_settles_via_ops(harness):
    order_id = await harness.prepare()
    await harness.buyer("pay_delayed")
    updates = await harness.drain_updates()
    # session.completed promotes; payment_intent.processing is ignored
    assert events_of(updates) == [None]
    assert harness.payment.external_id.startswith("pi_sim_")

    await harness.ops(order_id, "settle_delayed")
    updates = await harness.drain_updates()
    # async_payment_succeeded is ignored; the PI twin is the truth
    assert events_of(updates) == [PaymentEvent.PAYMENT_CAPTURED]


# --- pre-auth walks ------------------------------------------------------


async def test_auth_then_partial_capture(harness):
    await harness.prepare(capture_method="manual")
    await harness.buyer("pay")
    updates = await harness.drain_updates()

    assert events_of(updates) == [None, PaymentEvent.LOCKED]
    assert updates[-1].locked_amount == Decimal("100.00")
    assert updates[-1].provider_data["locked_at"] is not None

    result = await harness.processor.charge(Decimal("40.00"))
    assert result.async_call is True
    assert result.amount_charged == Decimal("40.00")

    updates = await harness.drain_updates()
    # charge.captured is ignored; remainder auto-released by Stripe
    assert events_of(updates) == [PaymentEvent.PAYMENT_CAPTURED]
    assert updates[-1].paid_amount == Decimal("40.00")


async def test_auth_then_release(harness):
    await harness.prepare(capture_method="manual")
    await harness.buyer("pay")
    await harness.drain_updates()

    released = await harness.processor.release_lock()
    assert released == Decimal("100.00")

    updates = await harness.drain_updates()
    assert events_of(updates) == [PaymentEvent.LOCK_RELEASED]


async def test_auth_time_travel_expiry(harness):
    order_id = await harness.prepare(capture_method="manual")
    await harness.buyer("pay")
    await harness.drain_updates()

    await harness.ops(order_id, "expire_auth")
    updates = await harness.drain_updates()

    assert events_of(updates) == [PaymentEvent.LOCK_RELEASED]
    assert updates[-1].provider_data["cancellation_reason"] == "automatic"


# --- refund walks --------------------------------------------------------


async def _paid_harness(harness) -> str:
    order_id = await harness.prepare()
    await harness.buyer("pay")
    updates = await harness.drain_updates()
    harness.payment.amount_paid = updates[-1].paid_amount
    return order_id


async def test_full_refund(harness):
    await _paid_harness(harness)

    result = await harness.processor.start_refund()
    assert result.amount == Decimal("100.00")
    refund_id = result.provider_data["refund_id"]

    updates = await harness.drain_updates()
    # charge.refunded is emitted between the two and ignored
    assert events_of(updates) == [
        PaymentEvent.REFUND_REQUESTED,
        PaymentEvent.REFUND_CONFIRMED,
    ]
    assert updates[0].provider_data["refund_id"] == refund_id
    assert updates[-1].refunded_amount == Decimal("100.00")


async def test_partial_refund(harness):
    await _paid_harness(harness)

    result = await harness.processor.start_refund(Decimal("25.00"))
    assert result.amount == Decimal("25.00")

    updates = await harness.drain_updates()
    assert updates[-1].payment_event == PaymentEvent.REFUND_CONFIRMED
    assert updates[-1].refunded_amount == Decimal("25.00")


async def test_late_refund_failure(harness):
    order_id = await _paid_harness(harness)
    result = await harness.processor.start_refund()
    await harness.drain_updates()

    await harness.ops(
        order_id,
        "fail_refund",
        refund_id=result.provider_data["refund_id"],
    )
    updates = await harness.drain_updates()

    assert events_of(updates) == [PaymentEvent.REFUND_CANCELLED]


async def test_cancel_refund_requires_action_path(harness):
    order_id = await _paid_harness(harness)
    await harness.ops(order_id, "set_refund_mode", mode="requires_action")

    result = await harness.processor.start_refund()
    harness.payment.provider_data["refund_id"] = result.provider_data[
        "refund_id"
    ]
    updates = await harness.drain_updates()
    assert events_of(updates) == [PaymentEvent.REFUND_REQUESTED]

    assert await harness.processor.cancel_refund() is True
    updates = await harness.drain_updates()
    assert events_of(updates) == [PaymentEvent.REFUND_CANCELLED]


async def test_cancel_refund_card_path_returns_false(harness):
    await _paid_harness(harness)
    result = await harness.processor.start_refund()
    harness.payment.provider_data["refund_id"] = result.provider_data[
        "refund_id"
    ]
    await harness.drain_updates()

    # card refunds settle; API cancel is refused by Stripe
    assert await harness.processor.cancel_refund() is False


# --- fraud review walks ---------------------------------------------------


async def test_review_open_and_approve(harness):
    order_id = await _paid_harness(harness)

    await harness.ops(order_id, "open_review")
    await harness.ops(order_id, "close_review", closed_reason="approved")
    updates = await harness.drain_updates()

    assert [update.fraud_event for update in updates] == [
        FraudEvent.REVIEW,
        FraudEvent.ACCEPT,
    ]


async def test_review_open_and_reject(harness):
    order_id = await _paid_harness(harness)

    await harness.ops(order_id, "open_review")
    await harness.ops(
        order_id, "close_review", closed_reason="refunded_as_fraud"
    )
    updates = await harness.drain_updates()

    assert [update.fraud_event for update in updates] == [
        FraudEvent.REVIEW,
        FraudEvent.REJECT,
    ]
    assert updates[-1].fraud_message == "refunded_as_fraud"


# --- pull backstop against the simulator ----------------------------------


async def test_fetch_payment_status_pull_walk(harness):
    await harness.prepare()
    # session still open → nothing to report
    assert await harness.processor.fetch_payment_status() is None

    await harness.buyer("pay")
    # pull while external_id is still cs_… follows the session to
    # the PI and promotes
    update = await harness.processor.fetch_payment_status()
    assert update is not None
    assert update.payment_event == PaymentEvent.PAYMENT_CAPTURED
    assert update.external_id.startswith("pi_sim_")
    assert update.provider_event_id is None


async def test_fetch_payment_status_sees_lock(harness):
    await harness.prepare(capture_method="manual")
    await harness.buyer("pay")

    update = await harness.processor.fetch_payment_status()
    assert update is not None
    assert update.payment_event == PaymentEvent.LOCKED
    assert update.locked_amount == Decimal("100.00")
