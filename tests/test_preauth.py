"""charge() / release_lock() tests — manual capture (SPEC §8)."""

from decimal import Decimal
from urllib.parse import parse_qsl

import pytest
import respx
from getpaid_core.exceptions import ChargeFailure
from getpaid_core.exceptions import LockFailure
from httpx import Response

from getpaid_stripe.processor import StripeProcessor

from .conftest import make_mock_payment


PI = "pi_test_0001"


def pi_json(status: str, **overrides):
    obj = {
        "id": PI,
        "object": "payment_intent",
        "amount": 10000,
        "amount_received": 0,
        "amount_capturable": 10000,
        "currency": "pln",
        "capture_method": "manual",
        "status": status,
        "metadata": {"payment_id": "test-payment-123"},
        "cancellation_reason": None,
    }
    obj.update(overrides)
    return obj


@pytest.fixture
def router():
    with respx.mock(base_url="https://api.stripe.com") as router:
        yield router


def locked_processor(stripe_config, external_id=PI):
    payment = make_mock_payment(
        external_id=external_id, provider_data={"locked_at": 1767000000}
    )
    return StripeProcessor(payment, stripe_config)


def mock_retrieve(router, status="requires_capture", **overrides):
    return router.get(f"/v1/payment_intents/{PI}").mock(
        return_value=Response(200, json=pi_json(status, **overrides))
    )


# --- charge() ----------------------------------------------------------


async def test_full_capture(stripe_config, router):
    mock_retrieve(router)
    capture = router.post(f"/v1/payment_intents/{PI}/capture").mock(
        return_value=Response(
            200,
            json=pi_json(
                "succeeded", amount_received=10000, amount_capturable=0
            ),
        )
    )
    processor = locked_processor(stripe_config)
    result = await processor.charge()

    assert result.amount_charged == Decimal("100.00")
    assert result.success is True
    # core applies CHARGE_REQUESTED and awaits payment_intent.succeeded
    assert result.async_call is True
    form = dict(parse_qsl(capture.calls.last.request.content.decode()))
    assert "amount_to_capture" not in form


async def test_partial_capture_sends_amount_to_capture(
    stripe_config, router
):
    mock_retrieve(router)
    capture = router.post(f"/v1/payment_intents/{PI}/capture").mock(
        return_value=Response(
            200,
            json=pi_json(
                "succeeded", amount_received=4000, amount_capturable=0
            ),
        )
    )
    processor = locked_processor(stripe_config)
    result = await processor.charge(Decimal("40.00"))

    # Stripe auto-releases the remainder; no separate release call
    form = dict(parse_qsl(capture.calls.last.request.content.decode()))
    assert form["amount_to_capture"] == "4000"
    assert result.amount_charged == Decimal("40.00")
    assert result.async_call is True


@pytest.mark.parametrize(
    "status", ["succeeded", "canceled", "processing", "requires_action"]
)
async def test_charge_outside_requires_capture_raises(
    stripe_config, router, status
):
    mock_retrieve(router, status=status)
    processor = locked_processor(stripe_config)
    with pytest.raises(ChargeFailure):
        await processor.charge()


async def test_charge_without_payment_intent_raises(stripe_config):
    processor = locked_processor(stripe_config, external_id="cs_test_a1")
    with pytest.raises(ChargeFailure):
        await processor.charge()


async def test_charge_stripe_error_raises_charge_failure(
    stripe_config, router
):
    mock_retrieve(router)
    router.post(f"/v1/payment_intents/{PI}/capture").mock(
        return_value=Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "Already captured.",
                }
            },
        )
    )
    processor = locked_processor(stripe_config)
    with pytest.raises(ChargeFailure):
        await processor.charge()


# --- release_lock() ----------------------------------------------------


async def test_release_lock_cancels_and_returns_locked_amount(
    stripe_config, router
):
    mock_retrieve(router, amount_capturable=10000)
    cancel = router.post(f"/v1/payment_intents/{PI}/cancel").mock(
        return_value=Response(
            200, json=pi_json("canceled", amount_capturable=0)
        )
    )
    processor = locked_processor(stripe_config)
    released = await processor.release_lock()

    assert released == Decimal("100.00")
    assert cancel.called


@pytest.mark.parametrize("status", ["succeeded", "canceled", "processing"])
async def test_release_lock_outside_requires_capture_raises(
    stripe_config, router, status
):
    mock_retrieve(router, status=status)
    processor = locked_processor(stripe_config)
    with pytest.raises(LockFailure):
        await processor.release_lock()


async def test_release_lock_without_payment_intent_raises(stripe_config):
    processor = locked_processor(stripe_config, external_id=None)
    with pytest.raises(LockFailure):
        await processor.release_lock()


async def test_release_lock_stripe_error_raises_lock_failure(
    stripe_config, router
):
    mock_retrieve(router)
    router.post(f"/v1/payment_intents/{PI}/cancel").mock(
        return_value=Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "Cannot cancel.",
                }
            },
        )
    )
    processor = locked_processor(stripe_config)
    with pytest.raises(LockFailure):
        await processor.release_lock()
