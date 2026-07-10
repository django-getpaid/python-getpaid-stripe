# Stripe webhooks vs getpaid-core's callback contract

Scope note (ticket #3): this document collects the facts needed to map Stripe
webhooks onto getpaid-core's callback contract — `BaseProcessor.verify_callback(data,
headers, **kwargs)` (raises `InvalidCallbackError` on failure) and
`handle_callback(data, headers, **kwargs) -> PaymentUpdate | None`
(`python-getpaid-core/src/getpaid_core/processor.py`), with
`PaymentUpdate.provider_event_id` deduplicated by
`fsm.apply_payment_update` (`python-getpaid-core/src/getpaid_core/fsm.py`).
Facts only, each claim cited to a primary source (docs.stripe.com,
stripe-python source); the event→`PaymentEvent` mapping decision happens in a
separate ticket. The Checkout-vs-PaymentIntents surface comparison lives in the
sibling asset [`checkout-vs-payment-intents.md`](checkout-vs-payment-intents.md)
and is not duplicated here.

## 0. The local contract being mapped onto

- `PaymentFlow.handle_callback(payment, data, headers, **kwargs)` runs
  validators, then `processor.verify_callback(...)`, then
  `processor.handle_callback(...)`, then `apply_payment_update(payment, update)`
  and saves (`getpaid_core/flow.py`). The framework adapter must therefore
  resolve the `Payment` *before* the processor sees the event.
- `apply_payment_update` calls `_record_provider_event(payment,
  update.provider_event_id)`: event ids are appended to
  `payment.provider_data["applied_event_ids"]`; if the id was already recorded,
  the **entire update is silently skipped** (function returns the payment
  unchanged) (`getpaid_core/fsm.py`, `_record_provider_event` /
  `apply_payment_update`). A `provider_event_id` of `None`/empty always applies.
  Dedup scope is per-payment, not global.
- Sibling convention (`getpaid_paynow/processor.py`): `verify_callback` reads
  `kwargs["raw_body"]` and raises `InvalidCallbackError("Missing raw_body in
  callback kwargs. The framework adapter must pass the raw HTTP body.")` when
  absent; it accepts `bytes`/`bytearray` (decoded UTF-8) or `str`.

## 1. Signature verification

### Raw body requirement

Stripe: "Stripe requires the raw body of the request to perform signature
verification. If you're using a framework, make sure it doesn't manipulate the
raw body. Any manipulation to the raw body of the request causes the
verification to fail." ([Webhooks](https://docs.stripe.com/webhooks)). This is
exactly the paynow `raw_body` kwarg convention: the framework adapter must pass
the unmodified HTTP body into `verify_callback(**kwargs)`; the parsed `data`
dict cannot be re-serialized for verification.

### `stripe.Webhook.construct_event`

Source: [stripe/_webhook.py](https://github.com/stripe/stripe-python/blob/master/stripe/_webhook.py).

```python
class Webhook(object):
    DEFAULT_TOLERANCE = 300

    @staticmethod
    def construct_event(
        payload, sig_header, secret, tolerance=DEFAULT_TOLERANCE, api_key=None
    ):
        if hasattr(payload, "decode"):
            payload = payload.decode("utf-8")
        WebhookSignature.verify_header(payload, sig_header, secret, tolerance)
        data = json.loads(payload, object_pairs_hook=OrderedDict)
        event = Event._construct_from(values=data, ..., api_mode="V1")
        if event.object == "v2.core.event":
            raise ValueError(
                "You passed a thin event notification to Webhook.construct_event, "
                "which expects a webhook payload. Use StripeClient.parse_event_notification instead."
            )
        return event
```

- **Payload type**: `bytes` or `str`; bytes are decoded as UTF-8
  (`if hasattr(payload, "decode"): payload = payload.decode("utf-8")`).
  Verification runs on the raw string *before* JSON parsing.
- **Return value**: a `stripe.Event` object (a `StripeObject`, attribute- and
  dict-style access), not a plain dict — built via `Event._construct_from(...,
  api_mode="V1")` (stripe/_webhook.py).
- **Errors**: `stripe.error.SignatureVerificationError` (from
  `stripe._error`) on any header/signature/tolerance failure; a malformed JSON
  body raises `json.JSONDecodeError` (a `ValueError` subclass) from
  `json.loads`; passing a v2 thin-event payload raises `ValueError`
  (stripe/_webhook.py). A processor `verify_callback` must translate these
  into `InvalidCallbackError` per the getpaid-core contract
  (`getpaid_core/processor.py` docstring).
- **Same API on the client object**: `StripeClient.construct_event(payload,
  sig_header, secret, tolerance=Webhook.DEFAULT_TOLERANCE)` exists on
  `StripeClient`
  ([stripe/_stripe_client.py](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_client.py)).
- **No async variant**: the stripe-python tree contains no
  `construct_event_async` / `async def construct_event` (grep over
  `stripe/` at master, 2026-07). Async support in stripe-python is for
  *request-making* methods, "by suffixing the method name with `_async`"
  ([README — Async](https://github.com/stripe/stripe-python#async)).
  Verification is pure computation (HMAC over the payload, no I/O), so it can
  be called directly inside getpaid's `async def verify_callback` without
  blocking concerns.

### Header scheme and tolerance

- "The `Stripe-Signature` header included in each signed event contains a
  timestamp and one or more signatures that you must verify. The timestamp has
  a `t=` prefix, and each signature has a *scheme* prefix" — e.g.
  `t=1492774577,v1=5257a869…,v0=6ffbb59b…` ([Webhooks](https://docs.stripe.com/webhooks)).
  stripe-python only checks the `v1` scheme (`EXPECTED_SCHEME = "v1"`) and
  computes `HMAC-SHA256(secret, f"{timestamp}.{payload}")`, comparing with
  `secure_compare` against **every** `v1=` signature in the header
  (`any(secure_compare(expected_sig, s) for s in signatures)`)
  (stripe/_webhook.py).
- **Tolerance**: "Our libraries have a default tolerance of 5 minutes between
  the timestamp and the current time. You can change this tolerance by
  providing an additional parameter when verifying signatures"
  ([Webhooks](https://docs.stripe.com/webhooks)); in stripe-python this is
  `DEFAULT_TOLERANCE = 300` (seconds), passed as the `tolerance` parameter.
  The check is `if tolerance and timestamp < time.time() - tolerance: raise
  SignatureVerificationError(...)` — so `0`/`None` disables the recency check
  entirely; Stripe warns "Don't use a tolerance value of `0`"
  (stripe/_webhook.py; [Webhooks](https://docs.stripe.com/webhooks)).

### Endpoint secret, rotation, multiple secrets

- Each endpoint has its own signing secret with the `whsec_` prefix; "Stripe
  generates a unique secret key for each endpoint. If you use the same endpoint
  for both test and live API keys, the secret is different for each one"
  ([Webhooks](https://docs.stripe.com/webhooks)).
- Rotation: "You can have multiple signatures with the same scheme-secret pair
  when you roll an endpoint's secret, and keep the previous secret active for
  up to 24 hours. During this time, your endpoint has multiple active secrets
  and Stripe generates one signature for each secret"
  ([Webhooks](https://docs.stripe.com/webhooks)). Because Stripe signs with
  *all* active secrets and stripe-python accepts a match against *any* `v1=`
  signature, a single configured secret keeps verifying throughout the roll
  window; `construct_event` itself takes exactly one `secret` argument
  (stripe/_webhook.py) — verifying against several locally stored secrets
  would require multiple calls, which the library does not provide.

## 2. Event catalogue (one-off payments, manual capture, refunds)

All descriptions below are quoted from
[Types of events](https://docs.stripe.com/api/events/types); `data.object` is
"Object containing the API resource relevant to the event"
([Event object](https://docs.stripe.com/api/events/object)).

### `payment_intent.*` — `data.object` is a PaymentIntent

| Event | When it occurs (verbatim) |
|---|---|
| `payment_intent.created` | "Occurs when a new PaymentIntent is created." |
| `payment_intent.processing` | "Occurs when a PaymentIntent has started processing." |
| `payment_intent.requires_action` | "Occurs when a PaymentIntent transitions to requires_action state" |
| `payment_intent.amount_capturable_updated` | "Occurs when a PaymentIntent has funds to be captured. Check the `amount_capturable` property on the PaymentIntent to determine the amount that can be captured." |
| `payment_intent.succeeded` | "Occurs when a PaymentIntent has successfully completed payment." |
| `payment_intent.payment_failed` | "Occurs when a PaymentIntent has failed the attempt to create a payment method or a payment." |
| `payment_intent.canceled` | "Occurs when a PaymentIntent is canceled." |
| `payment_intent.partially_funded` | "Occurs when funds are applied to a customer_balance PaymentIntent and the 'amount_remaining' changes." (bank-transfer/customer-balance only) |

Manual-capture flow (`capture_method=manual`): "when a customer completes the
payment process on a PaymentIntent with manual capture, it triggers the
`payment_intent.amount_capturable_updated` event"; "After the payment method
is authorized, the PaymentIntent status transitions to `requires_capture`";
capture "moves it to `processing` or `succeeded` depending on the payment
method"; uncaptured authorizations expire (7 days default for online card
payments) — "If the authorization expires before you capture the funds, the
funds are released and the payment status changes to `canceled`"; "A partial
capture automatically releases the remaining amount"
([Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method);
[PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle)).
So the manual-capture event sequence is: auth →
`payment_intent.amount_capturable_updated` → capture →
`payment_intent.succeeded` (or `payment_intent.canceled` on
cancel/expiry). The hold docs do not name a distinct event for automatic
expiry beyond the status changing to `canceled`.

Async/delayed payment methods: `processing` "Occurs after required actions are
handled and the payment uses an asynchronous payment method… These types of
payment methods can take up to a few days to process"
([PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle)),
i.e. `payment_intent.processing` precedes a later
`payment_intent.succeeded`/`payment_intent.payment_failed`.

### `charge.*` — `data.object` is a Charge (Dispute for `charge.dispute.*`)

| Event | When it occurs (verbatim) |
|---|---|
| `charge.succeeded` | "Occurs whenever a charge is successful." |
| `charge.failed` | "Occurs whenever a failed charge attempt occurs." |
| `charge.captured` | "Occurs whenever a previously uncaptured charge is captured." (i.e. the manual-capture capture step also emits a charge-level event) |
| `charge.refunded` | "Occurs whenever a charge is refunded, including partial refunds. Listen to `refund.created` for information about the refund." |
| `charge.dispute.created` | "Occurs whenever a customer disputes a charge with their bank." |
| `charge.dispute.updated` / `.closed` / `.funds_withdrawn` / `.funds_reinstated` | Dispute lifecycle events; `data.object` is a Dispute. Deep dive out of scope for this ticket — they exist and would need handling only if the plugin maps disputes to `FraudEvent`. |

### `refund.*` — `data.object` is a Refund

| Event | When it occurs (verbatim) |
|---|---|
| `refund.created` | "Occurs whenever a refund is created." |
| `refund.updated` | "Occurs whenever a refund is updated." |
| `refund.failed` | "Occurs whenever a refund has failed." |

Refund docs: "At a minimum, Stripe recommends that you listen for the
`refund.created` event"; refund statuses are `pending`, `succeeded`, `failed`,
`canceled`, `requires_action`; "In the rare instance that a refund fails, we
notify you using the `refund.failed` event… you need to arrange an alternative
way to provide your customer with a refund"; multiple partial refunds are
allowed but "you can't refund a total greater than the original charge amount"
([Refunds](https://docs.stripe.com/refunds)).

### `checkout.session.*` — `data.object` is a Checkout Session

| Event | When it occurs (verbatim) |
|---|---|
| `checkout.session.completed` | "Occurs when a Checkout Session has been successfully completed." |
| `checkout.session.expired` | "Occurs when a Checkout Session is expired." |
| `checkout.session.async_payment_succeeded` | "Occurs when a payment intent using a delayed payment method finally succeeds." |
| `checkout.session.async_payment_failed` | "Occurs when a payment intent using a delayed payment method fails." |

For delayed payment methods, `checkout.session.completed` fires when checkout
completes but the money may not have moved yet: "Delayed payment methods
generate a `checkout.session.async_payment_succeeded` event when payment
succeeds later. The status of the object is in processing until the payment
status either succeeds or fails"; the fulfillment guide's handler checks
`payment_status != 'unpaid'` before fulfilling, with `payment_status` taking
the values `paid`, `unpaid`, `no_payment_required`
([Fulfill orders](https://docs.stripe.com/checkout/fulfillment)). Note that
for a session in `payment` mode the underlying `payment_intent.*` and
`charge.*`/`refund.*` events fire as well (see sibling asset §4).

## 3. Idempotency and ordering

- **`event.id`**: "Unique identifier for the object" on the Event
  ([Event object](https://docs.stripe.com/api/events/object)); ids use the
  `evt_` prefix (all Event examples in the API reference, e.g.
  [Event object](https://docs.stripe.com/api/events/object)). Stripe's own
  duplicate-guard guidance is precisely the `provider_event_id` pattern: "You
  can guard against duplicated event receipts by logging the event IDs you've
  processed, and then not processing already-logged events"
  ([Webhooks — best practices](https://docs.stripe.com/webhooks)). This slots
  directly into `PaymentUpdate.provider_event_id` →
  `provider_data["applied_event_ids"]` dedup in `getpaid_core/fsm.py`
  (per-payment scope, silent skip on repeat).
- **Duplicates**: "Webhook endpoints might occasionally receive the same event
  more than once." Additionally, "In some cases, two separate Event objects
  are generated and sent. To identify these duplicates, use the ID of the
  object in `data.object` along with the `event.type`"
  ([Webhooks](https://docs.stripe.com/webhooks)) — i.e. `event.id`-based dedup
  does not catch *distinct* Event objects describing the same occurrence.
- **Ordering**: "Stripe doesn't guarantee the delivery of events in the order
  that they're generated." … "Make sure that your event destination isn't
  dependent on receiving events in a specific order"
  ([Webhooks](https://docs.stripe.com/webhooks)).
- **Retries**: "Stripe attempts to deliver events to your destination for up
  to three days with an exponential back off in live mode." In test
  mode/sandboxes: "We retry event deliveries created in a sandbox three times
  over the course of a few hours" ([Webhooks](https://docs.stripe.com/webhooks)).
- **`event.request.idempotency_key`**: "The idempotency key transmitted during
  the request, if any" (populated for events on or after 2017-05-23);
  `request.id` is the "ID of the API request that caused the event. If null,
  the event was automatic" ([Event object](https://docs.stripe.com/api/events/object)).
  This links an event back to the API call (e.g. our own capture/refund call)
  that produced it.
- **API-version pinning of payloads**: `event.api_version` is "The Stripe API
  version used to render `data` when the event was created. The contents of
  `data` never change, so this value remains static regardless of the API
  version currently in use" ([Event object](https://docs.stripe.com/api/events/object)).
  "The API version in your account settings when the event occurs dictates the
  API version, and therefore the structure of an Event sent to your
  destination"; endpoints can pin their own version, and existing Event
  objects are never retroactively restructured
  ([Webhooks — API versions](https://docs.stripe.com/webhooks)). stripe-python's
  types "describe the Stripe API version that was the latest at the time of
  release… If you're on an older API version or have a webhook endpoint tied
  to an older version, be aware that the data you see at runtime may not match
  the types" ([stripe-python README](https://github.com/stripe/stripe-python#types-and-api-versions)).
- **`stripe.Event.construct_from`**: `StripeObject.construct_from(values, key,
  stripe_version=None, stripe_account=None, ..., api_mode="V1")` builds a
  typed `Event` from an already-parsed dict **without any signature
  verification** ([stripe/_stripe_object.py](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_object.py)) —
  it is a deserialization helper (e.g. for stored payloads or after external
  verification), not a security boundary; `construct_event` is the verifying
  path.

## 4. Linking events back to our payment

The adapter must resolve the local `Payment` before `PaymentFlow.handle_callback`
runs (`getpaid_core/flow.py`), so the correlation handles below are what a
webhook endpoint has to work with.

- **Metadata does not propagate automatically**: "An object's metadata doesn't
  automatically copy to related objects. To view an object's metadata, you
  must inspect that object" ([Metadata](https://docs.stripe.com/metadata)).
  Documented one-time-snapshot exceptions relevant here:
  - **PaymentIntent → Charge**: "When a PaymentIntent creates a Charge, the
    metadata copies to the Charge in a one-time snapshot. Updates to the
    PaymentIntent's metadata won't apply to the Charge"
    ([Metadata](https://docs.stripe.com/metadata)). So metadata set on the
    intent is visible in `charge.*` payloads.
  - **Checkout Session → PaymentIntent**: the session's *own* `metadata` does
    NOT copy to the intent; instead `payment_intent_data[metadata]` at session
    creation sets metadata *on the PaymentIntent being created*
    (`payment_intent_data` is "A subset of parameters to be passed to
    PaymentIntent creation for Checkout Sessions in `payment` mode")
    ([Create a Session](https://docs.stripe.com/api/checkout/sessions/create);
    [Metadata](https://docs.stripe.com/metadata), which shows exactly this
    `payment_intent_data[metadata][order_id]` pattern).
  - Chain: `payment_intent_data.metadata` → PaymentIntent → (snapshot) Charge
    covers `payment_intent.*` and `charge.*` events; the session's own
    `metadata` appears only in `checkout.session.*` payloads. Refund objects
    have their own metadata; no documented automatic copy from
    charge/intent to Refund was found.
- **Checkout Session handles** (present in every `checkout.session.*` payload,
  since `data.object` is the session):
  - `client_reference_id`: "A unique string to reference the Checkout Session.
    This can be a customer ID, a cart ID, or similar, and can be used to
    reconcile the Session with your internal systems" (max 200 chars)
    ([Session object](https://docs.stripe.com/api/checkout/sessions/object);
    [Create a Session](https://docs.stripe.com/api/checkout/sessions/create)).
  - `metadata` on the session (up to 50 keys, 40-char keys, 500-char values —
    [Metadata](https://docs.stripe.com/metadata)); Stripe's fulfillment docs
    show storing an internal id (`metadata[cart_id]`) and reading it back from
    the `checkout.session.completed` payload
    ([Metadata — use cases](https://docs.stripe.com/metadata)).
  - `payment_intent`: "The ID of the PaymentIntent for Checkout Sessions in
    `payment` mode" ([Session object](https://docs.stripe.com/api/checkout/sessions/object)) —
    nullable; on current API versions the PaymentIntent is created at session
    completion, so it is null while the session is open (see sibling asset §7
    for the timing details). A completed-session payload carries the `pi_…`
    id, letting the handler store/refresh `external_id`.
- **PaymentIntent handles**: `metadata` on the intent appears in every
  `payment_intent.*` payload (the intent *is* `data.object`), and the intent's
  own `id` (`pi_…`) correlates against a stored
  `Payment.external_id` — mirroring paynow's `handle_callback`, which matches
  the notification's provider id against `self.payment.external_id`
  (`getpaid_paynow/processor.py`). `charge.*` payloads carry the charge's
  `payment_intent` id and the metadata snapshot; `refund.*` payloads carry the
  Refund object, whose `charge`/`payment_intent` fields point back at the
  payment ([Refunds](https://docs.stripe.com/refunds) — refunds are created
  against "a `charge` or a `payment_intent` id").
- `checkout.session.expired` payloads contain the session (with
  `client_reference_id`/`metadata`) but a null `payment_intent` on current API
  versions (sibling asset §7).

## 5. Endpoint and configuration facts

- **Event-type filtering per endpoint**: "You can register and create one
  endpoint to handle several different event types at the same time, or set up
  individual endpoints for specific events"; "Configure your webhook endpoints
  to receive only the types of events required by your integration"; the
  `enabled_events` list can be changed in the Dashboard or via the API
  ([Webhooks](https://docs.stripe.com/webhooks)).
- **Snapshot vs thin events**: a snapshot event "sends a notification
  containing the complete `Event` object, which includes an
  eventually-consistent snapshot of the updated resource" and is "Versioned by
  API version"; a thin event "sends a lightweight notification that includes
  only limited information about the v2 `Event` object and the affected
  object. You can make a subsequent API call to fetch the complete `Event`
  object or related resource" and is "Unversioned"
  ([Event destinations](https://docs.stripe.com/event-destinations)). All
  `payment_intent.*`/`charge.*`/`refund.*`/`checkout.session.*` types in §2
  are classic v1 snapshot events. stripe-python supports thin events via
  `StripeClient.parse_event_notification(raw, sig_header, secret,
  tolerance=...)` — "the V2 equivalent of `construct_event()`" — which
  verifies the same `Stripe-Signature` header and returns typed
  `EventNotification` objects; `Webhook.construct_event` explicitly rejects
  thin payloads with `ValueError`
  ([stripe/_stripe_client.py](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_client.py);
  [stripe/_webhook.py](https://github.com/stripe/stripe-python/blob/master/stripe/_webhook.py)).
- **Test vs live mode**: the signing secret differs per mode even for the same
  URL ("If you use the same endpoint for both test and live API keys, the
  secret is different for each one"); `event.livemode` marks the mode of each
  event; live endpoints "must be publicly accessible **HTTPS** URLs"
  ([Webhooks](https://docs.stripe.com/webhooks);
  [Event object](https://docs.stripe.com/api/events/object)).
- **Stripe CLI for local dev**: `stripe listen --forward-to
  localhost:4242/webhook` forwards events to a local endpoint and prints its
  own signing secret ("Ready! Your webhook signing secret is …"); flags exist
  to filter events (`--events`), forward thin events (`--forward-thin-to`),
  skip TLS verification (`--skip-verify`), and mirror a registered endpoint's
  subscription (`--load-from-webhooks-api`)
  ([Webhooks — test locally](https://docs.stripe.com/webhooks)).

## Sources

- getpaid-core: `python-getpaid-core/src/getpaid_core/processor.py`,
  `types.py`, `flow.py`, `fsm.py` (local, read 2026-07-10)
- getpaid-paynow: `python-getpaid-paynow/src/getpaid_paynow/processor.py` (local)
- Sibling asset: `docs/research/checkout-vs-payment-intents.md`
- [Webhooks](https://docs.stripe.com/webhooks) (fetched as
  https://docs.stripe.com/webhooks.md)
- [Event object](https://docs.stripe.com/api/events/object)
- [Types of events](https://docs.stripe.com/api/events/types)
- [Place a hold on a payment method](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
- [PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle)
- [Refunds](https://docs.stripe.com/refunds)
- [Fulfill orders (Checkout)](https://docs.stripe.com/checkout/fulfillment)
- [Metadata](https://docs.stripe.com/metadata)
- [Checkout Session object](https://docs.stripe.com/api/checkout/sessions/object)
- [Create a Checkout Session](https://docs.stripe.com/api/checkout/sessions/create)
- [Event destinations](https://docs.stripe.com/event-destinations)
- stripe-python source at master (2026-07-10):
  [stripe/_webhook.py](https://github.com/stripe/stripe-python/blob/master/stripe/_webhook.py),
  [stripe/_stripe_client.py](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_client.py),
  [stripe/_stripe_object.py](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_object.py),
  [README](https://github.com/stripe/stripe-python#async)
