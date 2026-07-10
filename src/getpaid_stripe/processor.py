"""Stripe payment processor."""

import logging
import time
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import cast

import stripe
from getpaid_core.exceptions import CredentialsError
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Payment
from getpaid_core.types import TransactionResult
from stripe import StripeClient

from .currencies import STRIPE_PRESENTMENT_CURRENCIES
from .currencies import to_minor


if TYPE_CHECKING:
    from stripe.checkout import SessionService


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

    def _get_client(self) -> StripeClient:
        """Build a StripeClient from processor config.

        ``api_base`` and ``http_client`` are internal hooks for tests
        and the simulator; they are not part of the public schema.
        """
        client_kwargs: dict[str, Any] = {
            "max_network_retries": int(
                self.get_setting("max_network_retries", 2)
            ),
        }
        api_base = self.get_setting("api_base")
        if api_base:
            client_kwargs["base_addresses"] = {"api": str(api_base)}
        http_client = self.get_setting("http_client")
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        return StripeClient(
            str(self.get_setting("api_key")), **client_kwargs
        )

    def _resolve_url(self, template: str) -> str:
        """Format ``{payment_id}``; leave Stripe's own
        ``{CHECKOUT_SESSION_ID}`` placeholder untouched."""
        return template.replace("{payment_id}", str(self.payment.id))

    def _capture_method(self, **kwargs) -> str:
        method = str(
            kwargs.get("capture_method")
            or self.get_setting("capture_method", "automatic")
        )
        if method not in ("automatic", "manual"):
            raise ValueError(
                f"capture_method must be 'automatic' or 'manual', "
                f"got {method!r}"
            )
        return method

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        """Create a payment-mode Checkout Session and return the
        redirect (SPEC §6)."""
        currency = self.payment.currency
        payment_id = str(self.payment.id)

        success_url = kwargs.get("success_url") or self.get_setting(
            "success_url"
        )
        cancel_url = kwargs.get("cancel_url") or self.get_setting(
            "cancel_url"
        )
        if not success_url or not cancel_url:
            raise ValueError(
                "Stripe processor requires 'success_url' and 'cancel_url' "
                "settings (or per-call kwargs)."
            )

        product_name = str(
            kwargs.get("product_name")
            or self.payment.description
            or f"Payment {payment_id}"
        )

        # Correlation, belt and braces: client_reference_id, session
        # metadata, and payment_intent_data metadata (the latter is the
        # only one Stripe copies onto the PaymentIntent and Charge).
        payment_intent_data: dict[str, Any] = {
            "metadata": {"payment_id": payment_id},
        }
        if self._capture_method(**kwargs) == "manual":
            payment_intent_data["capture_method"] = "manual"

        params: dict[str, Any] = {
            "mode": "payment",
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": to_minor(
                            self.payment.amount_required, currency
                        ),
                        "product_data": {"name": product_name},
                    },
                }
            ],
            "success_url": self._resolve_url(str(success_url)),
            "cancel_url": self._resolve_url(str(cancel_url)),
            "client_reference_id": payment_id,
            "metadata": {"payment_id": payment_id},
            "payment_intent_data": payment_intent_data,
        }

        expires_in = self.get_setting("session_expires_in")
        if expires_in is not None:
            params["expires_at"] = int(time.time()) + int(expires_in) * 60

        client = self._get_client()
        session = await client.checkout.sessions.create_async(
            cast("SessionService.CreateParams", params)
        )

        return TransactionResult(
            method="GET",
            redirect_url=session.url,
            external_id=session.id,
            provider_data={
                "session_id": session.id,
                "expires_at": session.expires_at,
            },
        )

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Verify the Stripe-Signature webhook header (SPEC §7).

        Requires the raw request body: framework adapters must pass
        ``raw_body``. v2 "thin" payloads are rejected — this plugin is
        v1-events-only.

        :raises InvalidCallbackError: On missing raw body / header or
            any signature, tolerance or payload-shape failure.
        """
        raw_body = kwargs.get("raw_body")
        if raw_body is None:
            raise InvalidCallbackError(
                "Missing raw_body in callback kwargs. Stripe signatures "
                "are computed over the raw HTTP body; the framework "
                "adapter must pass it through."
            )

        signature = ""
        for key, value in headers.items():
            if key.lower() == "stripe-signature":
                signature = value
                break
        if not signature:
            raise InvalidCallbackError(
                "Missing Stripe-Signature header in webhook request."
            )

        try:
            event = stripe.Webhook.construct_event(
                raw_body,
                signature,
                str(self.get_setting("webhook_secret")),
            )
            # Newer SDKs raise ValueError for v2 "thin" payloads inside
            # construct_event; stripe 12.x does not, so guard explicitly.
            if event.object != "event":
                raise ValueError(
                    "not a v1 snapshot event payload "
                    f"(object={event.object!r}); this plugin is "
                    "v1-events-only"
                )
        except stripe.SignatureVerificationError as exc:
            logger.error(
                "Stripe webhook bad signature for payment %s: %s",
                self.payment.id,
                exc,
            )
            raise InvalidCallbackError(f"BAD SIGNATURE: {exc}") from exc
        except ValueError as exc:
            raise InvalidCallbackError(
                f"Invalid webhook payload: {exc}"
            ) from exc
