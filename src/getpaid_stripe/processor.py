"""Stripe payment processor."""

from typing import ClassVar

from getpaid_core.processor import BaseProcessor


class StripeProcessor(BaseProcessor):
    """Stripe payment gateway processor."""

    slug: ClassVar[str] = "stripe"
    display_name: ClassVar[str] = "Stripe"
