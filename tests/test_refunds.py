"""start_refund() / cancel_refund() tests (SPEC §9)."""

from decimal import Decimal
from urllib.parse import parse_qsl

import pytest
import respx
from getpaid_core.exceptions import RefundFailure
from httpx import Response

from getpaid_stripe.processor import StripeProcessor

from .conftest import make_mock_payment


PI = "pi_test_0001"


def refund_json(status="pending", amount=10000, refund_id="re_test_0001"):
    return {
        "id": refund_id,
        "object": "refund",
        "amount": amount,
        "currency": "pln",
        "payment_intent": PI,
        "status": status,
        "reason": None,
        "metadata": {"payment_id": "test-payment-123"},
    }


@pytest.fixture
def router():
    with respx.mock(base_url="https://api.stripe.com") as router:
        yield router


def paid_processor(stripe_config, external_id=PI, provider_data=None):
    payment = make_mock_payment(
        external_id=external_id, provider_data=provider_data
    )
    payment.amount_paid = Decimal("100.00")
    return StripeProcessor(payment, stripe_config)


# --- start_refund() ----------------------------------------------------


async def test_full_refund_omits_amount(stripe_config, router):
    create = router.post("/v1/refunds").mock(
        return_value=Response(200, json=refund_json())
    )
    processor = paid_processor(stripe_config)
    result = await processor.start_refund()

    form = dict(parse_qsl(create.calls.last.request.content.decode()))
    assert form["payment_intent"] == PI
    assert "amount" not in form
    # refund metadata does not inherit from the PI — set at creation
    assert form["metadata[payment_id]"] == "test-payment-123"
    assert result.amount == Decimal("100.00")
    assert result.provider_data["refund_id"] == "re_test_0001"
    assert result.provider_data["status"] == "pending"


async def test_partial_refund_sends_minor_amount(stripe_config, router):
    create = router.post("/v1/refunds").mock(
        return_value=Response(200, json=refund_json(amount=2500))
    )
    processor = paid_processor(stripe_config)
    result = await processor.start_refund(Decimal("25.00"))

    form = dict(parse_qsl(create.calls.last.request.content.decode()))
    assert form["amount"] == "2500"
    assert result.amount == Decimal("25.00")


async def test_refund_without_payment_intent_raises(stripe_config):
    processor = paid_processor(stripe_config, external_id="cs_test_a1")
    with pytest.raises(RefundFailure):
        await processor.start_refund()


async def test_stripe_rejection_maps_to_refund_failure(
    stripe_config, router
):
    router.post("/v1/refunds").mock(
        return_value=Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "charge_already_refunded",
                    "message": "Charge … has already been refunded.",
                }
            },
        )
    )
    processor = paid_processor(stripe_config)
    with pytest.raises(RefundFailure):
        await processor.start_refund()


async def test_no_reason_parameter_is_sent(stripe_config, router):
    # `reason` is deliberately not exposed (fraudulent has block-list
    # side effects)
    create = router.post("/v1/refunds").mock(
        return_value=Response(200, json=refund_json())
    )
    processor = paid_processor(stripe_config)
    await processor.start_refund()

    form = dict(parse_qsl(create.calls.last.request.content.decode()))
    assert "reason" not in form


# --- cancel_refund() ---------------------------------------------------


async def test_cancel_refund_requires_action_path_returns_true(
    stripe_config, router
):
    cancel = router.post("/v1/refunds/re_test_0001/cancel").mock(
        return_value=Response(200, json=refund_json(status="canceled"))
    )
    processor = paid_processor(
        stripe_config, provider_data={"refund_id": "re_test_0001"}
    )
    assert await processor.cancel_refund() is True
    assert cancel.called


async def test_cancel_refund_stripe_rejection_returns_false(
    stripe_config, router
):
    # card refunds cannot be canceled via API — Stripe rejects
    router.post("/v1/refunds/re_test_0001/cancel").mock(
        return_value=Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": (
                        "Refunds in a pending status cannot be canceled."
                    ),
                }
            },
        )
    )
    processor = paid_processor(
        stripe_config, provider_data={"refund_id": "re_test_0001"}
    )
    assert await processor.cancel_refund() is False


async def test_cancel_refund_without_refund_id_raises(stripe_config):
    processor = paid_processor(stripe_config)
    with pytest.raises(RefundFailure):
        await processor.cancel_refund()
