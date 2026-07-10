"""Tests for the Stripe simulator plugin descriptor and payloads."""

from __future__ import annotations

import json
from importlib.metadata import entry_points

import pytest
import stripe
from getpaid_simulator.spi import SIMULATOR_PLUGIN_API_VERSION

from getpaid_stripe.simulator import get_plugin
from getpaid_stripe.simulator.plugin import load_provider_config
from getpaid_stripe.simulator.signing import sign_webhook
from getpaid_stripe.simulator.webhooks import build_event
from getpaid_stripe.simulator.webhooks import payment_intent_object
from getpaid_stripe.simulator.webhooks import refund_object
from getpaid_stripe.simulator.webhooks import review_object
from getpaid_stripe.simulator.webhooks import session_object


def _handler_name(handler: object) -> str:
    return str(handler.fn.__name__)


def test_stripe_simulator_entry_point_registered() -> None:
    simulator_plugins = [
        entry_point
        for entry_point in entry_points(group="getpaid.simulator.providers")
        if entry_point.name == "stripe"
    ]

    assert len(simulator_plugins) == 1
    assert simulator_plugins[0].value == "getpaid_stripe.simulator:get_plugin"


def test_get_plugin_returns_stripe_simulator_descriptor() -> None:
    plugin = get_plugin()

    assert plugin.api_version == SIMULATOR_PLUGIN_API_VERSION
    assert plugin.slug == "stripe"
    assert plugin.display_name == "Stripe"
    assert plugin.authorize_path_template == "/sim/stripe/authorize/{entity_id}"
    assert {_handler_name(handler) for handler in plugin.api_handlers} == {
        "create_checkout_session",
        "retrieve_checkout_session",
        "expire_checkout_session",
        "retrieve_payment_intent",
        "capture_payment_intent",
        "cancel_payment_intent",
        "create_refund",
        "cancel_refund",
    }
    assert {_handler_name(handler) for handler in plugin.ui_handlers} == {
        "stripe_authorize_get",
        "stripe_authorize_post",
        "stripe_ops",
    }


def test_load_provider_config_reads_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIMULATOR_STRIPE_API_KEY", "sk_test_override")
    monkeypatch.setenv("SIMULATOR_STRIPE_WEBHOOK_SECRET", "whsec_override")
    monkeypatch.setenv(
        "SIMULATOR_STRIPE_NOTIFY_URL",
        "https://merchant.example/stripe/callback",
    )

    config = load_provider_config()
    assert config["api_key"] == "sk_test_override"
    assert config["webhook_secret"] == "whsec_override"
    assert config["notify_url"] == "https://merchant.example/stripe/callback"


def test_load_provider_config_defaults() -> None:
    config = load_provider_config(env={})
    assert config["api_key"].startswith("sk_test_sim")
    assert config["webhook_secret"].startswith("whsec_sim")
    assert config["notify_url"] == ""


# --- payload honesty: type round-trip through stripe-python (SPEC §10) --


def _sample_order() -> dict:
    return {
        "id": "abc123",
        "provider": "stripe",
        "status": "succeeded",
        "session_id": "cs_sim_abc123",
        "pi_id": "pi_sim_abc123",
        "charge_id": "ch_sim_abc123",
        "amount": "10000",
        "amount_received": "10000",
        "amount_capturable": "0",
        "currency": "pln",
        "capture_method": "automatic",
        "metadata": {"payment_id": "payment-1"},
        "client_reference_id": "payment-1",
        "cancellation_reason": None,
        "has_payment_intent": True,
    }


def _sample_refund() -> dict:
    return {
        "id": "ref1",
        "order_id": "abc123",
        "amount": "2500",
        "status": "succeeded",
        "reason": None,
    }


ALL_EMITTED = [
    ("checkout.session.completed", lambda: session_object(_sample_order())),
    ("checkout.session.expired", lambda: session_object(_sample_order())),
    (
        "checkout.session.async_payment_succeeded",
        lambda: session_object(_sample_order()),
    ),
    (
        "payment_intent.amount_capturable_updated",
        lambda: payment_intent_object(_sample_order()),
    ),
    (
        "payment_intent.succeeded",
        lambda: payment_intent_object(_sample_order()),
    ),
    (
        "payment_intent.payment_failed",
        lambda: payment_intent_object(_sample_order()),
    ),
    (
        "payment_intent.canceled",
        lambda: payment_intent_object(_sample_order()),
    ),
    (
        "refund.created",
        lambda: refund_object(_sample_refund(), _sample_order()),
    ),
    (
        "refund.updated",
        lambda: refund_object(_sample_refund(), _sample_order()),
    ),
    (
        "refund.failed",
        lambda: refund_object(_sample_refund(), _sample_order()),
    ),
    (
        "review.opened",
        lambda: review_object(_sample_order(), is_open=True),
    ),
    (
        "review.closed",
        lambda: review_object(
            _sample_order(), is_open=False, closed_reason="approved"
        ),
    ),
    (
        "charge.succeeded",
        lambda: {"id": "ch_sim_abc123", "object": "charge"},
    ),
]


@pytest.mark.parametrize(
    ("event_type", "obj_factory"),
    ALL_EMITTED,
    ids=[event_type for event_type, _ in ALL_EMITTED],
)
def test_emitted_payload_round_trips_through_stripe_sdk(
    event_type, obj_factory
):
    """Every emitted payload must parse via stripe-python's typed
    classes AND verify through a real construct_event call, so an SDK
    upgrade flags drift in CI."""
    payload = build_event(event_type, obj_factory())
    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "whsec_sim_roundtrip"
    headers = sign_webhook(body, secret)

    event = stripe.Webhook.construct_event(
        body, headers["Stripe-Signature"], secret
    )

    assert event.type == event_type
    assert event.id.startswith("evt_sim_")
    assert event.object == "event"
    obj = event.data.object
    # typed attribute access must work on the constructed object
    assert obj["id"] == obj_factory()["id"]


def test_amounts_in_payloads_are_integers():
    # SimulatorStorage stringifies ints; payload builders must undo it
    obj = payment_intent_object(_sample_order())
    assert isinstance(obj["amount"], int)
    assert isinstance(obj["amount_received"], int)
    assert isinstance(obj["amount_capturable"], int)
    refund = refund_object(_sample_refund(), _sample_order())
    assert isinstance(refund["amount"], int)
