"""Plugin-local types and constants (SPEC §2)."""

from typing import Literal
from typing import TypedDict


#: Pre-auth opt-in: config setting or prepare_transaction() kwarg.
CaptureMethod = Literal["automatic", "manual"]

#: Prefixes a usable secret API key may carry.
API_KEY_PREFIXES = ("sk_test_", "sk_live_", "rk_")

#: Prefixes that mark a test-mode (sandbox) key.
SANDBOX_KEY_PREFIXES = ("sk_test_", "rk_test_")


class StripeProviderData(TypedDict, total=False):
    """Keys this plugin reads/writes in ``payment.provider_data``."""

    session_id: str
    expires_at: int | None
    locked_at: int | None
    refund_id: str
    status: str
    payment_status: str
    payment_intent_id: str
    cancellation_reason: str | None
