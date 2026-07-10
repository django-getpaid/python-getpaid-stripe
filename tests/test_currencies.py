"""Tests for Stripe-specific currency conversion (SPEC §5)."""

from decimal import Decimal

import pytest

from getpaid_stripe.currencies import STRIPE_PRESENTMENT_CURRENCIES
from getpaid_stripe.currencies import STRIPE_THREE_DECIMAL
from getpaid_stripe.currencies import STRIPE_ZERO_DECIMAL
from getpaid_stripe.currencies import from_minor
from getpaid_stripe.currencies import stripe_exponent
from getpaid_stripe.currencies import to_minor


def test_to_minor_two_decimal_pln():
    assert to_minor(Decimal("123.45"), "PLN") == 12345


def test_to_minor_zero_decimal_jpy():
    # "to charge 500 JPY, provide an amount value of 500"
    assert to_minor(Decimal("500"), "JPY") == 500


def test_to_minor_rounds_half_up_not_bankers():
    # quantize() defaults to banker's rounding which would give 1000
    assert to_minor(Decimal("10.005"), "PLN") == 1001


def test_to_minor_zero_decimal_rounds_half_up():
    assert to_minor(Decimal("500.5"), "JPY") == 501


def test_to_minor_three_decimal_kwd_rounds_to_nearest_ten():
    # 5.124 KWD must land on a multiple of 10 minor units
    assert to_minor(Decimal("5.124"), "KWD") == 5120
    assert to_minor(Decimal("5.125"), "KWD") == 5130
    assert to_minor(Decimal("5.12"), "KWD") == 5120


@pytest.mark.parametrize("code", ["ISK", "UGX", "isk", "ugx"])
def test_to_minor_isk_ugx_two_decimal_api_quirk(code):
    # "to charge 5 ISK, provide an amount value of 500" — exponent 2,
    # decimal part always 00, despite ISO 4217 saying exponent 0.
    assert to_minor(Decimal("5"), code) == 500


@pytest.mark.parametrize("code", ["ISK", "UGX"])
def test_to_minor_isk_ugx_rejects_fractions(code):
    # "You can't charge fractions of ISK."
    with pytest.raises(ValueError):
        to_minor(Decimal("5.50"), code)


def test_to_minor_accepts_lowercase_codes():
    # Stripe payload currency fields are lowercase ISO codes
    assert to_minor(Decimal("500"), "jpy") == 500
    assert to_minor(Decimal("1.23"), "pln") == 123


def test_from_minor_is_exact_never_via_float():
    assert from_minor(12345, "PLN") == Decimal("123.45")
    assert from_minor(500, "JPY") == Decimal("500")
    assert from_minor(5120, "KWD") == Decimal("5.120")
    assert from_minor(500, "ISK") == Decimal("5.00")


def test_from_minor_uses_payload_lowercase_currency():
    assert from_minor(999, "eur") == Decimal("9.99")


@pytest.mark.parametrize(
    "code",
    sorted(STRIPE_ZERO_DECIMAL | STRIPE_THREE_DECIMAL | {"PLN", "ISK", "UGX"}),
)
def test_round_trip_across_exponent_table(code):
    exponent = stripe_exponent(code)
    # A representable amount in this currency: 7 whole units plus the
    # smallest chargeable step (10 minor units for three-decimal).
    step = Decimal(10 if exponent == 3 else 1).scaleb(-exponent)
    amount = Decimal(7) + (step if code not in ("ISK", "UGX") else 0)
    assert from_minor(to_minor(amount, code), code) == amount


def test_presentment_currencies_uppercase_and_broad():
    assert "PLN" in STRIPE_PRESENTMENT_CURRENCIES
    assert "USD" in STRIPE_PRESENTMENT_CURRENCIES
    assert "JPY" in STRIPE_PRESENTMENT_CURRENCIES
    assert "EUR" in STRIPE_PRESENTMENT_CURRENCIES
    # core registry compares payment.currency (uppercase) via `in`
    assert all(c == c.upper() for c in STRIPE_PRESENTMENT_CURRENCIES)
    assert len(STRIPE_PRESENTMENT_CURRENCIES) > 100
