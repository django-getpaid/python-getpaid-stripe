"""Config validation tests (SPEC §3, §4, §11.2)."""

import pytest
from getpaid_core.exceptions import CredentialsError

from getpaid_stripe.processor import StripeProcessor

from .conftest import make_mock_payment


def test_valid_config_constructs(stripe_config, mock_payment):
    processor = StripeProcessor(mock_payment, stripe_config)
    assert processor.slug == "stripe"


def test_missing_api_key_fails_fast(stripe_config, mock_payment):
    del stripe_config["api_key"]
    with pytest.raises(CredentialsError):
        StripeProcessor(mock_payment, stripe_config)


@pytest.mark.parametrize(
    "bad_key",
    ["", "pk_test_123", "whsec_123", "not-a-key", "sk-test-123"],
)
def test_malformed_api_key_fails_fast(stripe_config, mock_payment, bad_key):
    stripe_config["api_key"] = bad_key
    with pytest.raises(CredentialsError):
        StripeProcessor(mock_payment, stripe_config)


@pytest.mark.parametrize(
    "good_key",
    ["sk_test_abc", "sk_live_abc", "rk_test_abc", "rk_live_abc"],
)
def test_valid_api_key_prefixes_accepted(
    stripe_config, mock_payment, good_key
):
    stripe_config["api_key"] = good_key
    StripeProcessor(mock_payment, stripe_config)


def test_missing_webhook_secret_fails_fast(stripe_config, mock_payment):
    del stripe_config["webhook_secret"]
    with pytest.raises(CredentialsError):
        StripeProcessor(mock_payment, stripe_config)


def test_malformed_webhook_secret_fails_fast(stripe_config, mock_payment):
    stripe_config["webhook_secret"] = "secret-but-not-whsec"
    with pytest.raises(CredentialsError):
        StripeProcessor(mock_payment, stripe_config)


@pytest.mark.parametrize(
    ("api_key", "expected"),
    [
        ("sk_test_abc", True),
        ("rk_test_abc", True),
        ("sk_live_abc", False),
        ("rk_live_abc", False),
    ],
)
def test_is_sandbox_derived_from_key_not_configured(
    stripe_config, api_key, expected
):
    # No `sandbox` flag exists; mode lives in the key.
    stripe_config["api_key"] = api_key
    processor = StripeProcessor(make_mock_payment(), stripe_config)
    assert processor.is_sandbox is expected


def test_defaults(stripe_config, mock_payment):
    processor = StripeProcessor(mock_payment, stripe_config)
    assert processor.get_setting("capture_method", "automatic") == "automatic"
    assert processor.get_setting("max_network_retries", 2) == 2
