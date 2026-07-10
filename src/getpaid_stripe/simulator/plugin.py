"""Stripe simulator plugin factory."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from typing import Any

from getpaid_simulator.spi import SIMULATOR_PLUGIN_API_VERSION
from getpaid_simulator.spi import SimulatorProviderPlugin

from getpaid_stripe.simulator.routes import cancel_payment_intent
from getpaid_stripe.simulator.routes import cancel_refund
from getpaid_stripe.simulator.routes import capture_payment_intent
from getpaid_stripe.simulator.routes import create_checkout_session
from getpaid_stripe.simulator.routes import create_refund
from getpaid_stripe.simulator.routes import expire_checkout_session
from getpaid_stripe.simulator.routes import retrieve_checkout_session
from getpaid_stripe.simulator.routes import retrieve_payment_intent
from getpaid_stripe.simulator.routes import stripe_authorize_get
from getpaid_stripe.simulator.routes import stripe_authorize_post
from getpaid_stripe.simulator.routes import stripe_ops
from getpaid_stripe.simulator.transitions import STRIPE_TRANSITIONS


if TYPE_CHECKING:
    from collections.abc import Mapping


def load_provider_config(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = env if env is not None else os.environ
    return {
        "api_key": environment.get(
            "SIMULATOR_STRIPE_API_KEY",
            "sk_test_sim_stripe_key",
        ),
        "webhook_secret": environment.get(
            "SIMULATOR_STRIPE_WEBHOOK_SECRET",
            "whsec_sim_stripe_secret",
        ),
        "notify_url": environment.get("SIMULATOR_STRIPE_NOTIFY_URL", ""),
    }


def get_plugin() -> SimulatorProviderPlugin:
    return SimulatorProviderPlugin(
        api_version=SIMULATOR_PLUGIN_API_VERSION,
        slug="stripe",
        display_name="Stripe",
        api_handlers=(
            create_checkout_session,
            retrieve_checkout_session,
            expire_checkout_session,
            retrieve_payment_intent,
            capture_payment_intent,
            cancel_payment_intent,
            create_refund,
            cancel_refund,
        ),
        ui_handlers=(
            stripe_authorize_get,
            stripe_authorize_post,
            stripe_ops,
        ),
        transitions=STRIPE_TRANSITIONS,
        load_config=load_provider_config,
        authorize_path_template="/sim/stripe/authorize/{entity_id}",
    )
