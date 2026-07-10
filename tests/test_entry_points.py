"""Tests for getpaid.backends entry point registration (SPEC §11.10)."""

from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import PluginRegistry


class TestEntryPoints:
    """Verify entry points are correctly registered."""

    def test_stripe_backend_entry_point(self):
        """StripeProcessor must be discoverable via entry points."""
        registry = PluginRegistry()
        registry.discover()

        processor_class = registry.get_by_slug("stripe")
        assert issubclass(processor_class, BaseProcessor)
        assert processor_class.slug == "stripe"
        assert processor_class.display_name == "Stripe"

    def test_stripe_accepted_currencies(self):
        """StripeProcessor must list supported currencies."""
        registry = PluginRegistry()
        registry.discover()

        processor_class = registry.get_by_slug("stripe")
        assert len(processor_class.accepted_currencies) > 100
        assert "PLN" in processor_class.accepted_currencies
        assert "USD" in processor_class.accepted_currencies

    def test_registry_selects_stripe_for_currency(self):
        """Core's registry filters by `currency in accepted_currencies`."""
        registry = PluginRegistry()
        registry.discover()

        slugs = [
            backend.slug for backend in registry.get_for_currency("PLN")
        ]
        assert "stripe" in slugs
