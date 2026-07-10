# python-getpaid-stripe — implementation specification

Status: **build-ready**. Produced by the wayfinder effort at
[map issue #1](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/1);
every design decision below links its deciding ticket. Research backing lives in
[`docs/research/`](docs/research/): [surface comparison](docs/research/checkout-vs-payment-intents.md),
[webhooks](docs/research/stripe-webhooks.md), [amounts & currencies](docs/research/stripe-amounts-currencies.md),
[refunds](docs/research/stripe-refunds.md). A fresh agent should be able to implement
the plugin from this document plus those four assets alone.

Implementation and PyPI release are a **separate effort** — this document is its input.

## 1. Overview and scope

A Stripe payment provider plugin for the getpaid ecosystem: an async
`BaseProcessor` subclass from `getpaid-core` (>= 3.2.0), shaped like
`python-getpaid-paynow` (the reference sibling), wrapping **Stripe Checkout
Sessions in payment mode** via the official `stripe-python` SDK (async
`*_async` methods; `pip install stripe[async]`).

**In scope**: one-off payments (redirect flow), manual capture / pre-auth
(`charge()` / `release_lock()` — first plugin to exercise core's pre-auth FSM
states), refunds (`start_refund()` / `cancel_refund()`), webhook handling,
pull-status, Radar fraud events, a `getpaid_stripe.simulator` sub-package for
`python-getpaid-simulator`.

**Out of scope** (map's ruling, not implementation laziness): direct
Payment-Intents/client-secret flow, subscriptions/recurring
([deferred — core lacks a recurring-agreement entity](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/12)),
Stripe Connect, saved cards / off-session payments.

## 2. Package layout

Mirror the paynow shape:

```
src/getpaid_stripe/
    __init__.py          # __version__
    processor.py         # StripeProcessor
    types.py             # plugin-local typed dicts / constants
    currencies.py        # Stripe exponent table + to_minor()/from_minor()
    simulator/           # optional-extra sub-package (§10)
        __init__.py      # get_plugin re-export
        plugin.py  routes.py  transitions.py  signing.py  webhooks.py
```

`pyproject.toml` (paynow's as template):

```toml
[project.entry-points."getpaid.backends"]
stripe = 'getpaid_stripe.processor:StripeProcessor'

[project.entry-points."getpaid.simulator.providers"]
stripe = 'getpaid_stripe.simulator:get_plugin'
```

Dependencies: `python-getpaid-core>=3.2.0`, `stripe[async]` (pin the tested
major); simulator extras depend on `python-getpaid-simulator` like paynow's.

## 3. Processor class surface

```python
class StripeProcessor(BaseProcessor):
    slug = "stripe"
    display_name = "Stripe"
    accepted_currencies = STRIPE_PRESENTMENT_CURRENCIES  # §5
    logo_url = ...          # Stripe brand asset
    sandbox_url = ""        # N/A — see §4 (mode lives in the key)
    production_url = ""     # N/A
```

`get_paywall_baseurl()` is **not used**: Stripe is always `api.stripe.com`;
test vs live mode is carried by the key, not a URL
([config ticket](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/9)).

`__init__` fail-fast (paynow precedent, `CredentialsError`): raise if
`api_key` is missing or has no known prefix (`sk_test_`, `sk_live_`, `rk_`),
or if `webhook_secret` is missing / not `whsec_…`. Build one `StripeClient`
per processor instance with `api_key` and `max_network_retries`; all API calls
use its `*_async` methods.

## 4. Configuration schema

Decided at the [configuration ticket](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/9):

| Setting | Type | Required | Default | Notes |
|---|---|---|---|---|
| `api_key` | str | **yes** | — | `sk_test_`/`sk_live_`/`rk_` prefix; fail-fast |
| `webhook_secret` | str | **yes** | — | `whsec_…`, per-endpoint, test/live distinct; fail-fast (flow is unusable without webhooks) |
| `success_url` | str template | **yes** | — | `{payment_id}` formatted by the plugin; Stripe's literal `{CHECKOUT_SESSION_ID}` passed through |
| `cancel_url` | str template | **yes** | — | same semantics |
| `capture_method` | `"automatic"` \| `"manual"` | no | `"automatic"` | per-payment kwarg override |
| `session_expires_in` | int minutes 30–1440 | no | absent → Stripe 24 h | maps to Checkout `expires_at` |
| `max_network_retries` | int | no | `2` | safe: SDK auto-generates idempotency keys |

Deliberate omissions: **no publishable key** (nothing in a hosted-Checkout
flow consumes it), **no `sandbox` flag** (derive: property
`is_sandbox = api_key.startswith(("sk_test_", "rk_test_"))` — restricted
keys analogous per the deciding ticket; a flag that can contradict the key
is a lie waiting to happen), no HTTP-timeout knob in v1.

*(Amended during implementation: two **internal, undocumented** config
keys exist for tests and the simulator wiring only — `api_base`
(overrides the SDK base address) and `http_client` (injects a stripe
HTTP client). They are not part of the public schema.)*

Webhook-endpoint `enabled_events` is **Stripe Dashboard configuration**, not a
setting; the exact list is in §7.

## 5. Amounts and currencies

Backing: [amounts research](docs/research/stripe-amounts-currencies.md). All
Stripe amounts are `int` minor units; currency codes lowercase ISO 4217.

- `currencies.py` ships a **Stripe-specific exponent table** — never derive
  from ISO 4217 (Stripe treats ISK and UGX as two-decimal; ISO says zero).
  Zero-decimal (16): BIF CLP DJF GNF JPY KMF KRW MGA PYG RWF UGX* VND VUV XAF
  XOF XPF (*UGX: API representation is two-decimal with `00` — encode per the
  special-cases section of the research doc). Three-decimal: BHD JOD KWD OMR
  TND — last digit must be 0 (round to the nearest ten).
- `to_minor(amount: Decimal, currency: str) -> int`: scale by the Stripe
  exponent, quantize with explicit `ROUND_HALF_UP` (paynow precedent;
  `quantize` defaults to banker's rounding and `int()` truncates).
- `from_minor(minor: int, currency: str) -> Decimal`:
  `Decimal(minor).scaleb(-exponent)` using **the payload's own `currency`
  field** — exact, never via float.
- `accepted_currencies`: a broad ClassVar list of Stripe presentment
  currencies (subclass to narrow). Core's registry does
  `currency in accepted_currencies`, so "unrestricted" is inexpressible —
  a concrete sequence is mandatory.

## 6. `prepare_transaction()` — Checkout Session creation

Decided at the [surface ticket](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/6).
Create a **payment-mode** Checkout Session (`ui_mode: hosted` — the API
literal; "hosted_page" was descriptive) with:

- `line_items`: single item from the payment (name/description per adapter
  kwargs), `amount = to_minor(payment.amount, currency)`, lowercase currency;
- `success_url` / `cancel_url` from settings templates (kwargs may override
  per call); format `{payment_id}`, pass `{CHECKOUT_SESSION_ID}` literally;
- **correlation, belt and braces**: `client_reference_id = payment.id`,
  session `metadata["payment_id"]`, and
  `payment_intent_data["metadata"]["payment_id"]` (Stripe copies the latter
  onto the PaymentIntent and its Charge — metadata does NOT auto-propagate
  otherwise);
- `payment_intent_data["capture_method"] = "manual"` when manual capture is
  selected (setting or kwarg, §8);
- `expires_at` from `session_expires_in` when set;
- payment methods: **automatic** — no `payment_method_types` (account
  configuration decides).

Return
`TransactionResult(method="GET", redirect_url=session.url, external_id=session.id, provider_data={"session_id": cs_…, "expires_at": …})`.
*(Amended during implementation: core's `BackendMethod` enum has no
REDIRECT — the redirect flow uses `GET`, the paynow precedent.)*

**Id lifecycle**: under deferred PI creation only `cs_…` exists at prepare
time. The first webhook carrying the PaymentIntent id re-points the payment:
`PaymentUpdate(external_id="pi_…", provider_data={"session_id": "cs_…"})`.
Invariant: **`external_id` is always the id money operations act on** —
`charge()`, `release_lock()`, `start_refund()` use it directly.

## 7. Webhooks

Backing: [webhooks research](docs/research/stripe-webhooks.md); mapping decided
at the [event-mapping ticket](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/7).

### `verify_callback(data, headers, **kwargs)`

`stripe.Webhook.construct_event(kwargs["raw_body"], headers["Stripe-Signature"], webhook_secret)`
(default 300 s tolerance). **Requires the raw request body** — framework
adapters must pass `raw_body` (paynow convention). Pure computation, no async
variant needed. `SignatureVerificationError` / missing raw_body →
`InvalidCallbackError`. v2 "thin" payloads → `InvalidCallbackError`; this
plugin is v1-events-only. *(Amended during implementation: stripe-python
12.x's `construct_event` does not yet raise ValueError on thin payloads —
that guard exists only on master — so the processor checks the v1 event
envelope, `event.object == "event"`, explicitly.)*

### `handle_callback(data, headers, **kwargs) -> PaymentUpdate | None`

Principles: **`payment_intent.*` is authoritative for money-state** (session
success events would double-fire with distinct `evt_` ids); refunds are
**status-driven** off the Refund payload; `payment_intent.canceled` splits by
`capture_method`. Every mapped update carries
`provider_event_id = event.id` (`evt_…` — core's idempotent dedup) and
`external_id = pi_…` where the payload exposes it. Amounts via
`from_minor(payload amount, payload currency)`.

| Event | Condition | PaymentUpdate |
|---|---|---|
| `checkout.session.completed` | — | no event; `external_id → pi_…` (if present), `provider_data={session_id, payment_status}` |
| `checkout.session.expired` | — | `FAILED` (only abandonment signal — no PI may exist) |
| `checkout.session.async_payment_succeeded/failed` | — | ignored (PI twins are truth) |
| `payment_intent.amount_capturable_updated` | — | `LOCKED`, `locked_amount=from_minor(amount_capturable)`; stamp `provider_data["locked_at"]=event.created` |
| `payment_intent.succeeded` | — | `PAYMENT_CAPTURED`, `paid_amount=from_minor(amount_received)` |
| `payment_intent.payment_failed` | — | `FAILED` |
| `payment_intent.canceled` | `capture_method=manual` | `LOCK_RELEASED`; `cancellation_reason` → `provider_data` |
| `payment_intent.canceled` | `capture_method=automatic` | `FAILED` |
| `payment_intent.created/processing/requires_action/partially_funded` | — | ignored |
| `refund.created/updated/failed` | `status ∈ {pending, requires_action}` | `REFUND_REQUESTED`, `provider_data={"refund_id": …}` |
| 〃 | `status = succeeded` | `REFUND_CONFIRMED`, `refunded_amount=from_minor(amount)` |
| 〃 | `status ∈ {failed, canceled}` | `REFUND_CANCELLED` |
| `charge.*` (all, incl. `charge.refunded`, deprecated `charge.refund.updated`) | — | ignored — duplicates PI/Refund truth |
| `review.opened` | — | `FraudEvent.REVIEW` |
| `review.closed` | `closed_reason = approved` | `FraudEvent.ACCEPT` |
| `review.closed` | otherwise | `FraudEvent.REJECT` *(verify payload semantics during implementation)* |
| anything else | — | log-and-ignore, return `None` |

Correlation back to the payment: `payment_id` from metadata /
`client_reference_id` (§6); refund payloads carry `payment_intent`
back-references. Duplicate deliveries and unordered arrival are expected
(at-least-once); core's `provider_event_id` dedup plus the stateless mapping
handle both. Subscription-born traffic on a shared account (`invoice.*`,
uncorrelatable `payment_intent.*`) falls to log-and-ignore **by design**
([subscriptions deferral](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/12)).

**Dashboard `enabled_events`** (documented for operators): exactly
`checkout.session.completed`, `checkout.session.expired`,
`payment_intent.amount_capturable_updated`, `payment_intent.succeeded`,
`payment_intent.payment_failed`, `payment_intent.canceled`, `refund.created`,
`refund.updated`, `refund.failed`, `review.opened`, `review.closed`.

### `fetch_payment_status(**kwargs) -> PaymentUpdate | None`

Pull path, same rules stateless: `cs_…` → retrieve Session, `pi_…` → retrieve
PaymentIntent. `requires_capture` → `LOCKED`; `succeeded` →
`PAYMENT_CAPTURED`; `canceled` → by `capture_method`; session `expired` →
`FAILED`; session `open` / PI `processing`/`requires_action`/
`requires_payment_method` → `None`. No `provider_event_id` on pull updates.
This is the **auth-expiry backstop** (§8).

## 8. Pre-auth / manual capture

Decided at the [pre-auth ticket](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/8).

- **Opt-in**: `capture_method` setting, `prepare_transaction(capture_method="manual")`
  overrides. Caveat to document: manual capture narrows offered payment
  methods (cards/Klarna/PayPal yes; ACH, iDEAL no) — behavior under automatic
  payment methods to verify during implementation.
- **`charge(amount=None)`**: `payment_intents.capture_async(external_id[, amount_to_capture=to_minor(amount)])`.
  Partial capture: Stripe **auto-releases the remainder**. Returns
  `ChargeResult(amount_charged=<captured Decimal>, success=<API response ok>, async_call=True)`
  — core applies `CHARGE_REQUESTED`; the single authoritative
  `PAYMENT_CAPTURED` arrives via `payment_intent.succeeded`. Legal only at
  `requires_capture`; otherwise raise `ChargeFailure`.
- **`release_lock()`**: `payment_intents.cancel_async(external_id)` — legal
  only at `requires_capture` (that *is* the lock state); returns the full
  locked amount (`from_minor(amount_capturable)`). No partial release exists.
  Other states raise `LockFailure`. Webhook confirms `LOCK_RELEASED`.
- **Auth expiry**: ~7-day card window (variance: Visa MIT 5 d, some in-person
  2 d, Japan 30 d) documented for operators; `provider_data["locked_at"]`
  stamped at lock (§7); no reliable expiry webhook (flagged doc gap) — the
  deterministic path is `fetch_payment_status()`; **polling cadence belongs to
  the application**, the plugin ships no scheduler.

## 9. Refunds

Backing: [refunds research](docs/research/stripe-refunds.md).

- **`start_refund(amount=None)`**: `refunds.create_async(payment_intent=external_id[, amount=to_minor(amount)], metadata={"payment_id": …})`
  (refund metadata does not inherit — set at creation). Omitted amount = full
  refund; multiple partials allowed up to the captured, unrefunded amount.
  Returns `RefundResult(amount=<requested/actual Decimal>, provider_data={"refund_id": re_…, "status": …})`.
  Persist the refund id via `provider_data["refund_id"]` (paynow convention;
  caveat: the slot holds the latest refund only). Stripe errors
  (`charge_already_refunded`, over-refund, disputed) → `RefundFailure`.
  `reason` is not exposed as a plugin API (Stripe allows only three values and
  `fraudulent` has block-list side effects).
- **`cancel_refund()`**: `refunds.cancel_async(provider_data["refund_id"])`.
  Works **only** for refunds in `requires_action` (bank-transfer-style
  methods); **card refunds cannot be canceled via API** (Dashboard-only) — on
  Stripe rejection return `False`, on success `True`. Document loudly that for
  card payments this effectively always returns `False`.
- Boundary: an uncaptured intent is **canceled, never refunded**
  (`release_lock()` territory); refunds apply post-capture only. Expired
  authorizations can produce Stripe-generated refunds
  (`reason=expired_uncaptured_charge`) arriving unprompted — handled by the
  status-driven mapping (§7).

## 10. Simulator sub-package

Decided at the [simulator ticket](http://192.168.129.37:30008/minder/python-getpaid-stripe/issues/10);
mirrors `getpaid_paynow/simulator/` (SPI: `SimulatorProviderPlugin`, slug
`"stripe"`, `SIMULATOR_STRIPE_*` env config).

- **API handlers**: `checkout.sessions.create/expire`,
  `payment_intents.retrieve/capture/cancel`, `refunds.create/cancel`. Fake
  ids: `cs_sim_…`, `pi_sim_…`, `re_sim_…`, `evt_sim_…`, `ch_sim_…`.
- **UI handlers**: fake hosted checkout page — pay / pay-delayed / decline /
  abandon; manual-capture sessions authorize (→ `amount_capturable_updated`).
- **Ops endpoint** forces actor-less transitions: auth-expiry time-travel,
  late refund failure after `succeeded`, Radar `review.opened/closed`.
- **Emitted events**: the 11 mapped ones **plus** ignore-list traffic
  (`charge.succeeded/captured/refunded`, `checkout.session.async_payment_*`)
  to prove ignore behavior under fire.
- **Transitions** follow real lifecycles: session `open → complete|expired`;
  PI `requires_payment_method → processing → requires_capture|succeeded|canceled`;
  refund `pending → succeeded|failed|canceled` (+ `requires_action` so
  `cancel_refund()`'s one legal path is exercisable).
- **Signing**: genuine `Stripe-Signature: t=…,v1=HMAC-SHA256("{t}.{body}", whsec_sim_…)`
  so the processor's real `construct_event` path runs.
- **Honesty**: payloads are minimal (envelope + fields the processor reads);
  simulator tests round-trip every payload through stripe-python's typed
  classes + a real `construct_event` call so SDK upgrades flag drift in CI.
  No stripe-mock, no recorded fixtures.

## 11. Test plan (TDD)

Write tests first per area; table-driven where a table exists in this spec.

1. **Currency conversion** (`currencies.py`): exponent table spot-checks (JPY,
   KWD nearest-ten, ISK/UGX two-decimal quirk, PLN), `ROUND_HALF_UP` edge
   (`Decimal("10.005")` → 1001), `from_minor` exactness, round-trip property
   test across the table.
2. **Config validation**: fail-fast matrix (missing/malformed `api_key`,
   `webhook_secret`; valid prefixes incl. `rk_`); `is_sandbox` derivation;
   defaults (`capture_method`, `max_network_retries=2`).
3. **`prepare_transaction`**: session params (correlation triple, urls with
   `{payment_id}` + literal `{CHECKOUT_SESSION_ID}`, `capture_method`
   plumbing, `expires_at`); `TransactionResult` shape.
4. **`verify_callback`**: valid signature passes; tampered body, wrong secret,
   stale timestamp, missing `raw_body`, thin-event payload → `InvalidCallbackError`.
5. **`handle_callback`**: one test per row of the §7 table (fixture payloads),
   including: both-events-fire dedup story (session.completed then
   pi.succeeded), id promotion, canceled×capture_method split, refund
   status-driven cells, ignore list returns `None`, unknown event returns `None`.
6. **`fetch_payment_status`**: each pull mapping cell, `cs_`/`pi_` dispatch.
7. **Pre-auth**: `charge` full/partial (capture args, `async_call=True`),
   wrong-state raises; `release_lock` returns locked amount, wrong-state raises.
8. **Refunds**: `start_refund` full/partial args + metadata, error mapping;
   `cancel_refund` True/False paths.
9. **Simulator**: scenario walks (success, decline, abandon→expired,
   auth→capture, auth→release, auth→time-travel-expire, refund
   succeed/late-fail, review open/close) asserting the processor lands the
   expected `PaymentUpdate` sequence end-to-end; payload type round-trips (§10).
10. **Entry points**: registry discovers `stripe` backend; simulator plugin
    loads via SPI.

Coverage/CI mirrors paynow (ruff, mypy/pyright as configured there,
`pip-audit --skip-editable` trap noted in the workspace release flow).

## 12. Flagged for verification during implementation

Collected from the research assets' "unverified" sections — check these
against live test mode before relying on them. *Implementation status
(2026-07-10): none could be verified against live test mode (no
credentials in the build environment); each is either defensively
implemented or TODO-tagged as noted. The live-mode check belongs to the
§13.3 manual smoke.*

- `review.closed.closed_reason` value semantics (§7 fraud rows).
  *Status: TODO tag at the mapping site; only `approved` → ACCEPT, all
  else → REJECT with the reason preserved in `fraud_message`.*
- Manual capture × automatic payment methods: does Checkout silently filter
  incompatible methods or error?
  *Status: documented as an operator caveat in the README; not
  code-relevant (the plugin never sends `payment_method_types`).*
- Whether `payment_intent.canceled` actually fires on automatic auth expiry.
  *Status: defensively implemented — if it fires, §7's mapping yields
  LOCK_RELEASED; if not, `fetch_payment_status()` is the deterministic
  backstop. The simulator emits it on `expire_auth` time-travel.*
- Three-decimal nearest-ten rule enforcement (rule text survives only in an
  archived docs page).
  *Status: implemented as specified (round-to-nearest-ten); harmless if
  Stripe stopped enforcing it.*
- Error type raised when canceling a non-`requires_action` refund (drives the
  `cancel_refund` False path).
  *Status: implementation catches all `stripe.StripeError` → False, so
  the exact type is irrelevant to correctness.*
- `charge.succeeded` timing at authorization in manual-capture flows
  (irrelevant to the FSM — charge events ignored — but affects simulator
  ignore-traffic realism).
  *Status: TODO tag in the simulator; it emits no charge event at
  authorization time.*

## 13. Acceptance criteria

The implementation is done when:

1. All §11 tests pass; the simulator scenario walks produce the exact
   `PaymentUpdate` sequences of §7/§8/§9.
2. A demo app using a framework adapter completes, against the simulator:
   pay-now success; decline; abandonment → `FAILED`;
   auth → partial capture (remainder released); auth → release;
   auth → time-traveled expiry → `LOCK_RELEASED`; full and partial refund
   → `REFUND_CONFIRMED`; late refund failure → `REFUND_CANCELLED`;
   review open/close → fraud events.
3. The same code path, pointed at Stripe **test mode** with real
   `sk_test_`/`whsec_` credentials and Stripe CLI forwarding, completes a
   pay-now success and a full refund (manual smoke, not CI).
4. Every §12 item is either confirmed (and its TODO comment removed) or the
   spec is amended.
5. Entry points registered; README documents settings (§4), the Dashboard
   `enabled_events` list (§7), and the auth-expiry operator note (§8).
