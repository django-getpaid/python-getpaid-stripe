"""handle_callback tests — one per row of the SPEC §7 mapping table."""

from decimal import Decimal

import pytest
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import PaymentEvent
from getpaid_core.exceptions import InvalidCallbackError

from getpaid_stripe.processor import StripeProcessor

from . import payloads
from .conftest import make_mock_payment


@pytest.fixture
def processor(stripe_config, mock_payment) -> StripeProcessor:
    return StripeProcessor(mock_payment, stripe_config)


@pytest.fixture
def bound_processor(stripe_config) -> StripeProcessor:
    """Processor whose payment is already bound to the PaymentIntent.

    Review payloads carry no metadata; they correlate through the
    ``payment_intent`` back-reference against ``external_id``.
    """
    payment = make_mock_payment(external_id="pi_test_0001")
    return StripeProcessor(payment, stripe_config)


async def handle(processor, evt):
    return await processor.handle_callback(evt, {})


# --- checkout.session.* ---------------------------------------------


async def test_session_completed_promotes_external_id_no_event(processor):
    evt = payloads.event(
        "checkout.session.completed", payloads.checkout_session()
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event is None  # PI events are money-truth
    assert update.external_id == "pi_test_0001"
    assert update.provider_event_id == "evt_test_0001"
    assert update.provider_data["session_id"] == "cs_test_a1b2c3"
    assert update.provider_data["payment_status"] == "paid"


async def test_session_completed_without_pi_keeps_external_id_unset(
    processor,
):
    evt = payloads.event(
        "checkout.session.completed",
        payloads.checkout_session(payment_intent=None),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.external_id is None


async def test_session_expired_maps_to_failed(processor):
    evt = payloads.event(
        "checkout.session.expired",
        payloads.checkout_session(
            payment_intent=None, status="expired", payment_status="unpaid"
        ),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.FAILED
    assert update.provider_event_id == "evt_test_0001"


@pytest.mark.parametrize(
    "event_type",
    [
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    ],
)
async def test_session_async_payment_events_ignored(processor, event_type):
    evt = payloads.event(event_type, payloads.checkout_session())
    assert await handle(processor, evt) is None


# --- payment_intent.* ------------------------------------------------


async def test_amount_capturable_updated_maps_to_locked(processor):
    evt = payloads.event(
        "payment_intent.amount_capturable_updated",
        payloads.payment_intent(
            status="requires_capture",
            amount_capturable=10000,
            capture_method="manual",
        ),
        created=1767001234,
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.LOCKED
    assert update.locked_amount == Decimal("100.00")
    assert update.external_id == "pi_test_0001"
    assert update.provider_data["locked_at"] == 1767001234


async def test_payment_intent_succeeded_maps_to_captured(processor):
    evt = payloads.event(
        "payment_intent.succeeded",
        payloads.payment_intent(amount_received=10000),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.PAYMENT_CAPTURED
    assert update.paid_amount == Decimal("100.00")
    assert update.external_id == "pi_test_0001"


async def test_payment_intent_succeeded_uses_payload_currency(processor):
    # from_minor must use the payload's own currency field
    evt = payloads.event(
        "payment_intent.succeeded",
        payloads.payment_intent(
            amount=500, amount_received=500, currency="jpy"
        ),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.paid_amount == Decimal("500")


async def test_payment_intent_payment_failed_maps_to_failed(processor):
    evt = payloads.event(
        "payment_intent.payment_failed",
        payloads.payment_intent(status="requires_payment_method"),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.FAILED


async def test_canceled_manual_capture_maps_to_lock_released(processor):
    evt = payloads.event(
        "payment_intent.canceled",
        payloads.payment_intent(
            status="canceled",
            capture_method="manual",
            cancellation_reason="requested_by_customer",
        ),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.LOCK_RELEASED
    assert (
        update.provider_data["cancellation_reason"]
        == "requested_by_customer"
    )


async def test_canceled_automatic_capture_maps_to_failed(processor):
    evt = payloads.event(
        "payment_intent.canceled",
        payloads.payment_intent(
            status="canceled",
            capture_method="automatic",
            cancellation_reason="abandoned",
        ),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.FAILED


@pytest.mark.parametrize(
    "event_type",
    [
        "payment_intent.created",
        "payment_intent.processing",
        "payment_intent.requires_action",
        "payment_intent.partially_funded",
    ],
)
async def test_payment_intent_noise_ignored(processor, event_type):
    evt = payloads.event(event_type, payloads.payment_intent())
    assert await handle(processor, evt) is None


# --- refund.* ---------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "requires_action"])
@pytest.mark.parametrize(
    "event_type", ["refund.created", "refund.updated"]
)
async def test_refund_in_flight_maps_to_refund_requested(
    processor, event_type, status
):
    evt = payloads.event(event_type, payloads.refund(status=status))
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.REFUND_REQUESTED
    assert update.provider_data["refund_id"] == "re_test_0001"
    assert update.external_id == "pi_test_0001"


@pytest.mark.parametrize(
    "event_type", ["refund.created", "refund.updated"]
)
async def test_refund_succeeded_maps_to_refund_confirmed(
    processor, event_type
):
    evt = payloads.event(
        event_type, payloads.refund(status="succeeded", amount=2500)
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.REFUND_CONFIRMED
    assert update.refunded_amount == Decimal("25.00")


@pytest.mark.parametrize("status", ["failed", "canceled"])
@pytest.mark.parametrize(
    "event_type", ["refund.updated", "refund.failed"]
)
async def test_refund_terminal_failure_maps_to_refund_cancelled(
    processor, event_type, status
):
    evt = payloads.event(event_type, payloads.refund(status=status))
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.REFUND_CANCELLED


async def test_stripe_generated_expiry_refund_still_maps(processor):
    # Expired uncaptured auths produce unprompted Stripe refunds;
    # status-driven mapping must handle them like any other refund.
    evt = payloads.event(
        "refund.created",
        payloads.refund(
            status="succeeded", reason="expired_uncaptured_charge"
        ),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.REFUND_CONFIRMED


# --- charge.* is never money-truth ------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "charge.succeeded",
        "charge.captured",
        "charge.refunded",
        "charge.refund.updated",
    ],
)
async def test_charge_events_ignored(processor, event_type):
    evt = payloads.event(
        event_type, {"id": "ch_test_1", "object": "charge"}
    )
    assert await handle(processor, evt) is None


# --- review.* → fraud events ------------------------------------------


async def test_review_opened_maps_to_fraud_review(bound_processor):
    processor = bound_processor
    evt = payloads.event("review.opened", payloads.review(is_open=True))
    update = await handle(processor, evt)

    assert update is not None
    assert update.fraud_event == FraudEvent.REVIEW
    assert update.external_id == "pi_test_0001"


async def test_review_closed_approved_maps_to_fraud_accept(
    bound_processor,
):
    processor = bound_processor
    evt = payloads.event(
        "review.closed",
        payloads.review(is_open=False, closed_reason="approved"),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.fraud_event == FraudEvent.ACCEPT


@pytest.mark.parametrize(
    "closed_reason", ["refunded", "refunded_as_fraud", "disputed"]
)
async def test_review_closed_otherwise_maps_to_fraud_reject(
    bound_processor, closed_reason
):
    processor = bound_processor
    evt = payloads.event(
        "review.closed",
        payloads.review(is_open=False, closed_reason=closed_reason),
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.fraud_event == FraudEvent.REJECT
    assert update.fraud_message == closed_reason


# --- everything else / integrity --------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "invoice.paid",
        "customer.subscription.created",
        "payment_method.attached",
        "some.future.event",
    ],
)
async def test_unknown_events_log_and_ignore(processor, event_type):
    evt = payloads.event(event_type, {"id": "x_1", "object": "unknown"})
    assert await handle(processor, evt) is None


async def test_every_mapped_update_carries_provider_event_id(processor):
    evt = payloads.event(
        "payment_intent.succeeded",
        payloads.payment_intent(amount_received=10000),
        event_id="evt_dedup_42",
    )
    update = await handle(processor, evt)

    assert update is not None
    assert update.provider_event_id == "evt_dedup_42"


async def test_foreign_payment_metadata_rejected(stripe_config):
    processor = StripeProcessor(
        make_mock_payment(payment_id="payment-A"), stripe_config
    )
    evt = payloads.event(
        "payment_intent.succeeded",
        payloads.payment_intent(
            amount_received=10000, payment_id="payment-B"
        ),
    )
    with pytest.raises(InvalidCallbackError):
        await handle(processor, evt)


async def test_uncorrelatable_payment_intent_ignored(stripe_config):
    # Subscription-born traffic on a shared account: no metadata, no
    # matching id — must never move money-state (SPEC §7).
    processor = StripeProcessor(
        make_mock_payment(external_id="pi_test_0001"), stripe_config
    )
    evt = payloads.event(
        "payment_intent.succeeded",
        payloads.payment_intent(
            intent_id="pi_foreign_sub",
            amount_received=9999,
            metadata={},
        ),
    )
    assert await handle(processor, evt) is None


async def test_expiry_refund_without_metadata_correlates_by_intent(
    bound_processor,
):
    # Stripe-generated expiry refunds carry none of our metadata;
    # they correlate via the payment_intent back-reference.
    evt = payloads.event(
        "refund.created",
        payloads.refund(
            status="succeeded",
            reason="expired_uncaptured_charge",
            metadata={},
        ),
    )
    update = await handle(bound_processor, evt)

    assert update is not None
    assert update.payment_event == PaymentEvent.REFUND_CONFIRMED


async def test_review_for_other_intent_ignored(stripe_config):
    processor = StripeProcessor(
        make_mock_payment(external_id="pi_other_042"), stripe_config
    )
    evt = payloads.event("review.opened", payloads.review(is_open=True))
    assert await handle(processor, evt) is None


async def test_both_events_fire_dedup_story(processor):
    # checkout.session.completed and payment_intent.succeeded both
    # arrive with distinct evt_ ids; only the PI event carries
    # PAYMENT_CAPTURED, so core's provider_event_id dedup is never
    # asked to dedup across the pair.
    session_evt = payloads.event(
        "checkout.session.completed",
        payloads.checkout_session(),
        event_id="evt_session_1",
    )
    pi_evt = payloads.event(
        "payment_intent.succeeded",
        payloads.payment_intent(amount_received=10000),
        event_id="evt_pi_1",
    )
    first = await handle(processor, session_evt)
    second = await handle(processor, pi_evt)

    assert first is not None and first.payment_event is None
    assert second is not None
    assert second.payment_event == PaymentEvent.PAYMENT_CAPTURED
    assert first.provider_event_id != second.provider_event_id
    # both point the payment at the same PaymentIntent
    assert first.external_id == second.external_id == "pi_test_0001"
