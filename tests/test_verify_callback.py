"""verify_callback tests — webhook signature verification (SPEC §7)."""

import json
import time

import pytest
from getpaid_core.exceptions import InvalidCallbackError

from getpaid_stripe.processor import StripeProcessor
from getpaid_stripe.simulator.signing import stripe_signature_header

from .conftest import WEBHOOK_SECRET


EVENT = {
    "id": "evt_test_1",
    "object": "event",
    "api_version": "2025-06-30",
    "created": 1767000000,
    "type": "payment_intent.succeeded",
    "data": {"object": {"id": "pi_test_1", "object": "payment_intent"}},
}


def signed(payload: dict, secret: str = WEBHOOK_SECRET, **kwargs):
    body = json.dumps(payload).encode()
    return body, {
        "Stripe-Signature": stripe_signature_header(body, secret, **kwargs)
    }


@pytest.fixture
def processor(stripe_config, mock_payment) -> StripeProcessor:
    return StripeProcessor(mock_payment, stripe_config)


async def test_valid_signature_passes(processor):
    body, headers = signed(EVENT)
    await processor.verify_callback(EVENT, headers, raw_body=body)


async def test_header_lookup_is_case_insensitive(processor):
    body, headers = signed(EVENT)
    lowercase = {"stripe-signature": headers["Stripe-Signature"]}
    await processor.verify_callback(EVENT, lowercase, raw_body=body)


async def test_tampered_body_rejected(processor):
    body, headers = signed(EVENT)
    tampered = body.replace(b"pi_test_1", b"pi_evil_9")
    with pytest.raises(InvalidCallbackError):
        await processor.verify_callback(EVENT, headers, raw_body=tampered)


async def test_wrong_secret_rejected(processor):
    body, headers = signed(EVENT, secret="whsec_other_secret")
    with pytest.raises(InvalidCallbackError):
        await processor.verify_callback(EVENT, headers, raw_body=body)


async def test_stale_timestamp_rejected(processor):
    # default tolerance is 300 s
    body, headers = signed(EVENT, timestamp=int(time.time()) - 3600)
    with pytest.raises(InvalidCallbackError):
        await processor.verify_callback(EVENT, headers, raw_body=body)


async def test_missing_raw_body_rejected(processor):
    _, headers = signed(EVENT)
    with pytest.raises(InvalidCallbackError):
        await processor.verify_callback(EVENT, headers)


async def test_missing_signature_header_rejected(processor):
    body, _ = signed(EVENT)
    with pytest.raises(InvalidCallbackError):
        await processor.verify_callback(EVENT, {}, raw_body=body)


async def test_thin_v2_payload_rejected(processor):
    # v2 "thin" events are not webhook payloads; this plugin is
    # v1-events-only.
    thin = {
        "id": "evt_thin_1",
        "object": "v2.core.event",
        "type": "v2.money_management.outbound_payment.created",
    }
    body, headers = signed(thin)
    with pytest.raises(InvalidCallbackError):
        await processor.verify_callback(thin, headers, raw_body=body)
