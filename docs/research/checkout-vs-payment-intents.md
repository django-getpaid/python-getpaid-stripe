# Stripe Checkout Sessions vs Payment Intents (direct) as the integration surface

Scope note (ticket #2): this document compares the two Stripe integration surfaces
— **Checkout Sessions** (Stripe-hosted payment page) and **Payment Intents used
directly** — strictly as candidate surfaces for a `python-getpaid` processor plugin
implementing the `getpaid_core.processor.BaseProcessor` contract
(`prepare_transaction() -> TransactionResult(redirect_url=...)`,
`verify_callback()`/`handle_callback() -> PaymentUpdate`, `charge()`,
`release_lock()`, `start_refund()`). Facts only, each claim cited to a primary
source (docs.stripe.com, stripe-python source/README); no recommendation — the
decision happens elsewhere.

## 1. Fit with getpaid's redirect-oriented flow

getpaid-core's `TransactionResult` carries `redirect_url` as its main field, and the
sibling processor (`getpaid_paynow`) implements `prepare_transaction()` as
"create payment at provider → return provider-hosted `redirectUrl`"
(`python-getpaid-paynow/src/getpaid_paynow/processor.py`,
`python-getpaid-core/src/getpaid_core/types.py`).

### Checkout Sessions — hosted URL, server-side only

- A session is created server-side with `POST /v1/checkout/sessions`
  (`stripe.checkout.Session.create` / `client.v1.checkout.sessions.create` in
  stripe-python). Notable parameters: `mode` (`payment` for one-off),
  `line_items` ("Required for `payment` and `subscription` mode"; each item is a
  `price` ID or inline `price_data`), `success_url`, `cancel_url`,
  `client_reference_id`, `metadata`, `payment_intent_data`, `expires_at`.
  [Create a Session](https://docs.stripe.com/api/checkout/sessions/create)
- The response contains a `url` field: "The URL to the Checkout Session. Applies to
  Checkout Sessions with `ui_mode: hosted_page`. Redirect customers to this URL to
  take them to Checkout. … This value is only present when the session is active."
  [Session object — url](https://docs.stripe.com/api/checkout/sessions/object)
- Session expiry: `expires_at` defaults to "24 hours from creation" and can be set
  "anywhere from 30 minutes to 24 hours after Checkout Session creation".
  [Create a Session — expires_at](https://docs.stripe.com/api/checkout/sessions/create)
- A session can also be expired programmatically: "A Checkout Session can be expired
  when it is in one of these statuses: `open`. After it expires, a customer can't
  complete a Checkout Session."
  [Expire a Session](https://docs.stripe.com/api/checkout/sessions/expire)
- This maps 1:1 onto the Paynow-style flow: `prepare_transaction()` returns
  `TransactionResult(method="GET", redirect_url=session.url, external_id=session.id)`.

### Payment Intents (direct) — no hosted URL by default

- A PaymentIntent is created server-side (`POST /v1/payment_intents` with `amount`,
  `currency`), but "Building an integration with the Payment Intents API involves
  two actions: creating and *confirming* a PaymentIntent."
  [Payment Intents overview](https://docs.stripe.com/payments/payment-intents)
- The PaymentIntent "includes a *client secret* that the client side uses to
  securely complete the payment process", passed to Stripe.js functions
  (e.g. `stripe.confirmCardPayment`) in the browser. "Don't log it, embed it in
  URLs, or expose it to anyone other than the customer."
  [Payment Intents overview](https://docs.stripe.com/payments/payment-intents)
- There is no provider-hosted page URL on this surface; the payment form is the
  merchant's own page running Stripe.js/Elements, confirmed via
  `stripe.confirmPayment`: "When called, `stripe.confirmPayment` will attempt to
  complete any required actions, such as authenticating your user by displaying a
  3DS dialog or redirecting them to a bank authorization page."
  [stripe.confirmPayment](https://docs.stripe.com/js/payment_intents/confirm_payment)
- Redirect-only variant: after confirmation, redirect-based payment methods surface
  `next_action.redirect_to_url` on the PaymentIntent object — "Contains instructions
  for authenticating a payment by redirecting your customer to another page or
  application"; `next_action.redirect_to_url.url` is "The URL you must redirect your
  customer to in order to authenticate the payment", and
  `next_action.redirect_to_url.return_url` is where the customer returns afterwards.
  `next_action.type` examples include `redirect_to_url`, `use_stripe_sdk`,
  `alipay_handle_redirect`.
  [PaymentIntent object — next_action](https://docs.stripe.com/api/payment_intents/object)
  This URL only exists once the intent has been confirmed with a concrete
  redirect-based payment method (`confirm=true` at creation is possible: "Set to
  `true` to attempt to confirm this PaymentIntent immediately"), i.e. the payment
  method must already be chosen server-side — it is not a general-purpose hosted
  payment page.
  [Create a PaymentIntent — confirm](https://docs.stripe.com/api/payment_intents/create)

## 2. Manual capture (pre-auth) — getpaid `charge()` / `release_lock()`

Both surfaces support manual capture; Checkout configures it on the underlying
PaymentIntent.

- PaymentIntents: `capture_method` is one of `automatic`, `automatic_async`
  ("(Default) Stripe asynchronously captures funds when the customer authorizes the
  payment"), and `manual` ("Place a hold on the funds when the customer authorizes
  the payment, but don't capture the funds until later. (Not all payment methods
  support this.)").
  [Create a PaymentIntent — capture_method](https://docs.stripe.com/api/payment_intents/create)
- Checkout: pass `payment_intent_data[capture_method]=manual` when creating the
  session: "Specify `capture_method` as `manual` when creating the Checkout Session.
  This parameter instructs Stripe to authorize the amount but not capture it."
  [Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
- Payment-method support: "Only some payment methods support separate authorization
  and capture. Some payment methods that support this include cards, Affirm,
  Afterpay, Cash App Pay, Klarna, and PayPal. Some payment methods that don't
  support this include ACH and iDEAL."
  [Place a hold — payment method limitations](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
- Authorization validity window: "Usually, an authorization for an online card
  payment is valid for 7 days." Card-not-present windows by network:
  Visa 7 days for customer-initiated / 5 days (exactly 4 days 18 hours) for
  merchant-initiated; Mastercard, American Express and Discover 7 days. In-person:
  Visa 5 days, Mastercard/Amex/Discover 2 days. Japan-based accounts can hold
  JPY-denominated transactions up to 30 days. Non-card methods differ (Klarna 28
  days, PayPal 10+10 days, Cash App Pay 7 days, Afterpay 13 days, Affirm 30 days).
  If not captured in time, "the funds are released and the payment status changes
  to `canceled`."
  [Place a hold — authorization validity windows](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
- Capture: after authorization the PaymentIntent status is `requires_capture`;
  capture with `POST /v1/payment_intents/{id}/capture`, optionally
  `amount_to_capture`. "A partial capture automatically releases the remaining
  amount." Capturing *more* than authorized is possible for certain online card
  payments (overcapture). For Checkout, "make sure you use the PaymentIntent ID that
  is returned in the Checkout Session object."
  [Place a hold — capture the funds](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
- Multicapture (multiple partial captures on one authorization): "It only supports
  online card payments", requires `capture_method=manual`, is available on Amex,
  Visa, Discover, Mastercard, Cartes Bancaires, Diners Club, CUP and JCB (CUP/JCB
  geographically restricted); request with
  `payment_method_options[card][request_multicapture]=if_available` and keep the
  remainder authorized by capturing with `final_capture=false`. "Stripe allows up to
  50 non-final captures for a single PaymentIntent."
  [Multicapture](https://docs.stripe.com/payments/multicapture)
- Cancellation / lock release: "You can cancel a PaymentIntent object when it's in
  one of these statuses: `requires_payment_method`, `requires_capture`,
  `requires_confirmation`, `requires_action` or, in rare cases, `processing`. …
  For PaymentIntents with a `status` of `requires_capture`, the remaining
  `amount_capturable` is automatically refunded." Constraint specific to Checkout:
  "You can directly cancel the PaymentIntent for a Checkout Session only when the
  PaymentIntent has a status of `requires_capture`. Otherwise, you must expire the
  Checkout Session."
  [Cancel a PaymentIntent](https://docs.stripe.com/api/payment_intents/cancel)

## 3. Automatic / dynamic payment methods

- PaymentIntents: `automatic_payment_methods[enabled]=true` turns on dynamic payment
  methods. Its `allow_redirects` sub-parameter "Controls whether this PaymentIntent
  will accept redirect-based payment methods": `always` (default) — "`return_url`
  may be required to confirm this PaymentIntent"; `never` — "Payment methods that
  require redirect will be filtered. `return_url` will not be required to confirm
  this PaymentIntent."
  [Create a PaymentIntent — automatic_payment_methods](https://docs.stripe.com/api/payment_intents/create)
- On API version 2023-08-16 or later, simply omitting `payment_method_types` gives
  dynamic payment methods by default; on older versions
  `automatic_payment_methods[enabled]=true` must be passed explicitly.
  [Dynamic payment methods](https://docs.stripe.com/payments/payment-methods/dynamic-payment-methods)
- Checkout uses the same Dashboard-driven mechanism natively: "When you use dynamic
  payment methods in an Element, Checkout, Payment Links, or Hosted Invoice Page
  integration, Stripe handles the logic for dynamically displaying the most relevant
  eligible payment methods to each customer" and "Only payment methods that you
  enabled can be shown to your customers." Redirect handling for those methods
  happens entirely on the Stripe-hosted page; the merchant server only supplies
  `success_url`/`cancel_url`.
  [Dynamic payment methods](https://docs.stripe.com/payments/payment-methods/dynamic-payment-methods)
- Note for manual capture: enabling dynamic payment methods can surface methods that
  do not support separate authorization and capture (e.g. iDEAL, ACH — see §2), so
  the two features interact on both surfaces.
  [Place a hold — payment method limitations](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)

## 4. Webhook events and server-side completion (getpaid `handle_callback()`)

### Checkout Sessions surface

Event types ([Types of events](https://docs.stripe.com/api/events/types)):

| Event | Emitted when |
|---|---|
| `checkout.session.completed` | "a Checkout Session has been successfully completed" |
| `checkout.session.expired` | "a Checkout Session is expired" |
| `checkout.session.async_payment_succeeded` | "a payment intent using a delayed payment method finally succeeds" |
| `checkout.session.async_payment_failed` | "a payment intent using a delayed payment method fails" |

Fulfillment guidance ([Fulfill orders](https://docs.stripe.com/checkout/fulfillment?payment-ui=stripe-hosted)):

- "When someone pays you, it creates a `checkout.session.completed` event." Handle
  it plus `checkout.session.async_payment_succeeded` /
  `checkout.session.async_payment_failed` for delayed-notification methods (ACH,
  bank transfers): "funds won't be immediately available when Checkout completes";
  the completed event then arrives with `payment_status: unpaid`.
- "Check the `payment_status` property to determine if it requires fulfillment"
  (`paid`, `unpaid`, `no_payment_required` — [Session object](https://docs.stripe.com/api/checkout/sessions/object)).
- Idempotency is on the integrator: "your `fulfill_checkout` function might be
  called multiple times, possibly concurrently, for the same Checkout Session"; it
  must "correctly handle being called multiple times with the same Checkout Session
  ID" and record fulfillment status.
- "Listening to webhooks is required to make sure you always trigger fulfillment for
  every payment"; fulfilling additionally from the success landing page is
  recommended, but "You can't rely on triggering fulfillment only from your Checkout
  landing page, because your customers aren't guaranteed to visit that page."

Because a `payment`-mode session confirms into a PaymentIntent (§7), the underlying
`payment_intent.*` and `charge.*`/`refund.*` events fire as well and can be
subscribed to in the same endpoint.

### Payment Intents surface

Event types ([Types of events](https://docs.stripe.com/api/events/types),
statuses per [PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle)):

| Event | Emitted when |
|---|---|
| `payment_intent.created` | "a new PaymentIntent is created" |
| `payment_intent.processing` | "a PaymentIntent has started processing" |
| `payment_intent.requires_action` | "a PaymentIntent transitions to requires_action state" |
| `payment_intent.amount_capturable_updated` | "a PaymentIntent has funds to be captured. Check the `amount_capturable` property" — the manual-capture (getpaid `LOCKED`) signal |
| `payment_intent.succeeded` | "a PaymentIntent has successfully completed payment" |
| `payment_intent.payment_failed` | "a PaymentIntent has failed the attempt to create a payment method or a payment" |
| `payment_intent.canceled` | "a PaymentIntent is canceled" |
| `payment_intent.partially_funded` | customer_balance funding changes |

"A PaymentIntent with a `succeeded` status means that the corresponding payment flow
is complete. The funds are now in your account and you can confidently fulfill the
order." On failure "the PaymentIntent's status returns to `requires_payment_method`
so that the payment can be retried."
[PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle)

### Refund events (identical on both surfaces — refunds target the charge/intent)

`charge.refunded` — "whenever a charge is refunded, including partial refunds.
Listen to `refund.created` for information about the refund"; `refund.created`,
`refund.updated`, `refund.failed`; `charge.refund.updated` covers "selected payment
methods" only ("For updates on all refunds, listen to `refund.updated` instead").
[Types of events](https://docs.stripe.com/api/events/types)
Refunds are created against a `charge` or a `payment_intent` id, with optional
partial `amount`, repeatable "until the entire charge has been refunded".
[Create a refund](https://docs.stripe.com/api/refunds/create)

## 5. client_secret / return_url handshake — demands on framework adapters

### Checkout Sessions

- The adapter supplies `success_url` (and optionally `cancel_url`; "If set, Checkout
  displays a back button") at session creation.
  [Create a Session](https://docs.stripe.com/api/checkout/sessions/create)
- Session identification on return uses a literal template variable: "Add the
  `{CHECKOUT_SESSION_ID}` template variable to the `success_url` when you create the
  Checkout Session. This is a literal string and you must add it exactly as you see
  it here. Don't substitute it with a Checkout Session ID — this happens
  automatically after your customer pays and is redirected to the success page."
  [Customize redirect behavior](https://docs.stripe.com/payments/checkout/custom-success-page)
- No publishable key, no JavaScript and no merchant-hosted payment form are involved
  in the hosted flow: the server creates the session with the secret key, redirects
  to `session.url`, and receives webhooks. This matches getpaid-core, where the
  adapter's only browser-facing duties are the redirect and the callback endpoint,
  and mirrors the existing `continue_url` template handling in `getpaid_paynow`
  (`_resolve_url()` formatting a placeholder).

### Payment Intents (direct)

- The server hands the `client_secret` to the browser; it is "Used for client-side
  retrieval using a publishable key" and "can be used to complete a payment from
  your frontend. It should not be stored, logged, or exposed to anyone other than
  the customer."
  [PaymentIntent object — client_secret](https://docs.stripe.com/api/payment_intents/object)
- The frontend must run Stripe.js/Elements and call `stripe.confirmPayment`, which
  needs `elements` or `clientSecret`; "By default, `stripe.confirmPayment` will
  always redirect to your `return_url` after a successful confirmation. If you set
  `redirect: "if_required"`, then `stripe.confirmPayment` will only redirect if your
  user chooses a redirect-based payment method."
  [stripe.confirmPayment](https://docs.stripe.com/js/payment_intents/confirm_payment)
- With dynamic payment methods enabled (default `allow_redirects: always`),
  "`return_url` may be required to confirm this PaymentIntent."
  [Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create)
- Consequence for a backend-only library: the getpaid `TransactionResult` has no
  natural slot for a browser handshake — the plugin would have to return the
  `client_secret` via `form_data`/`provider_data` and require every framework
  adapter (django-getpaid, FastAPI/Litestar) to ship a Stripe.js payment page,
  expose the publishable key, and host a `return_url` view, none of which the
  `BaseProcessor` contract or existing adapters provide today
  (`python-getpaid-core/src/getpaid_core/processor.py`).

## 6. stripe-python SDK specifics

Source: [stripe-python README](https://github.com/stripe/stripe-python/blob/master/README.md)
and repository source.

- Requirements: "we currently support **Python 3.9+**" (README, "Requirements").
- Async: "Asynchronous versions of request-making methods are available by suffixing
  the method name with `_async`" (README, "Async"). "The default HTTP client uses
  `requests` for making synchronous requests but `httpx` for making async requests."
  `pip install stripe[async]` installs an async-capable HTTP library (new in
  v13.0.1); `stripe.HTTPXClient()` (with `allow_sync_methods=True` opt-in for sync)
  and `stripe.AIOHTTPClient()` (async-only) are available and can be passed to
  `StripeClient(..., http_client=...)`.
- Creation calls: modern style is `StripeClient` with the `v1` namespace —
  `client.v1.checkout.sessions.create(...)` / `create_async(...)`
  ([stripe/checkout/_session_service.py](https://github.com/stripe/stripe-python/blob/master/stripe/checkout/_session_service.py))
  and `client.v1.payment_intents.create/confirm/capture/cancel` each with an
  `_async` twin
  ([stripe/_payment_intent_service.py](https://github.com/stripe/stripe-python/blob/master/stripe/_payment_intent_service.py)).
  Top-level `StripeClient.payment_intents` etc. are marked deprecated in favor of
  `StripeClient.v1.*`
  ([stripe/_stripe_client.py](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_client.py));
  the legacy global-API pattern (`stripe.PaymentIntent.create`,
  `stripe.checkout.Session.create`) still works.
- Webhook signature verification:
  `stripe.Webhook.construct_event(payload, sig_header, secret, tolerance=DEFAULT_TOLERANCE)`
  with `DEFAULT_TOLERANCE = 300` seconds, backed by
  `WebhookSignature.verify_header`, raising `SignatureVerificationError` on failure
  ([stripe/_webhook.py](https://github.com/stripe/stripe-python/blob/master/stripe/_webhook.py)).
  The webhooks docs additionally show the newer
  `client.parse_event_notification(webhook_body, sig_header, webhook_secret)` helper
  on `StripeClient` ([Webhooks](https://docs.stripe.com/webhooks)). This maps onto
  getpaid's `verify_callback(data, headers, raw_body=...)`, which already requires
  the raw HTTP body (same pattern as `getpaid_paynow`).
- Idempotency: "Idempotency keys are automatically generated and added to requests,
  when not given, to guarantee that retries are safe" (README); explicit keys can be
  passed per request. Network retries via `max_network_retries`.

## 7. Other materially distinguishing facts

- **Session ↔ PaymentIntent relationship and creation timing.** Since API version
  2022-08-01: "A PaymentIntent is no longer created during Checkout Session creation
  in payment mode. Instead, a PaymentIntent is created when the Session is
  confirmed."
  [Changelog 2022-08-01](https://docs.stripe.com/changelog/2022-08-01/deferred-paymentintent-checkout-session.md)
  Consequently `session.payment_intent` is nullable (an open, unpaid session shows
  `"payment_intent": null` — [Expire a Session response](https://docs.stripe.com/api/checkout/sessions/expire)),
  so at `prepare_transaction()` time the only stable `external_id` on the Checkout
  surface is the `cs_…` session id; the `pi_…` id becomes available from the
  completed session / webhooks. On the direct surface the `pi_…` id exists from
  creation.
- **Ownership of the underlying intent.** "You can't confirm or cancel the
  PaymentIntent for a Checkout Session. To cancel, expire the Checkout Session
  instead" (exception: direct cancel is allowed at `requires_capture`, §2).
  [Session object — payment_intent](https://docs.stripe.com/api/checkout/sessions/object),
  [Cancel a PaymentIntent](https://docs.stripe.com/api/payment_intents/cancel)
- **Expiry semantics.** Checkout Sessions expire (default 24 h, configurable
  30 min–24 h) and emit `checkout.session.expired`, giving a natural
  abandoned-payment terminal signal. A bare PaymentIntent has no expiry timer; an
  abandoned one simply stays at `requires_payment_method` unless the integrator
  cancels it.
  [Create a Session — expires_at](https://docs.stripe.com/api/checkout/sessions/create),
  [Types of events](https://docs.stripe.com/api/events/types),
  [PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle)
- **Metadata propagation.** Session `metadata` stays on the session; metadata
  intended for the PaymentIntent (and thus visible on `payment_intent.*` events and
  in Radar/reporting on the charge) must be passed separately as
  `payment_intent_data[metadata]`. `client_reference_id` (max 200 chars) exists only
  on the session, "used to reconcile the Session with your internal systems" — a
  ready-made slot for the getpaid payment id.
  [Create a Session](https://docs.stripe.com/api/checkout/sessions/create)
- **Session status model.** Sessions carry their own `status`
  (`open`/`complete`/`expired`) and `payment_status`
  (`paid`/`unpaid`/`no_payment_required`) on top of the PaymentIntent lifecycle;
  "The checkout session is complete. Payment processing may still be in progress."
  [Session object](https://docs.stripe.com/api/checkout/sessions/object)
- **Refunds and capture converge.** Once paid, both surfaces operate on the same
  PaymentIntent/Charge objects: capture, cancel and refund calls are identical
  (`getpaid` `charge()`, `release_lock()`, `start_refund()` would share code).
  [Place a hold — capture the funds](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method),
  [Create a refund](https://docs.stripe.com/api/refunds/create)

## Summary table

| Aspect | Checkout Session | PaymentIntent (direct) |
|---|---|---|
| Buyer-facing page | Stripe-hosted, `session.url` returned at creation | Merchant page with Stripe.js/Elements; no hosted URL |
| Fits `TransactionResult.redirect_url` | Yes, directly | No; needs `client_secret` + frontend JS (redirect only via `next_action.redirect_to_url` after confirm) |
| Adapter browser duties | Redirect + success/cancel URLs (`{CHECKOUT_SESSION_ID}` literal) | Payment form, publishable key, `confirmPayment`, `return_url` view |
| Manual capture | `payment_intent_data[capture_method]=manual` | `capture_method=manual` |
| Auth window (online cards) | ~7 days (Visa MIT 5 days); partial capture releases remainder; multicapture opt-in | same |
| Payment-method mix | Dashboard dynamic payment methods built in | `automatic_payment_methods` (default on API ≥ 2023-08-16); `allow_redirects` gates `return_url` need |
| Completion webhooks | `checkout.session.completed` / `.async_payment_succeeded` / `.async_payment_failed` / `.expired` (+ underlying `payment_intent.*`) | `payment_intent.succeeded` / `.payment_failed` / `.canceled` / `.amount_capturable_updated` / `.processing` |
| Abandonment signal | `checkout.session.expired` (30 min–24 h) | none built in; intent lingers unless canceled |
| Cancel pre-auth | expire session while `open`; cancel PI only at `requires_capture` | cancel PI in any pre-terminal status |
| `external_id` at prepare time | `cs_…` (PI created only at confirmation, API ≥ 2022-08-01) | `pi_…` immediately |
| Refunds | same API (`payment_intent`/`charge`, partial, repeatable) | same |
| stripe-python | `client.v1.checkout.sessions.create[_async]` | `client.v1.payment_intents.create[_async]` etc. |

## Sources

- Checkout Session create: https://docs.stripe.com/api/checkout/sessions/create
- Checkout Session object: https://docs.stripe.com/api/checkout/sessions/object
- Expire a Checkout Session: https://docs.stripe.com/api/checkout/sessions/expire
- Customize redirect behavior ({CHECKOUT_SESSION_ID}): https://docs.stripe.com/payments/checkout/custom-success-page
- How Checkout works: https://docs.stripe.com/payments/checkout/how-checkout-works
- Fulfill orders (Checkout): https://docs.stripe.com/checkout/fulfillment
- Payment Intents overview: https://docs.stripe.com/payments/payment-intents
- PaymentIntent lifecycle: https://docs.stripe.com/payments/paymentintents/lifecycle
- PaymentIntent create: https://docs.stripe.com/api/payment_intents/create
- PaymentIntent object: https://docs.stripe.com/api/payment_intents/object
- PaymentIntent cancel: https://docs.stripe.com/api/payment_intents/cancel
- Place a hold on a payment method: https://docs.stripe.com/payments/place-a-hold-on-a-payment-method
- Multicapture: https://docs.stripe.com/payments/multicapture
- Dynamic payment methods: https://docs.stripe.com/payments/payment-methods/dynamic-payment-methods
- stripe.confirmPayment (Stripe.js): https://docs.stripe.com/js/payment_intents/confirm_payment
- Types of events: https://docs.stripe.com/api/events/types
- Webhooks: https://docs.stripe.com/webhooks
- Create a refund: https://docs.stripe.com/api/refunds/create
- Changelog 2022-08-01 (deferred PaymentIntent creation): https://docs.stripe.com/changelog/2022-08-01/deferred-paymentintent-checkout-session.md
- stripe-python README: https://github.com/stripe/stripe-python/blob/master/README.md
- stripe-python webhook helper: https://github.com/stripe/stripe-python/blob/master/stripe/_webhook.py
- stripe-python services: https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_client.py, https://github.com/stripe/stripe-python/blob/master/stripe/checkout/_session_service.py, https://github.com/stripe/stripe-python/blob/master/stripe/_payment_intent_service.py
- Local contracts: `python-getpaid-core/src/getpaid_core/types.py`, `processor.py`, `enums.py`; `python-getpaid-paynow/src/getpaid_paynow/processor.py`
