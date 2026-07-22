"""Paymentless-webhook correlation extraction (SPEC §6/§7).

``extract_callback_correlation`` is a classmethod: a framework adapter that
receives Stripe's single Dashboard webhook (no payment pk in the URL) uses it
to resolve the local Payment *before* running the payment-bound verify +
``handle_callback`` machinery. It returns our ``payment_id`` (session/intent
events, via metadata / ``client_reference_id``) and/or the PaymentIntent-derived
``external_id`` (refund / review events, which carry no payment_id).

It needs neither a bound payment nor config, so it is a classmethod — callable
straight off the class the framework adapter looks up by slug.
"""

from getpaid_stripe.processor import StripeProcessor
from tests import payloads


def _extract(event_type, obj):
    return StripeProcessor.extract_callback_correlation(
        payloads.event(event_type, obj), {}
    )


def test_checkout_session_yields_payment_id_and_external_id():
    result = _extract("checkout.session.completed", payloads.checkout_session())
    assert result == {
        "payment_id": payloads.PAYMENT_ID,
        "external_id": "pi_test_0001",
    }


def test_payment_intent_yields_payment_id_and_own_id_as_external_id():
    result = _extract("payment_intent.succeeded", payloads.payment_intent())
    assert result == {
        "payment_id": payloads.PAYMENT_ID,
        "external_id": "pi_test_0001",
    }


def test_refund_yields_external_id_from_payment_intent():
    result = _extract("refund.updated", payloads.refund())
    assert result["external_id"] == "pi_test_0001"


def test_review_yields_external_id_only():
    # Radar reviews carry no metadata / client_reference_id — only
    # payment_intent, so external_id is the sole correlation handle.
    result = _extract("review.opened", payloads.review())
    assert result == {"external_id": "pi_test_0001"}


def test_client_reference_id_used_when_metadata_absent():
    session = payloads.checkout_session()
    session["metadata"] = {}
    result = _extract("checkout.session.completed", session)
    assert result["payment_id"] == payloads.PAYMENT_ID


def test_session_without_payment_intent_has_no_external_id():
    session = payloads.checkout_session(payment_intent=None)
    result = _extract("checkout.session.completed", session)
    assert result == {"payment_id": payloads.PAYMENT_ID}


def test_uncorrelatable_event_returns_none():
    # No metadata, no client_reference_id, no payment_intent, id not a pi_.
    result = _extract("invoice.created", {"id": "in_x", "object": "invoice"})
    assert result is None


def test_callable_off_the_class_without_instance():
    # The adapter looks the processor up by slug and calls this with no
    # payment and no config — a classmethod, not an instance method.
    assert callable(StripeProcessor.extract_callback_correlation)
