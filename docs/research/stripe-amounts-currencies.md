# Stripe integer minor-unit amounts vs getpaid-core's Decimal amounts

Scope note (ticket #4): this document collects the facts needed to convert
between getpaid-core's `Decimal` amounts and Stripe's integer minor-unit
`amount` fields — currency decimal classes (zero/two/three-decimal), special
cases, min/max charge amounts, safe `Decimal`↔`int` conversion patterns, the
constraint landscape for `accepted_currencies`, and amount semantics of
refunds and partial captures. Facts only, each claim cited to a primary
source (docs.stripe.com — fetched as `.md` where possible — stripe-python
source, ISO 4217 via its maintenance agency); the `accepted_currencies`
DECISION happens in a separate ticket. Webhook mechanics live in
[`stripe-webhooks.md`](stripe-webhooks.md), the surface choice in
[`checkout-vs-payment-intents.md`](checkout-vs-payment-intents.md).

All docs.stripe.com content quoted here was fetched 2026-07-10.

## 0. The local contract being converted from

- getpaid-core uses `Decimal` throughout: `BaseProcessor.charge(amount:
  Decimal | None = None, ...)`, `release_lock(**kwargs) -> Decimal`
  ("Release pre-authorized lock. Return released amount.") and
  `accepted_currencies: ClassVar[Sequence[str]] = ()` on the processor class
  (`python-getpaid-core/src/getpaid_core/processor.py`).
- The registry filters processors with a plain membership test:
  `get_for_currency` keeps a backend `if currency in
  backend.accepted_currencies`, and `get_all_currencies()` does
  `currencies.update(backend.accepted_currencies)`
  (`python-getpaid-core/src/getpaid_core/registry.py`). Consequence (local
  fact, relevant to the later decision): an empty sequence means "matches no
  currency", and a `None` value would raise `TypeError` in both call sites —
  "unrestricted" cannot be expressed as `None` without changing core.
- Sibling precedent (`python-getpaid-paynow/src/getpaid_paynow/client.py`):

  ```python
  @staticmethod
  def _to_lowest_unit(amount: Decimal) -> int:
      """Convert a Decimal amount to integer lowest currency unit,
      rounding half-up to avoid silent truncation."""
      return int(
          (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
      )

  @staticmethod
  def _from_lowest_unit(amount: int) -> Decimal:
      """Convert integer lowest currency unit to Decimal."""
      return Decimal(amount) / 100
  ```

  The `* 100` is unconditional; that is safe there only because paynow's
  `accepted_currencies` is PLN/EUR/USD/GBP
  (`getpaid_paynow/types.py`, `processor.py`), all two-decimal. A Stripe
  plugin accepting arbitrary currencies cannot hard-code 100.

## 1. Stripe's amount model: integers in the currency's minor unit

- "All API requests expect `amount` values in the *currency's minor unit* …
  For example, set `amount` as follows: `1000` to charge 10 USD (or any
  other two-decimal currency). `10` to charge 10 JPY (or any other
  zero-decimal currency)."
  ([Supported currencies](https://docs.stripe.com/currencies.md)).
- "Currencies are two-decimal currencies unless otherwise specified."
  ([Supported currencies](https://docs.stripe.com/currencies.md)).
- `currency` is the "Three-letter ISO currency code, in lowercase. Must be a
  supported currency" ([Create a
  PaymentIntent](https://docs.stripe.com/api/payment_intents/create.md));
  "Make sure to use all lowercase letters when entering the three-letter ISO
  code in any payment request"
  ([Supported currencies](https://docs.stripe.com/currencies.md)).
- stripe-python types every amount as `int`: `PaymentIntent.amount: int`
  ("A positive integer representing how much to charge in the smallest
  currency unit"), `amount_capturable: int` ("Amount that can be captured
  from this PaymentIntent"), `amount_received: int` ("Amount that this
  PaymentIntent collects")
  ([stripe/_payment_intent.py](https://github.com/stripe/stripe-python/blob/master/stripe/_payment_intent.py));
  `Refund.amount: int` ("Amount, in cents (or local equivalent)")
  ([stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py));
  Checkout Session `amount_total` / `amount_subtotal` are nullable integers
  ([Checkout Session object](https://docs.stripe.com/api/checkout/sessions/object.md)).
- One documented decimal-string escape hatch exists on price data:
  `line_items.price_data.unit_amount_decimal` — "Same as `unit_amount`, but
  accepts a decimal value in the smallest currency unit with at most 12
  decimal places. Only one of `unit_amount` and `unit_amount_decimal` can be
  set" ([Create a Checkout
  Session](https://docs.stripe.com/api/checkout/sessions/create.md)). It is
  still denominated in *minor units* (a decimal number of cents), so it does
  not remove the per-currency exponent problem.

## 2. Currency decimal classes

### Zero-decimal currencies (exponent 0)

"For the following zero-decimal currencies, the charge and the amount are
the same, without requiring multiplication. For example, to charge 500 JPY,
provide an `amount` value of `500`." Full list on the live page
([Supported currencies](https://docs.stripe.com/currencies), HTML render;
the `.md` export omits the list table):

> BIF, CLP, DJF, GNF, JPY, KMF, KRW, MGA, PYG, RWF, UGX, VND, VUV, XAF,
> XOF, XPF

Caveat from the same section: "This list contains zero-decimal currencies
that have general API support. Currencies listed here might not be available
in your specific country."

Note the internal inconsistency: UGX appears in this zero-decimal list *and*
in the special-cases table below, which mandates a two-decimal
representation. The special-case wording is the operative API rule (see
Flagged gaps).

### Three-decimal currencies (exponent 3): BHD, JOD, KWD, OMR, TND

Historical wording (Wayback Machine snapshot of stripe.com/docs/currencies,
2022-12-31 — see Flagged gaps for why an archive is cited):

> "The API supports three-decimal currencies for the standard payment
> flows, including Payment Intents, Refunds, and Disputes. However, to
> ensure compatibility with Stripe's payments partners, these API calls
> require the least-significant (last) digit to be 0. Your integration must
> round amounts to the nearest ten. For example, 5.124 KWD must be rounded
> to `5120` or `5130`. Three-decimal currencies: BHD JOD KWD OMR TND"
> ([archived Supported currencies](https://web.archive.org/web/20221231002856/https://stripe.com/docs/currencies)).

So for three-decimal currencies the amount is an integer count of
thousandths (5.124 KWD → nominally `5124`), but the last digit **must be 0**
— i.e. the amount must be a multiple of 10 minor units, and the rule applies
to refunds too. The five codes remain live presentment currencies today:
they appear in the presentment-currency table on the current page when
rendered for a UAE account
([Supported currencies?country=AE](https://docs.stripe.com/currencies?country=AE)),
and in FX Quotes API rate tables ("aed, … bhd, … jod, … kwd, … omr, …"
in [The FX Quotes API](https://docs.stripe.com/payments/currencies/localize-prices/fx-quotes-api.md)),
and the page metadata still advertises "zero-decimal and three-decimal
currency support".

### Special cases (exact documented semantics)

From the "Special cases" table — "The following currencies have special
conditions that you need to consider when creating payouts or charges"
([Supported currencies](https://docs.stripe.com/currencies.md)):

| Currency | Documented rule (verbatim) |
| --- | --- |
| ISK | "ISK transitioned to a zero-decimal currency, but backward compatibility requires you to represent it as a two-decimal value, where the decimal amount is always `00`. For example, to charge 5 ISK, provide an `amount` value of `500`. You can't charge fractions of ISK." |
| HUF | "Stripe treats HUF as a zero-decimal currency for payouts, even though you can charge two-decimal amounts. When you create a manual payout in HUF, you must provide integer amounts that are evenly divisible by 100. For example, if you have an available balance of HUF 10.45, you can pay out HUF 10 by submitting `1000` for the `amount` value. You can't submit a payout for the full balance, HUF 10.45, because the `amount` value of `1045` isn't evenly divisible by 100." |
| TWD | "Stripe treats TWD as a zero-decimal currency for payouts, even though you can charge two-decimal amounts. When you create a manual payout in TWD, you must provide integer amounts that are evenly divisible by 100. For example, if you have an available balance of TWD 800.45, you can pay out TWD 800 by submitting `80000` for the `amount` value. You can't submit a payout for the full balance, TWD 800.45, because the `amount` value of `80045` isn't evenly divisible by 100." |
| UGX | "UGX transitioned to a zero-decimal currency, but backwards compatibility requires you to represent it as a two-decimal value, where the decimal amount is always `00`. For example, to charge 5 UGX, provide an `amount` value of `500`. You can't charge fractions of UGX. For invoices where the `amount` is fractional after prorations, coupons, or taxes, Stripe automatically rounds that amount to the nearest number evenly divisible by 100. We credit or debit any difference from rounding to the customer balance." |

Plugin-relevant reading: for *charges* (the plugin's concern) HUF and TWD
are ordinary two-decimal currencies; the divisible-by-100 rule bites only
manual payouts, which the plugin does not create. ISK and UGX must be sent
as exponent-2 amounts that are multiples of 100 (whole units only).

### Stripe exponent ≠ ISO 4217 exponent

ISO 4217 minor units, from the maintenance agency's current list-one
([ISO 4217](https://www.iso.org/iso-4217-currency-codes.html) →
[SIX list-one.xml](https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml)):
ISK = 0, UGX = 0, HUF = 2, TWD = 2, JPY/KRW/VND/CLP = 0, BHD/JOD/KWD/OMR/TND
= 3, COP/IDR = 2.

For ISK and UGX, ISO says exponent 0 but Stripe's API requires an
exponent-2 representation ("decimal amount is always `00`"). A conversion
table derived blindly from ISO 4217 sends ISK/UGX amounts off by a factor of
100. The plugin therefore needs a **Stripe-specific exponent table**, not a
generic ISO 4217 one. (Conversely COP and IDR are two-decimal in both ISO
and Stripe, matching Stripe's zero-decimal list which contains neither.)

## 3. Minimum and maximum charge amounts

### Minimums

"Stripe enforces a minimum payment amount for all charges to make sure the
Stripe fee doesn't exceed your charge. The minimum amount you can charge
depends on the payout bank account settlement currency. … Charges requiring
conversion into your account's default settlement currency must meet the
equivalent minimum of the settlement currency"
([Supported currencies](https://docs.stripe.com/currencies.md)). Documented
per-currency minimums (major units): 0.50 USD, 2.00 AED, 0.50 ARS, 0.50
AUD, 0.50 BRL, 0.50 CAD, 0.50 CHF, 0.50 COP, 15.00 CZK, 2.50 DKK, 0.50 EUR,
0.30 GBP, 4.00 HKD, 175.00 HUF, 0.50 IDR, 0.50 ILS, 0.50 INR, 50 JPY, 50
KRW, 10 MXN, 2.00 MYR, 3.00 NOK, 0.50 NZD, 0.50 PHP, **2.00 PLN**, 2.00
RON, 0.50 RUB, 3.00 SEK, 0.50 SGD, 10 THB, 0.50 ZAR.

- Because the minimum is evaluated against the **settlement** currency after
  conversion, it is not statically checkable per presentment currency; the
  charge currency's own minimum is only the exact bound when it equals the
  settlement currency ("If you only have one bank account, the minimum
  amount shown applies to all charges in the same currency as the account").
- "Exceptions to the minimum charge amount apply to some payment methods,
  such as iDEAL (allows `amount` values as low as `1`)."
- "Subscription charges support zero-amount charges to account for coupons
  and free trials. However, any non-zero amount is still subject to the
  applicable minimum."

### Maximums

([Supported currencies](https://docs.stripe.com/currencies.md)):

- Card payments: "The `amount` value supports up to: 12 digits for most card
  payments, for a maximum of 999,999,999,999 in minor units; 9 digits for
  American Express in most currencies, for a maximum of 999,999,999 in minor
  units. Card networks can impose charge amount limits that are more
  restrictive". Japanese-account JCB/Diners/Discover: max 8 digits
  (99,999,999 JPY).
- Non-card payment methods: "12 digits for IDR … 10 digits for COP … 9
  digits for INR … 8 digits for all other currencies, for a maximum charge
  of 999,999.99 (`99999999`)". "Some payment methods enforce their own
  per-currency maximums that can be more restrictive."
- Inconsistency to be aware of: the PaymentIntent API reference still says
  "The amount value supports up to eight digits"
  ([Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create.md)),
  while the currencies page allows 12 for most cards. Do not hard-code
  either bound (see Flagged gaps).

## 4. Safe Decimal→int and int→Decimal conversion

### Decimal → int (outbound: create/capture/refund amounts)

Pattern implied by the above facts: per-currency exponent `e ∈ {0, 2, 3}`
from a Stripe-specific table, then `minor = amount × 10^e`, which must land
on an integer (and for three-decimal currencies on a multiple of 10; for
ISK/UGX on a multiple of 100).

Rounding facts (Python `decimal` stdlib, verifiable in
[the `decimal` docs](https://docs.python.org/3/library/decimal.html); the
worked example is arithmetic, not a Stripe claim):

- `Decimal` arithmetic is exact for these operations: `Decimal("10.005") *
  100 == Decimal("1000.500")` — no binary-float fuzz. The rounding question
  only arises when the input has more precision than the currency allows.
- `quantize(Decimal("1"))` defaults to `ROUND_HALF_EVEN` (banker's
  rounding): `1000.500 → 1000`. `ROUND_HALF_UP` gives `1001`. Plain
  `int(Decimal("1000.5"))` **truncates** to `1000`. The choice is
  observable: `Decimal("10.005")` in USD becomes 1000 or 1001 cents
  depending on mode.
- Going through `float` anywhere breaks exactness
  (`float("10.005") * 100 == 1000.4999999999999`); all paths must stay in
  `Decimal`/`int`.
- Sibling precedent: paynow uses `ROUND_HALF_UP` explicitly, commented
  "rounding half-up to avoid silent truncation"
  (`getpaid_paynow/client.py`).
- Alternative consistent with getpaid-core being the amount's source of
  truth: reject (raise) any `Decimal` that does not quantize exactly to the
  currency's minor unit instead of silently rounding — Stripe documents no
  server-side rounding for charges (it documents rounding only for
  UGX invoice prorations, quoted above), so a rounded amount silently
  charges a different value than the `Payment` record holds. This is a
  design option for the decision ticket, not a Stripe requirement.
- Three-decimal currencies need a second quantization step to a multiple of
  10 minor units ("Your integration must round amounts to the nearest ten",
  [archived Supported currencies](https://web.archive.org/web/20221231002856/https://stripe.com/docs/currencies))
  — e.g. quantize the major-unit amount to `Decimal("0.01")` before scaling
  by 10³, or quantize the minor-unit value to `Decimal("1E1")`.

### int → Decimal (inbound: webhook and API-response amounts)

`amount`, `amount_received`, `amount_capturable`
([PaymentIntent object](https://docs.stripe.com/api/payment_intents/object.md)),
`Refund.amount`
([stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py))
and Checkout's `amount_total`
([Checkout Session object](https://docs.stripe.com/api/checkout/sessions/object.md))
are all integers in the minor unit of the object's own `currency` field.
The inverse conversion is exact and needs no rounding mode:
`Decimal(minor).scaleb(-e)` (or `Decimal(minor) / (10 ** e)`, which is exact
for powers of ten in `decimal`). The paynow sibling's `Decimal(amount) /
100` is the two-decimal instance of this. Because every Stripe object
carries its `currency`, the inbound path should derive `e` from the event
payload's currency, not from the local payment record, and may assert both
match (the PaymentIntent currency is immutable once created; refunds are
denominated in the charge currency — see §6).

stripe-python also exposes `PresentmentDetails.presentment_amount: int` /
`presentment_currency: str` ("Amount intended to be collected by this
payment, denominated in `presentment_currency`" / "Currency presented to the
customer during payment") on PaymentIntents and Refunds
([stripe/_payment_intent.py](https://github.com/stripe/stripe-python/blob/master/stripe/_payment_intent.py),
[stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py))
— relevant only if a currency-conversion feature presents a different
currency than the integration currency.

## 5. `accepted_currencies` constraint landscape (facts for the decision ticket)

- Scale: "You can charge customers in over 135 currencies and receive funds
  in your preferred currency"
  ([Supported currencies](https://docs.stripe.com/currencies.md)).
- Three currency roles: the customer's payment-method currency; "the
  currency of the charge, called the *presentment* currency"; and "the
  currency accepted by your destination bank account or debit card, called
  the *settlement* currency"
  ([Supported currencies](https://docs.stripe.com/currencies.md)). "If the
  charge currency differs from your settlement currency, Stripe converts the
  charge to your settlement currency" — so an account can *charge* in any
  supported presentment currency regardless of its bank account currency;
  the cost is conversion, not rejection.
- The set of presentment currencies is **per account country**: the
  presentment table on the currencies page is country-selected, and the
  zero-decimal note warns "Currencies listed here might not be available in
  your specific country"
  ([Supported currencies](https://docs.stripe.com/currencies)). Concretely,
  the page rendered for Poland lists ~135 codes without BHD/JOD/KWD/OMR/TND,
  while the UAE render includes them
  ([Supported currencies?country=AE](https://docs.stripe.com/currencies?country=AE)).
- There **is** an API to query this: the Country Specs API. "Lists all
  Country Spec objects available in the API" via `GET /v1/country_specs`
  ([List Country Specs](https://docs.stripe.com/api/country_specs/list.md));
  each object has `default_currency` ("The default currency for this
  country. This applies to both payment methods and bank accounts") and
  `supported_payment_currencies` ("Currencies that can be accepted in the
  specified country (for payments)")
  ([Country Spec object](https://docs.stripe.com/api/country_specs/object.md)).
  A plugin could resolve the account's country (Accounts API) and fetch its
  `supported_payment_currencies` at runtime.
- Payment-method delegation interacts with currency: with
  `automatic_payment_methods[enabled]=true` "this PaymentIntent accepts
  payment methods that you enable in the Dashboard and that are compatible
  with this PaymentIntent's other parameters"
  ([Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create.md))
  — currency is such a parameter, so an unsupported-for-a-method currency
  narrows the offered methods rather than requiring plugin-side method
  logic. Per-method currency support is tabulated at
  [Payment method support](https://docs.stripe.com/payments/payment-methods/payment-method-support.md).
- Local constraint (from §0): getpaid-core's registry needs an iterable
  supporting `in`; empty means "never selectable"; `None` is not a valid
  value under the current core code. Stripe itself imposes no single static
  list — the real constraint is "presentment currencies of the account's
  country, queryable via `/v1/country_specs`, ultimately validated by the
  API at PaymentIntent creation".

## 6. Refunds and partial captures

### Refunds

- `amount` on refund creation: "A positive integer in the smallest currency
  unit representing how much of this charge to refund. Can refund only up
  to the remaining, unrefunded amount of the charge"
  ([Create a refund](https://docs.stripe.com/api/refunds/create.md)).
- "You can also refund only part of a PaymentIntent by specifying an
  `amount` … as an integer in cents (or the charge currency's smallest
  currency unit)" ([Refunds](https://docs.stripe.com/refunds.md)) — the
  refund is denominated in the **charge's** currency; there is no currency
  parameter on refund creation.
- The `Refund` object's `amount: int` / `currency: str` come back in webhook
  payloads (`refund.created`, `charge.refunded` — see
  [`stripe-webhooks.md`](stripe-webhooks.md)); invert with the same
  exponent table
  ([stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py)).
- Three-decimal currencies: the last-digit-0 rule explicitly covers
  "Payment Intents, Refunds, and Disputes"
  ([archived Supported currencies](https://web.archive.org/web/20221231002856/https://stripe.com/docs/currencies)).
- No per-currency *minimum* refund amount is documented on
  [Refunds](https://docs.stripe.com/refunds.md) or
  [Create a refund](https://docs.stripe.com/api/refunds/create.md); the only
  documented bound is the remaining unrefunded amount (see Flagged gaps).

### Partial capture

- `amount_to_capture`: "The amount to capture from the PaymentIntent, which
  must be less than or equal to the original amount. Defaults to the full
  `amount_capturable` if it's not provided"
  ([Capture a PaymentIntent](https://docs.stripe.com/api/payment_intents/capture.md))
  — an integer in the same minor units as the intent's `amount`.
- "This captures the total authorized amount by default. To capture less or
  (for certain online card payments) more than the initial amount, pass the
  `amount_to_capture` option. **A partial capture automatically releases
  the remaining amount.**"
  ([Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method.md)).
  Maps directly onto getpaid-core's `charge(amount)` +
  implicit-release semantics; `release_lock()`'s "return released amount" is
  the uncaptured remainder, computable as
  `amount - amount_to_capture` in minor units before converting back.
- Exception to auto-release: `final_capture=false` "notifies Stripe to not
  release the remaining uncaptured funds … You can only use this setting
  when multicapture is available"
  ([Capture a PaymentIntent](https://docs.stripe.com/api/payment_intents/capture.md)).
- Capturing *more* than authorized requires the separate overcapture
  feature ([Place a hold on a payment
  method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method.md)).
- No rounding or minimum-amount rule specific to captures is documented on
  the capture pages; the charge minimums of §3 apply to the charge itself
  (flagged below).

## Flagged gaps / unverified

1. **Three-decimal rule cited from an archive.** The "least-significant
   digit must be 0 / round to the nearest ten" wording is quoted from a
   Wayback Machine snapshot (2022-12-31) of stripe.com/docs/currencies. The
   live page (Poland and UAE renders, and the `.md` export, checked
   2026-07-10) no longer contains a "Three-decimal currencies" section,
   though its metadata still says "including zero-decimal and three-decimal
   currency support" and BHD/JOD/KWD/OMR/TND remain in the UAE presentment
   list. Whether the nearest-ten constraint is still enforced today is
   **unverified**; treat the conservative reading (enforce multiples of 10)
   as safe.
2. **UGX contradiction on the live page.** UGX is simultaneously in the
   zero-decimal list ("the charge and the amount are the same") and in the
   special-cases table ("represent it as a two-decimal value, where the
   decimal amount is always `00`" — charge 5 UGX as `500`). The
   special-case (exponent-2 ×100) reading is taken as operative because it
   is the more specific rule and matches ISK's identical wording, but Stripe
   does not resolve the conflict on the page.
3. **Maximum-digit inconsistency.** The PaymentIntent reference says
   `amount` "supports up to eight digits" while the currencies page says 12
   digits for most card payments. Both are primary sources; which one the
   API enforces per payment method is unverified. Do not hard-code a
   maximum; surface Stripe's own `invalid_request_error` instead.
4. **Error behavior for granularity violations is undocumented.** Stripe
   documents that you "can't charge fractions of ISK/UGX" and that
   three-decimal amounts must end in 0, but not which error the API returns
   if violated. Unverified.
5. **No documented refund/capture minimums.** Neither the refunds pages nor
   the capture reference documents a minimum amount for partial refunds or
   partial captures (the charge-minimum table in §3 speaks of "charges").
   Absence of documentation is not proof of absence of enforcement.
6. **Country Specs completeness.** The Country Specs API is documented, but
   Stripe nowhere states that `supported_payment_currencies` is exactly the
   presentment-currency table from the currencies page, nor how promptly it
   tracks changes. Inference: it is the only machine-readable source, so it
   is the best available runtime ground truth.
7. **Minimum-amount table currency scope.** The minimums list covers ~31
   currencies; behavior for presentment currencies outside the table
   (converted to the settlement currency's minimum) makes static validation
   of minimums impossible without knowing the account's settlement currency
   and FX rate. Fact as documented; the practical consequence (don't
   pre-validate minimums locally) is inference.

## Sources

- getpaid-core: `python-getpaid-core/src/getpaid_core/processor.py`,
  `registry.py` (local, read 2026-07-10)
- getpaid-paynow: `python-getpaid-paynow/src/getpaid_paynow/client.py`,
  `types.py`, `processor.py` (local)
- [Supported currencies](https://docs.stripe.com/currencies) (fetched as
  https://docs.stripe.com/currencies.md plus HTML renders for the
  zero-decimal list and `?country=AE` variant)
- [Archived Supported currencies, 2022-12-31](https://web.archive.org/web/20221231002856/https://stripe.com/docs/currencies)
  (three-decimal section)
- [Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create.md)
- [PaymentIntent object](https://docs.stripe.com/api/payment_intents/object.md)
- [Capture a PaymentIntent](https://docs.stripe.com/api/payment_intents/capture.md)
- [Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method.md)
- [Refunds](https://docs.stripe.com/refunds.md)
- [Create a refund](https://docs.stripe.com/api/refunds/create.md)
- [Checkout Session object](https://docs.stripe.com/api/checkout/sessions/object.md)
  and [Create a Checkout Session](https://docs.stripe.com/api/checkout/sessions/create.md)
- [Country Spec object](https://docs.stripe.com/api/country_specs/object.md)
  and [List Country Specs](https://docs.stripe.com/api/country_specs/list.md)
- [The FX Quotes API](https://docs.stripe.com/payments/currencies/localize-prices/fx-quotes-api.md)
- [Payment method support](https://docs.stripe.com/payments/payment-methods/payment-method-support.md)
- stripe-python source at master (2026-07-10):
  [stripe/_payment_intent.py](https://github.com/stripe/stripe-python/blob/master/stripe/_payment_intent.py),
  [stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py)
- ISO 4217: [iso.org currency codes](https://www.iso.org/iso-4217-currency-codes.html)
  via the maintenance agency list
  [SIX list-one.xml](https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml)
  (fetched 2026-07-10)
- Python stdlib: [`decimal` documentation](https://docs.python.org/3/library/decimal.html)
