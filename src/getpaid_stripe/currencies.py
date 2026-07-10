"""Stripe-specific amount/currency conversion (SPEC §5).

Stripe's per-currency exponents deviate from ISO 4217: ISK and UGX
are zero-decimal in ISO but the Stripe API requires an exponent-2
representation whose fraction is always ``00``. Never derive this
table from a generic ISO 4217 source.
"""

from decimal import ROUND_HALF_UP
from decimal import Decimal


#: Currencies whose Stripe amount is a whole number of major units.
STRIPE_ZERO_DECIMAL = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)

#: Three-decimal currencies; the last minor-unit digit must be 0.
STRIPE_THREE_DECIMAL = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})

#: Zero-decimal in ISO 4217 but represented as two-decimal in the
#: Stripe API, with the decimal amount always ``00`` (whole units only).
STRIPE_WHOLE_UNIT_TWO_DECIMAL = frozenset({"ISK", "UGX"})

#: Broad default list of Stripe presentment currencies. Availability
#: is per account country — subclass the processor to narrow. Uppercase
#: because core's registry compares ``payment.currency`` via ``in``.
STRIPE_PRESENTMENT_CURRENCIES: tuple[str, ...] = (
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG",
    "AZN", "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND",
    "BOB", "BRL", "BSD", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF",
    "CLP", "CNY", "COP", "CRC", "CVE", "CZK", "DJF", "DKK", "DOP",
    "DZD", "EGP", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GIP",
    "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR",
    "ILS", "INR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR",
    "KMF", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD",
    "LSL", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MUR",
    "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK",
    "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN",
    "PYG", "QAR", "RON", "RSD", "RWF", "SAR", "SBD", "SCR", "SEK",
    "SGD", "SHP", "SLE", "SOS", "SRD", "STD", "SZL", "THB", "TJS",
    "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD",
    "UYU", "UZS", "VND", "VUV", "WST", "XAF", "XCD", "XOF", "XPF",
    "YER", "ZAR", "ZMW",
)


def stripe_exponent(currency: str) -> int:
    """Return the Stripe API exponent for a currency code."""
    code = currency.upper()
    if code in STRIPE_ZERO_DECIMAL:
        return 0
    if code in STRIPE_THREE_DECIMAL:
        return 3
    return 2


def to_minor(amount: Decimal, currency: str) -> int:
    """Convert a major-unit ``Decimal`` to Stripe integer minor units."""
    code = currency.upper()
    exponent = stripe_exponent(code)
    if code in STRIPE_WHOLE_UNIT_TWO_DECIMAL and amount % 1 != 0:
        raise ValueError(
            f"{code} amounts must be whole units "
            f"(Stripe forbids fractions); got {amount}"
        )
    scaled = amount.scaleb(exponent)
    if exponent == 3:
        # Round to the nearest ten minor units (last digit must be 0).
        tens = scaled.scaleb(-1).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(tens.scaleb(1))
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> Decimal:
    """Convert Stripe integer minor units to an exact ``Decimal``.

    Always call with the payload's own ``currency`` field.
    """
    return Decimal(minor).scaleb(-stripe_exponent(currency))
