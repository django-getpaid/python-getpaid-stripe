"""Stripe payment processor."""

import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import cast

import stripe
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import PaymentEvent
from getpaid_core.exceptions import ChargeFailure
from getpaid_core.exceptions import CredentialsError
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.exceptions import LockFailure
from getpaid_core.exceptions import RefundFailure
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Payment
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult
from stripe import StripeClient

from .currencies import STRIPE_PRESENTMENT_CURRENCIES
from .currencies import from_minor
from .currencies import to_minor


if TYPE_CHECKING:
    from stripe import PaymentIntentService
    from stripe import RefundService
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

    def _reject_foreign_payment(self, obj: dict) -> None:
        """Raise when a payload is correlated to a different payment.

        Uses the belt-and-braces correlation written at session
        creation: object metadata and (sessions) client_reference_id.
        """
        my_id = str(self.payment.id)
        metadata = obj.get("metadata") or {}
        candidates = [
            metadata.get("payment_id"),
            obj.get("client_reference_id"),
        ]
        for candidate in candidates:
            if candidate and str(candidate) != my_id:
                raise InvalidCallbackError(
                    "Webhook payload is correlated to a different "
                    f"payment: got {candidate!r}, expected {my_id!r}."
                )

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> PaymentUpdate | None:
        """Map a verified Stripe event to a semantic update (SPEC §7).

        ``payment_intent.*`` is authoritative for money-state; refunds
        are status-driven off the Refund payload; everything unmapped
        is logged and ignored (subscription-born traffic on a shared
        account lands here by design).
        """
        event_type = str(data.get("type", ""))
        event_id = data.get("id")
        obj: dict = (data.get("data") or {}).get("object") or {}
        created = data.get("created")

        if event_type in (
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "payment_intent.created",
            "payment_intent.processing",
            "payment_intent.requires_action",
            "payment_intent.partially_funded",
        ) or event_type.startswith("charge."):
            # Duplicates of PI/Refund truth — deliberately ignored.
            logger.debug(
                "Ignoring stripe event %s (%s)", event_type, event_id
            )
            return None

        if event_type.startswith("checkout.session."):
            self._reject_foreign_payment(obj)
            return self._map_session_event(event_type, obj, event_id)
        if event_type.startswith("payment_intent."):
            self._reject_foreign_payment(obj)
            return self._map_payment_intent_event(
                event_type, obj, event_id, created
            )
        if event_type.startswith("refund."):
            self._reject_foreign_payment(obj)
            return self._map_refund_event(obj, event_id)
        if event_type.startswith("review."):
            return self._map_review_event(event_type, obj, event_id)

        logger.info(
            "Ignoring unmapped stripe event %s (%s)", event_type, event_id
        )
        return None

    def _map_session_event(
        self, event_type: str, obj: dict, event_id: str | None
    ) -> PaymentUpdate | None:
        session_data = {
            "session_id": obj.get("id"),
            "payment_status": obj.get("payment_status"),
        }
        if event_type == "checkout.session.completed":
            # No payment event: the PI twin carries the money-state.
            # This is where external_id gets promoted cs_… → pi_….
            return PaymentUpdate(
                external_id=obj.get("payment_intent"),
                provider_event_id=event_id,
                provider_data=session_data,
            )
        if event_type == "checkout.session.expired":
            # The only abandonment signal — no PI may exist at all.
            return PaymentUpdate(
                payment_event=PaymentEvent.FAILED,
                external_id=obj.get("payment_intent"),
                provider_event_id=event_id,
                provider_data=session_data,
            )
        logger.info("Ignoring stripe event %s (%s)", event_type, event_id)
        return None

    def _map_payment_intent_event(
        self,
        event_type: str,
        obj: dict,
        event_id: str | None,
        created: int | None,
    ) -> PaymentUpdate | None:
        currency = str(obj.get("currency", ""))
        external_id = obj.get("id")

        if event_type == "payment_intent.amount_capturable_updated":
            return PaymentUpdate(
                payment_event=PaymentEvent.LOCKED,
                locked_amount=from_minor(
                    int(obj.get("amount_capturable", 0)), currency
                ),
                external_id=external_id,
                provider_event_id=event_id,
                provider_data={"locked_at": created},
            )
        if event_type == "payment_intent.succeeded":
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=from_minor(
                    int(obj.get("amount_received", 0)), currency
                ),
                external_id=external_id,
                provider_event_id=event_id,
            )
        if event_type == "payment_intent.payment_failed":
            return PaymentUpdate(
                payment_event=PaymentEvent.FAILED,
                external_id=external_id,
                provider_event_id=event_id,
            )
        if event_type == "payment_intent.canceled":
            # Manual capture: cancel *is* releasing the lock.
            # Automatic: a canceled intent is a failed payment.
            manual = obj.get("capture_method") == "manual"
            return PaymentUpdate(
                payment_event=(
                    PaymentEvent.LOCK_RELEASED
                    if manual
                    else PaymentEvent.FAILED
                ),
                external_id=external_id,
                provider_event_id=event_id,
                provider_data={
                    "cancellation_reason": obj.get("cancellation_reason"),
                },
            )
        logger.info("Ignoring stripe event %s (%s)", event_type, event_id)
        return None

    def _map_refund_event(
        self, obj: dict, event_id: str | None
    ) -> PaymentUpdate | None:
        """Status-driven: the refund's ``status`` decides, not which
        of refund.created/updated/failed delivered it."""
        status = str(obj.get("status", ""))
        external_id = obj.get("payment_intent")
        provider_data = {"refund_id": obj.get("id")}

        if status in ("pending", "requires_action"):
            return PaymentUpdate(
                payment_event=PaymentEvent.REFUND_REQUESTED,
                external_id=external_id,
                provider_event_id=event_id,
                provider_data=provider_data,
            )
        if status == "succeeded":
            return PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=from_minor(
                    int(obj.get("amount", 0)), str(obj.get("currency", ""))
                ),
                external_id=external_id,
                provider_event_id=event_id,
                provider_data=provider_data,
            )
        if status in ("failed", "canceled"):
            return PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CANCELLED,
                external_id=external_id,
                provider_event_id=event_id,
                provider_data=provider_data,
            )
        logger.info(
            "Ignoring refund event with status %r (%s)", status, event_id
        )
        return None

    async def fetch_payment_status(self, **kwargs) -> PaymentUpdate | None:
        """PULL flow: retrieve the Session or PaymentIntent (SPEC §7).

        Same mapping rules as the webhooks, stateless, without a
        ``provider_event_id``. This is the deterministic auth-expiry
        backstop (SPEC §8) — polling cadence belongs to the
        application.
        """
        external_id = self.payment.external_id
        if not external_id:
            logger.info(
                "Payment %s has no external_id yet; nothing to fetch.",
                self.payment.id,
            )
            return None

        client = self._get_client()
        if external_id.startswith("cs_"):
            session = await client.checkout.sessions.retrieve_async(
                external_id
            )
            if session.status == "expired":
                return PaymentUpdate(
                    payment_event=PaymentEvent.FAILED,
                    provider_data={"session_id": session.id},
                )
            if session.status == "complete" and session.payment_intent:
                external_id = str(session.payment_intent)
            else:
                return None

        intent = await client.payment_intents.retrieve_async(external_id)
        return self._map_pulled_intent(intent)

    def _map_pulled_intent(
        self, intent: "stripe.PaymentIntent"
    ) -> PaymentUpdate | None:
        currency = str(intent.currency)
        if intent.status == "requires_capture":
            return PaymentUpdate(
                payment_event=PaymentEvent.LOCKED,
                locked_amount=from_minor(
                    intent.amount_capturable, currency
                ),
                external_id=intent.id,
            )
        if intent.status == "succeeded":
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=from_minor(intent.amount_received, currency),
                external_id=intent.id,
            )
        if intent.status == "canceled":
            manual = intent.capture_method == "manual"
            return PaymentUpdate(
                payment_event=(
                    PaymentEvent.LOCK_RELEASED
                    if manual
                    else PaymentEvent.FAILED
                ),
                external_id=intent.id,
                provider_data={
                    "cancellation_reason": intent.cancellation_reason,
                },
            )
        return None

    def _require_payment_intent_id(
        self, exc_class: type[Exception]
    ) -> str:
        """The invariant of SPEC §6: external_id is the id money
        operations act on. Before the promoting webhook it is still
        cs_… and no money operation is possible."""
        external_id = self.payment.external_id
        if not external_id or not external_id.startswith("pi_"):
            raise exc_class(
                "No PaymentIntent is bound to this payment yet "
                f"(external_id={external_id!r}). Money operations "
                "require the pi_… id delivered by the first webhook."
            )
        return external_id

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResult:
        """Capture a manually-authorized PaymentIntent (SPEC §8).

        Partial capture: Stripe auto-releases the remainder. Always
        returns ``async_call=True`` — core applies CHARGE_REQUESTED and
        the authoritative PAYMENT_CAPTURED arrives exactly once, via
        ``payment_intent.succeeded``.
        """
        intent_id = self._require_payment_intent_id(ChargeFailure)
        client = self._get_client()
        intent = await client.payment_intents.retrieve_async(intent_id)
        if intent.status != "requires_capture":
            raise ChargeFailure(
                f"PaymentIntent {intent_id} is not capturable: status is "
                f"{intent.status!r}, expected 'requires_capture'."
            )

        currency = str(intent.currency)
        params: dict[str, Any] = {}
        if amount is not None:
            params["amount_to_capture"] = to_minor(amount, currency)
        try:
            captured = await client.payment_intents.capture_async(
                intent_id,
                cast("PaymentIntentService.CaptureParams", params),
            )
        except stripe.StripeError as exc:
            raise ChargeFailure(
                f"Stripe capture failed for {intent_id}: {exc}"
            ) from exc

        return ChargeResult(
            amount_charged=from_minor(captured.amount_received, currency),
            success=captured.status in ("succeeded", "processing"),
            async_call=True,
            provider_data={
                "payment_intent_id": captured.id,
                "status": captured.status,
            },
        )

    async def release_lock(self, **kwargs) -> Decimal:
        """Cancel a manually-authorized PaymentIntent (SPEC §8).

        Legal only at ``requires_capture`` — that *is* the lock state;
        no partial release exists (use a partial ``charge()``). Returns
        the full released amount; ``payment_intent.canceled`` confirms
        LOCK_RELEASED in the FSM.
        """
        intent_id = self._require_payment_intent_id(LockFailure)
        client = self._get_client()
        intent = await client.payment_intents.retrieve_async(intent_id)
        if intent.status != "requires_capture":
            raise LockFailure(
                f"PaymentIntent {intent_id} holds no releasable lock: "
                f"status is {intent.status!r}, expected "
                "'requires_capture'."
            )

        locked = from_minor(intent.amount_capturable, str(intent.currency))
        try:
            await client.payment_intents.cancel_async(intent_id)
        except stripe.StripeError as exc:
            raise LockFailure(
                f"Stripe cancel failed for {intent_id}: {exc}"
            ) from exc
        return locked

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> RefundResult:
        """Create a refund against the PaymentIntent (SPEC §9).

        Omitted amount = full refund. ``reason`` is deliberately not
        exposed ('fraudulent' has block-list side effects).
        """
        intent_id = self._require_payment_intent_id(RefundFailure)
        params: dict[str, Any] = {
            "payment_intent": intent_id,
            # refund metadata does not inherit — set at creation
            "metadata": {"payment_id": str(self.payment.id)},
        }
        if amount is not None:
            params["amount"] = to_minor(amount, self.payment.currency)

        client = self._get_client()
        try:
            refund = await client.refunds.create_async(
                cast("RefundService.CreateParams", params)
            )
        except stripe.StripeError as exc:
            raise RefundFailure(
                f"Stripe refund failed for {intent_id}: {exc}"
            ) from exc

        return RefundResult(
            amount=from_minor(refund.amount, str(refund.currency)),
            provider_data={
                # paynow convention; the slot holds the latest refund
                "refund_id": refund.id,
                "status": refund.status,
            },
        )

    async def cancel_refund(self, **kwargs) -> bool:
        """Cancel a refund awaiting customer action (SPEC §9).

        Only refunds in ``requires_action`` (bank-transfer-style
        methods) are API-cancelable; **card refunds are Dashboard-only
        and this effectively always returns False for card payments.**
        """
        refund_id = self.payment.provider_data.get("refund_id")
        if not refund_id:
            raise RefundFailure(
                "Missing refund identifier. Expected "
                'provider_data["refund_id"] set by start_refund().'
            )
        client = self._get_client()
        try:
            await client.refunds.cancel_async(str(refund_id))
        except stripe.StripeError as exc:
            logger.info(
                "Stripe refused to cancel refund %s: %s", refund_id, exc
            )
            return False
        return True

    def _map_review_event(
        self, event_type: str, obj: dict, event_id: str | None
    ) -> PaymentUpdate | None:
        external_id = obj.get("payment_intent")
        if event_type == "review.opened":
            return PaymentUpdate(
                fraud_event=FraudEvent.REVIEW,
                external_id=external_id,
                provider_event_id=event_id,
            )
        if event_type == "review.closed":
            closed_reason = str(obj.get("closed_reason") or "")
            # TODO(SPEC §12): verify closed_reason semantics against
            # live test mode; "approved" is documented, the rest are
            # treated as rejection.
            return PaymentUpdate(
                fraud_event=(
                    FraudEvent.ACCEPT
                    if closed_reason == "approved"
                    else FraudEvent.REJECT
                ),
                fraud_message=closed_reason,
                external_id=external_id,
                provider_event_id=event_id,
            )
        logger.info("Ignoring stripe event %s (%s)", event_type, event_id)
        return None
