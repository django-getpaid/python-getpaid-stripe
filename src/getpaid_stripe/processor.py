"""Stripe payment processor."""

import logging
from typing import Any
from typing import ClassVar

from getpaid_core.exceptions import CredentialsError
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Payment
from getpaid_core.types import TransactionResult

from .currencies import STRIPE_PRESENTMENT_CURRENCIES


logger = logging.getLogger(__name__)

API_KEY_PREFIXES = ("sk_test_", "sk_live_", "rk_")
SANDBOX_KEY_PREFIXES = ("sk_test_", "rk_test_")


class StripeProcessor(BaseProcessor):
    """Stripe payment gateway processor (Checkout Sessions, SPEC §1).

    Wraps payment-mode Stripe Checkout Sessions: create session ->
    redirect -> webhooks drive the FSM. ``payment_intent.*`` events are
    authoritative for money-state. Supports manual capture (pre-auth),
    refunds, Radar fraud events and pull-status.
    """

    slug: ClassVar[str] = "stripe"
    display_name: ClassVar[str] = "Stripe"
    accepted_currencies: ClassVar[tuple[str, ...]] = (
        STRIPE_PRESENTMENT_CURRENCIES
    )
    logo_url: ClassVar[str] = "https://stripe.com/img/v3/home/twitter.png"
    # Stripe is always api.stripe.com; test vs live mode is carried by
    # the API key, so get_paywall_baseurl() is not used (SPEC §3).
    sandbox_url: ClassVar[str] = ""
    production_url: ClassVar[str] = ""

    def __init__(
        self, payment: Payment, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(payment, config)
        api_key = str(self.get_setting("api_key") or "")
        if not api_key.startswith(API_KEY_PREFIXES):
            raise CredentialsError(
                "Stripe processor is misconfigured: 'api_key' is missing "
                "or has no known prefix (sk_test_/sk_live_/rk_). Refusing "
                "to start with blank or malformed credentials."
            )
        webhook_secret = str(self.get_setting("webhook_secret") or "")
        if not webhook_secret.startswith("whsec_"):
            raise CredentialsError(
                "Stripe processor is misconfigured: 'webhook_secret' is "
                "missing or not a whsec_… value. The payment flow is "
                "driven by webhooks and unusable without it."
            )

    @property
    def is_sandbox(self) -> bool:
        """Test vs live mode, derived from the API key prefix."""
        api_key = str(self.get_setting("api_key") or "")
        return api_key.startswith(SANDBOX_KEY_PREFIXES)

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        raise NotImplementedError
