# Stripe refund semantics vs getpaid-core's `start_refund()` / `cancel_refund()`

Scope note (ticket #5): this document collects the facts needed to decide what
refund semantics the plugin can offer for getpaid-core's
`start_refund(amount: Decimal | None = None, **kwargs) -> RefundResult` and
`cancel_refund(**kwargs) -> bool`
(`python-getpaid-core/src/getpaid_core/processor.py`). Facts only, each claim
cited to a primary source (docs.stripe.com, stripe-python source); the
start_refund/cancel_refund DESIGN decision is a later ticket. Webhook
mechanics (signature verification, dedup, ordering, retries) are already
covered in the sibling asset [`stripe-webhooks.md`](stripe-webhooks.md) and are
not duplicated here — §3 below covers only refund-specific event semantics and
payload correlation.

## 0. The local contract being mapped onto

- `BaseProcessor.start_refund(self, amount: Decimal | None = None, **kwargs)
  -> RefundResult` — "Start a refund. Return refund metadata." —
  and `cancel_refund(self, **kwargs) -> bool` — "Cancel in-progress refund.
  Return True if ok." (`getpaid_core/processor.py`).
- Sibling convention (`getpaid_paynow/processor.py`): `start_refund` stashes
  the provider's refund id in `provider_data["refund_id"]` on the returned
  `RefundResult`; `cancel_refund` reads
  `self.payment.provider_data.get("refund_id")` and raises when absent
  ('Expected provider_data["refund_id"] set by start_refund().'). paynow can
  implement `cancel_refund` because paynow refunds sit in an awaiting state;
  §2 answers whether Stripe has an analogous window.

## 1. Creating refunds via stripe-python

### Endpoint and SDK surface

`POST /v1/refunds` ([Create a refund](https://docs.stripe.com/api/refunds/create)).
stripe-python exposes it both resource-style and client-style, each with an
async twin
([stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py),
[stripe/_refund_service.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund_service.py)):

| Sync | Async |
|---|---|
| `stripe.Refund.create(**params)` | `stripe.Refund.create_async(**params)` |
| `client.refunds.create(params, options)` | `client.refunds.create_async(params, options)` |
| `stripe.Refund.retrieve(id)` / `client.refunds.retrieve(id)` | `retrieve_async` |
| `stripe.Refund.modify(id, metadata=...)` / `client.refunds.update(id, params)` | `modify_async` / `update_async` |
| `stripe.Refund.cancel(id)` / `client.refunds.cancel(id)` | `cancel_async` |
| `stripe.Refund.list()` / `client.refunds.list()` | `list_async` |

`RefundService.create` posts to `/v1/refunds` with a typed
`RefundCreateParams`; the update endpoint "only accepts metadata as an
argument" (`_refund_service.py` docstrings). A sandbox-only test helper
`Refund.TestHelpers.expire` / `expire_async` (`POST
/v1/test_helpers/refunds/{refund}/expire`) can "Expire a refund with a status
of requires_action" — useful for testing the `requires_action` → `failed` path
(`_refund.py`).

### Target: `payment_intent` vs `charge`

- "When you create a new refund, you must specify a Charge or a PaymentIntent
  object on which to create it" (`Refund.create` docstring, `_refund.py`).
  Params: `charge` — "The identifier of the charge to refund."; `payment_intent`
  — "The identifier of the PaymentIntent to refund."
  ([Create a refund](https://docs.stripe.com/api/refunds/create)).
- Refunding by PaymentIntent is the recommended and equivalent path: "To
  refund a payment after the PaymentIntent succeeds, create a refund using the
  PaymentIntent, which is the same as refunding the underlying charge"
  ([Refunds](https://docs.stripe.com/refunds)). Since the plugin stores the
  `pi_…` id as `Payment.external_id` (webhooks asset §4), `payment_intent` is
  the natural parameter.

### Full vs partial, multiple partials

- Omitting `amount` refunds in full; "You can also refund only part of a
  PaymentIntent by specifying an amount… provide an `amount` parameter as an
  integer in cents (or the charge currency's smallest currency unit)"
  ([Refunds](https://docs.stripe.com/refunds)). `amount` is "A positive
  integer in the smallest currency unit representing how much of this charge
  to refund. Can refund only up to the remaining, unrefunded amount of the
  charge" ([Create a refund](https://docs.stripe.com/api/refunds/create)).
  Minor-unit conversion rules are in the sibling asset
  [`stripe-amounts-currencies.md`](stripe-amounts-currencies.md).
- Multiple partial refunds are allowed: "You can issue more than one refund
  against a charge, but you can't refund a total greater than the original
  charge amount" ([Refunds](https://docs.stripe.com/refunds)); "You can
  optionally refund only part of a charge. You can do so multiple times, until
  the entire charge has been refunded" (`Refund.create` docstring,
  `_refund.py`). Each partial refund is a distinct Refund object with its own
  id.

### `reason` and other create params

- `reason`: "If set, possible values are `duplicate`, `fraudulent`, and
  `requested_by_customer`. If you believe the charge to be fraudulent,
  specifying `fraudulent` as the reason will add the associated card and email
  to your block lists, and will also help us improve our fraud detection
  algorithms" ([Create a refund](https://docs.stripe.com/api/refunds/create);
  typed as the same three-value `Literal` in
  [stripe/params/_refund_create_params.py](https://github.com/stripe/stripe-python/blob/master/stripe/params/_refund_create_params.py)).
  Note `fraudulent` has the block-list side effect — not a neutral label.
- `metadata`: writable at creation ("Set of key-value pairs that you can
  attach to an object…", [Create a refund](https://docs.stripe.com/api/refunds/create)).
- `instructions_email`: "For payment methods without native refund support
  (e.g., Konbini, PromptPay), use this email from the customer to receive
  refund instructions" ([Create a refund](https://docs.stripe.com/api/refunds/create)).
- `origin=customer_balance`: refunds from a Customer Balance instead of a
  charge/intent ("If this value is provided, a Charge or PaymentIntent
  identifier is not required") — not the plugin's flow
  ([Create a refund](https://docs.stripe.com/api/refunds/create)).
- **Connect-only, out of scope**: `refund_application_fee` ("An application
  fee can be refunded only by the application that created the charge") and
  `reverse_transfer` ("A transfer can be reversed only by the application that
  created the charge") ([Create a refund](https://docs.stripe.com/api/refunds/create)).
  Both exist in `RefundCreateParams`; the plugin (direct charges, no Connect)
  has no use for them.

### Errors on create

- "Returns the `Refund` object if the refund succeeded. Raises an error if
  the Charge/PaymentIntent has already been refunded, or if an invalid
  identifier was provided" ([Create a refund](https://docs.stripe.com/api/refunds/create));
  "Once entirely refunded, a charge can't be refunded again. This method will
  raise an error when called on an already-refunded charge, or when trying to
  refund more money than is left on a charge" (`Refund.create` docstring,
  `_refund.py`).
- Documented error codes ([Error codes](https://docs.stripe.com/error-codes)):
  - `charge_already_refunded` — "The charge you're attempting to refund has
    already been refunded."
  - `charge_disputed` — "The charge you're attempting to refund has been
    charged back."
  - `refund_disputed_payment` — "You can't refund a disputed payment."
  These arrive as `stripe.InvalidRequestError` subtypes of `StripeError` with
  the `code` attribute set (standard stripe-python error surface; see
  webhooks asset §1 for the error-translation convention).
- **"Too old to refund"**: no general age limit and no dedicated error code is
  documented for card refunds (see Flagged gaps). Age-related failure
  surfaces asynchronously instead, via `failure_reason` values such as
  `expired_or_canceled_card` (§ below).

### Refund object lifecycle

Refund `status` is one of `pending`, `requires_action`, `succeeded`, `failed`,
`canceled` ([Refund object](https://docs.stripe.com/api/refunds/object);
`_refund.py` docstring). Documented transitions
([Refunds](https://docs.stripe.com/refunds)):

| Trigger | Status |
|---|---|
| Refund created for a method needing customer bank details (Konbini, PromptPay, Boleto, bank transfers); customer emailed | `requires_action` |
| Customer submits bank details; Stripe processing | `pending` |
| "Refund is expected to arrive in customer's bank" | `succeeded` |
| Bank returns funds (name mismatch, account typo) — Stripe re-emails customer | back to `requires_action` |
| Customer doesn't respond before the expiration threshold, or bank/issuer can't process | `failed` |
| Canceled while in `requires_action` | `canceled` |

- `pending_reason` (nullable enum on the Refund): `processing`,
  `insufficient_funds`, `charge_pending`
  ([Refund object](https://docs.stripe.com/api/refunds/object); `_refund.py`).
  Insufficient-balance case: "Refunds use your available Stripe balance (not
  including pending amounts). If your available balance doesn't cover the
  amount of the refund, Stripe holds the refund as pending for card
  transactions (refunds for other payment method types will fail) until your
  Stripe balance becomes sufficient" ([Refunds](https://docs.stripe.com/refunds)).
- `failure_reason` (nullable): `lost_or_stolen_card`,
  `expired_or_canceled_card`, `charge_for_pending_refund_disputed`,
  `insufficient_funds`, `declined`, `merchant_request`, `unknown`
  ([Refund object](https://docs.stripe.com/api/refunds/object); `_refund.py`).
  Notably `charge_for_pending_refund_disputed`: "A customer disputed the
  charge while the refund is pending. In this case, we recommend accepting or
  challenging the dispute instead of refunding to avoid duplicate
  reimbursements" ([Refunds](https://docs.stripe.com/refunds)).
- `reason` on the object adds a fourth, Stripe-generated value beyond the
  three writable ones: `expired_uncaptured_charge` — "generated by Stripe
  internally" when an uncaptured authorization expires and Stripe auto-refunds
  ([Refund object](https://docs.stripe.com/api/refunds/object); `_refund.py`
  types it `Literal["duplicate", "expired_uncaptured_charge", "fraudulent",
  "requested_by_customer"]`). A webhook handler reading `refund.reason` must
  tolerate it.
- `destination_details` (nullable object): "Transaction-specific details for
  the refund"; carries a `type` string naming the payment method ("An
  additional hash is included on `destination_details` with a name matching
  this value") plus a per-method hash
  ([Refund object](https://docs.stripe.com/api/refunds/object); `_refund.py`).
  For cards (`destination_details.card`): `reference` ("Value of the reference
  number assigned to the refund" — the ARN), `reference_status`
  (`pending`/`available`/`unavailable`), `reference_type` (e.g.
  `acquirer_reference_number`), and `type: Literal["pending", "refund",
  "reversal"]` (`_refund.py`). Reversal detection: "Some refunds — those
  issued shortly after the original charge — appear in the form of a reversal…
  If it's a reversal, it returns `destination_details[card][type] =
  'reversal'`"; "An ARN isn't available in the case of a reversal"
  ([Refunds](https://docs.stripe.com/refunds)). Several method hashes (blik,
  swish, paypal) also carry `network_decline_code` — "For refunds declined by
  the network, a decline code provided by the network which indicates the
  reason the refund failed" (`_refund.py`).
- Other lifecycle-relevant attributes: `balance_transaction` (impact on the
  Stripe balance), `failure_balance_transaction` ("After the refund fails,
  this balance transaction describes the adjustment made on your account
  balance that reverses the initial balance transaction"), `next_action`
  (populated in `requires_action`: `type`, `display_details.email_sent.
  email_sent_at/email_sent_to`, `display_details.expires_at`)
  ([Refund object](https://docs.stripe.com/api/refunds/object); `_refund.py`).

## 2. Refund cancellation feasibility (the crux)

`POST /v1/refunds/:id/cancel` exists, but its documented constraint is narrow:

> "Cancels a refund with a status of `requires_action`. You can't cancel
> refunds in other states. Only refunds for payment methods that require
> customer action can enter the `requires_action` state."
> ([Cancel a refund](https://docs.stripe.com/api/refunds/cancel); identical
> text in the `Refund.cancel` / `cancel_async` / `RefundService.cancel`
> docstrings, `_refund.py` / `_refund_service.py`.)

- The `requires_action` state is entered only by "payment methods without
  native refund support (for example, Konbini, PromptPay, Boleto, and bank
  transfers)" where "Stripe needs to collect bank account details from your
  customer before it can process the refund"
  ([Refunds](https://docs.stripe.com/refunds)). The cancel window is while
  banking information hasn't been collected: "For some payment methods, Stripe
  reaches out to the customer to collect banking information before processing
  the refund. You can cancel these refunds while banking information hasn't
  been collected. Both the API and Dashboard cancellations are supported for
  this type of refund" ([Refunds](https://docs.stripe.com/refunds)).
- **Card refunds are not API-cancelable.** Card refunds never enter
  `requires_action` (they go straight to `pending`/`succeeded`), and the only
  documented card-cancellation path is manual: "Some card refunds support
  cancellation for a short period of time. The refund must not have been
  processed as a charge reversal. **Only Dashboard cancellations are currently
  supported for card refunds**" ([Refunds](https://docs.stripe.com/refunds)).
  So for the card/BLIK/P24-style methods this plugin targets, there is no
  paynow-like "awaiting" window reachable through the API: `cancel_refund()`
  calling `client.refunds.cancel(...)` on a card refund will raise ("This
  call raises an error if you can't cancel the refund",
  [Cancel a refund](https://docs.stripe.com/api/refunds/cancel)).
- The narrow exception, if the plugin ever supports bank-transfer/Konbini-type
  methods: a refund whose `status == "requires_action"` can be canceled via
  `client.refunds.cancel(refund_id)` / `cancel_async` and "Returns the refund
  object if the cancellation succeeds" with `status: "canceled"`
  ([Cancel a refund](https://docs.stripe.com/api/refunds/cancel)).
- After cancellation: "Canceled refunds transition to a `canceled` status. As
  cancellations are a type of refund failure, the attributes `failure_reason`
  and `failure_balance_transaction` are included on the Refund object"
  ([Refunds](https://docs.stripe.com/refunds)) — i.e. a canceled refund looks
  like a failed one (money returns to the Stripe balance), so a webhook
  handler should treat `canceled` alongside `failed`, keyed on `status`.

## 3. Refund webhook events (refund-specific semantics only)

Event mechanics (signatures, dedup via `event.id`, ordering, retries) are in
[`stripe-webhooks.md`](stripe-webhooks.md) §§1, 3. Refund-specific facts:

- Event catalogue for refunds ([Refunds](https://docs.stripe.com/refunds);
  [Types of events](https://docs.stripe.com/api/events/types)):

| Event | `data.object` | When |
|---|---|---|
| `refund.created` | Refund | "Sent when a refund is created" |
| `refund.updated` | Refund | "Sent when the refund is updated. Updates include adding metadata and providing details like the ARN as a reference number to trace refunds" — i.e. also status transitions and `destination_details.card.reference` becoming available |
| `refund.failed` | Refund | "Sent when a refund has failed" |
| `charge.refunded` | Charge | "Sent when a charge is refunded, **including partial refunds**. Listen to `refund.created` for information about the refund" |

- "At a minimum, Stripe recommends that you listen for the `refund.created`
  event" ([Refunds](https://docs.stripe.com/refunds)).
- **Deprecated**: `charge.refund.updated` is listed under "Deprecated Events"
  with the instruction "use `refund.updated` instead" (as is
  `source.refund_attributes_required`)
  ([Refunds — webhooks section](https://docs.stripe.com/refunds)).
- Full vs partial: `charge.refunded` fires for both ("including partial
  refunds"), so it cannot alone distinguish them; the Charge payload
  disambiguates via `refunded` — "Whether the charge has been fully refunded.
  If the charge is only partially refunded, this attribute will still be
  false" — and `amount_refunded` — "Amount in cents (or local equivalent)
  refunded (can be less than the amount attribute on the charge if a partial
  refund was issued)"
  ([stripe/_charge.py](https://github.com/stripe/stripe-python/blob/master/stripe/_charge.py);
  same text at [Charge object](https://docs.stripe.com/api/charges/object)).
- Correlation from a `refund.*` payload back to the payment: `data.object` is
  the Refund, carrying `id` (`re_…`), `charge` ("ID of the charge that's
  refunded"), `payment_intent` ("ID of the PaymentIntent that's refunded" —
  matches the stored `Payment.external_id`), and the refund's own `metadata`
  ([Refund object](https://docs.stripe.com/api/refunds/object)). Metadata set
  at refund creation is available in every `refund.*` payload since the
  Refund *is* `data.object`; there is no documented automatic metadata copy
  from charge/intent to Refund (webhooks asset §4), so any correlation
  metadata must be set explicitly on the refund at create time.
- Failed-refund money flow: "In the rare instance that a refund fails, we
  notify you using the `refund.failed` event… you need to arrange an
  alternative way to provide your customer with a refund"; "the bank returns
  the refunded amount to us and we add it back to your Stripe account balance.
  This process can take up to 30 days from the post date"; the re-credit is
  visible as `failure_balance_transaction` on the Refund
  ([Refunds](https://docs.stripe.com/refunds);
  [Refund object](https://docs.stripe.com/api/refunds/object)). Stripe does
  NOT re-mark the charge as unrefunded in the sense of retrying — the merchant
  must arrange an alternative refund path.
- Dispute interaction: "Disputes and chargebacks aren't possible on credit
  card charges that are fully refunded", but a dispute can land while a refund
  is pending (`failure_reason: charge_for_pending_refund_disputed`)
  ([Refunds](https://docs.stripe.com/refunds)).

## 4. Refund ids and persistence

- Id: "Unique identifier for the object"
  ([Refund object](https://docs.stripe.com/api/refunds/object)). All API
  reference examples use the `re_` prefix (e.g. `re_1Nispe2eZvKYlo2Cd31jOCgZ`,
  [Cancel a refund](https://docs.stripe.com/api/refunds/cancel)); the Refunds
  guide's destination-details examples for local payment methods show a
  `pyr_` prefix (`"id": "pyr_1234"`) ([Refunds](https://docs.stripe.com/refunds))
  — so persistence code should treat the refund id as an opaque string, not
  assume `re_…`.
- `metadata` is writable at creation ([Create a refund](https://docs.stripe.com/api/refunds/create))
  and is the *only* thing updatable afterwards: "Updates the refund that you
  specify… This request only accepts metadata as an argument"
  (`RefundService.update` docstring, `_refund_service.py`;
  [Update a refund](https://docs.stripe.com/api/refunds/update)).
- The paynow convention (`provider_data["refund_id"]` stashed by
  `start_refund` for later `cancel_refund`/lookup, `getpaid_paynow/processor.py`)
  maps cleanly: `Refund.create` returns the Refund synchronously with `id`
  and `status` populated, and `client.refunds.retrieve(refund_id)` /
  `retrieve_async` looks it up later. With multiple partial refunds allowed
  (§1), a single `refund_id` slot only records the *latest* refund — a
  Stripe-specific consideration paynow doesn't have. Alternatively refunds
  are listable by charge (`RefundListParams`) and "The 10 most recent refunds
  are always available by default on the Charge object" (`RefundService.list`
  docstring, `_refund_service.py`).
- Pending→succeeded timing for cards: the customer-visible credit takes
  "approximately 5-10 business days" ("Your customer sees the refund as a
  credit approximately 5-10 business days later, depending upon the bank"),
  and the ARN "takes up to 7 business days after initiating the refund" to
  arrive ([Refunds](https://docs.stripe.com/refunds)) — but `status:
  succeeded` does NOT mean the money reached the customer: `succeeded` means
  "Refund is expected to arrive in customer's bank"
  ([Refunds](https://docs.stripe.com/refunds)). A card refund is `pending`
  (rather than immediately succeeded) when e.g. the available balance is
  insufficient (`pending_reason: insufficient_funds`) or the charge itself is
  still pending (`charge_pending`) (§1). The exact moment a normal card
  refund flips to `succeeded` is not spelled out in the docs (see Flagged
  gaps); a `succeeded` refund can still *fail later* (issuer can't process →
  `refund.failed`, funds returned to balance within up to 30 days, §3).

## 5. Boundary with manual capture / pre-auth (release_lock territory)

- Refund applies only post-capture. For an uncaptured PaymentIntent:
  "If you want to… refund a PaymentIntent that has a status of
  `requires_capture`… the charge attached to the PaymentIntent remains
  uncaptured and can't be refunded directly. **You must cancel the
  PaymentIntent**" ([Refunds](https://docs.stripe.com/refunds)); "If you need
  to cancel an authorization, you can cancel the PaymentIntent"
  ([Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)).
  That is `release_lock()` territory (PaymentIntent cancel), not
  `start_refund()`.
- Partial capture needs no refund for the remainder: "A partial capture
  automatically releases the remaining amount"; "If you partially capture a
  payment, you can't perform another capture for the difference"
  ([Place a hold](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)).
  A later refund of a partially-captured payment refunds against the
  *captured* amount — `amount` "Can refund only up to the remaining,
  unrefunded amount of the charge"
  ([Create a refund](https://docs.stripe.com/api/refunds/create)), and the
  Charge's `amount_captured` "can be less than the amount attribute on the
  charge if a partial capture was made" (`_charge.py`).
- Expiry closes the loop automatically: "If the authorization expires before
  you capture the funds, the funds are released and the payment status
  changes to `canceled`" ([Place a hold](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method));
  a Stripe-generated Refund with `reason: expired_uncaptured_charge` exists as
  a documented reason value (§1) — webhook handlers may see refund events not
  initiated by `start_refund()`.

## Flagged gaps / unverified

- **No documented "too old to refund" limit or error code**: neither
  [Refunds](https://docs.stripe.com/refunds) nor
  [Error codes](https://docs.stripe.com/error-codes) states a maximum charge
  age for card refunds or a dedicated error code for over-age charges.
  Payment-method-specific refund windows may exist on individual
  payment-method pages (not exhaustively checked). Treat "too old" as an
  unverified failure mode; the verified age-adjacent failure is the async
  `failure_reason: expired_or_canceled_card`.
- **Exact `pending`→`succeeded` moment for card refunds is not specified**:
  the docs define `succeeded` as "expected to arrive" and give 5-10 business
  days for customer visibility, but do not state whether a normal card refund
  is created already `succeeded` or briefly `pending` (`pending_reason:
  processing` exists, implying a pending phase is possible even without
  balance issues). Not verified in a primary source.
- **Cancel-endpoint error details**: [Cancel a refund](https://docs.stripe.com/api/refunds/cancel)
  says only "raises an error if you can't cancel the refund" without naming
  the error code/type for a non-`requires_action` refund; the concrete
  exception class/code for canceling a card refund via API is unverified.
- **Card Dashboard-cancellation window**: "a short period of time" is not
  quantified anywhere in the docs; and being Dashboard-only it is unusable by
  the plugin regardless.
- The claim that card refunds "never enter `requires_action`" is an inference
  from two documented statements (only methods "without native refund
  support… Konbini, PromptPay, Boleto, and bank transfers" enter
  `requires_action`; card cancellation is Dashboard-only) — Stripe does not
  state it as a single explicit sentence.

## Sources

- getpaid-core: `python-getpaid-core/src/getpaid_core/processor.py` (local,
  read 2026-07-10)
- getpaid-paynow: `python-getpaid-paynow/src/getpaid_paynow/processor.py` (local)
- Sibling assets: `docs/research/stripe-webhooks.md`,
  `docs/research/stripe-amounts-currencies.md`
- [Refunds](https://docs.stripe.com/refunds) (fetched as
  https://docs.stripe.com/refunds.md)
- [Create a refund](https://docs.stripe.com/api/refunds/create)
- [Refund object](https://docs.stripe.com/api/refunds/object)
- [Cancel a refund](https://docs.stripe.com/api/refunds/cancel)
- [Error codes](https://docs.stripe.com/error-codes) (fetched as
  https://docs.stripe.com/error-codes.md)
- [Charge object](https://docs.stripe.com/api/charges/object)
- [Types of events](https://docs.stripe.com/api/events/types) (via
  stripe-webhooks.md §2)
- [Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
  (fetched as …/place-a-hold-on-a-payment-method.md)
- stripe-python source at master (2026-07-10):
  [stripe/_refund.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund.py),
  [stripe/_refund_service.py](https://github.com/stripe/stripe-python/blob/master/stripe/_refund_service.py),
  [stripe/params/_refund_create_params.py](https://github.com/stripe/stripe-python/blob/master/stripe/params/_refund_create_params.py),
  [stripe/_charge.py](https://github.com/stripe/stripe-python/blob/master/stripe/_charge.py)
