"""prepare_transaction tests — Checkout Session creation (SPEC §6)."""

import time
from decimal import Decimal
from urllib.parse import parse_qsl

import pytest
import respx
from httpx import Response

from getpaid_stripe.processor import StripeProcessor

from .conftest import make_mock_payment


SESSION_RESPONSE = {
    "id": "cs_test_a1b2c3",
    "object": "checkout.session",
    "url": "https://checkout.stripe.com/c/pay/cs_test_a1b2c3",
    "status": "open",
    "payment_intent": None,
    "expires_at": 1767222000,
}


@pytest.fixture
def sessions_route():
    with respx.mock(base_url="https://api.stripe.com") as router:
        route = router.post("/v1/checkout/sessions").mock(
            return_value=Response(200, json=SESSION_RESPONSE)
        )
        yield route


def sent_form(route) -> dict[str, str]:
    return dict(parse_qsl(route.calls.last.request.content.decode()))


async def test_returns_redirect_transaction_result(
    stripe_config, mock_payment, sessions_route
):
    processor = StripeProcessor(mock_payment, stripe_config)
    result = await processor.prepare_transaction()

    assert result.redirect_url == SESSION_RESPONSE["url"]
    assert result.external_id == "cs_test_a1b2c3"
    assert result.method == "GET"
    assert result.provider_data["session_id"] == "cs_test_a1b2c3"
    assert result.provider_data["expires_at"] == 1767222000


async def test_session_params_payment_mode_and_amount(
    stripe_config, sessions_route
):
    payment = make_mock_payment(amount=Decimal("123.45"), currency="PLN")
    processor = StripeProcessor(payment, stripe_config)
    await processor.prepare_transaction()

    form = sent_form(sessions_route)
    assert form["mode"] == "payment"
    assert form["line_items[0][price_data][currency]"] == "pln"
    assert form["line_items[0][price_data][unit_amount]"] == "12345"
    assert form["line_items[0][quantity]"] == "1"
    assert (
        form["line_items[0][price_data][product_data][name]"] == "Test order"
    )


async def test_correlation_belt_and_braces(
    stripe_config, mock_payment, sessions_route
):
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction()

    form = sent_form(sessions_route)
    assert form["client_reference_id"] == "test-payment-123"
    assert form["metadata[payment_id]"] == "test-payment-123"
    # payment_intent_data metadata is the only one Stripe copies onto
    # the PaymentIntent and its Charge
    assert (
        form["payment_intent_data[metadata][payment_id]"]
        == "test-payment-123"
    )


async def test_urls_format_payment_id_and_keep_session_placeholder(
    stripe_config, mock_payment, sessions_route
):
    stripe_config["success_url"] = (
        "https://shop.example.com/ok/{payment_id}"
        "?session={CHECKOUT_SESSION_ID}"
    )
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction()

    form = sent_form(sessions_route)
    assert form["success_url"] == (
        "https://shop.example.com/ok/test-payment-123"
        "?session={CHECKOUT_SESSION_ID}"
    )
    assert form["cancel_url"] == (
        "https://shop.example.com/payments/cancel/test-payment-123"
    )


async def test_url_kwargs_override_settings(
    stripe_config, mock_payment, sessions_route
):
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction(
        success_url="https://other.example.com/done/{payment_id}"
    )

    form = sent_form(sessions_route)
    assert (
        form["success_url"] == "https://other.example.com/done/test-payment-123"
    )


async def test_missing_urls_raise(mock_payment, stripe_config):
    del stripe_config["success_url"]
    processor = StripeProcessor(mock_payment, stripe_config)
    with pytest.raises(ValueError):
        await processor.prepare_transaction()


async def test_automatic_capture_sends_no_capture_method(
    stripe_config, mock_payment, sessions_route
):
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction()

    form = sent_form(sessions_route)
    assert "payment_intent_data[capture_method]" not in form
    # payment methods stay automatic: account configuration decides
    assert not any(k.startswith("payment_method_types") for k in form)


async def test_manual_capture_from_setting(
    stripe_config, mock_payment, sessions_route
):
    stripe_config["capture_method"] = "manual"
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction()

    form = sent_form(sessions_route)
    assert form["payment_intent_data[capture_method]"] == "manual"


async def test_manual_capture_kwarg_overrides_setting(
    stripe_config, mock_payment, sessions_route
):
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction(capture_method="manual")

    form = sent_form(sessions_route)
    assert form["payment_intent_data[capture_method]"] == "manual"


async def test_invalid_capture_method_raises(stripe_config, mock_payment):
    processor = StripeProcessor(mock_payment, stripe_config)
    with pytest.raises(ValueError):
        await processor.prepare_transaction(capture_method="delayed")


async def test_session_expires_in_maps_to_expires_at(
    stripe_config, mock_payment, sessions_route
):
    stripe_config["session_expires_in"] = 45
    before = int(time.time())
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction()
    after = int(time.time())

    form = sent_form(sessions_route)
    expires_at = int(form["expires_at"])
    assert before + 45 * 60 <= expires_at <= after + 45 * 60


async def test_no_expires_at_by_default(
    stripe_config, mock_payment, sessions_route
):
    processor = StripeProcessor(mock_payment, stripe_config)
    await processor.prepare_transaction()

    assert "expires_at" not in sent_form(sessions_route)


async def test_zero_decimal_currency_amount(stripe_config, sessions_route):
    payment = make_mock_payment(amount=Decimal("500"), currency="JPY")
    processor = StripeProcessor(payment, stripe_config)
    await processor.prepare_transaction()

    form = sent_form(sessions_route)
    assert form["line_items[0][price_data][currency]"] == "jpy"
    assert form["line_items[0][price_data][unit_amount]"] == "500"
