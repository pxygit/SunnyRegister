"""Protocol-only UPI payment-link extraction using the vendored Sentinel 0810 SDK."""

from __future__ import annotations

import base64
import html
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from curl_cffi import requests

from upi_sentinel_0810 import issue_sentinel_token


STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
CHATGPT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHATGPT_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
CHATGPT_APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
STRIPE_PAGE_URL = "https://api.stripe.com/v1/payment_pages/{session_id}"
STRIPE_INIT_URL = STRIPE_PAGE_URL + "/init"
STRIPE_CONFIRM_URL = STRIPE_PAGE_URL + "/confirm"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
CLIENT_VERSION = "prod-71c4ba1079ebac861d68a9bec3ce2e36fd733c1a"
CLIENT_BUILD = "10961682"


class UpiExtractionError(RuntimeError):
    """Raised when the flow cannot produce a real UPI instructions/deep link."""


def stable_device_id(access_token: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"oai-did:{str(access_token or '')[:200]}"))


def build_checkout_payload(*, apply_promo: bool, campaign: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "IN", "currency": "INR"},
        "checkout_ui_mode": "custom",
    }
    if apply_promo:
        payload["promo_campaign"] = {
            "promo_campaign_id": campaign or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    return payload


def _session(proxy: str):
    client = requests.Session(impersonate="chrome136")
    client.trust_env = False
    if str(proxy or "").strip():
        client.proxies = {"http": proxy, "https": proxy}
    client.headers.update({"User-Agent": USER_AGENT})
    return client


def _json(response: Any, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception as exc:
        raise UpiExtractionError(f"{stage} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise UpiExtractionError(f"{stage} returned a non-object response")
    return value


def _response_error(response: Any) -> str:
    text = re.sub(r"\s+", " ", str(getattr(response, "text", "") or "")).strip()
    return text[:300]


def _request_headers(
    access_token: str,
    device_id: str,
    path: str,
    *,
    referer: str = "https://chatgpt.com/",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": referer,
        "User-Agent": USER_AGENT,
        "oai-device-id": device_id,
        "oai-session-id": str(uuid.uuid4()),
        "oai-language": "en-US",
        "oai-client-version": CLIENT_VERSION,
        "oai-client-build-number": CLIENT_BUILD,
        "oai-telemetry": "[1,null]",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
        "x-openai-web-frontend": "core_web",
        "x-oai-is-client-observation": "v1.r.p." + uuid.uuid4().hex[:12],
    }


def _nested(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _minor_amount(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return round(value)
    if isinstance(value, Mapping):
        for key in ("amount", "amount_due", "minor", "value"):
            amount = _minor_amount(value.get(key))
            if amount is not None:
                return amount
    return None


def extract_payment_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    amount: int | None = None
    for path in (
        ("total_summary", "due"),
        ("invoice", "amount_due"),
        ("elements_options", "amount"),
        ("amount_total",),
    ):
        amount = _minor_amount(_nested(payload, path))
        if amount is not None:
            break

    payment_methods: list[str] = []
    for path in (
        ("elements_options", "payment_method_types"),
        ("payment_method_types",),
        ("payment_method_preference", "payment_method_types"),
        ("session", "payment_method_types"),
        ("ordered_payment_method_types",),
    ):
        candidate = _nested(payload, path)
        if isinstance(candidate, list) and candidate:
            payment_methods = [str(item).strip().lower() for item in candidate if str(item).strip()]
            break
    return {"amount": amount, "payment_method_types": payment_methods}


def _merge_upi_value(result: dict[str, Any], key: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        if key.lower() in {"expires_at", "expires_after_timestamp", "qr_expires_at"}:
            try:
                expires = int(value)
            except (TypeError, ValueError):
                return
            if expires > 0:
                result.setdefault("expires_at", expires)
        return
    normalized = html.unescape(value).strip()
    lowered = normalized.lower()
    if lowered.startswith("upi://"):
        result.setdefault("upi_uri", normalized)
    elif lowered.startswith("https://payments.stripe.com/upi/instructions/"):
        result.setdefault("hosted_instructions_url", normalized)
    elif lowered.startswith("https://qr.stripe.com/"):
        result.setdefault("qr_image_url_png" if "png" in lowered else "qr_image_url_svg", normalized)
    normalized_key = key.lower()
    if normalized_key in {"upi_uri", "mobile_auth_url"} and lowered.startswith("upi://"):
        result.setdefault("upi_uri", normalized)
    elif (
        normalized_key == "hosted_instructions_url"
        and lowered.startswith("https://payments.stripe.com/upi/instructions/")
    ):
        result.setdefault("hosted_instructions_url", normalized)
    elif normalized_key in {"image_url_svg", "qr_image_url_svg"} and lowered.startswith(
        "https://qr.stripe.com/"
    ):
        result.setdefault("qr_image_url_svg", normalized)
    elif normalized_key in {"image_url_png", "qr_image_url_png"} and lowered.startswith(
        "https://qr.stripe.com/"
    ):
        result.setdefault("qr_image_url_png", normalized)


def extract_upi_details(payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def walk(value: Any, key: str = "") -> None:
        _merge_upi_value(result, key, value)
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return result


def extract_upi_details_from_html(document: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    meta = re.search(
        r'<meta\b[^>]*\bid=["\']payload["\'][^>]*\bdata-message=["\']([^"\']+)["\']',
        document,
        re.I,
    ) or re.search(
        r'<meta\b[^>]*\bdata-message=["\']([^"\']+)["\'][^>]*\bid=["\']payload["\']',
        document,
        re.I,
    )
    if meta:
        encoded = html.unescape(meta.group(1)).replace("-", "+").replace("_", "/")
        encoded += "=" * (-len(encoded) % 4)
        try:
            decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
            result.update(extract_upi_details(decoded))
        except Exception:
            pass
    for match in re.finditer(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', document, re.I):
        source = html.unescape(match.group(1))
        if "qr.stripe.com" in source:
            _merge_upi_value(result, "image_url_png" if "png" in source.lower() else "image_url_svg", source)
            break
    return result


def _merge_details(target: dict[str, Any], source: Any) -> None:
    for key, value in extract_upi_details(source).items():
        if value and not target.get(key):
            target[key] = value


def _ensure_not_cancelled(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check:
        cancel_check()


def generate_upi_payment_link(
    *,
    access_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    billing: Mapping[str, Any],
    apply_promo: bool,
    promo_campaign: str = "",
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    poll_attempts: int = 40,
    poll_interval: float = 2.5,
) -> dict[str, Any]:
    """Run checkout, Stripe UPI confirm, Sentinel approval, and link extraction."""
    emit = log or (lambda _message: None)
    device_id = stable_device_id(access_token)
    checkout_http = _session(checkout_proxy)
    stripe_http = _session(provider_proxy)
    try:
        checkout_http.cookies.set("oai-did", device_id, domain="chatgpt.com", path="/")
        checkout_http.cookies.set("oai-did", device_id, domain=".openai.com", path="/")
    except Exception:
        pass
    address = dict(billing.get("address") or {})
    sessions = (checkout_http, stripe_http)
    try:
        _ensure_not_cancelled(cancel_check)
        emit("第 2/7 步：生成 0810 Sentinel 并创建 IN/INR UPI Checkout")
        checkout_proof = issue_sentinel_token(
            flow="chatgpt_checkout",
            device_id=device_id,
            session=checkout_http,
            page_url="https://chatgpt.com/",
            timeout_seconds=90,
        )
        checkout_path = "/backend-api/payments/checkout"
        checkout_headers = _request_headers(access_token, device_id, checkout_path)
        checkout_headers["openai-sentinel-token"] = checkout_proof.token
        checkout_response = checkout_http.post(
            CHATGPT_CHECKOUT_URL,
            json=build_checkout_payload(apply_promo=apply_promo, campaign=promo_campaign),
            headers=checkout_headers,
            timeout=45,
        )
        if checkout_response.status_code >= 400:
            raise UpiExtractionError(
                f"UPI checkout failed HTTP {checkout_response.status_code}: {_response_error(checkout_response)}"
            )
        checkout_data = _json(checkout_response, "UPI checkout")
        session_id = str(checkout_data.get("checkout_session_id") or checkout_data.get("id") or "")
        if not session_id.startswith("cs_"):
            raise UpiExtractionError("UPI checkout did not return a cs_live session")
        publishable_key = str(checkout_data.get("publishable_key") or "")
        if not publishable_key:
            raise UpiExtractionError("UPI checkout did not return a Stripe publishable key")
        processor = str(checkout_data.get("processor_entity") or "openai_ie")
        checkout_page = f"https://chatgpt.com/checkout/{processor}/{session_id}"

        _ensure_not_cancelled(cancel_check)
        emit("第 3/7 步：初始化 Stripe UPI 支付页并确认金额")
        stripe_js_id = str(uuid.uuid4())
        init_body = {
            "browser_locale": "en-IN",
            "browser_timezone": "Asia/Kolkata",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
        }
        init_response = stripe_http.post(
            STRIPE_INIT_URL.format(session_id=session_id), data=init_body, timeout=30
        )
        if init_response.status_code >= 400:
            raise UpiExtractionError(
                f"Stripe UPI init failed HTTP {init_response.status_code}: {_response_error(init_response)}"
            )
        init_data = _json(init_response, "Stripe UPI init")
        init_checksum = str(init_data.get("init_checksum") or "")
        payment_state = extract_payment_state(init_data)
        amount = payment_state["amount"]
        methods = payment_state["payment_method_types"]
        if apply_promo and amount != 0:
            rendered = "unknown" if amount is None else str(amount)
            raise UpiExtractionError(f"UPI_ZERO_DUE_REQUIRED: amount={rendered} INR")
        if methods and "upi" not in methods:
            raise UpiExtractionError(f"UPI payment method unavailable: {','.join(methods)}")
        if amount is None:
            raise UpiExtractionError("Stripe UPI init did not return a verifiable amount")

        _ensure_not_cancelled(cancel_check)
        emit("第 4/7 步：提交印度税区与账单信息")
        tax_body = {
            "tax_region[country]": "IN",
            "tax_region[postal_code]": str(address.get("postal_code") or "560001"),
            "tax_region[state]": str(address.get("state") or "KA"),
            "tax_region[city]": str(address.get("city") or "Bengaluru"),
            "tax_region[line1]": str(address.get("line1") or "1 MG Road"),
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
        }
        tax_response = stripe_http.post(
            STRIPE_PAGE_URL.format(session_id=session_id), data=tax_body, timeout=30
        )
        if tax_response.status_code < 400:
            tax_data = _json(tax_response, "Stripe UPI tax update")
            refreshed = extract_payment_state(tax_data)["amount"]
            if refreshed is not None:
                amount = refreshed
            init_data = tax_data
            init_checksum = str(tax_data.get("init_checksum") or init_checksum)
        else:
            emit(f"印度税区更新 HTTP {tax_response.status_code}，沿用 init 数据继续")
        if apply_promo and amount != 0:
            raise UpiExtractionError(f"UPI_ZERO_DUE_REQUIRED_AFTER_TAX: amount={amount} INR")

        _ensure_not_cancelled(cancel_check)
        emit("第 5/7 步：以 UPI 支付方式确认 Stripe Payment Page")
        confirm_body = {
            "payment_method_data[type]": "upi",
            "payment_method_data[billing_details][name]": str(billing.get("name") or "Arjun Sharma"),
            "payment_method_data[billing_details][email]": str(billing.get("email") or ""),
            "payment_method_data[billing_details][address][line1]": str(address.get("line1") or "1 MG Road"),
            "payment_method_data[billing_details][address][city]": str(address.get("city") or "Bengaluru"),
            "payment_method_data[billing_details][address][state]": str(address.get("state") or "KA"),
            "payment_method_data[billing_details][address][postal_code]": str(address.get("postal_code") or "560001"),
            "payment_method_data[billing_details][address][country]": "IN",
            "expected_amount": str(amount),
            "expected_payment_method_type": "upi",
            "return_url": checkout_page,
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
        }
        if init_checksum:
            confirm_body["init_checksum"] = init_checksum
        stripe_confirm = stripe_http.post(
            STRIPE_CONFIRM_URL.format(session_id=session_id), data=confirm_body, timeout=30
        )
        if stripe_confirm.status_code >= 400:
            raise UpiExtractionError(
                f"Stripe UPI confirm failed HTTP {stripe_confirm.status_code}: {_response_error(stripe_confirm)}"
            )
        confirm_data = _json(stripe_confirm, "Stripe UPI confirm")

        _ensure_not_cancelled(cancel_check)
        emit("第 6/7 步：提交 ChatGPT confirm 与 0810 Sentinel approval")
        confirm_path = "/backend-api/payments/checkout/confirm"
        chatgpt_confirm = checkout_http.post(
            CHATGPT_CONFIRM_URL,
            json={"checkout_session_id": session_id, "selected_payment_method_type": "upi"},
            headers=_request_headers(access_token, device_id, confirm_path, referer=checkout_page),
            timeout=45,
        )
        approval_data: dict[str, Any] = {}
        if chatgpt_confirm.status_code < 400:
            approval_data = _json(chatgpt_confirm, "ChatGPT UPI confirm")
        approved = str(approval_data.get("result") or "").lower() == "approved"
        if not approved:
            approval_proof = issue_sentinel_token(
                flow="checkout_session_approval",
                device_id=device_id,
                session=checkout_http,
                page_url=checkout_page,
                timeout_seconds=90,
            )
            approve_path = "/backend-api/payments/checkout/approve"
            approve_headers = _request_headers(
                access_token, device_id, approve_path, referer=checkout_page
            )
            approve_headers["openai-sentinel-token"] = approval_proof.token
            if approval_proof.so_token:
                approve_headers["openai-sentinel-so-token"] = approval_proof.so_token
            approve_response = checkout_http.post(
                CHATGPT_APPROVE_URL,
                json={"checkout_session_id": session_id, "processor_entity": processor},
                headers=approve_headers,
                timeout=45,
            )
            if approve_response.status_code >= 400:
                raise UpiExtractionError(
                    f"UPI approval failed HTTP {approve_response.status_code}: {_response_error(approve_response)}"
                )
            approval_data = _json(approve_response, "UPI approval")
            approved = str(approval_data.get("result") or "").lower() == "approved"
        if not approved:
            result = str(approval_data.get("result") or "unknown")
            raise UpiExtractionError(f"UPI approval was not approved: result={result}")

        emit("第 7/7 步：轮询并提取 UPI instructions/deep link")
        details: dict[str, Any] = {}
        _merge_details(details, confirm_data)
        _merge_details(details, approval_data)
        attempts = max(1, int(poll_attempts))
        for attempt in range(attempts):
            _ensure_not_cancelled(cancel_check)
            if details.get("hosted_instructions_url") or details.get("upi_uri"):
                break
            if attempt:
                time.sleep(max(0.0, float(poll_interval)))
            page_response = stripe_http.get(
                STRIPE_PAGE_URL.format(session_id=session_id),
                params={"key": publishable_key, "_stripe_version": STRIPE_VERSION},
                timeout=30,
            )
            if page_response.status_code != 200:
                break
            _merge_details(details, _json(page_response, "Stripe UPI poll"))

        if not details.get("hosted_instructions_url") and not details.get("upi_uri"):
            refresh = stripe_http.post(
                STRIPE_INIT_URL.format(session_id=session_id), data=init_body, timeout=30
            )
            if refresh.status_code == 200:
                _merge_details(details, _json(refresh, "Stripe UPI refresh"))

        instructions_url = str(details.get("hosted_instructions_url") or "")
        if instructions_url and not details.get("upi_uri"):
            hydrate = stripe_http.get(
                instructions_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://js.stripe.com/",
                },
                timeout=30,
            )
            if hydrate.status_code < 400:
                for key, value in extract_upi_details_from_html(hydrate.text).items():
                    details.setdefault(key, value)

        upi_uri = str(details.get("upi_uri") or "")
        primary_link = instructions_url or upi_uri
        if not primary_link:
            raise UpiExtractionError(
                "UPI approval succeeded but no Stripe instructions URL or upi:// deep link was returned"
            )
        upi_link_type = "upi_instructions" if instructions_url else "upi_deep_link"
        return {
            "checkout_session_id": session_id,
            "processor_entity": processor,
            "link_type": "upi",
            "upi_link_type": upi_link_type,
            "checkout_url": primary_link,
            "short_link": primary_link,
            "provider_redirect_url": primary_link,
            "upi_instructions_url": instructions_url,
            "upi_uri": upi_uri,
            "qr_data": primary_link,
            "qr_image_url_svg": str(details.get("qr_image_url_svg") or ""),
            "qr_image_url_png": str(details.get("qr_image_url_png") or ""),
            "checkout_amount": amount,
            "amount_currency": "INR",
            "amount_verification": "verified_zero" if amount == 0 else "nonzero",
            "promo_applied": amount == 0 if apply_promo else None,
            "payment_method_type": "upi",
            "payment_method_types": methods,
            "approval_result": "approved",
            "expires_at": int(details.get("expires_at") or (time.time() + 300)),
            "upi_engine": "python-sentinel-0810",
            "sentinel_version": "20260810913b",
        }
    finally:
        for client in sessions:
            try:
                client.close()
            except Exception:
                pass


__all__ = [
    "UpiExtractionError",
    "build_checkout_payload",
    "extract_payment_state",
    "extract_upi_details",
    "extract_upi_details_from_html",
    "generate_upi_payment_link",
    "stable_device_id",
]
