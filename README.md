# python-getpaid-stripe

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Stripe payment processor for the
[python-getpaid](https://github.com/django-getpaid/python-getpaid-core)
ecosystem, wrapping **Stripe Checkout Sessions** (payment mode) via the
official `stripe-python` SDK.

Design and rationale live in [SPEC.md](SPEC.md); research backing in
[`docs/research/`](docs/research/).

## Key features

- **Hosted checkout** — `prepare_transaction()` creates a payment-mode
  Checkout Session and returns a redirect URL
- **Webhook handling** — signature verification via
  `stripe.Webhook.construct_event` over the raw body; semantic mapping
  where `payment_intent.*` events are authoritative for money-state
- **Pre-auth / manual capture** — `charge()` (full or partial capture,
  remainder auto-released) and `release_lock()`; the first getpaid
  plugin to exercise core's pre-auth FSM states
- **Refunds** — full and multiple partial refunds; status-driven
  webhook mapping handles late failures and Stripe-generated
  expiry refunds
- **Radar fraud reviews** — `review.opened/closed` map to core
  `FraudEvent`s
- **Status polling** — `fetch_payment_status()` PULL flow, doubling as
  the authorization-expiry backstop
- **Simulator plugin** — a local Stripe stand-in for
  `python-getpaid-simulator` with a fake hosted checkout, real HMAC
  webhook signatures, and time-travel ops

## Installation

```bash
pip install python-getpaid-stripe
```

With the local simulator plugin:

```bash
pip install python-getpaid-stripe[simulator]
```

## Configuration

| Setting | Type | Required | Default | Notes |
|---|---|---|---|---|
| `api_key` | str | **yes** | — | `sk_test_…` / `sk_live_…` / `rk_…`; validated at construction |
| `webhook_secret` | str | **yes** | — | `whsec_…`, per-endpoint, test/live distinct; validated at construction |
| `success_url` | str template | **yes** | — | `{payment_id}` is formatted by the plugin; Stripe's literal `{CHECKOUT_SESSION_ID}` is passed through |
| `cancel_url` | str template | **yes** | — | same semantics |
| `capture_method` | `"automatic"` \| `"manual"` | no | `"automatic"` | per-payment override: `prepare_transaction(capture_method="manual")` |
| `session_expires_in` | int minutes (30–1440) | no | Stripe's 24 h | maps to Checkout `expires_at` |
| `max_network_retries` | int | no | `2` | safe: the SDK auto-generates idempotency keys |

There is **no publishable key** setting (nothing in a hosted-Checkout
flow consumes one) and **no sandbox flag**: test vs live mode is
derived from the API key prefix (`processor.is_sandbox`). A flag that
can contradict the key is a lie waiting to happen.

`accepted_currencies` is a broad default list of Stripe presentment
currencies; availability is per account country — subclass the
processor to narrow it.

### Webhook endpoint (Stripe Dashboard)

Point a webhook endpoint at your framework adapter's callback URL and
enable **exactly** these events:

```
checkout.session.completed
checkout.session.expired
payment_intent.amount_capturable_updated
payment_intent.succeeded
payment_intent.payment_failed
payment_intent.canceled
refund.created
refund.updated
refund.failed
review.opened
review.closed
```

The framework adapter must pass the **raw request body** to
`verify_callback(..., raw_body=...)` — Stripe signatures are computed
over the raw bytes. Only classic v1 snapshot payloads are supported;
v2 "thin" events are rejected.

## Manual capture (pre-auth)

Opt in per configuration or per payment. The flow:

1. `prepare_transaction(capture_method="manual")` — the buyer
   authorizes; `payment_intent.amount_capturable_updated` locks the
   payment (`LOCKED`).
2. `charge()` captures the full locked amount;
   `charge(amount)` captures partially and **Stripe auto-releases the
   remainder**. Returns `async_call=True` — the authoritative
   `PAYMENT_CAPTURED` arrives via `payment_intent.succeeded`.
3. `release_lock()` cancels the intent and returns the full locked
   amount; `payment_intent.canceled` confirms `LOCK_RELEASED`.

**Operator note — authorization expiry:** card authorizations are
valid for ~7 days (variance: Visa MIT 5 days, some in-person 2, Japan
30). The lock's `provider_data["locked_at"]` timestamp is stamped when
`LOCKED` is applied, so your application can schedule
capture-or-release. There is **no reliable expiry webhook**; the
deterministic detection path is `fetch_payment_status()` (a canceled
manual-capture intent maps to `LOCK_RELEASED`). Polling cadence
belongs to the application — this plugin ships no scheduler. Note that
manual capture also narrows the payment methods Stripe offers (cards,
Klarna, PayPal yes; ACH, iDEAL no).

## Refunds

`start_refund()` refunds the full captured amount,
`start_refund(amount)` a part of it (multiple partials allowed). The
refund id is stored in `provider_data["refund_id"]` (latest refund
only). Refund `reason` is deliberately not exposed — Stripe's
`fraudulent` value has card-block side effects.

**`cancel_refund()` effectively always returns `False` for card
payments**: only refunds in `requires_action` (bank-transfer-style
methods) are API-cancelable; card refunds can only be canceled from
the Stripe Dashboard.

## Simulator plugin

With the `simulator` extra installed, `python-getpaid-simulator`
auto-discovers the `stripe` plugin. Configuration via environment:

```
SIMULATOR_STRIPE_API_KEY        (default sk_test_sim_stripe_key)
SIMULATOR_STRIPE_WEBHOOK_SECRET (default whsec_sim_stripe_secret)
SIMULATOR_STRIPE_NOTIFY_URL     (webhook delivery target)
```

Point the processor at the simulator with the internal `api_base`
config key. The fake hosted checkout offers pay / pay-delayed /
decline / abandon; `POST /sim/stripe/ops/{order_id}` forces
transitions with no natural actor (`expire_session`, `expire_auth`
time-travel, `settle_delayed` / `fail_delayed`, `fail_refund`,
`set_refund_mode`, `open_review` / `close_review`). Webhooks carry genuine HMAC
`Stripe-Signature` headers plus deliberate ignore-list traffic
(`charge.*`, `checkout.session.async_payment_*`) so the processor's
ignore behavior is exercised under realistic fire.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ty check
```

## License

MIT
