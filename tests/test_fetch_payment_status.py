"""fetch_payment_status tests — pull path mapping (SPEC §7)."""

from decimal import Decimal

import pytest
import respx
from getpaid_core.enums import PaymentEvent
from httpx import Response

from getpaid_stripe.processor import StripeProcessor

from .conftest import make_mock_payment


def pi_json(status: str, **overrides):
    obj = {
        "id": "pi_test_0001",
        "object": "payment_intent",
        "amount": 10000,
        "amount_received": 0,
        "amount_capturable": 0,
        "currency": "pln",
        "capture_method": "automatic",
        "status": status,
        "metadata": {"payment_id": "test-payment-123"},
        "cancellation_reason": None,
    }
    obj.update(overrides)
    return obj


def session_json(status: str, payment_intent=None, **overrides):
    obj = {
        "id": "cs_test_a1b2c3",
        "object": "checkout.session",
        "status": status,
        "payment_status": "unpaid",
        "payment_intent": payment_intent,
        "metadata": {"payment_id": "test-payment-123"},
    }
    obj.update(overrides)
    return obj


@pytest.fixture
def router():
    with respx.mock(base_url="https://api.stripe.com") as router:
        yield router


def processor_for(external_id, stripe_config):
    payment = make_mock_payment(external_id=external_id)
    return StripeProcessor(payment, stripe_config)


async def test_pi_succeeded_maps_to_captured(stripe_config, router):
    router.get("/v1/payment_intents/pi_test_0001").mock(
        return_value=Response(
            200, json=pi_json("succeeded", amount_received=10000)
        )
    )
    processor = processor_for("pi_test_0001", stripe_config)
    update = await processor.fetch_payment_status()

    assert update is not None
    assert update.payment_event == PaymentEvent.PAYMENT_CAPTURED
    assert update.paid_amount == Decimal("100.00")
    assert update.external_id == "pi_test_0001"
    # pull updates carry no provider_event_id
    assert update.provider_event_id is None


async def test_pi_requires_capture_maps_to_locked(stripe_config, router):
    router.get("/v1/payment_intents/pi_test_0001").mock(
        return_value=Response(
            200,
            json=pi_json(
                "requires_capture",
                amount_capturable=10000,
                capture_method="manual",
            ),
        )
    )
    processor = processor_for("pi_test_0001", stripe_config)
    update = await processor.fetch_payment_status()

    assert update is not None
    assert update.payment_event == PaymentEvent.LOCKED
    assert update.locked_amount == Decimal("100.00")


async def test_pi_canceled_manual_maps_to_lock_released(
    stripe_config, router
):
    # the deterministic auth-expiry detection path (SPEC §8)
    router.get("/v1/payment_intents/pi_test_0001").mock(
        return_value=Response(
            200,
            json=pi_json(
                "canceled",
                capture_method="manual",
                cancellation_reason="automatic",
            ),
        )
    )
    processor = processor_for("pi_test_0001", stripe_config)
    update = await processor.fetch_payment_status()

    assert update is not None
    assert update.payment_event == PaymentEvent.LOCK_RELEASED


async def test_pi_canceled_automatic_maps_to_failed(stripe_config, router):
    router.get("/v1/payment_intents/pi_test_0001").mock(
        return_value=Response(200, json=pi_json("canceled"))
    )
    processor = processor_for("pi_test_0001", stripe_config)
    update = await processor.fetch_payment_status()

    assert update is not None
    assert update.payment_event == PaymentEvent.FAILED


@pytest.mark.parametrize(
    "status", ["processing", "requires_action", "requires_payment_method"]
)
async def test_pi_pending_states_return_none(stripe_config, router, status):
    router.get("/v1/payment_intents/pi_test_0001").mock(
        return_value=Response(200, json=pi_json(status))
    )
    processor = processor_for("pi_test_0001", stripe_config)
    assert await processor.fetch_payment_status() is None


async def test_session_expired_maps_to_failed(stripe_config, router):
    router.get("/v1/checkout/sessions/cs_test_a1b2c3").mock(
        return_value=Response(200, json=session_json("expired"))
    )
    processor = processor_for("cs_test_a1b2c3", stripe_config)
    update = await processor.fetch_payment_status()

    assert update is not None
    assert update.payment_event == PaymentEvent.FAILED


async def test_session_open_returns_none(stripe_config, router):
    router.get("/v1/checkout/sessions/cs_test_a1b2c3").mock(
        return_value=Response(200, json=session_json("open"))
    )
    processor = processor_for("cs_test_a1b2c3", stripe_config)
    assert await processor.fetch_payment_status() is None


async def test_session_complete_follows_payment_intent(
    stripe_config, router
):
    router.get("/v1/checkout/sessions/cs_test_a1b2c3").mock(
        return_value=Response(
            200,
            json=session_json("complete", payment_intent="pi_test_0001"),
        )
    )
    router.get("/v1/payment_intents/pi_test_0001").mock(
        return_value=Response(
            200, json=pi_json("succeeded", amount_received=10000)
        )
    )
    processor = processor_for("cs_test_a1b2c3", stripe_config)
    update = await processor.fetch_payment_status()

    assert update is not None
    assert update.payment_event == PaymentEvent.PAYMENT_CAPTURED
    # pull also promotes cs_… → pi_…
    assert update.external_id == "pi_test_0001"


async def test_no_external_id_returns_none(stripe_config):
    processor = processor_for(None, stripe_config)
    assert await processor.fetch_payment_status() is None
