from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PAY153_DIR = Path(__file__).parents[1] / "tools" / "pay153_checkout"
if str(PAY153_DIR) not in sys.path:
    sys.path.insert(0, str(PAY153_DIR))

import upi_local_extractor as upi  # noqa: E402
from upi_sentinel_0810.bundle import (  # noqa: E402
    DEFAULT_SENTINEL_VERSION,
    sentinel_version,
    validate_runtime_bundle,
)


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str = "") -> None:
        self.payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *, posts=None, gets=None) -> None:
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.closed = False

    def post(self, *_args, **_kwargs):
        return self.posts.pop(0)

    def get(self, *_args, **_kwargs):
        return self.gets.pop(0)

    def close(self):
        self.closed = True


def test_upi_uses_fixed_0810_sentinel_bundle() -> None:
    sdk_path, runner_path = validate_runtime_bundle()

    assert sentinel_version() == DEFAULT_SENTINEL_VERSION == "20260810913b"
    assert sdk_path.stat().st_size == 33820
    assert runner_path.stat().st_size == 57592


def test_checkout_payload_follows_promotion_toggle() -> None:
    paid = upi.build_checkout_payload(apply_promo=False, campaign="ignored")
    trial = upi.build_checkout_payload(apply_promo=True, campaign="account-campaign")

    assert "promo_campaign" not in paid
    assert trial["promo_campaign"] == {
        "promo_campaign_id": "account-campaign",
        "is_coupon_from_query_param": False,
    }


def test_unknown_amount_is_not_treated_as_zero() -> None:
    assert upi.extract_payment_state({})["amount"] is None
    assert upi.extract_payment_state({"total_summary": {"due": 0}})["amount"] == 0


def test_upi_detail_extraction_prefers_real_payment_material() -> None:
    details = upi.extract_upi_details(
        {
            "next_action": {
                "hosted_instructions_url": "https://payments.stripe.com/upi/instructions/test_123",
                "mobile_auth_url": "upi://pay?pa=merchant%40upi",
            }
        }
    )

    assert details["hosted_instructions_url"].endswith("test_123")
    assert details["upi_uri"].startswith("upi://")


def test_paid_upi_flow_accepts_nonzero_amount_and_returns_instructions() -> None:
    checkout = FakeSession(
        posts=[
            FakeResponse(
                {
                    "checkout_session_id": "cs_live_test",
                    "publishable_key": "pk_live_test",
                    "processor_entity": "openai_ie",
                }
            ),
            FakeResponse({"result": "blocked"}),
            FakeResponse({"result": "approved"}),
        ]
    )
    stripe = FakeSession(
        posts=[
            FakeResponse(
                {
                    "total_summary": {"due": 1999},
                    "payment_method_types": ["card", "upi"],
                    "init_checksum": "checksum",
                }
            ),
            FakeResponse({"total_summary": {"due": 1999}}),
            FakeResponse({"status": "requires_action"}),
        ],
        gets=[
            FakeResponse(
                {
                    "hosted_instructions_url": (
                        "https://payments.stripe.com/upi/instructions/test_123"
                    )
                }
            ),
            FakeResponse(text="<html></html>"),
        ],
    )
    proof = SimpleNamespace(token='{"flow":"test"}', so_token='{"so":"test"}')

    with patch.object(upi, "_session", side_effect=[checkout, stripe]), patch.object(
        upi, "issue_sentinel_token", return_value=proof
    ):
        result = upi.generate_upi_payment_link(
            access_token="access-token",
            checkout_proxy="http://checkout",
            provider_proxy="http://provider",
            billing={
                "name": "Arjun Sharma",
                "email": "account@example.com",
                "address": {
                    "country": "IN",
                    "line1": "1 MG Road",
                    "city": "Bengaluru",
                    "state": "KA",
                    "postal_code": "560001",
                },
            },
            apply_promo=False,
            poll_attempts=1,
            poll_interval=0,
        )

    assert result["link_type"] == "upi"
    assert result["upi_link_type"] == "upi_instructions"
    assert result["checkout_amount"] == 1999
    assert result["promo_applied"] is None
    assert result["upi_engine"] == "python-sentinel-0810"
    assert checkout.closed and stripe.closed


def test_trial_upi_flow_rejects_nonzero_amount() -> None:
    checkout = FakeSession(
        posts=[
            FakeResponse(
                {
                    "checkout_session_id": "cs_live_test",
                    "publishable_key": "pk_live_test",
                    "processor_entity": "openai_ie",
                }
            )
        ]
    )
    stripe = FakeSession(
        posts=[
            FakeResponse(
                {
                    "total_summary": {"due": 1999},
                    "payment_method_types": ["upi"],
                }
            )
        ]
    )
    proof = SimpleNamespace(token='{"flow":"chatgpt_checkout"}', so_token="")

    with patch.object(upi, "_session", side_effect=[checkout, stripe]), patch.object(
        upi, "issue_sentinel_token", return_value=proof
    ):
        with pytest.raises(upi.UpiExtractionError, match="UPI_ZERO_DUE_REQUIRED"):
            upi.generate_upi_payment_link(
                access_token="access-token",
                checkout_proxy="http://checkout",
                provider_proxy="http://provider",
                billing={"address": {}},
                apply_promo=True,
            )


def test_approved_hosted_checkout_without_upi_material_is_rejected() -> None:
    checkout = FakeSession(
        posts=[
            FakeResponse(
                {
                    "checkout_session_id": "cs_live_test",
                    "publishable_key": "pk_live_test",
                    "processor_entity": "openai_ie",
                }
            ),
            FakeResponse({"result": "approved"}),
        ]
    )
    stripe = FakeSession(
        posts=[
            FakeResponse({"total_summary": {"due": 0}, "payment_method_types": ["upi"]}),
            FakeResponse({"total_summary": {"due": 0}}),
            FakeResponse({"status": "requires_action"}),
            FakeResponse({"stripe_hosted_url": "https://checkout.stripe.com/c/pay/test"}),
        ],
        gets=[FakeResponse({"stripe_hosted_url": "https://checkout.stripe.com/c/pay/test"})],
    )
    proof = SimpleNamespace(token='{"flow":"chatgpt_checkout"}', so_token="")

    with patch.object(upi, "_session", side_effect=[checkout, stripe]), patch.object(
        upi, "issue_sentinel_token", return_value=proof
    ):
        with pytest.raises(upi.UpiExtractionError, match="no Stripe instructions URL"):
            upi.generate_upi_payment_link(
                access_token="access-token",
                checkout_proxy="http://checkout",
                provider_proxy="http://provider",
                billing={"address": {}},
                apply_promo=True,
                poll_attempts=1,
                poll_interval=0,
            )
