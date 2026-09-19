from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import random
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from flask import Flask, jsonify, redirect, request, send_from_directory
from curl_cffi import requests

import stripe_checkout as sc
import kakao_extractor as kakao
from provider_checkout import (
    PROVIDER_DEFAULTS,
    canonical_ideal_payment_url,
    default_billing,
    enrich_ideal_redirect,
    generate_payment_qr_images,
    create_provider_payment_method,
    is_gopay_promo_amount,
    is_momo_promo_amount,
    is_valid_ideal_payment_url,
    published_payment_method_snapshot,
    stripe_to_provider,
)
from paypal_routing import reconcile_checkout_mode, session_checkout_kind
from proxy_routing import checkout_route_proxy, promotion_route_proxy, shares_checkout_proxy
from sentinel_fallback import resolve_payment_sentinel_headers
from billing_address_resolver import resolve_cached_country_address
from sentinel_token import SentinelTokenProvider as BaseSentinel
from checkout_identity import (
    CheckoutSessionIdentityConflictError,
    classify_checkout_session_identity,
)
from payment_proof_contracts import (
    CheckoutClientContext,
    PaymentEndpoint,
    PaymentFlowError,
    ProofBundle,
    ProofPolicy,
    ProofProviderKind,
    SentinelFlow,
    flow_for_endpoint,
    payment_endpoint as normalize_payment_endpoint,
    render_payment_diagnostic_event,
    sentinel_flow,
)
from upi_local_extractor import generate_upi_payment_link
from ph_short_extractor import (
    CheckoutExtractor as PhShortCheckoutExtractor,
    ExtractorConfig as PhShortExtractorConfig,
    checkout_amount_minor as custom_checkout_amount_minor,
    checkout_currency as custom_checkout_currency,
    checkout_state_from_html as custom_checkout_state_from_html,
    parse_credentials as parse_ph_short_credentials,
)

ROOT = Path(__file__).resolve().parent
BACKEND_LOG_DIR = Path(os.getenv("PAY153_LOG_DIR", str(ROOT / "logs")))
RUST_ALIAS_FILE = ROOT / "data" / "rust_job_aliases.json"
RUST_ALIAS_LOCK = threading.RLock()
LEGACY_SERVICE_BASE = str(os.getenv("PAY153_LEGACY_BASE", "")).rstrip("/")
UPI_ENABLED = str(os.getenv("PAY153_UPI_ENABLED", "1")).strip().lower() in {
    "1", "true", "yes", "on",
}
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config["JSON_AS_ASCII"] = False

ROTATING_PAYPAL_ADDRESS_COUNTRIES = {"NL", "GB", "TH", "BR", "US"}
DYNAMIC_PROXY_API_URL = str(
    os.getenv("PAY153_DYNAMIC_PROXY_API")
    or "https://white.1024proxy.com/white/api"
).strip()
DYNAMIC_PROXY_API_LOCK = threading.Lock()
DYNAMIC_PROXY_API_LAST_AT = 0.0
DYNAMIC_PROXY_API_MIN_INTERVAL = max(
    0.1, float(os.getenv("PAY153_DYNAMIC_PROXY_MIN_INTERVAL") or 0.35)
)
GOPAY_BLOCKED_REBUILD_ATTEMPTS = 10
MOMO_CHECKOUT_REBUILD_ATTEMPTS = 3
GOPAY_CHECKOUT_CREATION_LIMIT = 100
GOPAY_CHECKOUT_CREATION_DEADLINE_SECONDS = 600.0


class CheckoutCreationBudget:
    """Shared account-level budget for all nested Checkout rebuild loops."""

    def __init__(
        self,
        limit: int,
        *,
        deadline_seconds: float | None = GOPAY_CHECKOUT_CREATION_DEADLINE_SECONDS,
        clock=None,
    ) -> None:
        self.limit = max(0, int(limit))
        self.used = 0
        self._clock = clock or time.monotonic
        seconds = (
            GOPAY_CHECKOUT_CREATION_DEADLINE_SECONDS
            if deadline_seconds is None
            else max(0.0, float(deadline_seconds))
        )
        self.deadline_seconds = seconds
        self._deadline = self._clock() + seconds
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.used)

    def consume(self) -> None:
        with self._lock:
            if self._clock() >= self._deadline:
                raise RuntimeError(
                    "GOPAY_CHECKOUT_CREATION_DEADLINE_EXCEEDED: "
                    f"账户 Checkout 创建时限 {self.deadline_seconds:g} 秒已耗尽"
                )
            if self.used >= self.limit:
                raise RuntimeError(
                    "GOPAY_CHECKOUT_CREATION_BUDGET_EXHAUSTED: "
                    f"账户 Checkout 创建预算 {self.limit} 次已耗尽"
                )
            self.used += 1


def _ascii_key(value: Any) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(character)
    ).strip().lower()


def normalize_rotating_paypal_address(country: str, value: dict[str, Any]) -> dict[str, str] | None:
    country = str(country or "").upper()
    line1 = str(value.get("line1") or "").strip()
    city = str(value.get("city") or "").strip()
    postal = str(value.get("postal_code") or "").strip().upper()
    state = str(value.get("state") or "").strip()
    if not line1 or not city or not postal:
        return None
    if country == "NL":
        compact = re.sub(r"\s+", "", postal)
        if not re.fullmatch(r"[1-9][0-9]{3}[A-Z]{2}", compact):
            return None
        postal, state = f"{compact[:4]} {compact[4:]}", ""
    elif country == "GB":
        compact = re.sub(r"\s+", "", postal)
        if not re.fullmatch(r"[A-Z0-9]{5,7}", compact):
            return None
        postal, state = f"{compact[:-3]} {compact[-3:]}", ""
    elif country == "TH":
        digits = re.sub(r"\D", "", postal)
        if len(digits) != 5:
            return None
        postal, state = digits, ""
    elif country == "BR":
        digits = re.sub(r"\D", "", postal)
        if len(digits) != 8:
            return None
        br_states = {
            "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
            "bahia": "BA", "ceara": "CE", "distrito federal": "DF",
            "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
            "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
            "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE",
            "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
            "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
            "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
            "tocantins": "TO",
        }
        state = state.upper() if re.fullmatch(r"[A-Z]{2}", state.upper()) else br_states.get(_ascii_key(state), "")
        if not state:
            return None
        postal = digits
    elif country == "US":
        match = re.search(r"\b[0-9]{5}(?:-[0-9]{4})?\b", postal)
        if not match:
            return None
        us_states = {
            "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
            "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
            "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
            "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
            "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
            "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
            "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
            "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
            "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
            "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
            "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
            "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
            "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
        }
        state = state.upper() if re.fullmatch(r"[A-Z]{2}", state.upper()) else us_states.get(_ascii_key(state), "")
        if not state:
            return None
        postal = match.group(0)
    return {
        "country": country,
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal,
        "state": state,
    }


def remember_rust_job_alias(public_job_id: str, rust_job_id: str, metadata: dict[str, Any]) -> None:
    now = time.time()
    with RUST_ALIAS_LOCK:
        try:
            rows = json.loads(RUST_ALIAS_FILE.read_text(encoding="utf-8"))
            if not isinstance(rows, dict):
                rows = {}
        except Exception:
            rows = {}
        rows = {
            str(key): value for key, value in rows.items()
            if isinstance(value, dict) and now - float(value.get("created_at") or now) < 86400
        }
        rows[str(public_job_id)] = {
            "rust_job_id": str(rust_job_id),
            "created_at": now,
            **{str(key): value for key, value in metadata.items() if key in {
                "plan", "link_type", "country", "currency", "use_promo", "promo_campaign",
            }},
        }
        RUST_ALIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = RUST_ALIAS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RUST_ALIAS_FILE)


def get_rust_job_alias(public_job_id: str) -> dict[str, Any] | None:
    with RUST_ALIAS_LOCK:
        try:
            rows = json.loads(RUST_ALIAS_FILE.read_text(encoding="utf-8"))
            value = rows.get(str(public_job_id)) if isinstance(rows, dict) else None
            return dict(value) if isinstance(value, dict) else None
        except Exception:
            return None


def rust_job_public_snapshot(public_job_id: str) -> dict[str, Any] | None:
    alias = get_rust_job_alias(public_job_id)
    rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
    if not alias or not rust_base:
        return None
    rust_job_id = str(alias.get("rust_job_id") or "")
    if not rust_job_id:
        return None
    try:
        response = requests.get(f"{rust_base}/api/v1/jobs/{rust_job_id}", timeout=12)
        if response.status_code != 200:
            return None
        job = (response.json() or {}).get("job") or {}
    except Exception:
        return None
    status_map = {
        "queued": "queued",
        "running": "running",
        "succeeded": "done",
        "failed": "error",
        "cancelled": "cancelled",
    }
    result = dict(job.get("result") or {})
    explicit_promo_requested = result.get("promo_requested")
    if explicit_promo_requested is None:
        explicit_promo_requested = alias.get("use_promo")
    if explicit_promo_requested is None:
        # Older Rust aliases did not persist use_promo.  A Rust result only
        # contains promo_applied after the promotion branch was requested.
        explicit_promo_requested = result.get("promo_applied") is not None
    result.update({
        "plan": alias.get("plan") or result.get("plan"),
        "link_type": alias.get("link_type") or result.get("link_type"),
        "country": alias.get("country") or result.get("country"),
        "currency": str(result.get("currency") or alias.get("currency") or "").upper(),
        "promo_requested": bool(explicit_promo_requested),
        "promo_campaign_used": result.get("promo_campaign_used") or alias.get("promo_campaign") or "",
        "rust_workflow": True,
    })
    if result.get("link_type") == "paypal":
        result["paypal_link"] = result.get("paypal_url") or result.get("paypal_link") or ""
        result["provider_redirect_url"] = result.get("paypal_link") or result.get("stripe_redirect_url") or ""
    return {
        "id": public_job_id,
        "status": status_map.get(str(job.get("status") or ""), "running"),
        "percent": int(job.get("progress") or 0),
        "text": str(job.get("step") or "Rust 工作流运行中"),
        "logs": [],
        "result": result if job.get("status") == "succeeded" else None,
        "error": str(job.get("error") or ""),
        "queue_position": 0,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }

STRIPE_CHECKOUT_FRAGMENT = (
    "#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSdpamZkaWAnPyd%2FbScpJ3ZwZ3Zmd2x1cWxqa1Brb"
    "HRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxO"
    "YHwnPyd1blppbHNgWjA0TUp3VnJGM200a31Cakw2aVFEYldvXFN3fzFhUDZjU0pkZ3xGZk5XNnVnQ"
    "E9icEZTRGl0Rn1hfUZQc2pXbTRdUnJXZGZTbGpzUDZuSU5zdW5vbTJMdG5SNTVsXVR2b2o2aycpJ2"
    "N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyZjY2NjY2MnKSdpZHxqcHFRfHVgJ"
    "z8ndmxrYmlgWmxxYGgnKSdga2RnaWBVaWRmYG1qaWFgd3YnP3F3cGB4JSUl"
 )


def normalize_hosted_checkout_url(url: str, session_id: str = "") -> str:
    """Return an OpenAI hosted Checkout URL that can be opened directly."""
    value = str(url or "").strip()
    expected_session = str(session_id or "").strip()
    if not expected_session.startswith(("cs_live_", "cs_test_")):
        return ""
    if value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            try:
                port = parsed.port
            except ValueError:
                port = -1
            host = (parsed.hostname or "").lower().rstrip(".")
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if (
                parsed.scheme.lower() == "https"
                and host in {"checkout.stripe.com", "pay.openai.com"}
                and parsed.username is None
                and parsed.password is None
                and port in {None, 443}
                and len(parts) == 3
                and [part.lower() for part in parts[:2]] == ["c", "pay"]
                and parts[2] == expected_session
            ):
                if host == "checkout.stripe.com":
                    return "https://pay.openai.com" + parsed.path + (
                        f"?{parsed.query}" if parsed.query else ""
                    ) + (f"#{parsed.fragment}" if parsed.fragment else "")
                return value
    # Never surface an untrusted or stale URL.  The canonical URL is derived
    # only from the authoritative, already-classified Checkout ID.
    return f"https://pay.openai.com/c/pay/{expected_session}"


def is_valid_oaics_blik_payment_url(url: str, session_id: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    host = parsed.netloc.lower().rstrip(".")
    path = parsed.path.lower()
    expected_session = str(session_id or "").lower()
    return (
        parsed.scheme.lower() == "https"
        and host in {"chatgpt.com", "chat.openai.com"}
        and "/checkout/" in path
        and expected_session.startswith("oaics_")
        and f"/{expected_session}" in path
    )


PLANS = {
    "plus": "chatgptplusplan",
    "pro": "chatgptpro",
    "team": "chatgptteamplan",
    "codex_low": "chatgptbusiness_usage_based",
}

OPENAI_CHECKOUT_CURRENCIES = {
    "USD", "AUD", "CAD", "GBP", "EUR", "CLP", "JPY", "INR", "IDR", "PKR",
    "THB", "MYR", "TWD", "VND", "PHP", "NGN", "ZAR", "KZT", "TZS", "EGP",
    "BRL", "SEK", "CZK", "PLN", "DKK", "NOK", "KRW", "COP", "MXN", "PEN",
    "HUF", "QAR", "RON", "ILS", "AED", "SGD", "NZD", "CHF", "SAR",
}

# 国家接口可能返回 OpenAI Checkout 尚未接受的本地币种，例如 BA/BAM。
# 欧洲非欧元国家遇到未开放币种时优先使用 EUR，其余地区回退 USD。
EURO_CURRENCY_FALLBACK_COUNTRIES = {
    "AL", "AD", "AM", "BA", "BG", "BY", "CY", "EE", "GE", "HR", "IS", "LI",
    "LT", "LV", "MC", "MD", "ME", "MK", "MT", "RS", "SM", "SK", "SI", "TR",
    "UA", "VA", "XK",
}


def normalize_checkout_currency(country: str, currency: str = "") -> tuple[str, str]:
    country = str(country or "US").strip().upper()
    detected = str(currency or "").strip().upper()
    if detected in OPENAI_CHECKOUT_CURRENCIES:
        return detected, "代理地区接口"
    mapped = str(sc.currency_for_country(country) or "").upper()
    if country in EURO_CURRENCY_FALLBACK_COUNTRIES and detected not in OPENAI_CHECKOUT_CURRENCIES:
        return "EUR", f"OpenAI币种回退（{detected or mapped or '未知'}→EUR）"
    if mapped in OPENAI_CHECKOUT_CURRENCIES:
        return mapped, "国家币种映射"
    return "USD", f"OpenAI币种回退（{detected or mapped or '未知'}→USD）"


COUNTRY_CURRENCY = {
    country: normalize_checkout_currency(country, currency)[0]
    for country, currency in sc.COUNTRY_CURRENCY.items()
}

_TOKEN_JOB_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_JOB_LOCKS_GUARD = threading.Lock()


def checkout_token_lock(raw_token: str) -> threading.Lock:
    key = hashlib.sha256(str(raw_token or "").strip().encode("utf-8")).hexdigest()
    with _TOKEN_JOB_LOCKS_GUARD:
        return _TOKEN_JOB_LOCKS.setdefault(key, threading.Lock())

PAYPAL_CHECKOUT_REGIONS = {
    country: currency
    for country, currency in sc.COUNTRY_CURRENCY.items()
    if currency in OPENAI_CHECKOUT_CURRENCIES
}


def normalize_paypal_checkout_region(country: str, detected_currency: str = "") -> tuple[str, str, str]:
    # Unsupported requested countries use DE/EUR; supported countries retain
    # their own billing pair across every retry.
    country = str(country or "US").strip().upper()
    detected = str(detected_currency or "").strip().upper()
    direct_countries = {str(item).upper() for item in getattr(sc, "PAYPAL_ORDER_COUNTRIES", [])}
    if country in direct_countries:
        currency, source = normalize_checkout_currency(country, detected)
        return country, currency, f"\u5f53\u524d\u56fd\u5bb6\u652f\u6301 PayPal\uff08{source}\uff09"
    return "DE", "EUR", f"\u5f53\u524d\u56fd\u5bb6 {country} \u672a\u5217\u5165 PayPal \u8d26\u5355\u5730\u533a\uff0c\u56de\u9000 DE/EUR"


def resolve_paypal_checkout_region(
    requested_country: str,
    proxy_country: str = "",
    detected_currency: str = "",
    force_de_fallback: bool = False,
) -> tuple[str, str, str]:
    requested = str(requested_country or "US").strip().upper()
    detected_country = str(proxy_country or "").strip().upper()
    if force_de_fallback:
        return "DE", "EUR", f"用户选择的 {requested} 不支持 PayPal 账单，使用 DE/EUR 兼容账单"
    currency_hint = detected_currency if detected_country == requested else ""
    country, currency, source = normalize_paypal_checkout_region(requested, currency_hint)
    if detected_country and detected_country != requested:
        source = f"用户选择 {requested} 优先；代理实测 {detected_country} 不改变账单国家"
    return country, currency, source


class ProxySentinel(BaseSentinel):
    def __init__(self, proxy: str | None, cookies: dict[str, str]):
        super().__init__(impersonate="firefox144", cookies=cookies)
        self.proxy = proxy

    async def _get_session(self):
        if not self._session:
            kwargs: dict[str, Any] = {"impersonate": "firefox144", "timeout": 70}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = requests.AsyncSession(**kwargs)
        return self._session


def _decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
    except Exception:
        return {}


def extract_access_token(raw: str) -> tuple[str, dict]:
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("请填写 Access Token 或 Session JSON")
    token = ""
    meta: dict[str, Any] = {}
    if raw.startswith("{"):
        data = json.loads(raw)
        token = str(data.get("accessToken") or data.get("access_token") or "")
        account = data.get("account") or {}
        if isinstance(account, dict):
            meta.update(account)
    if not token:
        match = re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", raw)
        token = match.group(0) if match else raw.splitlines()[0].strip()
    if token.count(".") < 2:
        raise ValueError("Access Token 格式未识别")
    claims = _decode_jwt(token)
    meta.update({
        "email": claims.get("email") or meta.get("email") or "",
        "exp": claims.get("exp"),
        "account_id": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
            or meta.get("id") or "",
    })
    if meta.get("exp") and int(meta["exp"]) <= int(time.time()):
        raise ValueError("Access Token 已过期")
    return token, meta


def normalize_proxy(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""

    def host_port(text: str) -> tuple[str, int]:
        text = text.strip()
        if text.startswith("[") and "]:" in text:
            host, port_text = text[1:].split("]:", 1)
            host = f"[{host}]"
        else:
            if ":" not in text:
                raise ValueError("代理缺少端口")
            host, port_text = text.rsplit(":", 1)
        if not host or not port_text.isdigit():
            raise ValueError("代理主机或端口格式不正确")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("代理端口超出范围")
        return host, port

    def credentials(text: str) -> tuple[str, str]:
        if ":" not in text:
            raise ValueError("代理凭据格式应为 username:password")
        username, password = text.split(":", 1)
        if not username or not password:
            raise ValueError("代理用户名和密码为空")
        return username, password

    def build(scheme: str, host: str, port: int, username: str = "", password: str = "") -> str:
        if host.lower().rstrip(".").endswith("kookeey.info") and port in {1000, 1086} and scheme in {"http", "https"}:
            scheme = "socks5h"
        auth = ""
        if username or password:
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return f"{scheme}://{auth}{host}:{port}"

    if "://" in value:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"代理协议 {scheme} 暂未支持")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("代理端口格式不正确") from exc
        if not parsed.hostname or port is None:
            raise ValueError("代理 URL 缺少主机或端口")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return build(scheme, host, port, unquote(parsed.username or ""), unquote(parsed.password or ""))

    if value.count("@") == 1:
        left, right = value.split("@", 1)
        try:
            username, password = credentials(left)
            host, port = host_port(right)
            return build("http", host, port, username, password)
        except ValueError:
            host, port = host_port(left)
            username, password = credentials(right)
            return build("http", host, port, username, password)

    parts = value.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = host_port(f"{parts[0]}:{parts[1]}")
        return build("http", host, port, parts[2], ":".join(parts[3:]))
    if len(parts) >= 4 and parts[-1].isdigit():
        host, port = host_port(f"{parts[-2]}:{parts[-1]}")
        return build("http", host, port, parts[0], ":".join(parts[1:-2]))

    host, port = host_port(value)
    return build("http", host, port)


def proxy_route_label(proxy: str) -> str:
    """Return a credential-free proxy label suitable for diagnostics."""
    value = str(proxy or "").strip()
    if not value:
        return "-"
    try:
        parsed = urlsplit(value)
        # Rotating gateways often share one host/port but differ by encoded
        # credentials. Keep the credential hidden while making route changes
        # observable in task logs.
        fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"{parsed.scheme.lower() or 'unknown'}://{parsed.hostname or '?'}:{parsed.port or '?'}#route={fingerprint}"
    except Exception:
        return "invalid-proxy"


def normalize_proxy_pool(raw: Any, label: str) -> list[str]:
    if isinstance(raw, (list, tuple)):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [line.strip() for line in str(raw or "").replace("\r", "").split("\n")]
    values = [value for value in values if value]
    if len(values) > 500:
        raise ValueError(f"{label}最多填写 500 条")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values, 1):
        try:
            proxy = normalize_proxy(value)
        except ValueError as exc:
            raise ValueError(f"{label}第 {index} 条：{exc}") from exc
        if proxy not in seen:
            normalized.append(proxy)
            seen.add(proxy)
    return normalized


def fetch_dynamic_attempt_proxy(country: str, session_time: int = 10) -> str:
    """Fetch exactly one fresh regional proxy for the current outer attempt."""

    country = str(country or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError(f"Invalid dynamic proxy country: {country or '-'}")
    session_time = min(120, max(1, int(session_time or 10)))
    global DYNAMIC_PROXY_API_LAST_AT
    last_error = ""
    for api_attempt in range(1, 5):
        try:
            with DYNAMIC_PROXY_API_LOCK:
                wait_seconds = DYNAMIC_PROXY_API_MIN_INTERVAL - (
                    time.monotonic() - DYNAMIC_PROXY_API_LAST_AT
                )
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                response = requests.get(
                    DYNAMIC_PROXY_API_URL,
                    params={
                        "region": country,
                        "num": 1,
                        "time": session_time,
                        "format": "1",
                        "type": "txt",
                    },
                    timeout=25,
                    impersonate="firefox144",
                )
                DYNAMIC_PROXY_API_LAST_AT = time.monotonic()
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            proxies = normalize_proxy_pool(response.text, f"{country} dynamic proxy")
            if not proxies:
                raise RuntimeError("empty response")
            return proxies[0]
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if api_attempt < 4:
                time.sleep(0.25 * api_attempt + random.random() * 0.2)
    raise RuntimeError(
        f"Dynamic proxy API did not return a valid {country} proxy after 4 attempts: {last_error}"
    )


def generate_cpf() -> str:
    digits = [secrets.randbelow(10) for _ in range(9)]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def generate_cnpj() -> str:
    digits = [secrets.randbelow(10) for _ in range(12)]
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def generate_pix_identity(kind: str) -> dict[str, str]:
    first_names = ("Lucas", "Gabriel", "Rafael", "Matheus", "Mariana", "Beatriz", "Camila", "Larissa")
    last_names = ("Silva", "Santos", "Oliveira", "Souza", "Pereira", "Costa", "Rodrigues", "Almeida")
    locations = (
        ("Avenida Paulista 1000", "Sao Paulo", "SP", "01310-100"),
        ("Rua da Assembleia 10", "Rio de Janeiro", "RJ", "20011-901"),
        ("Avenida Afonso Pena 1500", "Belo Horizonte", "MG", "30130-005"),
        ("Rua XV de Novembro 500", "Curitiba", "PR", "80020-310"),
        ("Avenida Sete de Setembro 800", "Salvador", "BA", "40060-001"),
    )
    first, last = secrets.choice(first_names), secrets.choice(last_names)
    line1, city, state, postal_code = secrets.choice(locations)
    if kind == "cnpj":
        name = f"{first.upper()} {last.upper()} COMERCIO E SERVICOS LTDA"
        source = "generated_cnpj"
    else:
        name = f"{first} {last}"
        source = "generated_cpf"
    return {
        "name": name,
        "email": f"{first.lower()}.{last.lower()}{secrets.randbelow(9000) + 1000}@outlook.com",
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "source": source,
    }


def lookup_cnpj_identity(cnpj: str) -> dict[str, str]:
    value = re.sub(r"\D", "", cnpj or "")
    if len(value) != 14:
        return {}
    resp = requests.get(
        f"https://brasilapi.com.br/api/cnpj/v1/{value}",
        headers={"Accept": "application/json", "User-Agent": sc.CHROME_UA},
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"CNPJ 登记信息查询 HTTP {resp.status_code}")
    data = resp.json() or {}
    street = " ".join(filter(None, [str(data.get("logradouro") or "").strip(), str(data.get("numero") or "").strip()]))
    complement = str(data.get("complemento") or "").strip()
    if complement:
        street = f"{street}, {complement}" if street else complement
    return {
        "name": str(data.get("razao_social") or data.get("nome_fantasia") or "").strip(),
        "line1": street,
        "city": str(data.get("municipio") or "").strip(),
        "state": str(data.get("uf") or "").strip(),
        "postal_code": str(data.get("cep") or "").strip(),
        "status": str(data.get("descricao_situacao_cadastral") or "").strip(),
        "source": "brasilapi_cnpj",
    }


async def sentinel_headers(
    proxy: str,
    flow: str,
    device_id: str,
    cookie: str,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    client_context: CheckoutClientContext | None = None,
    proof_policy: ProofPolicy | None = None,
    payment_endpoint: PaymentEndpoint | str | None = None,
) -> dict[str, str]:
    # The legacy provider remains available for compatibility, but protected
    # GoPay routes must pass through the explicit contract.  The policy owns
    # the required-proof decision so request options cannot silently disable it.
    contract_request = None
    if proof_policy is not None:
        if client_context is None:
            raise PaymentFlowError(
                "CHECKOUT_CLIENT_CONTEXT_REQUIRED",
                "protected Sentinel proof generation requires the bound Checkout context",
                phase="proof",
            )
        flow_value = sentinel_flow(flow)
        if payment_endpoint is None:
            raise PaymentFlowError(
                "PAYMENT_ENDPOINT_REQUIRED",
                "protected Sentinel proof generation requires an explicit payment endpoint",
                phase="configuration",
            )
        endpoint = normalize_payment_endpoint(payment_endpoint)
        if flow_for_endpoint(endpoint) is not flow_value:
            raise PaymentFlowError(
                "ENDPOINT_FLOW_MISMATCH",
                "Sentinel flow does not match the protected payment endpoint",
                phase="configuration",
            )
        contract_request = client_context.proof_request(endpoint)
        # GoPay's policy is authoritative.  ``use_sen/use_so`` are retained in
        # the public signature for older callers but cannot turn off required
        # proof generation on this path.
        if proof_policy.payment_provider == "gopay":
            use_sen = True
            use_so = True
        if client_context.proof_issuer is not None:
            try:
                bundle = await client_context.proof_issuer.issue(contract_request)
                proof_policy.validate(client_context, contract_request, bundle)
                return bundle.http_headers()
            except PaymentFlowError:
                raise
            except Exception as exc:
                raise PaymentFlowError(
                    "PROOF_PROVIDER_FAILED",
                    f"configured proof provider failed: {type(exc).__name__}",
                    phase="proof",
                    retryable=True,
                ) from exc
    if not use_sen and not use_so:
        if proof_policy is not None and not proof_policy.allow_empty_fallback:
            raise PaymentFlowError(
                "SENTINEL_PROOF_REQUIRED",
                "the protected payment route cannot disable all Sentinel proofs",
                phase="proof",
            )
        return {}
    last_error = "empty token"
    for attempt in range(2):
        provider = ProxySentinel(proxy or None, {"oai-did": cookie})
        try:
            token, so, diag = await provider.get_token_pair(flow, device_id)
            init_error = str(diag.get("init_error") or getattr(provider, "_last_init_error", "") or "")
            if use_sen and not token:
                last_error = init_error or "empty token"
                if "SENTINEL_INIT_BLOCKED" in last_error:
                    break
            elif use_sen and diag.get("turnstile_required") and not diag.get("has_t"):
                last_error = "required t proof was not generated"
            elif use_so and diag.get("so_required") and not diag.get("has_so"):
                last_error = "required so proof was not generated"
            else:
                out: dict[str, str] = {}
                if use_sen and token:
                    out["OpenAI-Sentinel-Token"] = json.dumps(token, separators=(",", ":"))
                if use_so and so:
                    out["OpenAI-Sentinel-SO-Token"] = json.dumps(so, separators=(",", ":"))
                if proof_policy is not None and contract_request is not None and client_context is not None:
                    bundle = ProofBundle(
                        endpoint=contract_request.endpoint,
                        flow=contract_request.flow,
                        payment_provider=contract_request.payment_provider,
                        proof_provider=client_context.proof_provider,
                        device_id=contract_request.device_id,
                        did=contract_request.did,
                        user_agent=contract_request.user_agent,
                        proxy_route=contract_request.proxy_route,
                        session_owner=contract_request.session_owner,
                        headers=out,
                        turnstile_required=bool(diag.get("turnstile_required")),
                        session_observer_required=bool(diag.get("so_required")),
                        cookie_identity=contract_request.cookie_identity,
                    )
                    proof_policy.validate(client_context, contract_request, bundle)
                    return bundle.http_headers()
                return out
        except PaymentFlowError:
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            # Cleanup failures must not replace a typed proof-policy failure.
            # The proof outcome is authoritative; provider cleanup is best effort.
            try:
                await provider.close()
            except Exception:
                pass
        if attempt == 0:
            await asyncio.sleep(0.6)
    if proof_policy is not None:
        raise PaymentFlowError(
            "SENTINEL_PROOF_GENERATION_FAILED",
            f"Sentinel token generation failed after fresh-session retry: {last_error[:320]}",
            phase="proof",
            retryable=True,
        )
    raise RuntimeError(f"Sentinel token generation failed after fresh-session retry: {last_error[:320]}")


def checkout_payload(options: dict, meta: dict) -> dict[str, Any]:
    plan = options["plan"]
    country = options.get("checkout_country") or options["country"]
    requested_currency = options.get("checkout_currency") or options["currency"]
    currency, _currency_source = normalize_checkout_currency(country, requested_currency)
    options["currency"] = currency
    options["checkout_currency"] = currency
    billing = {"country": country, "currency": currency}
    checkout_ui_mode = str(options.get("checkout_ui_mode") or "").strip().lower()
    if checkout_ui_mode not in {"custom", "redirect"}:
        checkout_ui_mode = "redirect" if options["link_type"] == "hosted" else "custom"
    common: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": PLANS[plan],
        "billing_details": billing,
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": checkout_ui_mode,
        "check_card_proxy": True,
    }
    promo = options.get("promo_campaign", "").strip()
    if plan == "team":
        common["entry_point"] = "team_workspace_purchase_modal"
        team_data = {
            "workspace_name": options.get("workspace_name") or "Codex Workspace",
            "price_interval": options.get("price_interval") or "month",
            "seat_quantity": int(options.get("seat_quantity") or 5),
        }
        if options.get("workspace_id"):
            team_data["existing_workspace_id"] = options["workspace_id"]
        common["team_plan_data"] = team_data
        if options.get("promo_code"):
            common["promo_code"] = options["promo_code"]
    elif plan == "codex_low":
        common["entry_point"] = "codex_team_start"
        common["usage_based_workspace_credit_purchase_data"] = {
            "quantity": int(options.get("credit_quantity") or 13),
            "unit": "credit",
            "workspace_name": options.get("workspace_name") or "Codex Space",
            "plan_type": "team",
            "auto_top_up_enabled": True,
        }
    elif plan == "plus" and options.get("use_promo") and (
        options.get("link_type") not in {"pix", "momo", "gcash", "gopay", "blik", "paypal", "upi", "ideal", "twint"}
        or options.get("promo_on_create")
    ):
        common["promo_campaign"] = {
            "promo_campaign_id": promo or "plus-1-month-free",
            "is_coupon_from_query_param": bool(options.get("promo_from_query_param")),
        }
    return common


def _verify_checkout_context_cookies(
    http,
    client_context: CheckoutClientContext,
    *,
    phase: str,
) -> None:
    cookie_jar = getattr(http, "cookies", None)
    get_dict = getattr(cookie_jar, "get_dict", None)
    if not callable(get_dict):
        raise PaymentFlowError(
            "CLIENT_COOKIE_IDENTITY_UNVERIFIABLE",
            "protected Checkout session cookie identity cannot be read",
            phase=phase,
        )
    try:
        session_cookies = get_dict()
    except Exception as exc:
        raise PaymentFlowError(
            "CLIENT_COOKIE_IDENTITY_UNVERIFIABLE",
            "protected Checkout session cookie identity cannot be read",
            phase=phase,
        ) from exc
    if not isinstance(session_cookies, dict):
        raise PaymentFlowError(
            "CLIENT_COOKIE_IDENTITY_UNVERIFIABLE",
            "protected Checkout session returned an invalid cookie snapshot",
            phase=phase,
        )
    if any(
        str(session_cookies.get(name) or "") != value
        for name, value in client_context.cookies.items()
    ):
        raise PaymentFlowError(
            "CLIENT_CONTEXT_MISMATCH",
            "protected Checkout session cookie identity differs from its creation context",
            phase=phase,
        )


def _bind_checkout_context_cookies(
    http,
    client_context: CheckoutClientContext,
    *,
    phase: str,
) -> None:
    cookie_jar = getattr(http, "cookies", None)
    setter = getattr(cookie_jar, "set", None)
    if not callable(setter):
        raise PaymentFlowError(
            "CLIENT_COOKIE_BINDING_FAILED",
            "protected Checkout session cannot bind its required cookies",
            phase=phase,
        )
    try:
        for cookie_name, cookie_value in client_context.cookies.items():
            setter(cookie_name, cookie_value, domain="chatgpt.com")
    except Exception as exc:
        raise PaymentFlowError(
            "CLIENT_COOKIE_BINDING_FAILED",
            "protected Checkout session cannot bind its required cookies",
            phase=phase,
        ) from exc
    _verify_checkout_context_cookies(http, client_context, phase=phase)


def _create_checkout_unmanaged(
    token: str,
    payload: dict,
    proxy: str,
    device_id: str,
    did: str,
    log,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    allow_sentinel_fallback: bool = False,
    diagnostic_label: str = "",
    _session_holder: list[Any] | None = None,
    client_context: CheckoutClientContext | None = None,
    proof_policy: ProofPolicy | None = None,
) -> dict:
    http = sc.build_http(proxy or None)
    if _session_holder is not None:
        _session_holder.append(http)
    is_gopay = str(diagnostic_label or "").strip().lower() == "gopay"
    if is_gopay:
        # GoPay always uses a strict proof policy.  The legacy boolean flags
        # remain for generic callers but cannot disable a protected route.
        if str(device_id or "").strip() != str(did or "").strip():
            raise PaymentFlowError(
                "CLIENT_IDENTITY_MISMATCH",
                "GoPay requires oai-did and OAI-Device-Id to be identical",
                phase="configuration",
            )
        if client_context is None:
            client_context = CheckoutClientContext(
                payment_provider="gopay",
                device_id=device_id,
                did=did,
                user_agent=sc.CHROME_UA,
                proxy_route=proxy,
                session_owner=f"checkout-http:{id(http)}",
                proof_provider=ProofProviderKind.LEGACY_PYTHON_NODE.value,
                cookies={"oai-did": did},
            )
        else:
            expected_session_owner = f"checkout-http:{id(http)}"
            if client_context.session_owner != expected_session_owner:
                raise PaymentFlowError(
                    "CLIENT_CONTEXT_MISMATCH",
                    "GoPay Checkout context is not bound to the HTTP session used for creation",
                    phase="configuration",
                )
            expected = CheckoutClientContext(
                payment_provider="gopay",
                device_id=device_id,
                did=did,
                user_agent=client_context.user_agent,
                proxy_route=proxy,
                session_owner=client_context.session_owner,
                proof_provider=client_context.proof_provider,
                cookies=client_context.cookies,
            )
            client_context.validate_request(
                expected.proof_request(PaymentEndpoint.CHECKOUT_CREATE)
            )
        proof_policy = proof_policy or ProofPolicy.strict_gopay()
        if proof_policy.payment_provider != "gopay":
            raise PaymentFlowError(
                "PROOF_POLICY_PROVIDER_MISMATCH",
                "GoPay Checkout must use the GoPay proof policy",
                phase="configuration",
            )
        use_sen = True
        use_so = True
        allow_sentinel_fallback = False
    if is_gopay and client_context is not None:
        _bind_checkout_context_cookies(http, client_context, phase="checkout")
    else:
        try:
            http.cookies.set("oai-did", did, domain="chatgpt.com")
            if client_context is not None:
                for cookie_name, cookie_value in client_context.cookies.items():
                    http.cookies.set(cookie_name, cookie_value, domain="chatgpt.com")
        except Exception:
            pass
    try:
        http.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={
                "User-Agent": client_context.user_agent if client_context is not None else sc.CHROME_UA,
                "Accept": "application/json,text/plain,*/*",
            },
            timeout=20,
        )
    except Exception as exc:
        log(f"ChatGPT 暖身提示：{type(exc).__name__}")
    proof_started_at = time.monotonic()
    s_headers = resolve_payment_sentinel_headers(
        sentinel_headers, proxy, "chatgpt_checkout", device_id, did,
        use_sen=use_sen, use_so=use_so,
        allow_fallback=allow_sentinel_fallback, log=log,
        client_context=client_context,
        proof_policy=proof_policy,
        payment_endpoint=PaymentEndpoint.CHECKOUT_CREATE if proof_policy is not None else None,
    )
    if diagnostic_label:
        log(
            f"{diagnostic_label} 客户端证明：flow=chatgpt_checkout，"
            f"SEN={'yes' if s_headers.get('OpenAI-Sentinel-Token') else 'no'}，"
            f"SO={'yes' if s_headers.get('OpenAI-Sentinel-SO-Token') else 'no'}，"
            f"identity={(client_context.identity_hash if client_context is not None else hashlib.sha256(str(device_id or '').encode('utf-8')).hexdigest()[:12])}"
        )
        if client_context is not None:
            log(render_payment_diagnostic_event(
                client_context,
                phase="checkout_create_proof",
                flow=SentinelFlow.CHATGPT_CHECKOUT,
                sen_present=bool(s_headers.get("OpenAI-Sentinel-Token")),
                so_present=bool(s_headers.get("OpenAI-Sentinel-SO-Token")),
                elapsed_ms=(time.monotonic() - proof_started_at) * 1000,
            ))
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": client_context.user_agent if client_context is not None else sc.CHROME_UA,
        "OAI-Language": "zh-CN",
        "OAI-Device-Id": device_id,
        **s_headers,
    }
    resp = http.post(sc.OPENAI_CHECKOUT_URL, json=payload, headers=headers, timeout=60)
    text = resp.text or ""
    if resp.status_code != 200:
        lowered = text.lower()
        if diagnostic_label.strip().lower() == "momo":
            rebuild_error = momo_create_checkout_rebuild_error(
                f"OpenAI Checkout HTTP {resp.status_code}: {text}",
                bool(payload.get("promo_campaign")),
            )
            if rebuild_error:
                raise RuntimeError(rebuild_error)
        if resp.status_code in {400, 403} and any(
            marker in lowered for marker in ("device", "oai-did", "oai_device", "device_id")
        ):
            raise PaymentFlowError(
                "DEVICE_SESSION_MISMATCH",
                f"OpenAI Checkout HTTP {resp.status_code}: device identity was rejected",
                phase="checkout",
                http_status=resp.status_code,
            )
        if resp.status_code in {400, 403} and any(
            marker in lowered for marker in ("sentinel", "proof", "blocked", "challenge")
        ):
            raise PaymentFlowError(
                "SENTINEL_PROOF_REJECTED",
                f"OpenAI Checkout HTTP {resp.status_code}: client proof was rejected",
                phase="checkout",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            )
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_CHECKOUT_HTTP_ERROR",
                f"GoPay Checkout creation failed: HTTP {resp.status_code}",
                phase="checkout",
                http_status=resp.status_code,
                retryable=(
                    resp.status_code in {408, 425, 429}
                    or resp.status_code >= 500
                ),
            )
        raise RuntimeError(f"OpenAI Checkout HTTP {resp.status_code}: {text[:500]}")
    try:
        data = resp.json()
    except Exception:
        raise PaymentFlowError(
            "CHECKOUT_RESPONSE_INVALID",
            "OpenAI Checkout returned a non-JSON response",
            phase="checkout",
            http_status=resp.status_code,
            retryable=True,
            rebuild_checkout=True,
        )
    if not isinstance(data, dict):
        raise PaymentFlowError(
            "CHECKOUT_RESPONSE_INVALID",
            "OpenAI Checkout response must be a JSON object",
            phase="checkout",
            http_status=resp.status_code,
            retryable=True,
            rebuild_checkout=True,
        )
    url = data.get("url") or ""
    try:
        identity = classify_checkout_session_identity(data, text)
    except CheckoutSessionIdentityConflictError as exc:
        raise PaymentFlowError(
            "CHECKOUT_SESSION_ID_CONFLICT",
            str(exc),
            phase="checkout",
            retryable=True,
            rebuild_checkout=True,
        ) from exc
    if identity is None:
        raise PaymentFlowError(
            "CHECKOUT_SESSION_ID_MISSING",
            "Checkout response did not contain a recognized session identity",
            phase="checkout",
            retryable=True,
            rebuild_checkout=True,
        )
    sid = identity.session_id
    if diagnostic_label and client_context is not None:
        log(render_payment_diagnostic_event(
            client_context,
            phase="checkout_create_result",
            flow=SentinelFlow.CHATGPT_CHECKOUT,
            sen_present=bool(s_headers.get("OpenAI-Sentinel-Token")),
            so_present=bool(s_headers.get("OpenAI-Sentinel-SO-Token")),
            checkout_type=identity.kind,
            payment_method_source="checkout_response",
            elapsed_ms=(time.monotonic() - proof_started_at) * 1000,
        ))
    if identity.kind == "oaics":
        processor = str(data.get("processor_entity") or "openai_ie").strip() or "openai_ie"
        data["checkout_session_id"] = sid
        data["checkout_provider"] = str(data.get("checkout_provider") or "open_ai")
        data["processor_entity"] = processor
        data["is_custom_checkout"] = True
        data["source_checkout_url"] = str(url or "")
        data["checkout_url"] = f"https://chatgpt.com/checkout/{processor}/{sid}"
        result = {"data": data, "http": http}
        if client_context is not None:
            result["client_context"] = client_context
        return result
    data["checkout_session_id"] = sid
    data["checkout_url"] = normalize_hosted_checkout_url(url, sid)
    result = {"data": data, "http": http}
    if client_context is not None:
        result["client_context"] = client_context
    return result


def create_checkout(
    token: str,
    payload: dict,
    proxy: str,
    device_id: str,
    did: str,
    log,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    allow_sentinel_fallback: bool = False,
    diagnostic_label: str = "",
    client_context: CheckoutClientContext | None = None,
    proof_policy: ProofPolicy | None = None,
) -> dict:
    """Create a Checkout and close its HTTP session when creation fails.

    A successful result transfers ownership of ``http`` to the caller, which
    closes it after the full payment flow.  Any exception before that handoff
    must release the session here so retries do not accumulate open pools.
    """
    session_holder: list[Any] = []
    try:
        return _create_checkout_unmanaged(
            token,
            payload,
            proxy,
            device_id,
            did,
            log,
            use_sen=use_sen,
            use_so=use_so,
            allow_sentinel_fallback=allow_sentinel_fallback,
            diagnostic_label=diagnostic_label,
            _session_holder=session_holder,
            client_context=client_context,
            proof_policy=proof_policy,
        )
    except BaseException:
        close_http_sessions(session_holder)
        raise


def create_local_method_cs_live_checkout(
    token: str,
    payload: dict,
    proxy: str,
    device_id: str,
    did: str,
    log,
    *,
    attempts: int = 10,
    use_sen: bool = True,
    use_so: bool = True,
    method_name: str,
    error_prefix: str,
    allow_sentinel_fallback: bool = True,
    creation_budget: CheckoutCreationBudget | None = None,
    cancel_check=None,
    proof_policy: ProofPolicy | None = None,
) -> tuple[dict, str, str]:
    """Rebuild redirect Checkouts until a local method receives CS Live."""
    max_attempts = max(1, min(int(attempts or 10), 10))
    current_device_id = str(device_id or did or uuid.uuid4())
    current_did = current_device_id if error_prefix == "GOPAY" else str(did or current_device_id)
    if error_prefix == "GOPAY" and str(did or "") != current_device_id:
        log("GoPay 设备身份已统一：oai-did 与 OAI-Device-Id 使用同一值")
    last_kind = "unknown"
    for attempt in range(1, max_attempts + 1):
        if callable(cancel_check):
            cancel_check()
        if creation_budget is not None:
            creation_budget.consume()
            log(
                f"{method_name} Checkout 共享创建预算："
                f"已使用 {creation_budget.used}/{creation_budget.limit}"
            )
        try:
            created = create_checkout(
                token,
                payload,
                proxy,
                current_device_id,
                current_did,
                log,
                use_sen=use_sen,
                use_so=use_so,
                allow_sentinel_fallback=allow_sentinel_fallback,
                diagnostic_label=method_name if error_prefix == "GOPAY" else "",
                proof_policy=(
                    proof_policy
                    if proof_policy is not None
                    else (ProofPolicy.strict_gopay() if error_prefix == "GOPAY" else None)
                ),
            )
        except Exception as exc:
            message = str(exc)
            retryable = (
                exc.retryable
                if isinstance(exc, PaymentFlowError)
                else (
                    bool(_proxy_transport_error_kind(message))
                    or bool(re.search(r"OpenAI Checkout HTTP (?:429|5\d\d)\b", message))
                    or message.startswith("Sentinel token generation failed")
                )
            )
            if not retryable:
                raise
            log(
                f"{method_name} CS Live 创建尝试 {attempt}/{max_attempts}："
                f"临时失败 {type(exc).__name__}: {message[:180]}"
            )
            if attempt >= max_attempts:
                raise PaymentFlowError(
                    f"{error_prefix}_CS_LIVE_CREATE_RETRY_EXHAUSTED",
                    f"连续 {max_attempts} 次创建均未完成；最后错误类型={type(exc).__name__}",
                    phase="checkout",
                    retryable=True,
                    rebuild_checkout=True,
                ) from exc
            next_identity = str(uuid.uuid4())
            current_device_id = next_identity
            current_did = next_identity if error_prefix == "GOPAY" else str(uuid.uuid4())
            continue
        checkout_data = created.get("data") or {}
        session_id = str(checkout_data.get("checkout_session_id") or "")
        last_kind = session_checkout_kind(session_id)
        kind_label = {
            "oaics": "OAICS",
            "cs_live": "CS Live",
            "cs_test": "CS Test",
        }.get(last_kind, "unknown")
        log(f"{method_name} CS Live 创建尝试 {attempt}/{max_attempts}：{kind_label}")
        if last_kind == "cs_live":
            return created, current_device_id, current_did

        close_http_sessions([created.get("http")])
        if last_kind != "oaics":
            raise PaymentFlowError(
                f"{error_prefix}_CHECKOUT_TYPE_UNKNOWN",
                f"{method_name} redirect Checkout 未返回可识别的 cs_live/oaics 会话",
                phase="checkout",
                retryable=True,
                rebuild_checkout=True,
            )
        if attempt < max_attempts:
            log(f"{method_name} 当前返回 OAICS；丢弃该会话并刷新设备标识，继续强制重建 CS Live")
            next_identity = str(uuid.uuid4())
            current_device_id = next_identity
            current_did = next_identity if error_prefix == "GOPAY" else str(uuid.uuid4())

    raise PaymentFlowError(
        f"{error_prefix}_CS_LIVE_REBUILD_EXHAUSTED",
        f"同一 Checkout 代理连续 {max_attempts} 次返回 {last_kind.upper()}，将切换代理后继续重建",
        phase="checkout",
        retryable=True,
        rebuild_checkout=True,
    )


def create_gopay_cs_live_checkout(
    token: str,
    payload: dict,
    proxy: str,
    device_id: str,
    did: str,
    log,
    *,
    attempts: int = 10,
    use_sen: bool = True,
    use_so: bool = True,
    creation_budget: CheckoutCreationBudget | None = None,
    cancel_check=None,
    proof_policy: ProofPolicy | None = None,
) -> tuple[dict, str, str]:
    # Never derive GoPay proof requirements from user-controlled booleans.
    strict_policy = proof_policy or ProofPolicy.strict_gopay()
    return create_local_method_cs_live_checkout(
        token, payload, proxy, device_id, did, log,
        attempts=attempts, use_sen=True, use_so=True,
        method_name="GoPay", error_prefix="GOPAY",
        allow_sentinel_fallback=False,
        creation_budget=creation_budget,
        cancel_check=cancel_check,
        proof_policy=strict_policy,
    )


def preflight_trial_eligibility(
    token: str,
    account_id: str,
    proxy: str,
    device_id: str,
    did: str,
    log,
    *,
    coupon_fallback: bool = False,
) -> dict:
    rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
    if rust_base:
        try:
            rust_response = requests.post(
                f"{rust_base}/api/v1/offers/check",
                json={
                    "access_token": token,
                    "account_id": account_id,
                    "proxy": proxy,
                    "transport": str(os.getenv("PAY153_RUST_TRANSPORT") or "curl_cffi"),
                },
                timeout=50,
            )
            if rust_response.status_code == 200:
                rust_data = rust_response.json() or {}
                offer = rust_data.get("offer") or {}
                campaign_id = str(offer.get("campaign_id") or "").strip()
                normalized = {
                    "promotion_source": "pay153_rust",
                    "promotion_http_status": 200,
                    "one_click_trial_eligible": bool(offer.get("eligible")),
                    "promo_campaign_id": campaign_id,
                    "promotion_label": str(offer.get("label") or ""),
                    "promotion_title": str(offer.get("title") or ""),
                    "promotion_discount_percentage": offer.get("discount_percentage"),
                    "promotion_duration_months": (
                        offer.get("duration_periods")
                        if offer.get("duration_unit") == "month"
                        else None
                    ),
                    "promotion_duration_period": str(offer.get("duration_unit") or ""),
                    "promotion_processor": str(offer.get("processor") or ""),
                    "promotion_transport": str(offer.get("transport") or ""),
                }
                log(
                    f"Rust \u4f18\u60e0\u68c0\u6d4b\u5b8c\u6210\uff1a"
                    f"{campaign_id or '\u5f53\u524d\u65e0\u4f18\u60e0'}\uff08{normalized['promotion_transport']}\uff09"
                )
                if normalized["one_click_trial_eligible"] or campaign_id or not coupon_fallback:
                    return normalized
                log("Rust 优惠检测未匹配活动，继续使用 GoPay 优惠券协议复核")
            log(f"Rust \u4f18\u60e0\u68c0\u6d4b HTTP {rust_response.status_code}\uff0c\u56de\u9000 Python")
        except Exception as rust_exc:
            log(f"Rust \u4f18\u60e0\u68c0\u6d4b\u5f02\u5e38\uff1a{type(rust_exc).__name__}\uff0c\u56de\u9000 Python")

    """Read the account catalog, with GoPay's explicit coupon protocol as fallback."""
    http = sc.build_http(proxy)

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        close = getattr(http, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return result

    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Language": "zh-CN",
        "OAI-Device-Id": device_id,
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    account_result: dict[str, Any] = {}
    try:
        if account_id:
            resp = http.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                headers=headers,
                timeout=35,
            )
            if resp.status_code != 200:
                log(f"账号活动目录返回 HTTP {resp.status_code}")
                account_result = {
                    "promotion_source": "accounts_check",
                    "promotion_http_status": resp.status_code,
                }
            else:
                data = resp.json() or {}
                accounts = data.get("accounts") or {}
                account = accounts.get(account_id) or accounts.get("default") or {}
                campaigns = account.get("eligible_promo_campaigns") or {}
                plus = campaigns.get("plus") or {}
                metadata = plus.get("metadata") or {}
                discount_data = metadata.get("discount") or {}
                duration_data = metadata.get("duration") or {}
                campaign_id = str(plus.get("id") or plus.get("campaign_id") or "").strip()
                discount = discount_data.get("percentage")
                duration = duration_data.get("num_periods")
                duration_period = duration_data.get("period") or ""
                label = metadata.get("promotion_type_label") or metadata.get("title") or metadata.get("summary") or ""
                processor = metadata.get("processor") or ""
                account_result = {
                    "promotion_source": "accounts_check",
                    "promotion_http_status": resp.status_code,
                    "one_click_trial_eligible": bool(campaign_id),
                    "promo_campaign_id": campaign_id,
                    "promotion_label": label,
                    "promotion_title": metadata.get("title") or "",
                    "promotion_discount_percentage": discount,
                    "promotion_duration_months": duration if duration_period == "month" else None,
                    "promotion_duration_period": duration_period,
                    "promotion_processor": processor,
                    "eligible_offers": account.get("eligible_offers") or {},
                }
                if campaign_id:
                    log(f"账号活动目录已匹配：{campaign_id}（{label or 'Plus 活动'}）")
                    return finish(account_result)
                log("账号活动目录未返回 Plus 优惠")
    except Exception as exc:
        log(f"账号活动目录读取失败：{type(exc).__name__}")

    if not coupon_fallback:
        return finish(account_result)

    try:
        coupon_resp = http.get(
            "https://chatgpt.com/backend-api/promo_campaign/check_coupon",
            params={
                "coupon": "plus-1-month-free",
                "is_coupon_from_query_param": "true",
            },
            headers=headers,
            timeout=35,
        )
        coupon_payload = coupon_resp.json() if coupon_resp.status_code == 200 else {}
        coupon_payload = coupon_payload if isinstance(coupon_payload, dict) else {}
        coupon_state = str(coupon_payload.get("state") or "").strip().lower()
        coupon_eligible = coupon_resp.status_code == 200 and coupon_state == "eligible"
        log(
            "GoPay 优惠券复核：HTTP {}，state={}，flow=query_param".format(
                coupon_resp.status_code,
                coupon_state or "unknown",
            )
        )
        return finish({
            **account_result,
            "promotion_source": "coupon_check",
            "promotion_http_status": coupon_resp.status_code,
            "one_click_trial_eligible": coupon_eligible,
            "promo_campaign_id": "plus-1-month-free" if coupon_eligible else "",
            "promotion_label": "Plus coupon trial" if coupon_eligible else "",
            "coupon_state": coupon_state,
            "is_coupon_from_query_param": True,
        })
    except Exception as exc:
        log(f"GoPay 优惠券复核失败：{type(exc).__name__}")
        return finish(account_result)

def promo_campaign_from_payload(payload: Any) -> str:
    """Extract the account-specific campaign id returned by OpenAI.

    Campaign ids are not guaranteed to stay equal to the UI label.  The update
    endpoint may accept a stale/default id and still return ``success=true``,
    while final approval rejects it as ``invalid_promotion``.
    """
    candidates: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower in {
                    "promo_campaign_id",
                    "promotion_campaign_id",
                    "campaign_id",
                } and isinstance(item, str):
                    candidate = item.strip()
                    if candidate:
                        candidates.append(candidate)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return candidates[0] if candidates else ""


def proxy_country_hint(proxy: str) -> str:
    value = unquote(str(proxy or ""))
    for pattern in (
        r"(?i)(?:^|[-_:])region[-_=]?([a-z]{2})(?:[-_:]|$)",
        r"(?i)(?:^|[-_:])country[-_=]?([a-z]{2})(?:[-_:]|$)",
        r"(?i)(?:^|[-_:])location[-_=]?([a-z]{2})(?:[-_:]|$)",
    ):
        match = re.search(pattern, value)
        if match:
            return match.group(1).upper()
    return ""


def proxy_geo(proxy: str) -> dict[str, str]:
    hinted_country = proxy_country_hint(proxy)
    if hinted_country:
        return {
            "country": hinted_country, "currency": "", "region": "代理参数",
            "city": "", "postal": "", "timezone": "", "source": "proxy_hint",
        }
    probes = (
        "https://ipapi.co/json/",
        "https://ipwho.is/",
        "https://api.country.is/",
        "https://api.ip.sb/geoip",
        "https://ipinfo.io/json",
    )
    errors: list[str] = []
    for url in probes:
        http = None
        try:
            # A failed TLS tunnel can poison a keep-alive connection. Use a
            # fresh browser session for each independent geo provider.
            http = sc.build_http(proxy)
            resp = http.get(url, timeout=12)
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
                continue
            data = resp.json() or {}
            if data.get("success") is False:
                errors.append("provider_failed")
                continue
            country = str(
                data.get("country_code") or data.get("countryCode")
                or data.get("country") or data.get("country_code2") or ""
            ).upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                continue
            currency_value = data.get("currency") or ""
            if isinstance(currency_value, dict):
                currency_value = currency_value.get("code") or ""
            currency = str(currency_value).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                currency = ""
            return {
                "country": country,
                "currency": currency,
                "region": str(data.get("region") or data.get("region_name") or data.get("regionName") or ""),
                "city": str(data.get("city") or ""),
                "postal": str(data.get("postal") or data.get("zip") or ""),
                "timezone": str(data.get("timezone") or ""),
                "source": url,
            }
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            close_http_sessions([http])
    raise RuntimeError(f"代理地区检测失败：{' / '.join(errors[-5:]) or 'no response'}")


_PROXY_GEO_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_PROXY_GEO_CACHE_LOCK = threading.Lock()


def proxy_geo_cached(proxy: str, ttl: int = 900) -> dict[str, str]:
    now = time.time()
    with _PROXY_GEO_CACHE_LOCK:
        cached = _PROXY_GEO_CACHE.get(proxy)
        if cached and now - cached[0] <= ttl:
            return dict(cached[1])
    data = proxy_geo(proxy)
    with _PROXY_GEO_CACHE_LOCK:
        _PROXY_GEO_CACHE[proxy] = (now, dict(data))
    return data


def select_paypal_exit_proxy(
    preferred: str,
    pool: list[str],
    scan_limit: int = 24,
    expected_country: str = "",
) -> tuple[str, dict[str, str], list[str]]:
    """Prefer a proxy matching the user-selected PayPal billing country."""
    rest = [proxy for proxy in dict.fromkeys(pool) if proxy and proxy != preferred]
    random.SystemRandom().shuffle(rest)
    candidates = ([preferred] if preferred else []) + rest
    candidates = candidates[:max(1, min(int(scan_limit), len(candidates)))]
    if not candidates:
        raise RuntimeError("Checkout 代理池为空")

    rejected: list[str] = []
    expected = str(expected_country or "").strip().upper()
    fallback: tuple[str, dict[str, str]] | None = None
    executor = ThreadPoolExecutor(max_workers=min(6, len(candidates)), thread_name_prefix="paypal-geo")
    future_map = {executor.submit(proxy_geo_cached, proxy): proxy for proxy in candidates}
    try:
        for future in as_completed(future_map):
            proxy = future_map[future]
            try:
                geo = future.result()
            except Exception:
                continue
            country = str(geo.get("country") or "").upper()
            if re.fullmatch(r"[A-Z]{2}", country) and (not expected or country == expected):
                for pending in future_map:
                    if pending is not future:
                        pending.cancel()
                return proxy, geo, rejected
            if re.fullmatch(r"[A-Z]{2}", country) and fallback is None:
                fallback = (proxy, geo)
            if country and country not in rejected:
                rejected.append(country)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if fallback is not None:
        return fallback[0], fallback[1], rejected
    summary = "/".join(rejected[:12]) or "未识别"
    raise RuntimeError(
        f"Checkout 代理池本轮未找到 OpenAI 支持的 PayPal 账单地区；已检测：{summary}。"
        "系统将更换代理继续尝试"
    )


def proxy_country(proxy: str, expected_country: str = "") -> tuple[str, str]:
    expected = str(expected_country or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", expected):
        return expected, "国家代理池"
    data = proxy_geo_cached(proxy)
    return data["country"], data["region"]


def momo_promotion_action(
    native_methods: list[str],
    custom_method_id: str,
    amount: Any,
    currency: str,
    promo_requested: bool,
    promo_on_create: bool = False,
) -> str:
    """Choose the next OAICS promotion step without mutating the checkout."""
    momo_ready = (
        "momo" in native_methods
        or str(custom_method_id or "").strip().startswith("cpmt_")
    )
    if not momo_ready:
        return "rebuild"
    if promo_requested and is_momo_promo_amount(amount, currency):
        return "already_discounted"
    if promo_requested and promo_on_create:
        return "rebuild_late"
    return "refresh" if promo_requested else "continue"


def momo_promotion_is_payment_method_incompatible(error: Any) -> bool:
    message = str(error or "").lower()
    return "promotion is not compatible with the checkout's payment methods" in message


def momo_create_checkout_rebuild_error(error: Any, promo_on_create: bool) -> str:
    """Map an OAICS creation-time campaign rejection to an inner rebuild error."""
    if not momo_promotion_is_payment_method_incompatible(error):
        return ""
    strategy = "create_with_promo" if promo_on_create else "create_without_promo"
    return (
        "MOMO_PROMOTION_INCOMPATIBLE_REBUILD_REQUIRED: MoMo Checkout 创建时服务端拒绝当前优惠与支付方式集合；"
        f"strategy={strategy}；将丢弃创建请求并切换优惠时序"
    )


def momo_checkout_requires_rebuild(error: Any) -> bool:
    message = str(error or "").lower()
    return any(marker in message for marker in (
        "momo_checkout_rebuild_required",
        "momo_promotion_incompatible_rebuild_required",
        "momo_create_promotion_not_applied_rebuild_required",
        "momo_method_removed_rebuild_required",
        "momo_promo_amount_required",
        "momo_oaics_confirm_blocked",
        "custom_confirm_blocked",
        "momo_redirect_missing",
    ))


def momo_reuses_checkout_http_session(
    provider: str,
    checkout_proxy: str,
    promotion_proxy: str,
) -> bool:
    return (
        str(provider or "").strip().lower() == "momo"
        and bool(str(checkout_proxy or "").strip())
        and str(checkout_proxy or "").strip() == str(promotion_proxy or "").strip()
    )


def oaics_stage_native_payment_method_types(
    current: dict[str, Any],
    fallback: dict[str, Any] | None = None,
) -> list[str]:
    """Use a stage's explicit methods before falling back to an earlier response.

    An explicitly present empty method list is meaningful: the server may have
    withdrawn a previously published payment method.  In that case falling
    back to an earlier response would make a stale MoMo method look usable.
    """
    current_methods = oaics_native_payment_method_types(current)
    if oaics_payload_declares_payment_methods(current):
        return current_methods
    return oaics_native_payment_method_types(fallback or {})


def oaics_payload_declares_payment_methods(value: Any, depth: int = 0) -> bool:
    del depth  # Retained for compatibility with older direct callers.
    return published_payment_method_snapshot(value)[1]


def oaics_nested_custom_payment_method_id(
    value: Any,
    provider: str,
    depth: int = 0,
) -> str:
    if depth > 6:
        return ""
    if isinstance(value, dict):
        method_id = custom_payment_method_id_for(
            value,
            provider,
            allow_unlabelled_sole=str(provider or "").strip().lower() != "momo",
        )
        if method_id:
            return method_id
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                method_id = oaics_nested_custom_payment_method_id(
                    item, provider, depth + 1,
                )
                if method_id:
                    return method_id
    elif isinstance(value, (list, tuple)):
        for item in value:
            method_id = oaics_nested_custom_payment_method_id(
                item, provider, depth + 1,
            )
            if method_id:
                return method_id
    return ""


def oaics_stage_custom_payment_method_id(
    current: dict[str, Any],
    fallback: dict[str, Any] | None,
    provider: str,
) -> str:
    """Do not preserve a cpmt when the current stage explicitly replaced its list."""
    current_declares_methods = oaics_payload_declares_payment_methods(current)
    current_method_id = oaics_nested_custom_payment_method_id(current, provider)
    if current_method_id or current_declares_methods:
        return current_method_id
    return oaics_nested_custom_payment_method_id(fallback or {}, provider)


def close_http_sessions(sessions: list[Any]) -> None:
    seen: set[int] = set()
    for session in sessions:
        if session is None or id(session) in seen:
            continue
        seen.add(id(session))
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class HttpSessionRegistry:
    """Own synchronous HTTP sessions until explicitly released or closed."""

    def __init__(self) -> None:
        self._sessions: list[Any] = []

    def track(self, session: Any) -> Any:
        if session is not None:
            self._sessions.append(session)
        return session

    def release(self, session: Any) -> Any:
        self._sessions = [item for item in self._sessions if item is not session]
        return session

    def close(self) -> None:
        sessions, self._sessions = self._sessions, []
        close_http_sessions(sessions)


def update_checkout_promo(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    campaign_id: str,
    log,
    *,
    device_id: str = "",
    is_coupon_from_query_param: bool = False,
) -> dict:
    body = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "plan_name": PLANS["plus"],
        "price_interval": "month",
        "seat_quantity": 1,
        "discount_code": None,
        "promo_campaign": {
            "promo_campaign_id": campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": bool(is_coupon_from_query_param),
        },
    }
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/update",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        },
        timeout=45,
    )
    text = resp.text or ""
    log(f"[promo] checkout/update: {resp.status_code} {text[:180]}")
    if resp.status_code != 200:
        if resp.status_code == 403 and "promotion is not available" in text.lower():
            raise RuntimeError(
                "PROMOTION_NOT_AVAILABLE: 当前 Checkout 会话未接受该 0 元活动，不能继续生成支付链接"
            )
        raise RuntimeError(f"应用 Plus 优惠失败：HTTP {resp.status_code} {text[:300]}")
    try:
        payload = resp.json() or {}
    except Exception:
        return {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"应用 Plus 优惠失败：HTTP 200 success=false {text[:300]}")
    returned_campaign = promo_campaign_from_payload(payload)
    log(
        "[promo] update accepted: requested={} returned={} query_param={}".format(
            campaign_id or "plus-1-month-free",
            returned_campaign or "not_echoed",
            str(bool(is_coupon_from_query_param)).lower(),
        )
    )
    return payload if isinstance(payload, dict) else {}


def fetch_custom_checkout_session(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
) -> dict[str, Any]:
    resp = http.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{session_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Device-Id": device_id,
        },
        timeout=45,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"读取自定义 Checkout 失败：HTTP {resp.status_code} {text[:300]}")
    try:
        return resp.json() or {}
    except Exception:
        raise RuntimeError(f"读取自定义 Checkout 返回非 JSON：{text[:300]}")


def fetch_custom_checkout_session_with_retry(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    log=None,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.8,
    require_paypal: bool = False,
    required_provider: str = "",
    preserve_payment_methods_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read an OAICS session until its custom payment methods are published."""
    last: dict[str, Any] = {}
    # Only carry forward methods that are explicitly identified as the
    # requested provider.  Carrying every cpmt_* entry from the creation
    # response can make a later response look ready for the wrong method.
    preserved_methods = (
        list(preserve_payment_methods_from.get("custom_payment_methods") or [])
        if isinstance(preserve_payment_methods_from, dict) else []
    )
    if required_provider:
        preserved_methods = custom_payment_methods_for(
            {"custom_payment_methods": preserved_methods}, required_provider,
        )
    total_attempts = max(1, int(attempts))
    for attempt in range(total_attempts):
        last = fetch_custom_checkout_session(
            http, token, session_id, processor_entity, device_id,
        )
        # A response with any explicit method container is the authoritative
        # snapshot for this Checkout, including an explicit empty list.  Only
        # responses that omit all method containers may inherit a prior method
        # while the server is still publishing the session asynchronously.
        explicit_methods = oaics_payload_declares_payment_methods(last)
        methods = oaics_custom_payment_method_items(last)
        if preserved_methods and not explicit_methods:
            known_ids = {
                str(method.get("id") or "")
                for method in methods if isinstance(method, dict)
            }
            methods.extend(
                method for method in preserved_methods
                if not isinstance(method, dict)
                or not str(method.get("id") or "")
                or str(method.get("id") or "") not in known_ids
            )
            if methods != (last.get("custom_payment_methods") or []):
                last = dict(last)
                last["custom_payment_methods"] = methods
        paypal_ready = any(
            "paypal" in json.dumps(method, ensure_ascii=False).lower()
            for method in methods
        )
        provider_ready = (
            bool(custom_payment_method_id_for(last, required_provider))
            if required_provider else True
        )
        if methods and (not require_paypal or paypal_ready) and provider_ready:
            if attempt:
                if log:
                    log(f"OAICS 支付方式延迟就绪（第 {attempt + 1} 次读取）")
            return last
        if attempt + 1 < total_attempts:
            if log:
                log(f"OAICS 支付方式尚未就绪（第 {attempt + 1} 次读取）")
            time.sleep(max(0.0, float(delay_seconds)) * (attempt + 1))
    return last


def oaics_custom_payment_method_items(payload: dict[str, Any]) -> list[Any]:
    found: list[Any] = []
    seen: set[str] = set()
    for key in (
        "custom_payment_methods",
        "customPaymentMethods",
        "payment_methods",
        "paymentMethods",
    ):
        methods = payload.get(key)
        if not isinstance(methods, list):
            continue
        for method in methods:
            method_id = str(method.get("id") or "") if isinstance(method, dict) else ""
            marker = (
                f"id:{method_id}"
                if method_id
                else json.dumps(method, ensure_ascii=False, sort_keys=True, default=str)
            )
            if marker in seen:
                continue
            seen.add(marker)
            found.append(method)
    return found


def custom_payment_methods_for(payload: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    """Return OAICS custom methods identified by provider-specific fields.

    OAICS has returned several shapes over time (for example ``name``,
    ``paymentMethodType`` and nested ``provider`` objects).  Matching the
    complete JSON string is too brittle and can also match unrelated values.
    """
    provider_name = str(provider or "").strip().lower().replace("-", "_")
    aliases = {
        "gopay": {"gopay", "gopay_wallet", "gopay_tokenization", "gopay_tokenization_linking"},
        "gcash": {"gcash", "gcash_wallet"},
        "paypal": {"paypal"},
        "blik": {"blik"},
        "ideal": {"ideal", "ideal_bank"},
        "kakao": {"kakao", "kakao_pay", "kakaopay"},
    }.get(provider_name, {provider_name})
    methods = oaics_custom_payment_method_items(payload)

    def values(value: Any, depth: int = 0) -> list[str]:
        if depth > 3:
            return []
        if isinstance(value, dict):
            result: list[str] = []
            for key in (
                "id", "type", "name", "label", "display_name", "provider",
                "payment_method_type", "paymentMethodType", "method_type",
                "custom_payment_method_type", "customPaymentMethodType",
            ):
                if key in value:
                    result.extend(values(value.get(key), depth + 1))
            return result
        if isinstance(value, (list, tuple)):
            result: list[str] = []
            for item in value:
                result.extend(values(item, depth + 1))
            return result
        text = str(value or "").strip().lower().replace("-", "_")
        return [text] if text else []

    matched: list[dict[str, Any]] = []
    for item in methods:
        if not isinstance(item, dict) or not str(item.get("id") or "").startswith("cpmt_"):
            continue
        tokens = set(values(item))
        if tokens & aliases:
            matched.append(item)
            continue
        # Some payloads use a human label such as "GoPay wallet".  Restrict
        # this fallback to labels and only when the provider is unambiguous.
        labels = {
            str(item.get(key) or "").strip().lower().replace("-", "_")
            for key in ("name", "label", "display_name")
        }
        if any(alias in label.replace(" ", "_") for alias in aliases for label in labels):
            matched.append(item)
    return matched


def custom_payment_method_id_for(
    payload: dict[str, Any],
    provider: str,
    *,
    allow_unlabelled_sole: bool = True,
) -> str:
    """Select a provider-specific cpmt id, optionally allowing a sole fallback."""
    methods = [
        item for item in oaics_custom_payment_method_items(payload)
        if isinstance(item, dict) and str(item.get("id") or "").startswith("cpmt_")
    ]
    matched = custom_payment_methods_for(payload, provider)
    if matched:
        return str(matched[0].get("id") or "")
    if allow_unlabelled_sole and len(methods) == 1:
        method = methods[0]
        descriptors = [
            method.get(key)
            for key in (
                "type", "name", "label", "display_name", "displayName", "provider",
                "payment_method_type", "paymentMethodType", "method_type", "methodType",
                "custom_payment_method_type", "customPaymentMethodType",
            )
            if method.get(key) not in (None, "", [], {})
        ]
        if not descriptors:
            return str(methods[0].get("id") or "")
    return ""


def custom_payment_methods_diagnostic(payload: dict[str, Any]) -> str:
    """Return a compact, secret-free method summary for failure diagnostics."""
    methods = oaics_custom_payment_method_items(payload)
    out: list[str] = []
    for item in methods:
        if not isinstance(item, dict):
            continue
        method_id = str(item.get("id") or "")
        labels = [
            str(item.get(key) or "")
            for key in ("type", "name", "payment_method_type", "paymentMethodType")
            if item.get(key)
        ]
        out.append(": ".join(part for part in (method_id, "/".join(labels)) if part))
    return ", ".join(out) or "[]"


def submit_custom_checkout_taxes(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    billing: dict[str, Any],
    currency: str,
    device_id: str,
) -> dict[str, Any]:
    address = dict(billing.get("address") or {})
    clean_address = {
        "country": str(address.get("country") or "PH").upper(),
        "line1": str(address.get("line1") or ""),
        "line2": str(address.get("line2") or ""),
        "city": str(address.get("city") or ""),
        "state": str(address.get("state") or ""),
        "postal_code": str(address.get("postal_code") or ""),
    }
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        json={
            "checkout_session_id": session_id,
            "checkout_email": str(billing.get("email") or ""),
            "billing_country": clean_address["country"],
            "billing_name": str(billing.get("name") or ""),
            "currency": str(currency or "PHP").upper(),
            "tax_id": str(billing.get("tax_id") or "") or None,
            "processor_entity": processor_entity,
            "billing_address": clean_address,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        },
        timeout=50,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"提交 Checkout 账单地址失败：HTTP {resp.status_code} {text[:300]}")
    try:
        payload = resp.json() or {}
    except Exception as exc:
        raise RuntimeError(f"提交 Checkout 账单地址返回非 JSON：{text[:300]}") from exc
    checkout = payload.get("checkout_session") or {}
    return checkout if isinstance(checkout, dict) else {}


def oaics_native_payment_method_types(*payloads: Any) -> list[str]:
    """Collect enabled OAICS types through the shared OAICS/CS Live contract."""
    found: list[str] = []
    for payload in payloads:
        methods, _explicit = published_payment_method_snapshot(
            payload,
            include_custom_methods=False,
        )
        for method in methods:
            if method not in found:
                found.append(method)
    return found


def fetch_oaics_native_checkout_with_retry(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    provider: str,
    *,
    preserve_from: dict[str, Any] | None = None,
    attempts: int = 6,
    delay_seconds: float = 0.8,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Poll OAICS until a native method such as ``momo`` is published."""
    wanted = str(provider or "").strip().lower().replace("-", "_")
    last: dict[str, Any] = {}
    total = max(1, int(attempts))
    for attempt in range(total):
        last = fetch_custom_checkout_session(
            http, token, session_id, processor_entity, device_id,
        )
        # A response that explicitly contains an empty method list supersedes
        # the previous stage.  Only responses without any method declaration
        # may inherit the prior snapshot while the OAICS payload settles.
        methods = oaics_stage_native_payment_method_types(last, preserve_from)
        if wanted in methods:
            if attempt:
                log(f"OAICS 原生 {provider} 延迟就绪（第 {attempt + 1} 次读取）")
            return last
        if attempt + 1 < total:
            log(
                f"OAICS 原生 {provider} 尚未就绪（第 {attempt + 1} 次读取）；"
                f"available={methods or []}"
            )
            time.sleep(max(0.0, float(delay_seconds)) * (attempt + 1))
    return last


def fetch_momo_discounted_checkout_with_retry(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    initial_state: dict[str, Any] | None = None,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.9,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Allow a create-time MoMo campaign a short window to settle.

    The Checkout creation response can publish ``momo`` before its discounted
    total is reflected in the session snapshot.  Poll the same session briefly
    before discarding it, while still treating an explicit method withdrawal as
    authoritative through the stage helpers.
    """
    state = dict(initial_state or {})
    fallback = dict(state)
    total = max(1, int(attempts))
    for attempt in range(total):
        methods = oaics_stage_native_payment_method_types(state, fallback)
        custom_method_id = oaics_stage_custom_payment_method_id(
            state, fallback, "momo",
        )
        amount = custom_checkout_amount_minor(state)
        if amount is None:
            amount = custom_checkout_amount_minor(fallback)
        currency = (
            custom_checkout_currency(state)
            or custom_checkout_currency(fallback)
            or "VND"
        )
        if (
            ("momo" in methods or custom_method_id)
            and is_momo_promo_amount(amount, currency)
        ):
            return state
        if attempt + 1 >= total:
            break
        log(
            "MoMo 创建时优惠尚未稳定（第 {}/{} 次读取）：available={}，amount={} {}".format(
                attempt + 1,
                total,
                methods or [],
                amount if amount is not None else "?",
                currency,
            )
        )
        time.sleep(max(0.0, float(delay_seconds)))
        state = fetch_custom_checkout_session(
            http, token, session_id, processor_entity, device_id,
        )
        if not isinstance(state, dict):
            state = {}
    return state


def fetch_momo_checkout_stable_with_retry(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    initial_state: dict[str, Any] | None = None,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.9,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Wait for two consecutive late-promotion OAICS snapshots to agree."""
    state = dict(initial_state or {})
    fallback = dict(state)
    previous_signature: tuple[Any, ...] | None = None
    total = max(2, int(attempts))
    for attempt in range(total):
        methods = tuple(oaics_stage_native_payment_method_types(state, fallback))
        method_id = oaics_stage_custom_payment_method_id(state, fallback, "momo")
        amount = custom_checkout_amount_minor(state)
        if amount is None:
            amount = custom_checkout_amount_minor(fallback)
        currency = (
            custom_checkout_currency(state)
            or custom_checkout_currency(fallback)
            or "VND"
        )
        signature = (methods, method_id, amount, currency.upper())
        if previous_signature == signature:
            return state
        previous_signature = signature
        if attempt + 1 >= total:
            break
        log(
            "MoMo 支付方式集合稳定检查（第 {}/{} 次读取）：available={}，amount={} {}".format(
                attempt + 1,
                total,
                list(methods),
                amount if amount is not None else "?",
                currency,
            )
        )
        time.sleep(max(0.0, float(delay_seconds)))
        state = fetch_custom_checkout_session(
            http, token, session_id, processor_entity, device_id,
        )
        if not isinstance(state, dict):
            state = {}
    raise RuntimeError(
        "MOMO_CHECKOUT_REBUILD_REQUIRED: late-promotion OAICS 的支付方式或金额"
        "在稳定窗口内持续变化；将丢弃当前 Session 并完整重建"
    )


def _nested_scalar(payload: Any, keys: tuple[str, ...], depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        for value in payload.values():
            found = _nested_scalar(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _nested_scalar(value, keys, depth + 1)
            if found:
                return found
    return ""


def _redact_oaics_payment_error(value: Any) -> str:
    return re.sub(
        r"\b(?:ctoken|seti|pi)_[A-Za-z0-9_\-]+",
        "[PAYMENT_SECRET]",
        str(value or ""),
    )[:300]


def checkout_confirmation_is_blocked(payload: Any, raw_text: str = "") -> bool:
    markers: list[str] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key in ("status", "result", "code"):
                candidate = value.get(key)
                if isinstance(candidate, (str, int, float)):
                    markers.append(str(candidate).strip().lower())
            for nested in value.values():
                if isinstance(nested, (dict, list, tuple)):
                    collect(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested, depth + 1)

    def is_blocked_marker(value: str) -> bool:
        normalized = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
        if normalized in {"not_blocked", "notblocked", "unblocked"}:
            return False
        return (
            normalized == "blocked"
            or normalized.startswith("blocked_")
            or normalized.endswith("_blocked")
        )

    collect(payload)
    if any(is_blocked_marker(marker) for marker in markers):
        return True
    raw_markers = re.findall(
        r'(?i)"(?:status|result|code)"\s*:\s*"([^"]*)"',
        str(raw_text or ""),
    )
    return any(is_blocked_marker(marker) for marker in raw_markers)


def create_oaics_confirmation_token(
    stripe_http,
    publishable_key: str,
    payment_method_id: str,
) -> str:
    """Create the short-lived Stripe token consumed by OAICS checkout/confirm."""
    if not str(publishable_key or "").startswith("pk_"):
        raise RuntimeError("MOMO_OAICS_PUBLISHABLE_KEY_MISSING: OAICS 未返回 Stripe publishable key")
    if not str(payment_method_id or "").startswith("pm_"):
        raise RuntimeError("MOMO_OAICS_PAYMENT_METHOD_INVALID: MoMo 未返回 Stripe pm_* id")
    response = stripe_http.post(
        f"{sc.STRIPE_API}/v1/confirmation_tokens",
        data={
            "payment_method": payment_method_id,
            "key": publishable_key,
            "_stripe_version": sc.STRIPE_VERSION_FULL,
        },
        headers=sc._stripe_headers(),
        timeout=40,
    )
    text = response.text or ""
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(
            f"创建 OAICS MoMo confirmation_token 失败：HTTP "
            f"{getattr(response, 'status_code', '?')} {_redact_oaics_payment_error(text)}"
        )
    try:
        token_id = str((response.json() or {}).get("id") or "")
    except Exception as exc:
        raise RuntimeError("创建 OAICS MoMo confirmation_token 返回非 JSON") from exc
    if not token_id.startswith("ctoken_"):
        raise RuntimeError("创建 OAICS MoMo confirmation_token 未返回 ctoken_* id")
    return token_id


def confirm_oaics_native_payment_method(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    provider: str,
    confirmation_token_id: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    allow_sentinel_fallback: bool = False,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Confirm a native OAICS method using Stripe's ConfirmationToken contract."""
    sentinel = resolve_payment_sentinel_headers(
        sentinel_headers, proxy, "checkout_session_approval", device_id, did,
        use_sen=use_sen, use_so=use_so,
        allow_fallback=allow_sentinel_fallback, log=log,
    )
    common_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
        "User-Agent": sc.CHROME_UA,
        "OAI-Device-Id": device_id,
        **sentinel,
    }
    if sentinel:
        ping_response = http.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                **common_headers,
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=40,
        )
        if getattr(ping_response, "status_code", 0) >= 400:
            raise RuntimeError(
                "MOMO_OAICS_SENTINEL_PING_FAILED: 原生 MoMo confirm 前的 Sentinel ping 失败；"
                f"HTTP {getattr(ping_response, 'status_code', '?')}"
            )
    response = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": session_id,
            "selected_payment_method_type": str(provider or "").lower(),
            "confirm_token": confirmation_token_id,
        },
        headers={
            **common_headers,
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
        },
        timeout=60,
    )
    text = response.text or ""
    payload: dict[str, Any] = {}
    json_error: Exception | None = None
    try:
        value = response.json() or {}
        payload = value if isinstance(value, dict) else {}
    except Exception as exc:
        json_error = exc
    if checkout_confirmation_is_blocked(payload, text):
        raise RuntimeError(
            f"MOMO_OAICS_CONFIRM_BLOCKED: 当前 {session_id} 的原生 MoMo confirm 被 blocked；"
            "需要重建完整 Checkout"
        )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(
            f"OAICS {provider} checkout/confirm 失败：HTTP "
            f"{getattr(response, 'status_code', '?')} {_redact_oaics_payment_error(text)}"
        )
    if json_error is not None:
        raise RuntimeError(f"OAICS {provider} checkout/confirm 返回非 JSON") from json_error
    return payload


def confirm_oaics_momo_intent(
    stripe_http,
    publishable_key: str,
    payment_method_id: str,
    confirm_payload: dict[str, Any],
    session_id: str,
    processor_entity: str,
) -> dict[str, Any]:
    """Advance a returned OAICS SetupIntent when checkout/confirm has no action yet."""
    client_secret = _nested_scalar(confirm_payload, ("client_secret", "clientSecret"))
    if "_secret_" not in client_secret:
        return {}
    intent_id = client_secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("seti_", "pi_")):
        return {}
    endpoint = "setup_intents" if intent_id.startswith("seti_") else "payment_intents"
    return_url = (
        "https://chatgpt.com/checkout/verify"
        f"?stripe_session_id={quote(session_id)}&processor_entity={quote(processor_entity)}&plan_type=plus"
    )
    response = stripe_http.post(
        f"{sc.STRIPE_API}/v1/{endpoint}/{intent_id}/confirm",
        data={
            "client_secret": client_secret,
            "payment_method": payment_method_id,
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "key": publishable_key,
            "_stripe_version": sc.STRIPE_VERSION_FULL,
        },
        headers=sc._stripe_headers(),
        timeout=60,
    )
    text = response.text or ""
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(
            f"确认 OAICS MoMo {endpoint} 失败：HTTP "
            f"{getattr(response, 'status_code', '?')} {_redact_oaics_payment_error(text)}"
        )
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise RuntimeError(f"确认 OAICS MoMo {endpoint} 返回非 JSON") from exc
    return payload if isinstance(payload, dict) else {}


def poll_oaics_momo_intent(
    stripe_http,
    publishable_key: str,
    *payloads: dict[str, Any],
    attempts: int = 6,
    delay_seconds: float = 1.0,
) -> dict[str, Any]:
    """Poll the same OAICS MoMo Intent after an OpenAI approval response."""
    client_secret = ""
    for payload in payloads:
        client_secret = _nested_scalar(payload, ("client_secret", "clientSecret"))
        if client_secret:
            break
    if "_secret_" not in client_secret:
        return {}
    intent_id = client_secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("seti_", "pi_")):
        return {}
    endpoint = "setup_intents" if intent_id.startswith("seti_") else "payment_intents"
    last: dict[str, Any] = {}
    for attempt in range(max(1, int(attempts))):
        response = stripe_http.get(
            f"{sc.STRIPE_API}/v1/{endpoint}/{intent_id}",
            params={
                "client_secret": client_secret,
                "key": publishable_key,
                "_stripe_version": sc.STRIPE_VERSION_FULL,
            },
            headers=sc._stripe_headers(),
            timeout=40,
        )
        if getattr(response, "status_code", 0) != 200:
            return last
        try:
            value = response.json() or {}
        except Exception:
            value = {}
        last = value if isinstance(value, dict) else {}
        if momo_authorization_url(last):
            return last
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(max(0.0, float(delay_seconds)))
    return last


def validate_bound_checkout_context(
    client_context: CheckoutClientContext | None,
    http,
    *,
    expected_provider: str,
    proxy: str,
    device_id: str,
    did: str,
    phase: str,
) -> CheckoutClientContext:
    """Fail closed when a protected stage drifts from its creation session."""
    if client_context is None or http is None:
        raise PaymentFlowError(
            "CHECKOUT_CLIENT_CONTEXT_REQUIRED",
            "protected payment stages require the creation context and HTTP session",
            phase=phase,
        )
    mismatches: list[str] = []
    if client_context.payment_provider != str(expected_provider or "").strip().lower():
        mismatches.append("payment_provider")
    if client_context.device_id != str(device_id or "").strip():
        mismatches.append("device_id")
    if client_context.did != str(did or "").strip():
        mismatches.append("did")
    if client_context.proxy_route != str(proxy or "").strip():
        mismatches.append("proxy_route")
    if client_context.session_owner != f"checkout-http:{id(http)}":
        mismatches.append("session_owner")
    if mismatches:
        raise PaymentFlowError(
            "CLIENT_CONTEXT_MISMATCH",
            "protected payment stage differs from creation context: " + ", ".join(mismatches),
            phase=phase,
        )
    _verify_checkout_context_cookies(http, client_context, phase=phase)
    return client_context


def confirm_custom_checkout_method(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    method_name: str = "GCash",
    allow_sentinel_fallback: bool = False,
    client_context: CheckoutClientContext | None = None,
    proof_policy: ProofPolicy | None = None,
    log=lambda _message: None,
) -> dict[str, Any]:
    is_gopay = (
        str(method_name or "").strip().lower() == "gopay"
        or (proof_policy is not None and proof_policy.payment_provider == "gopay")
    )
    if is_gopay:
        proof_policy = proof_policy or ProofPolicy.strict_gopay()
        client_context = validate_bound_checkout_context(
            client_context,
            http,
            expected_provider="gopay",
            proxy=proxy,
            device_id=device_id,
            did=did,
            phase="confirm",
        )
        use_sen = True
        use_so = True
        allow_sentinel_fallback = False
        device_id = client_context.device_id
        did = client_context.did
        proxy = client_context.proxy_route
    proof_started_at = time.monotonic()
    sentinel = resolve_payment_sentinel_headers(
        sentinel_headers, proxy, "checkout_session_approval", device_id, did,
        use_sen=use_sen, use_so=use_so,
        allow_fallback=allow_sentinel_fallback, log=log,
        client_context=client_context,
        proof_policy=proof_policy,
        payment_endpoint=PaymentEndpoint.CHECKOUT_CONFIRM if proof_policy is not None else None,
    )
    if client_context is not None and proof_policy is not None:
        log(render_payment_diagnostic_event(
            client_context,
            phase="checkout_confirm_proof",
            flow=SentinelFlow.CHECKOUT_SESSION_APPROVAL,
            sen_present=bool(sentinel.get("OpenAI-Sentinel-Token")),
            so_present=bool(sentinel.get("OpenAI-Sentinel-SO-Token")),
            elapsed_ms=(time.monotonic() - proof_started_at) * 1000,
        ))
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": session_id,
            "selected_payment_method_type": custom_payment_method_id,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": client_context.user_agent if client_context is not None else sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
            **sentinel,
        },
        timeout=50,
    )
    text = resp.text or ""
    payload: dict[str, Any] = {}
    json_error: Exception | None = None
    try:
        value = resp.json()
        if isinstance(value, dict):
            payload = value
        elif is_gopay:
            raise PaymentFlowError(
                "GOPAY_CONFIRM_RESPONSE_INVALID",
                "GoPay confirmation response must be a JSON object",
                phase="confirm",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            )
    except Exception as exc:
        if isinstance(exc, PaymentFlowError):
            raise
        json_error = exc
    if checkout_confirmation_is_blocked(payload, text):
        detail = ""
        for key in ("code", "reason", "error", "message", "detail"):
            candidate = payload.get(key)
            if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                detail = str(candidate).strip()
                break
        detail = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", detail)[:240]
        if log:
            log(f"{method_name} confirm 被上游标记 blocked" + (f"：{detail}" if detail else "，响应未提供原因"))
        raise PaymentFlowError(
            "GOPAY_CONFIRM_BLOCKED_REBUILD_REQUIRED" if is_gopay else "CUSTOM_CONFIRM_BLOCKED",
            f"{method_name} 支付方式确认被上游拦截"
            + (f"：{detail}" if detail else ""),
            phase="confirm",
            retryable=True,
            rebuild_checkout=is_gopay,
            http_status=resp.status_code,
        )
    if resp.status_code != 200:
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_CONFIRM_HTTP_ERROR",
                f"GoPay 支付方式确认失败：HTTP {resp.status_code}",
                phase="confirm",
                http_status=resp.status_code,
                retryable=resp.status_code == 429 or resp.status_code >= 500,
            )
        raise RuntimeError(f"确认 {method_name} 支付方式失败：HTTP {resp.status_code} {text[:300]}")
    if json_error is not None:
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_CONFIRM_RESPONSE_INVALID",
                "GoPay confirmation returned a non-JSON response",
                phase="confirm",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            ) from json_error
        raise RuntimeError(f"确认 {method_name} 支付方式返回非 JSON：{text[:300]}") from json_error
    status = str(payload.get("status") or payload.get("result") or "unknown").lower()
    if status != "success":
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_CONFIRM_REJECTED",
                f"GoPay confirmation returned status={status[:40]}",
                phase="confirm",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            )
        raise RuntimeError(f"确认 {method_name} 支付方式失败：status={status}；{text[:300]}")
    return payload


def confirm_custom_checkout_method_with_retry(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    use_sen: bool = True,
    use_so: bool = True,
    method_name: str = "GCash",
    allow_sentinel_fallback: bool = False,
    max_retries: int = 2,
    delay_seconds: float = 1.2,
    rebuild_on_blocked: bool = False,
    client_context: CheckoutClientContext | None = None,
    proof_policy: ProofPolicy | None = None,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Retry only upstream ``blocked`` confirmations within one Checkout.

    A blocked confirmation is transient in the same way as the reference
    GoPay flow's final-review rejection.  Method-unavailable and HTTP errors
    are intentionally propagated immediately so the outer job can rebuild
    the Checkout and rotate its proxy pair.
    """
    total = max(1, min(6, int(max_retries) + 1))
    last_error: RuntimeError | None = None
    for attempt in range(total):
        if attempt:
            log(f"{method_name} confirm 第 {attempt + 1}/{total} 次重试（刷新 SEN/SO）")
            time.sleep(max(0.0, float(delay_seconds)) * min(attempt, 2))
        try:
            return confirm_custom_checkout_method(
                http, token, session_id, processor_entity,
                custom_payment_method_id, proxy, device_id, did,
                use_sen=True if attempt else use_sen,
                use_so=True if attempt else use_so,
                method_name=method_name,
                allow_sentinel_fallback=allow_sentinel_fallback,
                client_context=client_context,
                proof_policy=proof_policy,
                log=log,
            )
        except RuntimeError as exc:
            if isinstance(exc, PaymentFlowError):
                blocked_error = exc.code in {
                    "CUSTOM_CONFIRM_BLOCKED",
                    "GOPAY_CONFIRM_BLOCKED_REBUILD_REQUIRED",
                }
            else:
                blocked_error = "CUSTOM_CONFIRM_BLOCKED" in str(exc)
            if not blocked_error:
                raise
            if rebuild_on_blocked or (
                isinstance(exc, PaymentFlowError) and exc.rebuild_checkout
            ):
                raise
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError(f"确认 {method_name} 支付方式失败：未执行确认")


def start_custom_checkout_method(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    device_id: str,
    *,
    method_name: str = "GCash",
    proxy: str = "",
    did: str = "",
    client_context: CheckoutClientContext | None = None,
) -> dict[str, Any]:
    is_gopay = str(method_name or "").strip().lower() == "gopay"
    if is_gopay:
        client_context = validate_bound_checkout_context(
            client_context,
            http,
            expected_provider="gopay",
            proxy=proxy,
            device_id=device_id,
            did=did,
            phase="start",
        )
        device_id = client_context.device_id
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start",
        json={
            "checkout_session_id": session_id,
            "custom_payment_method_type_id": custom_payment_method_id,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": client_context.user_agent if client_context is not None else sc.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/custom_payment_method/start",
            "x-openai-target-route": "/backend-api/payments/checkout/custom_payment_method/start",
        },
        timeout=60,
    )
    text = resp.text or ""
    if resp.status_code != 200:
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_START_HTTP_ERROR",
                f"GoPay payment start failed: HTTP {resp.status_code}",
                phase="start",
                http_status=resp.status_code,
                retryable=resp.status_code == 429 or resp.status_code >= 500,
            )
        raise RuntimeError(f"启动 {method_name} 支付失败：HTTP {resp.status_code} {text[:300]}")
    try:
        payload = resp.json()
    except Exception as exc:
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_START_RESPONSE_INVALID",
                "GoPay payment start returned a non-JSON response",
                phase="start",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            ) from exc
        raise RuntimeError(f"启动 {method_name} 支付返回非 JSON：{text[:300]}")
    if not isinstance(payload, dict):
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_START_RESPONSE_INVALID",
                "GoPay payment start response must be a JSON object",
                phase="start",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            )
        raise RuntimeError(f"启动 {method_name} 支付返回无效 JSON：{text[:300]}")
    action = payload.get("next_action") or {}
    if not isinstance(action, dict):
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_START_RESPONSE_INVALID",
                "GoPay payment start next_action must be a JSON object",
                phase="start",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            )
        action = {}
    if str(payload.get("status") or "").lower() != "requires_action" or not action.get("url"):
        if is_gopay:
            raise PaymentFlowError(
                "GOPAY_START_REDIRECT_MISSING",
                "GoPay payment start did not return a redirect action",
                phase="start",
                http_status=resp.status_code,
                retryable=True,
                rebuild_checkout=True,
            )
        raise RuntimeError(f"{method_name} 未返回跳转链接：{text[:300]}")
    return payload


_GOPAY_MIDTRANS_PATH_RE = re.compile(
    r"^/snap/v[34]/redirection/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?$",
    re.IGNORECASE,
)


def is_valid_gopay_midtrans_url(value: str) -> bool:
    """Return whether GoPay can consume this Midtrans Snap redirect URL."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower().rstrip(".") == "app.midtrans.com"
        and bool(_GOPAY_MIDTRANS_PATH_RE.fullmatch(parsed.path))
    )


def gopay_midtrans_url(*payloads: Any) -> str:
    """Find the strict Midtrans Snap handoff returned by GoPay confirm/start."""
    found: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                walk(nested, depth + 1)
        elif isinstance(value, str):
            candidate = value.strip()
            if is_valid_gopay_midtrans_url(candidate) and candidate not in found:
                found.append(candidate)
                return
            decoded = unquote(candidate)
            if decoded != candidate:
                walk(decoded, depth + 1)

    for payload in payloads:
        walk(payload)
    return found[0] if found else ""


def require_gopay_midtrans_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a generic GoPay result to the final Midtrans handoff URL."""
    redirect_url = gopay_midtrans_url(payload)
    if not redirect_url:
        raise PaymentFlowError(
            "GOPAY_MIDTRANS_LINK_MISSING",
            "GoPay 未返回有效的 Midtrans Snap v3/v4 跳转链接",
            phase="start",
            retryable=True,
            rebuild_checkout=True,
        )
    normalized = dict(payload)
    normalized.update({
        "provider_redirect_url": redirect_url,
        "gopay_midtrans_url": redirect_url,
        "short_link": redirect_url,
        "checkout_url": redirect_url,
    })
    return normalized


_MOMO_AUTHORIZE_PATH_RE = re.compile(
    r"^/authorize/acct_[A-Za-z0-9]+/(?:pa|sa)_nonce_[A-Za-z0-9]+/?$",
    re.IGNORECASE,
)


def is_valid_momo_authorization_url(value: str) -> bool:
    """Return whether a URL is a Stripe-hosted MoMo authorization handoff."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower().rstrip(".") == "pm-redirects.stripe.com"
        and bool(_MOMO_AUTHORIZE_PATH_RE.fullmatch(parsed.path))
    )


def momo_authorization_url(*payloads: Any) -> str:
    """Find the final Stripe MoMo handoff in nested confirm/start payloads."""
    found: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                walk(nested, depth + 1)
        elif isinstance(value, str):
            candidate = value.strip()
            if is_valid_momo_authorization_url(candidate) and candidate not in found:
                found.append(candidate)
                return
            decoded = unquote(candidate)
            if decoded != candidate:
                walk(decoded, depth + 1)

    for payload in payloads:
        walk(payload)
    return found[0] if found else ""


def is_valid_gcash_authorization_url(value: str) -> bool:
    """Return whether a URL is the final public GCash authorization page."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower().rstrip(".") == "m.gcash.com"
        and bool(parsed.path)
    )


def gcash_authorization_url(*payloads: Any) -> str:
    """Find the final m.gcash.com URL returned by confirm/start payloads."""
    found: list[str] = []
    url_pattern = re.compile(r"https://m\.gcash\.com/[^\s\"'<>]+", re.IGNORECASE)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            decoded = unquote(value)
            candidates = [value, decoded, *url_pattern.findall(value), *url_pattern.findall(decoded)]
            for candidate in candidates:
                candidate = candidate.strip().rstrip(".,)")
                if is_valid_gcash_authorization_url(candidate) and candidate not in found:
                    found.append(candidate)

    for payload in payloads:
        walk(payload)
    return found[0] if found else ""


def gcash_authorization_params(*payloads: Any) -> dict[str, str]:
    """Extract the public GCash authorization identifiers from nested data."""
    authorization = gcash_authorization_url(*payloads)
    if not authorization:
        return {"net_auth_id": "", "client_id": ""}
    try:
        query = parse_qs(urlsplit(authorization).query, keep_blank_values=True)
    except ValueError:
        return {"net_auth_id": "", "client_id": ""}
    return {
        "net_auth_id": str((query.get("netAuthId") or [""])[-1]).strip(),
        "client_id": str((query.get("clientId") or [""])[-1]).strip(),
    }


def resolve_gcash_authorization_url(http: Any, redirect_url: str, log=lambda _message: None) -> str:
    """Resolve the Adyen handoff to the signed public GCash authorization URL."""
    candidate = str(redirect_url or "").strip()
    if not is_valid_gcash_adyen_redirect_url(candidate):
        return ""
    try:
        response = http.get(
            candidate,
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": sc.CHROME_UA},
            allow_redirects=False,
            timeout=30,
        )
        for _ in range(3):
            location = str(response.headers.get("location") or "").strip()
            if not location:
                break
            if is_valid_gcash_authorization_url(location):
                return location
            if not is_valid_gcash_adyen_redirect_url(location):
                break
            response = http.get(
                location,
                headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": sc.CHROME_UA},
                allow_redirects=False,
                timeout=30,
            )
    except Exception as exc:
        log(f"GCash 授权链接解析提示：{type(exc).__name__}")
    return ""


def fetch_gcash_public_qr(authorization_url: str, proxy: str = "", log=lambda _message: None) -> dict[str, Any]:
    """Call GCash's public stateless consult API to obtain its login-free QR."""
    if not is_valid_gcash_authorization_url(authorization_url):
        return {}
    query = urlsplit(authorization_url).query
    request_body = {
        "channel": "generic",
        "urlParameters": query,
        "originalUrl": authorization_url,
        "expireSeconds": 300,
        "bizType": "ACQUIRING",
        "extParams": {"sessionType": "APLUS", "sessionId": ""},
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": sc.CHROME_UA,
        "Origin": "https://m.gcash.com",
        "Referer": authorization_url,
    }
    gateway = "https://mgs-gw.paas.mynt.xyz/mgw.htm"
    form = {
        "operationType": "ap.mobilewallet.gka.authorisation.stateless.consult",
        "requestData": json.dumps([request_body], ensure_ascii=False, separators=(",", ":")),
        "version": "2.0",
        "workspaceId": "PROD",
        "appId": "D54528A131559",
        "tenantId": "MYNTPH",
    }
    try:
        response = requests.post(
            gateway,
            data=form,
            headers=headers,
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=30,
            impersonate="chrome",
        )
        payload = response.json() if response.text else {}
        if not isinstance(payload, dict) or int(payload.get("resultStatus") or 0) != 1000:
            log(f"GCash 免登录二维码接口未成功：HTTP {response.status_code}")
            return {}
        result = payload.get("result")
        return dict(result) if isinstance(result, dict) else {}
    except Exception as exc:
        log(f"GCash 免登录二维码获取失败：{type(exc).__name__}")
        return {}


def _gcash_qr_candidate(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # A public QR payload is data consumed by the GCash scanner. Never treat
    # the login page or an Adyen handoff as that payload.
    if is_valid_gcash_authorization_url(text) or is_valid_gcash_adyen_redirect_url(text):
        return ""
    if text.startswith("data:image/") or text.startswith("gcash://"):
        return text
    if len(text) < 16:
        return ""
    return text


def gcash_qr_data(*payloads: Any) -> str:
    """Find the real GCash scanner payload returned by the provider."""
    keys = {
        "qrcode", "qrdata", "qrpayload", "qrcontent", "qrstring",
        "paymentqrcode", "paymentqr", "gcashqrcode", "gcashqrdata",
    }
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in keys:
                    candidate = _gcash_qr_candidate(nested)
                    if candidate and candidate not in found:
                        found.append(candidate)
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for payload in payloads:
        walk(payload)
    return found[0] if found else ""


def gcash_qr_expires_at(*payloads: Any, now: float | None = None) -> int:
    """Normalize provider QR expiry to a Unix timestamp (GCash uses 300s)."""
    current = time.time() if now is None else float(now)
    absolute_keys = {"qrcodeexpiresat", "qrexpiresat", "expiresat", "expireat", "expiration"}
    ttl_keys = {"qrcodeexpireseconds", "qrexpireseconds", "expireseconds", "ttl", "validityseconds"}
    absolute: float | None = None
    ttl: float | None = None

    def walk(value: Any, key: str = "") -> None:
        nonlocal absolute, ttl
        if isinstance(value, dict):
            for name, nested in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
                walk(nested, normalized)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if key in absolute_keys and absolute is None:
                number = float(value)
                absolute = number / 1000 if number > 10_000_000_000 else number
            elif key in ttl_keys and ttl is None:
                ttl = float(value)
        elif isinstance(value, str) and value.strip():
            try:
                number = float(value.strip())
            except ValueError:
                return
            if key in absolute_keys and absolute is None:
                absolute = number / 1000 if number > 10_000_000_000 else number
            elif key in ttl_keys and ttl is None:
                ttl = number

    for payload in payloads:
        walk(payload)
    if absolute and absolute > current:
        return int(absolute)
    if ttl and ttl > 0:
        return int(current + min(ttl, 300))
    return int(current + 300) if gcash_qr_data(*payloads) else 0


def is_valid_gcash_adyen_redirect_url(value: str) -> bool:
    """Return whether a URL is the Adyen redirect emitted by GCash start."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    host = parsed.netloc.lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and host in {"checkoutshopper-live.adyen.com", "checkoutshopper-test.adyen.com"}
        and parsed.path.rstrip("/") == "/checkoutshopper/checkoutPaymentRedirect"
    )


def gcash_payment_url(confirmed: dict[str, Any], started: dict[str, Any]) -> str:
    """Prefer a final GCash URL and fall back to its validated Adyen handoff."""
    authorization_url = gcash_authorization_url(confirmed, started)
    if authorization_url:
        return authorization_url
    action = started.get("next_action") or {}
    candidate = str(action.get("url") or "").strip() if isinstance(action, dict) else ""
    return candidate if is_valid_gcash_adyen_redirect_url(candidate) else ""


def _callback_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def parse_gcash_callback(value: Any, expected_session_id: str = "") -> dict[str, str]:
    """Extract the checkout id and Adyen redirectResult from a browser return.

    Adyen has used query strings, URL fragments, form posts and JSON wrappers
    for the return payload.  Keep parsing deliberately permissive, while
    requiring the caller to compare the returned checkout id with the order
    that initiated the payment.
    """
    values: dict[str, str] = {}
    session_id = ""
    redirect_result = ""
    candidates: list[Any] = [value]
    def visit(item: Any, depth: int = 0) -> None:
        nonlocal session_id, redirect_result
        if depth > 8:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = _callback_key(key)
                text = str(nested or "").strip() if not isinstance(nested, (dict, list)) else ""
                if normalized in {"checkoutsessionid", "sessionid", "checkoutid"} and text:
                    session_id = session_id or unquote(text)
                if normalized in {"redirectresult", "redirectdata", "redirectresultvalue"} and text:
                    redirect_result = redirect_result or unquote(text)
                visit(nested, depth + 1)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested, depth + 1)
            return
        if not isinstance(item, str):
            return
        text = item.strip()
        if not text:
            return
        if text.startswith("{") or text.startswith("["):
            try:
                visit(json.loads(text), depth + 1)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        try:
            parsed = urlsplit(text)
            for component in (parsed.query, parsed.fragment):
                if component:
                    for key, item_values in parse_qs(component, keep_blank_values=True).items():
                        visit({key: item_values[-1] if item_values else ""}, depth + 1)
            path_match = re.search(r"(?:checkout/verify|checkoutPaymentReturn)[^/]*[/]([A-Za-z0-9_-]{8,})", text, re.I)
            if path_match and not session_id:
                session_id = unquote(path_match.group(1))
        except ValueError:
            pass
        decoded = unquote(text)
        if decoded != text:
            visit(decoded, depth + 1)

    for candidate in candidates:
        visit(candidate)
    if expected_session_id and session_id and session_id != str(expected_session_id):
        raise ValueError("GCASH_CALLBACK_SESSION_MISMATCH: 回跳 Checkout 会话与当前订单不一致")
    if not session_id and expected_session_id:
        session_id = str(expected_session_id)
    values.update({
        "checkout_session_id": session_id,
        "redirectResult": redirect_result,
        "has_redirect_result": "1" if bool(redirect_result) else "0",
    })
    return values


def _gcash_callback_status(payload: Any) -> str:
    """Normalize provider/Checkout status without trusting a single field."""
    success = {"success", "succeeded", "paid", "complete", "completed", "authorized"}
    failed = {"failed", "failure", "declined", "refused", "cancelled", "canceled", "expired"}
    pending = {"pending", "processing", "requiresaction", "requires_action", "waiting", "open"}
    found: list[str] = []

    def walk(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            for name, nested in item.items():
                walk(nested, str(name).lower())
        elif isinstance(item, list):
            for nested in item:
                walk(nested, key)
        elif isinstance(item, str):
            normalized = re.sub(r"[^a-z]", "", item.lower())
            if normalized in success | failed | pending and any(marker in key for marker in ("status", "state", "result", "payment")):
                found.append(normalized)

    walk(payload)
    if any(value in success for value in found):
        return "success"
    if any(value in failed for value in found):
        return "failed"
    if found:
        return "paying"
    return "unknown"


def continue_custom_checkout_method(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    redirect_result: str,
    device_id: str,
    did: str = "",
    *,
    retries: int = 3,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Submit Adyen's redirectResult on the original ChatGPT Checkout session."""
    session_id = str(session_id or "").strip()
    redirect_result = str(redirect_result or "").strip()
    if not session_id or not redirect_result:
        raise ValueError("GCASH_CALLBACK_INCOMPLETE: 缺少 checkout_session_id 或 redirectResult")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", session_id):
        raise ValueError("GCASH_CALLBACK_SESSION_INVALID: Checkout 会话格式无效")
    if did:
        try:
            http.cookies.set("oai-did", did, domain="chatgpt.com")
        except Exception:
            pass
    attempts = max(1, min(4, int(retries) + 1))
    last_error = ""
    for attempt in range(attempts):
        resp = http.post(
            "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/continue",
            json={"checkout_session_id": session_id, "action_result": {"redirectResult": redirect_result}},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
                "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
                "User-Agent": sc.CHROME_UA,
                "OAI-Device-Id": device_id,
                "x-openai-target-path": "/backend-api/payments/checkout/custom_payment_method/continue",
                "x-openai-target-route": "/backend-api/payments/checkout/custom_payment_method/continue",
            },
            timeout=50,
        )
        body_text = resp.text or ""
        retryable = resp.status_code in {408, 425, 429} or resp.status_code >= 500
        if resp.status_code == 200:
            try:
                return resp.json() or {}
            except Exception as exc:
                raise RuntimeError(f"GCASH_CALLBACK_RESPONSE_INVALID: continuation 返回非 JSON：{body_text[:300]}") from exc
        last_error = f"HTTP {resp.status_code} {body_text[:300]}"
        if not retryable or attempt + 1 >= attempts:
            break
        log(f"GCash continuation 第 {attempt + 1} 次失败，正在重试")
        time.sleep(min(2.5, 0.5 * (attempt + 1)))
    raise RuntimeError(f"GCASH_CALLBACK_CONTINUE_FAILED: {last_error}")


def complete_gcash_callback(context: dict[str, Any], callback: Any) -> dict[str, Any]:
    parsed = parse_gcash_callback(callback, str(context.get("checkout_session_id") or ""))
    continuation = continue_custom_checkout_method(
        context["http"], context["token"], parsed["checkout_session_id"],
        str(context.get("processor_entity") or "openai_ie"), parsed["redirectResult"],
        str(context.get("device_id") or ""), str(context.get("did") or ""),
        log=context.get("log") or (lambda _message: None),
    )
    checkout = fetch_custom_checkout_session(
        context["http"], context["token"], parsed["checkout_session_id"],
        str(context.get("processor_entity") or "openai_ie"), str(context.get("device_id") or ""),
    )
    checkout_status = _gcash_callback_status(checkout)
    continuation_status = _gcash_callback_status(continuation)
    status = checkout_status if checkout_status in {"success", "failed"} else continuation_status
    if status == "unknown":
        status = "paying"
    return {
        "status": status,
        "checkout_session_id": parsed["checkout_session_id"],
        "continuation": {key: value for key, value in continuation.items() if key not in {"redirectResult", "action_result"}},
        "checkout": checkout,
    }



class CheckoutApprovalError(RuntimeError):
    """Base error for a syntactically valid, non-approved approval result."""


class CheckoutApprovalBlockedError(CheckoutApprovalError):
    """The approval response explicitly invalidated the current Checkout."""


class CheckoutApprovalRejectedError(CheckoutApprovalError):
    """The approval failed without proving that a fresh Checkout is required."""


def approval_result_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "missing"
    result = str(payload.get("result") or "").strip().lower()
    if not result:
        return "missing"
    if result in {"approved", "blocked", "invalid_promotion"}:
        return result
    if result in {
        "failed", "denied", "declined", "rejected", "requires_action",
        "cancelled", "canceled", "expired", "error",
    }:
        return "failed"
    return "unknown"


def approve_checkout(
    token: str,
    session_id: str,
    processor: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    http=None,
    log=lambda _message: None,
    allow_sentinel_fallback: bool = False,
    require_explicit_result: bool = False,
    client_context: CheckoutClientContext | None = None,
    proof_policy: ProofPolicy | None = None,
) -> dict:
    strict_gopay = (
        (proof_policy is not None and proof_policy.payment_provider == "gopay")
        or (
            isinstance(client_context, CheckoutClientContext)
            and client_context.payment_provider == "gopay"
        )
    )
    if strict_gopay:
        proof_policy = proof_policy or ProofPolicy.strict_gopay()
    if strict_gopay and http is None:
        raise PaymentFlowError(
            "CHECKOUT_CLIENT_CONTEXT_REQUIRED",
            "GoPay approval must reuse the HTTP session that created the Checkout",
            phase="approval",
        )
    owns_http = http is None
    http = http or sc.build_http(proxy or None)
    try:
        if strict_gopay:
            allow_sentinel_fallback = False
            client_context = validate_bound_checkout_context(
                client_context,
                http,
                expected_provider="gopay",
                proxy=proxy,
                device_id=device_id,
                did=did,
                phase="approval",
            )
            device_id = client_context.device_id
            did = client_context.did
            proxy = client_context.proxy_route
        proof_started_at = time.monotonic()
        headers = resolve_payment_sentinel_headers(
            sentinel_headers, proxy, "checkout_session_approval", device_id, did,
            allow_fallback=allow_sentinel_fallback, log=log,
            client_context=client_context,
            proof_policy=proof_policy,
            payment_endpoint=PaymentEndpoint.CHECKOUT_APPROVE if strict_gopay else None,
        )
        if strict_gopay and client_context is not None:
            log(render_payment_diagnostic_event(
                client_context,
                phase="checkout_approval_proof",
                flow=SentinelFlow.CHECKOUT_SESSION_APPROVAL,
                sen_present=bool(headers.get("OpenAI-Sentinel-Token")),
                so_present=bool(headers.get("OpenAI-Sentinel-SO-Token")),
                elapsed_ms=(time.monotonic() - proof_started_at) * 1000,
            ))
        body = {"checkout_session_id": session_id, "processor_entity": processor}
        resp = http.post(
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://chatgpt.com",
                "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
                "OAI-Device-Id": device_id,
                "User-Agent": client_context.user_agent if client_context is not None else sc.CHROME_UA,
                "OAI-Language": "zh-CN",
                "x-openai-target-path": "/backend-api/payments/checkout/approve",
                "x-openai-target-route": "/backend-api/payments/checkout/approve",
                **headers,
            },
            timeout=40,
        )
        text = resp.text or ""
        log(f"[stripe] manual_approval approve+sentinel: HTTP {resp.status_code}")
        if resp.status_code != 200:
            if strict_gopay:
                raise PaymentFlowError(
                    "GOPAY_APPROVAL_HTTP_ERROR",
                    f"Checkout approve HTTP {resp.status_code}",
                    phase="approval",
                    http_status=resp.status_code,
                    retryable=resp.status_code == 429 or resp.status_code >= 500,
                )
            raise RuntimeError(f"Checkout approve HTTP {resp.status_code}: {text[:300]}")
        try:
            payload = resp.json()
        except Exception as exc:
            if strict_gopay:
                raise PaymentFlowError(
                    "GOPAY_APPROVAL_RESPONSE_INVALID",
                    "GoPay approval returned a non-JSON response",
                    phase="approval",
                    http_status=resp.status_code,
                    retryable=True,
                    rebuild_checkout=True,
                ) from exc
            if require_explicit_result:
                raise CheckoutApprovalRejectedError(
                    f"MANUAL_APPROVAL_RESPONSE_INVALID: {text[:300]}"
                ) from exc
            payload = {}
        if not isinstance(payload, dict):
            if strict_gopay:
                raise PaymentFlowError(
                    "GOPAY_APPROVAL_RESPONSE_INVALID",
                    "GoPay approval response must be a JSON object",
                    phase="approval",
                    http_status=resp.status_code,
                    retryable=True,
                    rebuild_checkout=True,
                )
            if require_explicit_result:
                raise CheckoutApprovalRejectedError(
                    "MANUAL_APPROVAL_RESPONSE_INVALID: approval response must be an object"
                )
            payload = {}
        if not require_explicit_result:
            result = str(payload.get("result") or "").strip().lower()
            if result and result != "approved":
                raise RuntimeError(f"manual_approval approve blocked: result={result}")
            return payload
        status = approval_result_status(payload)
        log(f"[stripe] manual_approval result={status}")
        if status == "approved":
            return payload
        raw_result = str(payload.get("result") or "").strip().lower() or "<missing>"
        if status == "blocked":
            raise CheckoutApprovalBlockedError(
                "MANUAL_APPROVAL_BLOCKED: result=blocked"
            )
        if strict_gopay:
            code_by_status = {
                "invalid_promotion": "GOPAY_APPROVAL_INVALID_PROMOTION",
                "failed": "GOPAY_APPROVAL_FAILED",
                "missing": "GOPAY_APPROVAL_MISSING_RESULT",
                "unknown": "GOPAY_APPROVAL_UNKNOWN_RESULT",
            }
            retryable = status in {"missing", "unknown"}
            raise PaymentFlowError(
                code_by_status.get(status, "GOPAY_APPROVAL_REJECTED"),
                f"GoPay approval returned status={status}",
                phase="approval",
                http_status=resp.status_code,
                retryable=retryable,
                rebuild_checkout=retryable,
            )
        raise CheckoutApprovalRejectedError(
            f"MANUAL_APPROVAL_{status.upper()}: result={raw_result}"
        )
    finally:
        if owns_http:
            close_http_sessions([http])


def approve_gopay_checkout_or_rebuild(
    *args,
    log=lambda _message: None,
    **kwargs,
) -> dict:
    """Submit one GoPay approval and invalidate this Checkout when blocked."""
    session_id = str(args[1] if len(args) > 1 else kwargs.get("session_id") or "")
    if kwargs.get("http") is None or not isinstance(
        kwargs.get("client_context"), CheckoutClientContext,
    ):
        raise PaymentFlowError(
            "CHECKOUT_CLIENT_CONTEXT_REQUIRED",
            "GoPay approval requires the Checkout creation context and HTTP session",
            phase="approval",
        )
    kwargs["allow_sentinel_fallback"] = False
    kwargs["require_explicit_result"] = True
    kwargs["proof_policy"] = ProofPolicy.strict_gopay()
    try:
        return approve_checkout(*args, log=log, **kwargs)
    except CheckoutApprovalBlockedError as exc:
        log(
            f"GoPay approval 返回 blocked；当前 {session_id or 'CS Live'} 已失效，"
            "停止复用并重建完整支付提链"
        )
        raise PaymentFlowError(
            "GOPAY_APPROVAL_BLOCKED_REBUILD_REQUIRED",
            "当前 GoPay CS Live approval 被 blocked；必须重新创建 Checkout、"
            "应用优惠并生成新的 cs_live_*",
            phase="approval",
            retryable=True,
            rebuild_checkout=True,
        ) from exc


def _proxy_transport_error_kind(message: str) -> str:
    """Classify transport failures that should rotate the proxy route."""
    lowered = str(message or "").lower()
    if any(marker in lowered for marker in (
        "sslerror",
        "curl: (35)",
        "curl: (56)",
        "recv failure",
        "connection reset by peer",
        "connection reset",
        "connection aborted",
        "connection closed abruptly",
        "connection closed unexpectedly",
        "ssl connect error",
        "ssl_error_syscall",
        "wrong_version_number",
        "wrong version number",
        "boringssl ssl_read",
        "tls handshake",
        "tls eof",
        "unexpected eof",
        "eof occurred in violation of protocol",
    )):
        return "SSL/连接重置"
    if any(marker in lowered for marker in (
        "curl: (28)",
        "operation timed out",
        "timed out after",
        "failed to perform, curl: (28)",
    )):
        return "代理超时"
    return ""


def _is_proxy_ssl_error(message: str) -> bool:
    """Identify SSL/TLS failures isolated to one proxy route."""
    return _proxy_transport_error_kind(message) == "SSL/连接重置"


def _is_proxy_timeout_error(message: str) -> bool:
    """Identify curl timeout failures isolated to one proxy route."""
    return _proxy_transport_error_kind(message) == "代理超时"


class JobStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.file_lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.worker_limit = max(1, int(os.getenv("PAY153_WORKERS", "20")))
        self.global_rpm = max(1, int(os.getenv("PAY153_GLOBAL_RPM", "20")))
        self.pool = ThreadPoolExecutor(max_workers=self.worker_limit)
        # The Go batch scheduler already applies the user-selected account
        # concurrency. Keep enough internal slots so this pool does not
        # silently reduce that configured value to a fixed default.
        self.internal_worker_limit = max(1, int(os.getenv("PAY153_INTERNAL_WORKERS", "100")))
        self.internal_pool = ThreadPoolExecutor(max_workers=self.internal_worker_limit)
        self.gcash_orders: dict[str, dict[str, Any]] = {}
        self.pending: deque[tuple[str, dict]] = deque()
        self.start_times: deque[float] = deque()
        self.active_workers = 0
        threading.Thread(target=self._dispatch_loop, name="pay153-dispatcher", daemon=True).start()

    @staticmethod
    def _is_major_log(message: str) -> bool:
        text = str(message or "")
        lowered = text.lower()
        return any(marker in text for marker in (
            "提链尝试", "代理池", "代理校验", "自动设置地区", "计划=",
            "优惠已", "优惠更新", "优惠同步", "金额校验", "今日应付",
            "Checkout 创建", "支付方式已创建", "二维码生成", "链接生成",
            "提交 Checkout approval", "错误：", "本次未成功", "轮未命中",
        )) or any(marker in lowered for marker in (
            "init ok", "payment_method:", "manual_approval approve", "checkout/update",
        ))

    def _append_backend_log(self, job_id: str, kind: str, message: str):
        safe_message = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(message))
        day = time.strftime("%Y-%m-%d")
        path = BACKEND_LOG_DIR / day / f"{job_id}.log"
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{kind}] {safe_message}\n"
        try:
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:
            pass

    def _record_success(self, job_id: str, result: dict):
        """Persist successful link results so batch runs survive restarts."""
        try:
            record = {
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "job_id": job_id,
                "combination": "{}-{}".format(
                    str(result.get("entry_country") or "?").upper(),
                    str(result.get("payment_proxy_country") or result.get("checkout_country") or "?").upper(),
                ),
                "attempt": result.get("attempt"),
                "max_attempts": result.get("max_attempts"),
                "account_email": result.get("account_email") or "",
                "link_type": result.get("link_type") or "",
                "checkout_amount": result.get("checkout_amount"),
                "currency": result.get("checkout_currency") or result.get("currency") or "",
                "url": result.get("provider_redirect_url") or result.get("paypal_link") or result.get("url") or result.get("link") or result.get("checkout_url") or "",
            }
            path = ROOT / "data" / "success_links.jsonl"
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                path.chmod(0o600)
        except Exception:
            pass

    def _refresh_queue_locked(self):
        for position, (job_id, _options) in enumerate(self.pending, 1):
            job = self.jobs.get(job_id)
            if not job:
                continue
            job["queue_position"] = position
            job["text"] = f"正在排队，前方 {position - 1} 个任务" if position > 1 else "正在排队，等待执行"
            job["updated_at"] = time.time()

    def _worker_done(self, _future):
        with self.condition:
            self.active_workers = max(0, self.active_workers - 1)
            self.condition.notify_all()

    def _internal_worker_done(self, _future):
        # Private jobs use a separate executor and do not consume public
        # queue/RPM capacity.
        with self.condition:
            self.condition.notify_all()

    def _dispatch_loop(self):
        while True:
            with self.condition:
                now = time.time()
                while self.start_times and now - self.start_times[0] >= 60:
                    self.start_times.popleft()

                if not self.pending or self.active_workers >= self.worker_limit:
                    self.condition.wait(timeout=1)
                    continue

                next_job_id, next_options = self.pending[0]
                next_internal = bool(next_options.get("_internal_request"))
                if not next_internal and len(self.start_times) >= self.global_rpm:
                    wait_seconds = max(0.1, 60 - (now - self.start_times[0]))
                    self.condition.wait(timeout=min(wait_seconds, 2))
                    continue

                job_id, options = self.pending.popleft()
                job = self.jobs.get(job_id)
                if not job or job.get("cancel"):
                    if job:
                        job.update(status="cancelled", percent=100, text="任务已停止", queue_position=0)
                    self._refresh_queue_locked()
                    continue

                self.active_workers += 1
                if not bool(options.get("_internal_request")):
                    self.start_times.append(now)
                job.update(text="排队完成，即将开始", queue_position=0, dispatched=True, updated_at=now)
                self._refresh_queue_locked()
                future = self.pool.submit(self._run, job_id, options)
                future.add_done_callback(self._worker_done)

    def create(self, options: dict, *, internal: bool = False) -> str:
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self.lock:
            expired = [
                key for key, value in self.jobs.items()
                if now - float(value.get("updated_at") or now) > 7200
            ]
            for key in expired:
                self.jobs.pop(key, None)
            if len(self.jobs) >= 500:
                oldest = sorted(self.jobs, key=lambda key: self.jobs[key].get("updated_at", 0))
                for key in oldest[: len(self.jobs) - 499]:
                    self.jobs.pop(key, None)
            self.jobs[job_id] = {
                "id": job_id, "status": "queued", "percent": 2, "text": "任务已创建",
                "logs": [], "result": None, "error": "", "last_retry_error": "", "cancel": False,
                "created_at": now, "updated_at": now, "queue_position": 0, "dispatched": False,
            }
            options = dict(options)
            options["_internal_request"] = bool(internal)
            if internal:
                self.jobs[job_id].update(
                    internal=True,
                    dispatched=True,
                    queue_position=0,
                    text="内部任务已启动",
                )
                future = self.internal_pool.submit(self._run, job_id, options)
                future.add_done_callback(self._internal_worker_done)
            else:
                self.pending.append((job_id, options))
            self._refresh_queue_locked()
            self.condition.notify_all()
        self._append_backend_log(
            job_id,
            "SYSTEM",
            "内部任务已直接分发" if internal else "公开任务已入队",
        )
        return job_id

    def queue_position(self, job_id: str) -> int:
        with self.lock:
            return int((self.jobs.get(job_id) or {}).get("queue_position") or 0)

    def update(self, job_id: str, **fields):
        backend_line = ""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            # A running worker can still be inside a synchronous HTTP request
            # for a few seconds after the user presses stop.  Keep the public
            # state terminal immediately and do not let that worker overwrite
            # `cancelled` with another running/error progress update.
            if (
                job.get("cancel")
                and job.get("status") == "cancelled"
                and fields.get("status") != "cancelled"
            ):
                return
            job.update(fields)
            job["updated_at"] = time.time()
            if "text" in fields or "status" in fields:
                backend_line = f"status={job.get('status')} percent={job.get('percent')} text={job.get('text')}"
        if backend_line:
            self._append_backend_log(job_id, "STATUS", backend_line)

    def log(self, job_id: str, message: str):
        safe = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(message))
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job["log_sequence"] = int(job.get("log_sequence") or 0) + 1
                job["logs"].append({
                    "sequence": job["log_sequence"],
                    "time": time.strftime("%H:%M:%S"),
                    "message": safe[:800],
                    "major": self._is_major_log(safe),
                })
                job["logs"] = job["logs"][-1000:]
                job["updated_at"] = time.time()
        self._append_backend_log(job_id, "DETAIL", safe)

    def get(self, job_id: str, public: bool = False) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            snapshot = json.loads(json.dumps(job, ensure_ascii=False)) if job else None
        if snapshot and public:
            snapshot["logs"] = [item for item in snapshot.get("logs") or [] if item.get("major")]
        return snapshot

    def register_gcash_order(self, result: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Keep the original Checkout HTTP session for callback continuation."""
        order_id = uuid.uuid4().hex[:24]
        callback_token = secrets.token_urlsafe(24)
        now = time.time()
        expires_at = int(result.get("expires_at") or now + 1800)
        order_fields = {
            "gcash_order_id": order_id,
            "payment_status": "waiting_callback",
            "payment_callback_path": f"/api/gcash/orders/{order_id}/callback",
            "payment_expires_at": expires_at,
            "callback_token": callback_token,
        }
        with self.lock:
            self.gcash_orders[order_id] = {
                "id": order_id,
                "callback_token_hash": hashlib.sha256(callback_token.encode()).hexdigest(),
                "status": "waiting_callback",
                "created_at": now,
                "updated_at": now,
                "expires_at": expires_at,
                "result": {**dict(result), **order_fields},
                "context": dict(context),
                "processing": False,
            }
            if len(self.gcash_orders) > 500:
                stale = sorted(self.gcash_orders, key=lambda key: self.gcash_orders[key].get("updated_at", 0))
                for key in stale[: max(0, len(stale) - 500)]:
                    self.gcash_orders.pop(key, None)
        return order_fields

    def gcash_order(self, order_id: str, callback_token: str = "") -> dict[str, Any] | None:
        with self.lock:
            order = self.gcash_orders.get(str(order_id))
            if not order:
                return None
            if callback_token and not hmac.compare_digest(
                str(order.get("callback_token_hash") or ""),
                hashlib.sha256(str(callback_token).encode()).hexdigest(),
            ):
                return None
            if order.get("status") == "waiting_callback" and time.time() >= float(order.get("expires_at") or 0):
                order["status"] = "expired"
                order["updated_at"] = time.time()
            result = dict(order.get("result") or {})
            result.pop("callback_token", None)
            return {
                "order_id": order["id"],
                "status": order.get("status") or "unknown",
                "created_at": order.get("created_at"),
                "updated_at": order.get("updated_at"),
                "expires_at": order.get("expires_at"),
                "error": str(order.get("error") or ""),
                "result": result,
            }

    def refresh_gcash_order_qr(self, order_id: str, callback_token: str = "") -> dict[str, Any] | None:
        """Request a fresh provider QR while keeping the Checkout session."""
        with self.lock:
            order = self.gcash_orders.get(str(order_id))
            if not order or not callback_token or not hmac.compare_digest(
                str(order.get("callback_token_hash") or ""),
                hashlib.sha256(str(callback_token).encode()).hexdigest(),
            ):
                return None
            if order.get("status") in {"success", "failed", "expired", "cancelled"}:
                return self.gcash_order(order_id, callback_token)
            context = dict(order.get("context") or {})
            result = dict(order.get("result") or {})
        try:
            started = start_custom_checkout_method(
                context.get("http"), str(context.get("token") or ""),
                str(context.get("checkout_session_id") or ""),
                str(context.get("processor_entity") or ""),
                str(context.get("custom_payment_method_id") or ""),
                str(context.get("device_id") or ""),
            )
            action = started.get("next_action") or {}
            adyen_url = str(action.get("url") or "").strip() if isinstance(action, dict) else ""
            authorization_url = gcash_authorization_url(started) or resolve_gcash_authorization_url(
                context.get("http"), adyen_url,
            )
            qr_payload = fetch_gcash_public_qr(authorization_url, str(context.get("proxy") or "")) if authorization_url else {}
            qr_data = gcash_qr_data(qr_payload, started)
            if not qr_data:
                raise RuntimeError("GCASH_QR_REFRESH_UNAVAILABLE: 上游未返回新的免登录二维码")
            qr_expires_at = gcash_qr_expires_at(qr_payload, started)
            fields = {
                "qr_data": qr_data,
                "qr_status": "ready",
                "qr_expires_at": qr_expires_at,
                "payment_status": "waiting_callback",
            }
            if authorization_url:
                fields["gcash_authorization_url"] = authorization_url
                fields["provider_redirect_url"] = authorization_url
                fields["short_link"] = authorization_url
                fields["checkout_url"] = authorization_url
            with self.lock:
                order = self.gcash_orders.get(str(order_id))
                if order:
                    order["result"] = {**dict(order.get("result") or result), **fields}
                    order["updated_at"] = time.time()
            return self.gcash_order(order_id, callback_token)
        except Exception as exc:
            with self.lock:
                order = self.gcash_orders.get(str(order_id))
                if order:
                    order["updated_at"] = time.time()
                    order["error"] = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(exc))[:500]
            return self.gcash_order(order_id, callback_token)

    def complete_gcash_order(self, order_id: str, callback: Any, callback_token: str = "") -> dict[str, Any] | None:
        with self.lock:
            order = self.gcash_orders.get(str(order_id))
            if not order or not callback_token or not hmac.compare_digest(
                str(order.get("callback_token_hash") or ""),
                hashlib.sha256(str(callback_token).encode()).hexdigest(),
            ):
                return None
            if order.get("status") in {"success", "failed", "expired", "cancelled"}:
                return self.gcash_order(order_id, callback_token)
            if order.get("processing"):
                return self.gcash_order(order_id, callback_token)
            if time.time() >= float(order.get("expires_at") or 0):
                order["status"] = "expired"
                order["updated_at"] = time.time()
                return self.gcash_order(order_id, callback_token)
            order["processing"] = True
            context = dict(order.get("context") or {})
        try:
            outcome = complete_gcash_callback(context, callback)
            status = str(outcome.get("status") or "paying")
            with self.lock:
                order = self.gcash_orders.get(str(order_id))
                if order:
                    order["status"] = status
                    order["updated_at"] = time.time()
                    order["result"] = {**dict(order.get("result") or {}), "payment_status": status}
                    order["processing"] = False
                    job_id = str((order.get("context") or {}).get("job_id") or "")
                    job = self.jobs.get(job_id)
                    if job and isinstance(job.get("result"), dict):
                        job["result"] = {**job["result"], "payment_status": status}
                        job["updated_at"] = time.time()
            return self.gcash_order(order_id, callback_token)
        except Exception as exc:
            with self.lock:
                order = self.gcash_orders.get(str(order_id))
                if order:
                    order["status"] = "failed"
                    order["updated_at"] = time.time()
                    order["error"] = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(exc))[:500]
                    order["processing"] = False
            return self.gcash_order(order_id, callback_token)

    def cancel(self, job_id: str) -> bool:
        with self.condition:
            if job_id not in self.jobs:
                return False
            job = self.jobs[job_id]
            job["cancel"] = True
            if job.get("status") == "queued" and not job.get("dispatched"):
                self.pending = deque((jid, opts) for jid, opts in self.pending if jid != job_id)
                job.update(
                    status="cancelled", percent=100, text="任务已停止",
                    error="任务已停止", queue_position=0,
                )
                self._refresh_queue_locked()
                self._append_backend_log(job_id, "STATUS", "status=cancelled percent=100 text=任务已停止")
            else:
                # Report the terminal state at once.  Cooperative checks in
                # the worker stop the remaining stages at the next boundary.
                job.update(
                    status="cancelled", percent=100, text="任务已停止",
                    error="任务已停止", queue_position=0,
                )
                self._append_backend_log(job_id, "STATUS", "status=cancelled percent=100 text=任务已停止")
            job["updated_at"] = time.time()
            self.condition.notify_all()
            return True

    def cancelled(self, job_id: str) -> bool:
        with self.lock:
            return bool((self.jobs.get(job_id) or {}).get("cancel"))

    def ensure_not_cancelled(self, job_id: str) -> None:
        if self.cancelled(job_id):
            raise InterruptedError("任务已停止")

    def _run(self, job_id: str, options: dict):
        account_lock = checkout_token_lock(str(options.get("token_raw") or ""))
        if not account_lock.acquire(blocking=False):
            message = "同一账号已有提链任务正在运行；并发创建 Checkout 会让旧 Session 失效"
            self.log(job_id, f"错误：RuntimeError: {message}")
            self.update(job_id, status="error", percent=100, text="任务失败", error=message)
            return
        try:
            self._run_locked(job_id, options)
        finally:
            account_lock.release()

    def _run_locked(self, job_id: str, options: dict):
        retry_count = min(50, max(0, int(options.get("retry_count") or 0)))
        max_attempts = min(51, retry_count + 1)
        gopay_creation_budget: CheckoutCreationBudget | None = None
        if options.get("link_type") == "gopay":
            raw_deadline = (
                options.get("gopay_creation_deadline_seconds")
                or os.getenv("PAY153_GOPAY_CREATION_DEADLINE_SECONDS")
                or GOPAY_CHECKOUT_CREATION_DEADLINE_SECONDS
            )
            try:
                deadline_seconds = float(raw_deadline)
            except (TypeError, ValueError):
                deadline_seconds = GOPAY_CHECKOUT_CREATION_DEADLINE_SECONDS
            deadline_seconds = min(3600.0, max(1.0, deadline_seconds))
            try:
                cs_live_create_attempts = max(
                    1,
                    min(10, int(options.get("gopay_cs_live_attempts") or 10)),
                )
            except (TypeError, ValueError):
                cs_live_create_attempts = 10
            # One blocked CS Live candidate may itself require several
            # Checkout creations while OAICS responses are discarded.  Count
            # both nested dimensions before applying the account safety cap,
            # otherwise one OAICS-heavy pass consumes the entire budget before
            # the requested ten distinct CS Live candidates can be attempted.
            creation_limit = min(
                GOPAY_CHECKOUT_CREATION_LIMIT,
                max_attempts
                * GOPAY_BLOCKED_REBUILD_ATTEMPTS
                * cs_live_create_attempts,
            )
            gopay_creation_budget = CheckoutCreationBudget(
                creation_limit,
                deadline_seconds=deadline_seconds,
            )
            self.log(
                job_id,
                f"GoPay 账户共享 Checkout 创建预算：最多 {creation_limit} 次，"
                f"时限 {deadline_seconds:g} 秒；覆盖全部外层重试与 CS Live 重建",
            )
        used_pairs: set[tuple[str, str]] = set()
        proxy_transport_retries = 0
        retry_same_strategy = False
        last_error = ""
        oaics_hits = 0
        requested_paypal_country = str(
            options.get("checkout_country") or options.get("country") or "US"
        ).upper()
        direct_paypal_countries = {
            str(item).upper() for item in getattr(sc, "PAYPAL_ORDER_COUNTRIES", [])
        }
        paypal_force_de_fallback = bool(
            options.get("link_type") == "paypal"
            and requested_paypal_country not in direct_paypal_countries
        )
        if paypal_force_de_fallback:
            self.log(
                job_id,
                f"PayPal billing country {requested_paypal_country} uses DE/EUR fallback from attempt 1",
            )
        attempt = 1
        while attempt <= max_attempts:
            if self.cancelled(job_id):
                self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                return
            current = dict(options)
            current["retry_wrapper"] = True
            if gopay_creation_budget is not None:
                current["_gopay_creation_budget"] = gopay_creation_budget
            if current.get("dynamic_proxy_api"):
                try:
                    entry_country = str(
                        current.get("entry_proxy_country") or current.get("country") or "US"
                    ).upper()
                    exit_country = str(
                        current.get("exit_proxy_country") or current.get("country") or entry_country
                    ).upper()
                    proxy_session_time = int(current.get("proxy_session_time") or 10)
                    entry_proxy = fetch_dynamic_attempt_proxy(entry_country, proxy_session_time)
                    if shares_checkout_proxy(current, str(current.get("link_type") or "")):
                        exit_proxy = entry_proxy
                    else:
                        exit_proxy = fetch_dynamic_attempt_proxy(exit_country, proxy_session_time)
                    pair = (entry_proxy, exit_proxy)
                    current["entry_proxies"] = [entry_proxy]
                    current["exit_proxies"] = [exit_proxy]
                    self.log(
                        job_id,
                        f"Attempt {attempt}/{max_attempts}: proxy API issued fresh {entry_country}/{exit_country} routes",
                    )
                except Exception as exc:
                    last_error = f"Dynamic proxy fetch failed: {type(exc).__name__}: {exc}"
                    transport_kind = _proxy_transport_error_kind(last_error)
                    if transport_kind and proxy_transport_retries >= 3:
                        self.log(job_id, f"{transport_kind}已达到 3 次代理切换上限，停止继续尝试")
                        self.update(job_id, status="error", percent=100, text="代理传输失败", error=last_error[:1200])
                        return
                    if transport_kind and proxy_transport_retries < 3:
                        proxy_transport_retries += 1
                        retry_same_strategy = True
                        self.log(job_id, f"检测到{transport_kind}，切换第 {proxy_transport_retries} 次代理后重试")
                    can_retry = attempt < max_attempts
                    self.update(
                        job_id,
                        status="running" if can_retry else "error",
                        percent=4 if can_retry else 100,
                        text=("正在重新获取代理" if can_retry else "任务失败"),
                        error=last_error[:1200],
                        last_retry_error=last_error[:500],
                    )
                    self.log(job_id, f"第 {attempt}/{max_attempts} 轮代理获取失败：{last_error[:260]}")
                    if not can_retry:
                        return
                    time.sleep(min(4, 1 + attempt * 0.35))
                    attempt += 1
                    continue
            else:
                entry_pool = current["entry_proxies"]
                exit_pool = current.get("exit_proxies") or entry_pool
                if current.get("paired_proxy_rotation"):
                    # Keep deterministic rotation while skipping combinations
                    # already used by an earlier attempt. This prevents an
                    # SSL retry from accidentally reusing the reset route.
                    same_route = shares_checkout_proxy(current, str(current.get("link_type") or ""))
                    pair = None
                    max_offsets = max(len(entry_pool), len(exit_pool), 1)
                    for offset in range(max_offsets):
                        entry_proxy = entry_pool[(attempt - 1 + offset) % len(entry_pool)]
                        exit_proxy = entry_proxy if same_route else exit_pool[(attempt - 1 + offset) % len(exit_pool)]
                        candidate = (entry_proxy, exit_proxy)
                        if candidate not in used_pairs or len(used_pairs) >= len(entry_pool) * len(exit_pool):
                            pair = candidate
                            break
                    if pair is None:
                        pair = (
                            entry_pool[(attempt - 1) % len(entry_pool)],
                            entry_pool[(attempt - 1) % len(entry_pool)]
                            if same_route else exit_pool[(attempt - 1) % len(exit_pool)],
                        )
                else:
                    pair = None
                    for _ in range(40):
                        if shares_checkout_proxy(current, str(current.get("link_type") or "")):
                            proxy = secrets.choice(entry_pool)
                            candidate = (proxy, proxy)
                        else:
                            candidate = (secrets.choice(entry_pool), secrets.choice(exit_pool))
                        if candidate not in used_pairs or len(used_pairs) >= len(entry_pool) * len(exit_pool):
                            pair = candidate
                            break
                    if pair is None:
                        pair = (secrets.choice(entry_pool), secrets.choice(exit_pool))
            used_pairs.add(pair)
            current["fixed_entry_proxy"], current["fixed_exit_proxy"] = pair
            current["_proxy_round"] = attempt
            self.log(
                job_id,
                f"本轮代理路由：Promotion={proxy_route_label(pair[0])}；Checkout={proxy_route_label(pair[1])}",
            )
            logical_attempt = max(1, attempt - proxy_transport_retries)
            if current.get("link_type") == "paypal":
                current["force_paypal_de_fallback"] = paypal_force_de_fallback
                current["requested_paypal_country"] = requested_paypal_country
                # Strategy A creates the Checkout with the campaign already
                # attached.  This preserves the merchant's native zero-due
                # PayPal SetupIntent configuration.  Strategy B keeps the
                # existing cross-entry checkout/update flow as a fallback.
                current["promo_on_create"] = bool(
                    (logical_attempt - 1) % 2 == 0 and not paypal_force_de_fallback
                )
            if current.get("link_type") in {"pix", "upi", "momo", "gcash"}:
                # Alternate both Stripe submission shapes across outer retries.
                # Some Checkout revisions accept a pre-created pm_* while
                # others only complete the local mandate with inline data.
                momo_create_promo = False
                if current.get("link_type") == "pix":
                    strategy_cycle = ("standalone", "late_promo", "inline")
                elif current.get("link_type") == "momo":
                    # OAICS deployments differ on whether the campaign must be
                    # attached at creation or applied after the native MoMo
                    # method is published. Alternate the complete contract
                    # across account retries instead of replaying one rejected
                    # checkout/update shape every time.
                    momo_create_promo = bool(
                        current.get("use_promo", False)
                        and (logical_attempt - 1) % 2 == 0
                    )
                    strategy_cycle = (
                        ("standalone",)
                        if momo_create_promo
                        else (
                            ("late_promo",)
                            if current.get("use_promo", False)
                            else ("standalone", "inline")
                        )
                    )
                elif current.get("link_type") == "upi" and current.get("named_proxy_pools"):
                    strategy_cycle = ("inline", "late_promo", "standalone")
                elif current.get("use_promo", False):
                    strategy_cycle = ("go_b", "go_b", "inline", "late_promo")
                else:
                    strategy_cycle = ("standalone", "inline")
                strategy_attempt = (
                    logical_attempt if current.get("link_type") == "momo" else attempt
                )
                current["local_method_strategy"] = strategy_cycle[
                    (strategy_attempt - 1) % len(strategy_cycle)
                ]
                # PIX/UPI need a non-zero Stage1 to publish their Stripe
                # mandate configuration. MoMo uses the strategy selected
                # above for both OAICS and the CS Live compatibility fallback.
                current["promo_on_create"] = (
                    momo_create_promo
                    if current.get("link_type") == "momo"
                    else (
                        current.get("link_type") == "gcash"
                        and bool(current.get("use_promo"))
                    )
                )
            if current.get("link_type") == "momo":
                # Successful MoMo sessions use OAICS native momo with a Stripe
                # ConfirmationToken. A cs_live_* response remains supported by
                # the generic provider path as a compatibility fallback.
                current["checkout_ui_mode"] = "custom"
            if current.get("link_type") == "gopay":
                # Midtrans GoPay is exposed by the CS Live/Stripe Checkout.
                # Keep every retry aligned with the account-management ID
                # payment probe.  Switching later attempts to custom mode
                # asks for OAICS and can hide Stripe's GoPay method even when
                # the same account was just probed successfully.
                current["checkout_ui_mode"] = "redirect"
                current["promo_from_query_param"] = bool(current.get("use_promo"))
                # First use the late-update path so Stripe can publish GoPay.
                # If the server accepts update but keeps the full amount, the
                # next complete attempt creates Checkout with the verified
                # coupon attached instead of repeating the same failed shape.
                current["promo_on_create"] = bool(
                    current.get("use_promo") and logical_attempt % 2 == 0
                )
            if current.get("link_type") == "pix" and current.get("pix_tax_id_auto"):
                auto_kind = current.get("pix_auto_kind") or "cpf"
                kind = ("cpf" if attempt % 2 else "cnpj") if auto_kind == "mixed" else auto_kind
                current["pix_tax_id"] = generate_cnpj() if kind == "cnpj" else generate_cpf()
                current["pix_identity"] = generate_pix_identity(kind)
            self.update(
                job_id, status="running", percent=4,
                text=f"第 {attempt}/{max_attempts} 次尝试：正在准备任务",
                error="",
                payment_error=None,
            )
            self.log(job_id, f"========== 提链尝试 {attempt}/{max_attempts} ==========")
            if current.get("link_type") == "paypal" and current.get("use_promo"):
                strategy = "Checkout 创建时原生带优惠" if current.get("promo_on_create") else "创建后通过 Promotion代理池更新优惠"
                self.log(job_id, f"PayPal 优惠策略：{strategy}")
                if retry_same_strategy:
                    self.log(job_id, "上一轮为代理传输失败；本轮仅更换代理并沿用相同 PayPal 优惠策略")
            if current.get("link_type") == "gopay" and current.get("use_promo"):
                strategy = (
                    "Checkout 创建时携带已验证优惠券"
                    if current.get("promo_on_create")
                    else "确认 GoPay 后通过 Promotion代理池更新优惠券"
                )
                self.log(job_id, f"GoPay 优惠策略：{strategy}（query_param=true）")
            retry_same_strategy = False
            gopay_chain_attempt = 0
            momo_chain_attempt = 0
            while True:
                if current.get("link_type") == "gopay":
                    gopay_chain_attempt += 1
                    self.log(
                        job_id,
                        f"GoPay 本轮 CS Live 顺序尝试 {gopay_chain_attempt}/{GOPAY_BLOCKED_REBUILD_ATTEMPTS}",
                    )
                    # _run_single pops token_raw and refreshes device ids. A
                    # copy keeps the selected outer proxy pair reusable while
                    # every inner pass creates a brand-new Checkout identity.
                    single_options = dict(current)
                elif current.get("link_type") == "momo":
                    momo_chain_attempt += 1
                    single_options = dict(current)
                    if single_options.get("use_promo"):
                        base_create_promo = bool(current.get("promo_on_create"))
                        create_promo = (
                            base_create_promo
                            if momo_chain_attempt % 2 == 1
                            else not base_create_promo
                        )
                        single_options["promo_on_create"] = create_promo
                        single_options["local_method_strategy"] = (
                            "standalone" if create_promo else "late_promo"
                        )
                    if not single_options.get("use_promo"):
                        strategy = "不带优惠创建"
                    elif single_options.get("promo_on_create"):
                        strategy = "创建时携带优惠"
                    else:
                        strategy = "发布 MoMo 后更新优惠"
                    self.log(
                        job_id,
                        f"MoMo 本轮 OAICS 顺序尝试 {momo_chain_attempt}/"
                        f"{MOMO_CHECKOUT_REBUILD_ATTEMPTS}：{strategy}",
                    )
                else:
                    single_options = current
                self._run_single(job_id, single_options)
                state = self.get(job_id) or {}
                if state.get("status") in {"done", "cancelled"}:
                    break
                inner_error = str(state.get("error") or "")
                inner_payment_error = (
                    state.get("payment_error")
                    if isinstance(state.get("payment_error"), dict)
                    else {}
                )
                inner_error_code = str(inner_payment_error.get("code") or "").upper()
                gopay_blocked = (
                    current.get("link_type") == "gopay"
                    and bool(inner_payment_error.get("retryable"))
                    and bool(inner_payment_error.get("rebuild_checkout"))
                    and inner_error_code in {
                        "GOPAY_APPROVAL_BLOCKED_REBUILD_REQUIRED",
                        "GOPAY_CONFIRM_BLOCKED_REBUILD_REQUIRED",
                    }
                )
                momo_rebuild = (
                    current.get("link_type") == "momo"
                    and (
                        (
                            bool(inner_payment_error.get("retryable"))
                            and bool(inner_payment_error.get("rebuild_checkout"))
                        )
                        if inner_payment_error
                        else momo_checkout_requires_rebuild(inner_error)
                    )
                )
                if not gopay_blocked and not momo_rebuild:
                    break
                if gopay_blocked and gopay_chain_attempt >= GOPAY_BLOCKED_REBUILD_ATTEMPTS:
                    self.log(
                        job_id,
                        "GoPay 本轮顺序创建并尝试的 10 个 CS Live 均被 blocked；本次账户完整任务尝试结束",
                    )
                    break
                if momo_rebuild and momo_chain_attempt >= MOMO_CHECKOUT_REBUILD_ATTEMPTS:
                    self.log(
                        job_id,
                        f"MoMo 本轮顺序创建的 {MOMO_CHECKOUT_REBUILD_ATTEMPTS} 个 OAICS "
                        "均未形成兼容优惠链路；本次账户完整任务尝试结束",
                    )
                    break
                if gopay_blocked:
                    rebuild_text = (
                        f"GoPay 本轮 CS Live {gopay_chain_attempt}/10 已 blocked；"
                        f"保持当前外层代理并重建完整链路 {gopay_chain_attempt + 1}/10"
                    )
                    next_text = f"正在重建 GoPay CS Live {gopay_chain_attempt + 1}/10"
                else:
                    rebuild_text = (
                        f"MoMo 本轮 OAICS {momo_chain_attempt}/"
                        f"{MOMO_CHECKOUT_REBUILD_ATTEMPTS} 未兼容；丢弃当前 Session 并重建完整链路 "
                        f"{momo_chain_attempt + 1}/{MOMO_CHECKOUT_REBUILD_ATTEMPTS}"
                    )
                    next_text = (
                        f"正在重建 MoMo OAICS {momo_chain_attempt + 1}/"
                        f"{MOMO_CHECKOUT_REBUILD_ATTEMPTS}"
                    )
                self.log(job_id, rebuild_text)
                self.update(
                    job_id,
                    status="running",
                    percent=4,
                    text=(
                        f"第 {attempt}/{max_attempts} 次账户任务："
                        f"{next_text}"
                    ),
                    error="",
                    payment_error=None,
                )
            state = self.get(job_id) or {}
            if state.get("status") in {"done", "cancelled"}:
                if state.get("status") == "done" and isinstance(state.get("result"), dict):
                    result = state["result"]
                    result["attempt"] = attempt
                    result["max_attempts"] = max_attempts
                    self.update(job_id, result=result)
                    self._record_success(job_id, result)
                return
            last_error = str(state.get("error") or "")
            payment_error = (
                state.get("payment_error")
                if isinstance(state.get("payment_error"), dict)
                else {}
            )
            payment_error_code = str(payment_error.get("code") or "").upper()
            if last_error:
                self.update(job_id, last_retry_error=last_error[:500])
            lowered = last_error.lower()
            gopay_oaics_rebuild_exhausted = (
                payment_error_code == "GOPAY_CS_LIVE_REBUILD_EXHAUSTED"
            )
            if current.get("link_type") != "momo" and (
                gopay_oaics_rebuild_exhausted
                or (
                    not payment_error
                    and (
                        "custom_checkout_rebuild_required" in lowered
                        or "oaics_" in lowered
                    )
                )
            ):
                oaics_hits += 1
                self.log(job_id, f"OAICS Checkout 命中 {oaics_hits}/3；PayPal/本地支付将重建 Checkout")
                if oaics_hits >= 3:
                    threshold_error = (
                        "OAICS_THRESHOLD_REACHED: selected payment channel requires Stripe cs_*; "
                        "use Official Checkout for this account"
                    )
                    self.update(
                        job_id, status="error", percent=100,
                        text="当前账号仅返回 OAICS，请改用官方 Checkout",
                        error=threshold_error,
                        last_retry_error=last_error[:500],
                    )
                    return
            non_retryable = (
                not bool(payment_error.get("retryable"))
                if payment_error
                else any(marker in lowered for marker in (
                    "access token", "token_invalidated", "token_expired", "token_revoked", "jwt expired",
                    "openai checkout http 401", "unauthorized_unknown",
                    "计划类型", "提取方式", "任务已停止", "promotion_not_available",
                    "gopay_checkout_creation_budget_exhausted",
                    "gopay_checkout_creation_deadline_exceeded",
                ))
            )
            transport_kind = (
                _proxy_transport_error_kind(last_error)
                if not payment_error or bool(payment_error.get("retryable"))
                else ""
            )
            if transport_kind:
                if proxy_transport_retries >= 3:
                    self.log(job_id, f"{transport_kind}已达到 3 次代理切换上限，停止继续尝试")
                    self.update(job_id, status="error", percent=100, text="代理传输失败", error=last_error[:1200])
                    return
                proxy_transport_retries += 1
                retry_same_strategy = True
                self.log(job_id, f"检测到{transport_kind}，切换第 {proxy_transport_retries} 次代理后重试")
            if non_retryable or attempt >= max_attempts:
                self.update(job_id, status="error", percent=100, text="任务失败", error=last_error[:1200])
                return
            if (
                options.get("link_type") == "gopay"
                and payment_error_code in {
                    "GOPAY_APPROVAL_BLOCKED_REBUILD_REQUIRED",
                    "GOPAY_CONFIRM_BLOCKED_REBUILD_REQUIRED",
                }
            ):
                self.log(
                    job_id,
                    "GoPay 本轮 10 次 CS Live 重建预算已耗尽；下一次账户任务将更换代理后重新开始",
                )
            self.log(job_id, f"第 {attempt}/{max_attempts} 轮未命中：{last_error[:260] or '上游未返回可用链接'}")
            if options.get("link_type") == "pix":
                self.log(job_id, "正在更换代理与 PIX 资料后重新尝试")
            else:
                self.log(job_id, "正在更换代理后重新尝试")
            time.sleep(min(4, 1 + attempt * 0.35))
            attempt += 1

    def _run_rust_workflow(self, job_id: str, options: dict, rust_base: str):
        """Prepare one existing outer retry, then execute the payment stages in Rust."""
        try:
            self.update(job_id, status="running", percent=6, text="解析账号与 Rust 任务参数", error="")
            provider = str(options.get("link_type") or "").lower()
            entry_proxy = str(options.get("fixed_entry_proxy") or "").strip()
            payment_proxy = str(options.get("fixed_exit_proxy") or entry_proxy).strip()
            if shares_checkout_proxy(options, provider):
                payment_proxy = entry_proxy
            if not entry_proxy or not payment_proxy:
                raise RuntimeError("Rust 工作流缺少本轮 Checkout 或 Promotion 代理")
            self.log(
                job_id,
                f"Checkout 代理池共 {len(options.get('exit_proxies') or [])} 条，"
                f"Promotion 代理池共 {len(options.get('entry_proxies') or [])} 条，本轮已分别选择",
            )

            country = str(options.get("checkout_country") or options.get("country") or "US").upper()
            payment_geo: dict[str, str] = {}
            if provider == "paypal":
                # reg153 prepares country-specific paired pools.  Re-probing as
                # many as 24 entries for every batch task creates hundreds of
                # simultaneous helper processes and used to surface as HTTP 408.
                # Trust the requested checkout country for paired pools; public
                # free-form pools still keep a bounded probe with a soft fallback.
                if options.get("paired_proxy_rotation"):
                    payment_geo = {
                        "country": country,
                        "currency": str(COUNTRY_CURRENCY.get(country) or options.get("checkout_currency") or options.get("currency") or ""),
                        "region": "",
                        "city": "",
                        "postal": "",
                        "timezone": "",
                        "source": "paired_pool",
                    }
                    self.log(job_id, f"已使用配对代理池地区 {country}，跳过重复代理探测")
                else:
                    exit_pool = list(options.get("exit_proxies") or [payment_proxy])
                    proxy_response = requests.post(
                        f"{rust_base}/api/v1/proxies/select",
                        json={
                            "proxies": exit_pool,
                            "preferred": payment_proxy,
                            "scan_limit": min(8, max(1, int(os.getenv("PAYPAL_PROXY_SCAN_LIMIT", "6") or 6))),
                            "transport": str(os.getenv("PAY153_RUST_TRANSPORT") or "curl_cffi"),
                        },
                        timeout=45,
                    )
                    if proxy_response.status_code == 200:
                        proxy_selection = proxy_response.json() or {}
                        payment_proxy = str(proxy_selection.get("selected") or payment_proxy).strip()
                        payment_geo = dict(proxy_selection.get("geo") or {})
                    elif proxy_response.status_code in {408, 429, 500, 502, 503, 504}:
                        payment_geo = {
                            "country": country,
                            "currency": str(options.get("checkout_currency") or options.get("currency") or ""),
                            "region": "",
                            "city": "",
                            "postal": "",
                            "timezone": "",
                            "source": "probe_timeout_fallback",
                        }
                        self.log(job_id, f"代理探测 HTTP {proxy_response.status_code}，沿用本轮固定代理继续")
                    else:
                        raise RuntimeError(
                            f"Rust 代理选择失败 HTTP {proxy_response.status_code}: "
                            f"{(proxy_response.text or '')[:500]}"
                        )
                    if not payment_proxy or not payment_geo.get("country"):
                        payment_geo = {
                            "country": country,
                            "currency": str(options.get("checkout_currency") or options.get("currency") or ""),
                            "region": "",
                            "city": "",
                            "postal": "",
                            "timezone": "",
                            "source": "empty_geo_fallback",
                        }
                payment_country = str(payment_geo.get("country") or country).upper()
                detected_currency = str(payment_geo.get("currency") or "").upper()
                requested_country = str(options.get("requested_paypal_country") or country).upper()
                country, currency, source = resolve_paypal_checkout_region(
                    requested_country,
                    payment_country,
                    detected_currency,
                    bool(options.get("force_paypal_de_fallback")),
                )
                self.log(
                    job_id,
                    f"PayPal Checkout代理实测={payment_country or '?'}；"
                    f"账单={country}/{currency}（{source}）",
                )
                options["checkout_country"] = country
                options["checkout_currency"] = currency
                options["country"] = country
                options["currency"] = currency
            elif provider == "ideal":
                country, options["currency"] = "NL", "EUR"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "EUR"
            elif provider == "twint":
                country, options["currency"] = "CH", "CHF"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "CHF"
            elif provider == "upi":
                country, options["currency"] = "IN", "INR"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "INR"
            elif provider == "pix":
                country, options["currency"] = "BR", "BRL"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "BRL"
            elif provider == "momo":
                country, options["currency"] = "VN", "VND"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "VND"
            elif provider == "gcash":
                country, options["currency"] = "PH", "PHP"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "PHP"
            elif provider == "kakao":
                country, options["currency"] = "KR", "KRW"
                options["country"] = options["checkout_country"] = country
                options["checkout_currency"] = "KRW"

            prepare_response = requests.post(
                f"{rust_base}/api/v1/legacy/prepare",
                json={
                    "token_raw": str(options.get("token_raw") or ""),
                    "options": options,
                },
                timeout=20,
            )
            if prepare_response.status_code != 200:
                raise RuntimeError(
                    f"Rust 参数准备失败 HTTP {prepare_response.status_code}: "
                    f"{(prepare_response.text or '')[:500]}"
                )
            prepared = dict((prepare_response.json() or {}).get("prepared") or {})
            token = str(prepared.get("access_token") or "")
            meta = dict(prepared.get("meta") or {})
            prepared_payload = prepared.get("payload") or {}
            country = str(prepared.get("country") or country).upper()
            options["currency"] = options["checkout_currency"] = str(
                prepared.get("currency") or options.get("currency") or ""
            ).upper()
            if not token or not meta.get("account_id") or not prepared_payload:
                raise RuntimeError("Rust 参数准备结果不完整")

            device_id, did = str(uuid.uuid4()), str(uuid.uuid4())
            self.update(job_id, status="running", percent=12, text="准备 Rust Checkout 任务")
            billing_geo = payment_geo if str(payment_geo.get("country") or "").upper() == country else None
            billing_response = requests.post(
                f"{rust_base}/api/v1/billing/generate",
                json={
                    "country": country,
                    "email": str(meta.get("email") or ""),
                    "tax_id": str(options.get("pix_tax_id") or ""),
                    "geo": billing_geo,
                    "rotate_public_address": provider == "paypal",
                },
                timeout=20,
            )
            if billing_response.status_code != 200:
                raise RuntimeError(
                    f"Rust 账单生成失败 HTTP {billing_response.status_code}: "
                    f"{(billing_response.text or '')[:500]}"
                )
            billing = dict(((billing_response.json() or {}).get("profile") or {}).get("billing") or {})
            if not billing.get("address"):
                raise RuntimeError("Rust 账单生成未返回地址")
            if provider == "paypal" and country in ROTATING_PAYPAL_ADDRESS_COUNTRIES:
                normalized_address = None
                for _ in range(8):
                    cached_address = resolve_cached_country_address(country)
                    normalized_address = normalize_rotating_paypal_address(country, cached_address or {})
                    if normalized_address:
                        break
                if normalized_address:
                    address = billing.setdefault("address", {})
                    address.update(normalized_address)
            if provider == "pix":
                identity = dict(options.get("pix_identity") or {})
                if identity:
                    billing["name"] = identity.get("name") or billing.get("name")
                    billing["email"] = identity.get("email") or billing.get("email")
                    address = billing.setdefault("address", {})
                    for key in ("line1", "city", "state", "postal_code"):
                        if identity.get(key):
                            address[key] = identity[key]
            address = dict(billing.get("address") or {})
            address.setdefault("line2", "")
            rust_billing = {
                "name": str(billing.get("name") or ""),
                "email": str(billing.get("email") or ""),
                "tax_id": str(billing.get("tax_id") or ""),
                "address": {
                    "country": str(address.get("country") or country),
                    "line1": str(address.get("line1") or ""),
                    "line2": str(address.get("line2") or ""),
                    "city": str(address.get("city") or ""),
                    "postal_code": str(address.get("postal_code") or ""),
                    "state": str(address.get("state") or ""),
                },
            }
            profile = sc._profile(country)
            self.log(
                job_id,
                "令牌字段：SEN={}，SO={}".format(
                    "ON" if options.get("use_sen", True) else "OFF",
                    "ON" if options.get("use_so", True) else "OFF",
                ),
            )
            common = {
                "access_token": token,
                "account_id": str(meta.get("account_id") or ""),
                "payload": prepared_payload,
                "billing": rust_billing,
                "browser_locale": str(profile.get("browser_locale") or "en-US"),
                "browser_timezone": str(profile.get("browser_timezone") or "America/Chicago"),
                "use_sen": bool(options.get("use_sen", True)),
                "use_so": bool(options.get("use_so", True)),
                "attempts": [{
                    "chatgpt_proxy": payment_proxy,
                    "stripe_proxy": payment_proxy,
                    "promotion_proxy": entry_proxy,
                    "device_id": device_id,
                    "oai_did": did,
                    "checkout_sentinel_token": None,
                    "checkout_sentinel_so_token": None,
                    "approval_sentinel_token": None,
                    "approval_sentinel_so_token": None,
                }],
                "transport": str(os.getenv("PAY153_RUST_TRANSPORT") or "curl_cffi"),
            }
            if options.get("use_promo") and options.get("plan") == "plus":
                common["promo"] = {
                    "campaign_id": str(options.get("promo_campaign") or "plus-1-month-free"),
                    "plan_name": PLANS["plus"],
                    "price_interval": "month",
                    "seat_quantity": 1,
                    "require_zero_due": True,
                    "always_update": provider == "kakao",
                }
            if provider == "paypal":
                try:
                    common["fingerprint"] = json.loads(
                        Path(__file__).with_name("paypal_fingerprint.json").read_text(encoding="utf-8")
                    )
                    if "_stripe_version" in common["fingerprint"]:
                        common["fingerprint"]["stripe_version"] = common["fingerprint"].pop("_stripe_version")
                except Exception:
                    common["fingerprint"] = {}
                endpoint = "/api/v1/jobs/paypal-workflow"
            elif provider == "hosted":
                endpoint = "/api/v1/jobs/hosted-workflow"
            else:
                common["provider"] = provider
                endpoint = "/api/v1/jobs/local-workflow"

            response = requests.post(
                f"{rust_base}{endpoint}", json=common, timeout=90,
            )
            if response.status_code != 202:
                raise RuntimeError(
                    f"Rust 工作流创建失败 HTTP {response.status_code}: {(response.text or '')[:500]}"
                )
            rust_job_id = str((response.json() or {}).get("job", {}).get("id") or "")
            if not rust_job_id:
                raise RuntimeError("Rust 工作流未返回任务 ID")
            remember_rust_job_alias(job_id, rust_job_id, {
                "plan": options.get("plan"),
                "link_type": provider,
                "country": country,
                "currency": options.get("currency"),
                "use_promo": bool(options.get("use_promo")),
                "promo_campaign": str(options.get("promo_campaign") or ""),
            })
            step_labels = {
                "creating_checkout": "创建 OpenAI Checkout",
                "stripe_bootstrap": "初始化 Stripe 支付方式",
                "applying_promotion": "应用优惠并同步金额",
                "syncing_billing": "同步账单地址",
                "creating_paypal_payment_method": "创建 PayPal PaymentMethod",
                "creating_local_payment_method": f"创建 {provider.upper()} PaymentMethod",
                "preconfirming_kakao": "准备 Kakao Pay 支付会话",
                "creating_kakao_payment_method": "创建 Kakao Pay PaymentMethod",
                "confirming_kakao": "提交 Kakao Pay confirm",
                "polling_kakao_redirect": "读取 Kakao / Nicepay 跳转",
                "confirming_paypal": "提交 PayPal confirm",
                "confirming_local_payment": f"提交 {provider.upper()} confirm",
                "approving_checkout": "提交 Checkout approval",
                "polling_paypal_redirect": "读取 PayPal 跳转",
                "polling_local_result": f"读取 {provider.upper()} 支付结果",
                "retrying_with_fresh_checkout": "更换参数并重建 Checkout",
            }
            while True:
                if self.cancelled(job_id):
                    try:
                        requests.post(f"{rust_base}/api/v1/jobs/{rust_job_id}/cancel", timeout=8)
                    except Exception:
                        pass
                    self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                    return
                progress_response = requests.get(
                    f"{rust_base}/api/v1/jobs/{rust_job_id}", timeout=15,
                )
                if progress_response.status_code != 200:
                    raise RuntimeError(f"Rust 任务状态 HTTP {progress_response.status_code}")
                rust_job = (progress_response.json() or {}).get("job") or {}
                rust_status = str(rust_job.get("status") or "")
                rust_step = str(rust_job.get("step") or "")
                if rust_status not in {"succeeded", "failed", "cancelled"}:
                    self.update(
                        job_id,
                        status="running",
                        percent=int(rust_job.get("progress") or 0),
                        text=step_labels.get(rust_step, rust_step or "Rust 工作流运行中"),
                        error=str(rust_job.get("error") or "")[:1200],
                    )
                if rust_status == "succeeded":
                    result = dict(rust_job.get("result") or {})
                    result.update({
                        "plan": options.get("plan"),
                        "link_type": provider,
                        "account_email": str(meta.get("email") or ""),
                        "account_id": str(meta.get("account_id") or ""),
                        "country": country,
                        "currency": str(result.get("currency") or options.get("currency") or "").upper(),
                        "entry_country": str(proxy_country(entry_proxy)[0] or "").upper(),
                        "payment_proxy_country": str(proxy_country(payment_proxy)[0] or "").upper(),
                        "rust_workflow": True,
                        "sen_requested": bool(options.get("use_sen", True)),
                        "so_requested": bool(options.get("use_so", True)),
                    })
                    if provider == "paypal":
                        result["paypal_link"] = result.get("paypal_url") or ""
                        result["provider_redirect_url"] = result.get("paypal_url") or result.get("stripe_redirect_url") or ""
                        result_kind = session_checkout_kind(str(result.get("checkout_session_id") or ""))
                        result["checkout_kind"] = "cs_live" if result_kind == "unknown" else result_kind
                    self.update(job_id, status="done", percent=100, text="提取完成", error="", result=result)
                    return
                if rust_status == "failed":
                    rust_error = str(rust_job.get("error") or "Rust 工作流失败")[:1200]
                    if options.get("retry_wrapper"):
                        self.update(
                            job_id,
                            status="running",
                            percent=8,
                            text="本轮未成功，正在更换代理重试",
                            error=rust_error,
                        )
                    else:
                        self.update(job_id, status="error", percent=100, text="任务失败", error=rust_error)
                    return
                if rust_status == "cancelled":
                    self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                    return
                time.sleep(0.5)
        except InterruptedError as exc:
            self.update(job_id, status="cancelled", percent=100, text="任务已停止", error=str(exc))
        except Exception as exc:
            self.log(job_id, f"Rust 工作流异常：{type(exc).__name__}: {exc}")
            if options.get("retry_wrapper"):
                self.update(
                    job_id,
                    status="running",
                    percent=8,
                    text="本轮未成功，正在更换代理重试",
                    error=str(exc)[:1200],
                )
            else:
                self.update(job_id, status="error", percent=100, text="任务失败", error=str(exc)[:1200])

    def _run_single(self, job_id: str, options: dict):
        rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
        rust_execute = str(os.getenv("PAY153_RUST_WORKFLOWS") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        paypal_mode = str(options.get("paypal_checkout_mode") or "auto")
        if options.get("link_type") == "paypal":
            mode_label = {"oaics": "OAICS", "cs_live": "CS Live"}.get(paypal_mode, "自动识别")
            self.log(job_id, f"PayPal Checkout 类型：{mode_label}；将使用对应提链流程")
        if rust_execute and rust_base and options.get("link_type") in {"paypal", "pix", "ideal"} and not (
            options.get("link_type") == "paypal" and options.get("oaics_paypal")
        ):
            return self._run_rust_workflow(job_id, options, rust_base)
        if options.get("link_type") == "kakao":
            return self._run_kakao_pidan(job_id, options)
        transport_stage = "初始化"
        http_sessions = HttpSessionRegistry()
        flow_started_at = time.monotonic()
        provider = str(options.get("link_type") or "").strip().lower()
        client_context: CheckoutClientContext | None = None
        actual_checkout_kind = "unknown"
        try:
            self.update(job_id, status="running", percent=6, text="解析 Access Token")
            raw_token = options.pop("token_raw")
            token, meta = extract_access_token(raw_token)
            self.ensure_not_cancelled(job_id)
            provider = options["link_type"]
            country = options["country"]
            entry_pool = options["entry_proxies"]
            exit_pool = entry_pool if shares_checkout_proxy(options, provider) else (options.get("exit_proxies") or entry_pool)
            entry_proxy = options.get("fixed_entry_proxy") or secrets.choice(entry_pool)
            exit_proxy = entry_proxy if shares_checkout_proxy(options, provider) else (options.get("fixed_exit_proxy") or secrets.choice(exit_pool))
            payment_geo: dict[str, str] = {}
            if provider == "hosted" and not options.get("named_proxy_pools"):
                self.log(job_id, f"Checkout 代理池共 {len(entry_pool)} 条，本次已自动选择 1 条")
            elif provider in {"pix", "momo"} and not options.get("named_proxy_pools"):
                self.log(job_id, f"Promotion 代理池共 {len(entry_pool)} 条，本次已自动选择 1 条")
            else:
                self.log(job_id, f"Checkout 代理池共 {len(exit_pool)} 条，Promotion 代理池共 {len(entry_pool)} 条，本次已分别自动选择")
            # Every outer retry creates a brand-new Checkout, so it must also
            # use a fresh browser/device identity.  Within this single attempt
            # the same ids are kept for create -> update -> approve.
            device_id, did = str(uuid.uuid4()), str(uuid.uuid4())
            if provider in {"gopay", "momo"}:
                # Keep Sentinel proof, oai-did and every Checkout request on
                # one device identity for the complete payment attempt.
                did = device_id
                identity_hash = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:10]
                self.log(
                    job_id,
                    f"{provider.upper()} 会话身份已锁定："
                    f"oai-did=OAI-Device-Id，identity={identity_hash}",
                )
            client_context = None

            if provider == "ph_short":
                short_country = country if country in {"PH", "GB", "US"} else "PH"
                short_currency = {"PH": "PHP", "GB": "GBP", "US": "USD"}[short_country]
                named_proxy_pools = bool(options.get("named_proxy_pools"))
                checkout_proxy_country = str(options.get("exit_proxy_country") if named_proxy_pools else options.get("entry_proxy_country") or short_country).upper()
                update_proxy_country = str(
                    (options.get("entry_proxy_country") if named_proxy_pools else options.get("exit_proxy_country"))
                    or (options.get("promo_country") if options.get("use_promo") else short_country)
                    or short_country
                ).upper()
                short_checkout_proxy = exit_proxy if named_proxy_pools else entry_proxy
                short_promotion_proxy = entry_proxy if named_proxy_pools else exit_proxy
                self.update(job_id, percent=9, text=f"Validate {checkout_proxy_country} Checkout and {update_proxy_country} promotion proxy")
                credentials = parse_ph_short_credentials(raw_token)
                extractor = PhShortCheckoutExtractor(
                    credentials,
                    PhShortExtractorConfig(
                        billing_country=short_country,
                        currency=short_currency,
                        payment_locale="en",
                        checkout_proxy_country=checkout_proxy_country,
                        update_proxy_country=update_proxy_country,
                        checkout_proxy=short_checkout_proxy,
                        update_proxy=short_promotion_proxy,
                        plan_name="chatgptplusplan",
                        promo_campaign_id=options.get("promo_campaign") or "plus-1-month-free",
                        apply_promo=bool(options.get("use_promo")),
                        checkout_attempts=10,
                        update_attempts=15,
                        full_attempts=1,
                        cf_same_identity_attempts=5,
                        verify_proxy_country=True,
                        allow_missing_customer_session=bool(options.get("allow_missing_customer_session")),
                    ),
                    logger=lambda message: self.log(job_id, message),
                )
                self.update(job_id, percent=24, text=f"Create {short_country}/{short_currency} Checkout")
                extracted = extractor.extract()
                verification = str(extracted.amount_verification or "pending")
                promo_requested = bool(options.get("use_promo"))
                promo_applied = (verification == "verified_zero") if promo_requested and verification != "pending" else None
                result = {
                    "plan": options["plan"],
                    "link_type": "ph_short",
                    "checkout_session_id": extracted.cs_id,
                    "checkout_url": extracted.long_url,
                    "short_link": extracted.long_url,
                    "processor_entity": extracted.processor_entity,
                    "account_email": meta.get("email") or "",
                    "account_id": meta.get("account_id") or "",
                    "country": short_country,
                    "currency": short_currency,
                    "checkout_country": short_country,
                    "checkout_currency": short_currency,
                    "entry_country": checkout_proxy_country,
                    "payment_proxy_country": update_proxy_country,
                    "proxy_mode": f"{checkout_proxy_country.lower()}_checkout_{update_proxy_country.lower()}_update",
                    "entry_proxy_pool_size": len(entry_pool),
                    "exit_proxy_pool_size": len(exit_pool),
                    "promo_requested": promo_requested,
                    "promo_applied": promo_applied,
                    "promo_campaign_used": (options.get("promo_campaign") or "plus-1-month-free") if promo_requested else "",
                    "amount_verification": verification,
                    "checkout_amount": extracted.amount_minor,
                    "amount_currency": extracted.amount_currency,
                    "checkout_device_id": extracted.device_id,
                    "checkout_chatgpt_session_id": extracted.chatgpt_session_id,
                    "checkout_user_agent": extracted.user_agent,
                    "stripe_publishable_key": extracted.publishable_key,
                    "extractor": "simon_short_link",
                }
                done_text = "菲律宾短链生成完成"
                if verification == "pending":
                    done_text += "（金额待页面复核）"
                self.update(job_id, percent=100, text=done_text, status="done", result=result)
                return

            if provider == "pix":
                self.update(job_id, percent=9, text="第 1/7 步：选择并检测代理")
                promotion_country, promotion_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                checkout_country, checkout_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                main_country, main_region = promotion_country, promotion_region
                stripe_country, stripe_region = checkout_country, checkout_region
                self.log(job_id, f"PIX 代理校验：Checkout代理池={checkout_country}/{checkout_region}，Promotion代理池={promotion_country}/{promotion_region}")
                if checkout_country != "BR" or promotion_country != "BR":
                    self.log(
                        job_id,
                        f"PIX 当前代理为 Checkout {checkout_country or '?'} + Promotion {promotion_country or '?'}；不限制国家，继续由上游判断支付方式",
                    )
                self.ensure_not_cancelled(job_id)
            elif provider == "momo":
                self.update(job_id, percent=9, text="第 1/7 步：选择并检测越南代理")
                promotion_country, promotion_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                checkout_country, checkout_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                main_country, main_region = promotion_country, promotion_region
                self.log(job_id, f"MoMo 代理校验：Checkout代理池={checkout_country}/{checkout_region}，Promotion代理池={promotion_country}/{promotion_region}")
                if checkout_country != "VN":
                    self.log(job_id, f"MoMo Checkout 代理当前为 {checkout_country or '?'}；继续由上游判断支付方式")
                country = options["country"] = options["checkout_country"] = "VN"
                options["currency"] = options["checkout_currency"] = "VND"
                self.ensure_not_cancelled(job_id)

            promo_requested = options["plan"] == "plus" and options.get("use_promo", False)
            if provider == "gcash":
                gcash_checkout_proxy = exit_proxy if options.get("named_proxy_pools") else entry_proxy
                checkout_country_hint = options.get("exit_proxy_country") if options.get("named_proxy_pools") else options.get("entry_proxy_country")
                main_country, main_region = proxy_country(gcash_checkout_proxy, checkout_country_hint)
                country = options["country"] = options["checkout_country"] = "PH"
                options["currency"] = options["checkout_currency"] = "PHP"
                self.update(job_id, percent=9, text="校验 GCash PH 单代理链路")
                self.log(job_id, f"GCash 路由：Checkout、优惠、taxes、confirm、start 统一使用 {main_country}/{main_region}，账单=PH/PHP")
                if main_country != "PH":
                    self.log(job_id, f"GCash Checkout 代理当前为 {main_country or '?'}；目标为 PH，继续由上游校验")
                self.ensure_not_cancelled(job_id)
            if provider == "gopay":
                self.update(job_id, percent=9, text="校验 GoPay 印尼支付代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                country = options["country"] = options["checkout_country"] = "ID"
                options["currency"] = options["checkout_currency"] = "IDR"
                self.log(
                    job_id,
                    f"GoPay 路由：Promotion={main_country}/{main_region}，"
                    f"Checkout={payment_country}/{payment_region}，账单=ID/IDR",
                )
                if payment_country != "ID":
                    self.log(job_id, f"GoPay Checkout 代理当前为 {payment_country or '?'}；目标为 ID，继续由上游校验")
                self.ensure_not_cancelled(job_id)
            if provider == "blik":
                self.update(job_id, percent=9, text="校验 BLIK 波兰支付代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                country = options["country"] = options["checkout_country"] = "PL"
                options["currency"] = options["checkout_currency"] = "PLN"
                self.log(
                    job_id,
                    f"BLIK 路由：Promotion={main_country}/{main_region}，"
                    f"Checkout={payment_country}/{payment_region}，账单=PL/PLN",
                )
                if payment_country != "PL" or main_country != "PL":
                    self.log(job_id, "BLIK 检测到用户指定的非默认代理国家；保留当前 Checkout/Promotion 路由，由上游判断支付方式")
                self.ensure_not_cancelled(job_id)
            if provider == "paypal":
                self.update(job_id, percent=9, text="第 1/7 步：校验 PayPal 优惠识别代理与支付代理")
                main_country, main_region = proxy_country(entry_proxy)
                exit_proxy, payment_geo, rejected_countries = select_paypal_exit_proxy(
                    exit_proxy,
                    exit_pool,
                    scan_limit=int(os.getenv("PAYPAL_PROXY_SCAN_LIMIT", "24") or 24),
                    expected_country=str(options.get("requested_paypal_country") or country),
                )
                payment_country = payment_geo.get("country") or ""
                payment_region = payment_geo.get("region") or ""
                if not payment_country:
                    raise RuntimeError("Checkout 代理池未检测到国家地区")
                if rejected_countries:
                    self.log(job_id, f"PayPal 已跳过不兼容地区：{'/'.join(rejected_countries[:8])}")
                detected_currency = str(payment_geo.get("currency") or "").upper()
                requested_country = str(options.get("requested_paypal_country") or country).upper()
                checkout_country, checkout_currency, currency_source = resolve_paypal_checkout_region(
                    requested_country,
                    payment_country,
                    detected_currency,
                    bool(options.get("force_paypal_de_fallback")),
                )
                country = checkout_country
                options["country"] = checkout_country
                options["currency"] = checkout_currency
                options["checkout_country"] = checkout_country
                options["checkout_currency"] = checkout_currency
                options["payment_proxy_country"] = payment_country
                self.log(
                    job_id,
                    f"PayPal Checkout代理池地区：{payment_country}/{payment_region}；"
                    f"Checkout={checkout_country}/{checkout_currency}（{currency_source}）",
                )
                if promo_requested and main_country not in {"TR", "JP"}:
                    self.log(job_id, f"PayPal 优惠识别代理当前为 {main_country or '?'}；不限制国家，继续尝试")
                self.ensure_not_cancelled(job_id)
            if provider == "upi":
                self.update(job_id, percent=9, text="第 1/7 步：校验 UPI 优惠识别代理与印度支付代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(job_id, f"UPI 代理校验：优惠识别={main_country}/{main_region}，UPI 支付={payment_country}/{payment_region}，账单=IN/INR")
                if promo_requested and main_country not in {"TR", "JP"}:
                    self.log(job_id, f"UPI 优惠识别代理当前为 {main_country or '?'}；不限制国家，继续尝试")
                if payment_country != "IN":
                    self.log(job_id, f"UPI 支付代理当前为 {payment_country or '?'}；不限制国家，继续由上游判断支付方式")
                self.ensure_not_cancelled(job_id)
            if provider == "ideal":
                self.update(job_id, percent=9, text="校验 iDEAL 荷兰支付代理")
                main_country, main_region = proxy_country(entry_proxy, options.get("entry_proxy_country"))
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(
                    job_id,
                    f"iDEAL 代理校验：Promotion代理池={main_country}/{main_region}，"
                    f"Checkout代理池={payment_country}/{payment_region}，账单=NL/EUR",
                )
                if payment_country != "NL":
                    raise RuntimeError(
                        f"iDEAL Checkout代理池出口为 {payment_country or '未知'}，需要 NL 荷兰出口"
                    )
                self.ensure_not_cancelled(job_id)
            if provider == "twint":
                self.update(job_id, percent=9, text="校验 TWINT 瑞士支付代理")
                payment_country, payment_region = proxy_country(exit_proxy, options.get("exit_proxy_country"))
                self.log(job_id, f"TWINT 代理校验：支付={payment_country}/{payment_region}，账单=CH/CHF")
                if payment_country != "CH":
                    raise RuntimeError(f"TWINT Checkout代理池出口为 {payment_country or '未知'}，需要 CH 瑞士出口")
                country = options["country"] = options["checkout_country"] = "CH"
                options["currency"] = options["checkout_currency"] = "CHF"
                self.ensure_not_cancelled(job_id)
            preflight = {}
            if promo_requested:
                transport_stage = "Promotion 优惠预检"
                preflight_route = "GCash PH Checkout 代理" if provider == "gcash" else "Promotion 代理池"
                self.update(job_id, percent=12, text=f"通过 {preflight_route}读取试用资格与活动标记")
                preflight = preflight_trial_eligibility(
                    token, meta.get("account_id") or "", entry_proxy, device_id, did,
                    lambda m: self.log(job_id, m),
                    coupon_fallback=provider == "gopay",
                )
                detected_campaign = promo_campaign_from_payload(preflight)
                if provider == "ideal":
                    if preflight.get("one_click_trial_eligible") is not True:
                        raise RuntimeError(
                            "IDEAL_TRIAL_NOT_ELIGIBLE: Promotion 代理未确认当前账号具备 Plus 0 元试用资格"
                        )
                    if not detected_campaign:
                        raise RuntimeError(
                            "IDEAL_TRIAL_CAMPAIGN_MISSING: Promotion 代理未返回可用的 Plus 试用活动"
                        )
                if preflight.get("one_click_trial_eligible") is True:
                    options["promo_marker_eligible"] = True
                if provider == "gopay" and preflight.get("is_coupon_from_query_param"):
                    options["promo_from_query_param"] = True
                if detected_campaign:
                    options["promo_campaign"] = detected_campaign
                    options["promo_campaign_verified"] = True
                    self.log(job_id, f"优惠预检已匹配账号活动：{detected_campaign}")
                self.ensure_not_cancelled(job_id)

            if provider == "upi":
                self.update(job_id, percent=22, text="准备 UPI 0810 Sentinel 提链流程")
                upi_billing = default_billing("IN", meta.get("email") or "")
                upi_address = upi_billing.get("address") or {}
                self.log(
                    job_id,
                    "UPI 印度账单：城市={}，州={}，邮编={}".format(
                        upi_address.get("city") or "-",
                        upi_address.get("state") or "-",
                        upi_address.get("postal_code") or "-",
                    ),
                )
                self.log(
                    job_id,
                    "UPI 新链路：Checkout、Stripe 与 approval 使用 Checkout代理池；"
                    + ("Promotion代理池已完成优惠资格预检" if promo_requested else "优惠已关闭，不校验资格且不限制金额为 0"),
                )
                upi_result = generate_upi_payment_link(
                    access_token=token,
                    checkout_proxy=exit_proxy,
                    provider_proxy=exit_proxy,
                    billing=upi_billing,
                    apply_promo=promo_requested,
                    promo_campaign=str(options.get("promo_campaign") or "plus-1-month-free"),
                    log=lambda message: self.log(job_id, message),
                    cancel_check=lambda: self.ensure_not_cancelled(job_id),
                )
                self.ensure_not_cancelled(job_id)
                payment_link = str(upi_result.get("checkout_url") or "")
                qr_images = generate_payment_qr_images(
                    payment_link, lambda message: self.log(job_id, message)
                )
                result: dict[str, Any] = {
                    "plan": options["plan"],
                    "account_email": meta.get("email") or "",
                    "account_id": meta.get("account_id") or "",
                    "country": "IN",
                    "currency": "INR",
                    "checkout_country": "IN",
                    "checkout_currency": "INR",
                    "entry_proxy_pool_size": len(entry_pool),
                    "exit_proxy_pool_size": len(exit_pool),
                    "proxy_mode": "named_checkout_promotion" if options.get("named_proxy_pools") else "dual_chain",
                    "promo_requested": promo_requested,
                    "promo_campaign_used": (
                        options.get("promo_campaign") or "plus-1-month-free"
                    ) if promo_requested else "",
                    "entry_trial_eligible": preflight.get("one_click_trial_eligible"),
                    "entry_country": str(main_country or "").upper(),
                    "payment_proxy_country": str(payment_country or "").upper(),
                }
                result.update(upi_result)
                result.update(qr_images)
                self.update(
                    job_id,
                    percent=100,
                    text="UPI 支付链接提取完成",
                    status="done",
                    result=result,
                )
                return

            self.update(job_id, percent=18, text="生成 Sentinel 校验")
            payload = checkout_payload(options, meta)
            if provider == "paypal":
                self.log(job_id, f"计划={options['plan']}，方式=paypal，账单={country}/{options['currency']}，PayPal订单={options.get('checkout_country')}/{options.get('checkout_currency')}")
            else:
                self.log(job_id, f"计划={options['plan']}，方式={provider}，地区={country}/{options['currency']}")
            stage2_text = "第 2/7 步：BR 创建 Checkout（首段不带优惠）" if provider == "pix" else (
                (f"第 2/7 步：使用 {country} 代理创建 PayPal Checkout"
                 + ("（原生携带优惠）" if options.get("promo_on_create") else "（稍后更新优惠）"))
                if provider == "paypal" and promo_requested else (
                    "第 2/7 步：使用 IN 代理创建 UPI Checkout" if provider == "upi" else (
                        "第 2/7 步：使用 VN 代理创建 MoMo Checkout" if provider == "momo" else (
                            "第 2/7 步：使用 PH 代理创建原生带优惠的 GCash Checkout"
                            if provider == "gcash" and promo_requested else (
                                "使用 ID 代理创建 GoPay Checkout" if provider == "gopay" else (
                                    "使用 Checkout代理池创建 PL/PLN BLIK Checkout" if provider == "blik" else "创建 OpenAI Checkout"
                                )
                            )
                        )
                    )
                )
            )
            self.update(job_id, percent=34, text=stage2_text)
            transport_stage = "OpenAI Checkout 创建"
            checkout_proxy = checkout_route_proxy(options, provider, entry_proxy, exit_proxy)
            if provider in {"pix", "momo"}:
                self.log(
                    job_id,
                    f"Stage1 Checkout、Stripe 和 approval 使用 Checkout代理池；优惠检查与更新使用 Promotion代理池"
                    + ("；本轮优惠随 Checkout 创建" if options.get("promo_on_create") else ""),
                )
            elif provider == "gcash":
                self.log(job_id, "GCash 设置：单个 PH Checkout 代理与同一 HTTP 会话贯穿创建、优惠、taxes、confirm 和 start")
            elif provider == "gopay":
                self.log(job_id, "GoPay 设置：Checkout代理池创建 ID/IDR Checkout 并贯穿 taxes、confirm 和 start；Promotion代理池负责试用检查与优惠更新")
            elif provider == "blik":
                self.log(job_id, "BLIK 设置：Checkout代理池创建 PL/PLN Checkout 并提交 taxes；Promotion代理池负责试用检查与优惠更新")
            elif provider == "paypal" and promo_requested:
                self.log(job_id, f"PayPal 设置：Promotion代理池用于优惠检查，Checkout代理池创建 {country}/{options['currency']} Checkout")
            elif provider == "upi":
                self.log(job_id, "UPI 设置：Promotion代理池用于优惠检查，Checkout代理池创建 IN/INR Checkout")
            elif provider == "ideal":
                self.log(job_id, "iDEAL 设置：Checkout代理池创建 NL/EUR Checkout 并贯穿 Stripe 支付处理，Promotion代理池负责优惠更新")
            elif provider == "twint":
                self.log(job_id, "TWINT 设置：Checkout代理池使用 CH 创建 CHF Checkout；Promotion代理池负责首月优惠更新")
            elif provider == "hosted" and options.get("named_proxy_pools"):
                self.log(job_id, "官方 Checkout 使用 Checkout代理池创建和读取支付页面，Promotion代理池负责试用检查与优惠更新")
            elif provider != "hosted":
                self.log(job_id, f"Checkout 将使用所选的 {country} 地区代理")
            if provider == "gopay":
                created, device_id, did = create_gopay_cs_live_checkout(
                    token,
                    payload,
                    checkout_proxy,
                    device_id,
                    did,
                    lambda message: self.log(job_id, message),
                    attempts=int(options.get("gopay_cs_live_attempts") or 10),
                    use_sen=bool(options.get("use_sen", True)),
                    use_so=bool(options.get("use_so", True)),
                    creation_budget=options.get("_gopay_creation_budget"),
                    cancel_check=lambda: self.ensure_not_cancelled(job_id),
                    proof_policy=ProofPolicy.strict_gopay(),
                )
            else:
                try:
                    created = create_checkout(
                        token,
                        payload,
                        checkout_proxy,
                        device_id,
                        did,
                        lambda m: self.log(job_id, m),
                        use_sen=(True if provider == "gcash" else bool(options.get("use_sen", True))),
                        use_so=(True if provider == "gcash" else bool(options.get("use_so", True))),
                        allow_sentinel_fallback=provider == "paypal",
                        diagnostic_label="MoMo" if provider == "momo" else "",
                    )
                except RuntimeError as exc:
                    # When a campaign is attached at creation, some OAICS
                    # deployments reject the whole request before returning a
                    # session. Convert that business response into the same
                    # inner-rebuild marker used by checkout/update so the next
                    # MoMo pass creates a fresh session with the late-update
                    # contract. Other providers and unrelated 400s retain
                    # their existing error handling.
                    if provider == "momo":
                        rebuild_error = momo_create_checkout_rebuild_error(
                            exc, bool(options.get("promo_on_create")),
                        )
                        if rebuild_error:
                            raise RuntimeError(rebuild_error) from exc
                    raise
            if provider == "gopay":
                if not isinstance(created, dict):
                    raise PaymentFlowError(
                        "CHECKOUT_RESPONSE_INVALID",
                        "GoPay Checkout creation did not return a result object",
                        phase="checkout",
                        retryable=True,
                        rebuild_checkout=True,
                    )
                created_http = created.get("http")
                created_data = created.get("data")
                created_context = created.get("client_context")
                if created_http is None or not isinstance(created_data, dict):
                    raise PaymentFlowError(
                        "CHECKOUT_RESPONSE_INVALID",
                        "GoPay Checkout creation did not return its HTTP session and response object",
                        phase="checkout",
                        retryable=True,
                        rebuild_checkout=True,
                    )
                validate_bound_checkout_context(
                    created_context if isinstance(created_context, CheckoutClientContext) else None,
                    created_http,
                    expected_provider="gopay",
                    proxy=checkout_proxy,
                    device_id=device_id,
                    did=did,
                    phase="checkout",
                )
            http_sessions.track((created or {}).get("http"))
            self.ensure_not_cancelled(job_id)
            self.update(job_id, percent=44, text="Checkout 创建完成，正在准备支付方式")
            checkout_data = created["data"]
            chatgpt_http = created["http"]
            client_context = created.get("client_context")
            if provider == "gopay" and not isinstance(client_context, CheckoutClientContext):
                raise PaymentFlowError(
                    "CHECKOUT_CLIENT_CONTEXT_REQUIRED",
                    "GoPay Checkout creation did not return its bound client context",
                    phase="checkout",
                )
            stage1_campaign = promo_campaign_from_payload(checkout_data)
            if checkout_data.get("one_click_trial_eligible") is True:
                options["promo_marker_eligible"] = True
            if stage1_campaign:
                options["promo_campaign"] = stage1_campaign
                options["promo_campaign_verified"] = True
                self.log(job_id, f"Checkout 已返回活动标识：{stage1_campaign}")
            provider_chatgpt_http = chatgpt_http
            promo_chatgpt_http = chatgpt_http
            promotion_proxy = promotion_route_proxy(options, provider, entry_proxy, exit_proxy)
            split_promotion_session = (
                provider in {"paypal", "upi", "ideal", "twint", "gopay", "blik"}
                or (
                    options.get("named_proxy_pools")
                    and promo_requested
                    and provider != "gcash"
                )
            )
            if momo_reuses_checkout_http_session(provider, checkout_proxy, promotion_proxy):
                split_promotion_session = False
                self.log(
                    job_id,
                    "MoMo Promotion 与 Checkout 命中同一路由；复用同一 HTTP 会话、Cookie 与设备身份",
                )
            if split_promotion_session:
                promo_chatgpt_http = http_sessions.track(sc.build_http(promotion_proxy))
                try:
                    promo_chatgpt_http.cookies.set("oai-did", did, domain="chatgpt.com")
                    for cookie_name, cookie_value in chatgpt_http.cookies.get_dict().items():
                        promo_chatgpt_http.cookies.set(cookie_name, cookie_value, domain="chatgpt.com")
                    promo_chatgpt_http.get(
                        "https://chatgpt.com/api/auth/csrf",
                        headers={"User-Agent": sc.CHROME_UA, "Accept": "application/json,text/plain,*/*"},
                        timeout=20,
                    )
                except Exception as exc:
                    self.log(job_id, f"{provider.upper()} 优惠线路暖身提示：{type(exc).__name__}")
                if provider == "paypal":
                    self.log(job_id, f"PayPal 支付处理使用 Checkout代理池（{country}），优惠更新使用 Promotion代理池")
                elif provider == "upi":
                    self.log(job_id, "UPI 支付处理使用 Checkout代理池（IN），优惠更新使用 Promotion代理池")
                elif provider == "ideal":
                    self.log(job_id, "iDEAL 优惠更新使用 Promotion代理池，NL/EUR Checkout 与 Stripe 使用 Checkout代理池")
                elif provider == "twint":
                    self.log(job_id, "TWINT 支付处理使用 Checkout代理池（CH/CHF），优惠更新使用 Promotion代理池")
                elif provider == "gopay":
                    self.log(job_id, "GoPay Checkout、taxes、confirm 与 start 使用 Checkout代理池（ID/IDR），优惠更新使用 Promotion代理池")
                elif provider == "blik":
                    self.log(job_id, "BLIK Checkout、taxes 与支付页面读取使用 Checkout代理池，优惠更新使用 Promotion代理池")
                elif provider in {"pix", "momo", "hosted"}:
                    self.log(job_id, f"{provider.upper()} 优惠更新使用 Promotion代理池，Checkout 与支付处理使用 Checkout代理池")
            session_id = checkout_data.get("checkout_session_id") or ""
            if not session_id and provider != "hosted":
                raise RuntimeError("Checkout 未返回 Stripe Session ID")
            actual_checkout_kind = ""
            if provider in {"paypal", "gopay", "momo"}:
                actual_checkout_kind = session_checkout_kind(session_id)
            if provider == "momo":
                actual_label = {
                    "oaics": "OAICS",
                    "cs_live": "CS Live",
                    "cs_test": "CS Test",
                }.get(actual_checkout_kind, "未知")
                self.log(job_id, f"MoMo 实际 Checkout 类型：{actual_label}")
            if provider == "gopay":
                actual_label = {
                    "oaics": "OAICS",
                    "cs_live": "CS Live",
                    "cs_test": "CS Test",
                }.get(actual_checkout_kind, "未知")
                self.log(job_id, f"GoPay 实际 Checkout 类型：{actual_label}")
            if provider == "paypal":
                actual_mode, mode_mismatch = reconcile_checkout_mode(paypal_mode, actual_checkout_kind)
                if actual_checkout_kind == "unknown":
                    raise RuntimeError("PAYPAL_CHECKOUT_TYPE_UNKNOWN: Checkout 会话未返回可识别的 OAICS/CS 类型")
                if mode_mismatch:
                    expected_label = "OAICS" if paypal_mode == "oaics" else "CS Live"
                    actual_label = "OAICS" if actual_checkout_kind == "oaics" else "CS Live"
                    self.log(
                        job_id,
                        f"PayPal Checkout 类型与预检不一致：预检={expected_label}，实际={actual_label}；"
                        f"自动切换为{actual_label}提链分支继续执行",
                    )
                paypal_mode = actual_mode
                options["paypal_checkout_mode"] = actual_mode
                options["oaics_paypal"] = actual_checkout_kind == "oaics"
                actual_label = {
                    "oaics": "OAICS",
                    "cs_live": "CS Live",
                    "cs_test": "CS Test",
                }.get(actual_checkout_kind, "未知")
                self.log(job_id, f"PayPal 实际 Checkout 类型：{actual_label}；分支已确认")
            if self.cancelled(job_id):
                raise InterruptedError("任务已停止")

            result: dict[str, Any] = {
                "plan": options["plan"],
                "link_type": provider,
                "checkout_session_id": session_id,
                "checkout_url": checkout_data.get("checkout_url") or "",
                "account_email": meta.get("email") or "",
                "account_id": meta.get("account_id") or "",
                "country": country,
                "currency": options["currency"],
                "checkout_country": options.get("checkout_country") or country,
                "checkout_currency": options.get("checkout_currency") or options["currency"],
                "entry_proxy_pool_size": len(entry_pool),
                "exit_proxy_pool_size": len(exit_pool) if options.get("named_proxy_pools") or provider not in {"hosted", "pix", "momo"} else 0,
                "proxy_mode": ("named_checkout_promotion" if options.get("named_proxy_pools") else ("us_checkout_promo_update" if provider == "gcash" else ("single_chain" if provider in {"pix", "momo"} else ("entry_only" if provider == "hosted" else "dual_chain")))),
                "promo_requested": promo_requested,
                "promo_applied": None,
                "promo_campaign_used": (
                    options.get("promo_campaign") or "plus-1-month-free"
                    if promo_requested
                    else ""
                ),
                "entry_trial_eligible": preflight.get("one_click_trial_eligible"),
                "checkout_trial_eligible": checkout_data.get("one_click_trial_eligible"),
                "entry_one_click_marker": preflight.get("one_click_trial_eligible"),
                "checkout_one_click_marker": checkout_data.get("one_click_trial_eligible"),
                "promotion_eligibility_decided_by": "checkout_approve",
                "promotion_source": str(preflight.get("promotion_source") or ""),
                "promotion_coupon_state": str(preflight.get("coupon_state") or ""),
                "promo_from_query_param": bool(options.get("promo_from_query_param")),
                "entry_country": str(locals().get("main_country") or "").upper(),
                "promo_country": str(options.get("promo_country") or "").upper(),
                "payment_proxy_country": str(options.get("payment_proxy_country") or locals().get("payment_country") or "").upper(),
            }
            if provider in {"paypal", "gopay", "momo"}:
                result["checkout_kind"] = actual_checkout_kind
            if promo_requested:
                checkout_trial = checkout_data.get("one_click_trial_eligible")
                self.log(
                    job_id,
                    "支付标记（仅供诊断）：Promotion one_click={}，Checkout Stage1 one_click={}".format(
                        preflight.get("one_click_trial_eligible"), checkout_trial
                    ),
                )
                if checkout_trial is False:
                    self.log(
                        job_id,
                        "Stage1 one_click 标记为 false；该字段不代表活动资格，继续以金额与 approval 结果判定",
                    )
            if str(session_id).startswith("oaics_"):
                custom_processor = (
                    str(checkout_data.get("processor_entity") or "").strip()
                    or ("openai_llc" if country == "US" else "openai_ie")
                )
                if provider == "blik":
                    self.update(job_id, percent=58, text="正在读取 OAICS BLIK 支付方式")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=6,
                        required_provider="blik",
                        preserve_payment_methods_from=checkout_data,
                    )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or "PLN"
                    if promo_requested and custom_amount != 0:
                        self.update(job_id, percent=66, text="正在为 OAICS BLIK 应用优惠")
                        updated = update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda message: self.log(job_id, message), device_id=device_id,
                        )
                        updated_amount = custom_checkout_amount_minor(updated)
                        if updated_amount is not None:
                            custom_amount = updated_amount
                        custom_currency = custom_checkout_currency(updated) or custom_currency
                        custom_state = fetch_custom_checkout_session_with_retry(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                            log=lambda message: self.log(job_id, message), attempts=6,
                            required_provider="blik",
                            preserve_payment_methods_from=custom_state,
                        )
                        refreshed_amount = custom_checkout_amount_minor(custom_state)
                        if refreshed_amount is not None:
                            custom_amount = refreshed_amount
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency

                    blik_billing = default_billing("PL", meta.get("email") or "")
                    self.update(job_id, percent=74, text="正在提交 PL BLIK 账单信息")
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        blik_billing, custom_currency, device_id,
                    )
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=6,
                        required_provider="blik",
                        preserve_payment_methods_from=custom_state,
                    )
                    refreshed_amount = custom_checkout_amount_minor(custom_state)
                    if refreshed_amount is None and tax_checkout:
                        refreshed_amount = custom_checkout_amount_minor(tax_checkout)
                    if refreshed_amount is not None:
                        custom_amount = refreshed_amount
                    custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    methods = custom_payment_methods_for(custom_state, "blik")
                    if not methods:
                        raise RuntimeError(
                            "BLIK_METHOD_UNAVAILABLE: PL Checkout 在 taxes 后未返回明确的 BLIK 支付方式"
                        )
                    if promo_requested and custom_amount != 0:
                        raise RuntimeError(
                            f"BLIK_ZERO_DUE_REQUIRED: BLIK 优惠未生效或金额未知：amount={custom_amount} {custom_currency}"
                        )
                    custom_url = str(checkout_data.get("checkout_url") or "").strip()
                    if not is_valid_oaics_blik_payment_url(custom_url, session_id):
                        custom_url = f"https://chatgpt.com/checkout/{custom_processor}/{session_id}"
                    if not is_valid_oaics_blik_payment_url(custom_url, session_id):
                        raise RuntimeError("BLIK_PAYMENT_LINK_INVALID: 无法生成当前 OAICS BLIK Checkout 的公共支付页面")
                    method_id = str(methods[0].get("id") or "")
                    qr_images = generate_payment_qr_images(custom_url, lambda message: self.log(job_id, message))
                    result.update({
                        "link_type": "blik",
                        "checkout_provider": "open_ai_oaics",
                        "checkout_ui_mode": "custom",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": method_id,
                        "payment_method_type": "blik",
                        "provider_redirect_url": custom_url,
                        "blik_payment_url": custom_url,
                        "short_link": custom_url,
                        "checkout_url": custom_url,
                        **qr_images,
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": "verified_zero" if custom_amount == 0 else "nonzero",
                        "promo_applied": custom_amount == 0 if promo_requested else None,
                        "expires_at": int(time.time()) + 1800,
                    })
                    self.log(job_id, "BLIK 支付方式已确认；支付页将由付款人输入银行 App 生成的动态码")
                    self.update(job_id, percent=100, text="BLIK 支付页面提取完成", status="done", result=result)
                    return
                if provider == "gopay":
                    self.update(job_id, percent=58, text="正在读取 GoPay 自定义支付方式")
                    stage1_gopay_method_id = custom_payment_method_id_for(checkout_data, "gopay")
                    if stage1_gopay_method_id:
                        self.log(job_id, "GoPay 已在 Checkout 创建响应中发布；将锁定该支付方式并贯穿优惠、taxes、confirm 和 start")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=6,
                        required_provider="gopay",
                        preserve_payment_methods_from=checkout_data,
                    )
                    custom_method_id = custom_payment_method_id_for(custom_state, "gopay")
                    if not custom_method_id:
                        raise RuntimeError(
                            "GOPAY_STRIPE_CHECKOUT_REQUIRED: 账户支付探测与当前 OAICS 属于不同协议；"
                            "OAICS 未发布 cpmt_gopay，需要更换代理重建 redirect/Stripe Checkout"
                            f"；OAICS实际返回={custom_payment_methods_diagnostic(custom_state)}"
                        )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or "IDR"
                    if (
                        promo_requested
                        and custom_amount is not None
                        and not is_gopay_promo_amount(custom_amount, custom_currency)
                    ):
                        self.update(job_id, percent=66, text="正在应用优惠并刷新 GoPay Checkout")
                        update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda message: self.log(job_id, message), device_id=device_id,
                            is_coupon_from_query_param=bool(options.get("promo_from_query_param")),
                        )
                        custom_state = fetch_custom_checkout_session_with_retry(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                            log=lambda message: self.log(job_id, message), attempts=6,
                            required_provider="gopay",
                            preserve_payment_methods_from=custom_state,
                        )
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency

                    gopay_billing = default_billing("ID", meta.get("email") or "")
                    gopay_address = gopay_billing.get("address") or {}
                    self.update(job_id, percent=72, text="正在提交 ID 账单地址")
                    self.log(
                        job_id,
                        "GoPay ID 账单：name={}，city={}，state={}，postal={}，source={}，place={}".format(
                            gopay_billing.get("name") or "-",
                            gopay_address.get("city") or "-",
                            gopay_address.get("state") or "-",
                            gopay_address.get("postal_code") or "-",
                            gopay_billing.get("_address_source") or "fallback",
                            gopay_billing.get("_place_name") or "-",
                        ),
                    )
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        gopay_billing, custom_currency, device_id,
                    )
                    self.log(job_id, "GoPay taxes 已提交，正在通过同一 Checkout 会话刷新支付方式")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=6,
                        required_provider="gopay",
                        preserve_payment_methods_from=custom_state,
                    )
                    refreshed_amount = custom_checkout_amount_minor(custom_state)
                    if refreshed_amount is None and tax_checkout:
                        refreshed_amount = custom_checkout_amount_minor(tax_checkout)
                    if refreshed_amount is not None:
                        custom_amount = refreshed_amount
                    custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    self.log(
                        job_id,
                        f"GoPay taxes 后 Checkout 应付金额：{custom_amount if custom_amount is not None else '?'} {custom_currency}",
                    )
                    gopay_promo_applied = is_gopay_promo_amount(custom_amount, custom_currency)
                    if promo_requested and not gopay_promo_applied:
                        raise RuntimeError(
                            "GOPAY_PROMO_AMOUNT_REQUIRED: GoPay 优惠未生效或金额未知；"
                            f"要求 0 <= amount < 50 IDR，实际 amount={custom_amount} {custom_currency}"
                        )

                    custom_method_id = custom_payment_method_id_for(custom_state, "gopay") or custom_method_id
                    if not custom_method_id:
                        raise RuntimeError(
                            "GOPAY_STRIPE_CHECKOUT_REQUIRED: 当前 OAICS 未发布 cpmt_gopay，"
                            "需要更换代理重建 redirect/Stripe Checkout"
                            f"；OAICS实际返回={custom_payment_methods_diagnostic(custom_state)}"
                        )
                    self.update(job_id, percent=78, text="正在确认 GoPay 支付方式")
                    confirmed = confirm_custom_checkout_method_with_retry(
                        chatgpt_http, token, session_id, custom_processor,
                        custom_method_id, checkout_proxy, device_id, did,
                        use_sen=True,
                        use_so=True,
                        method_name="GoPay",
                        allow_sentinel_fallback=False,
                        max_retries=0,
                        rebuild_on_blocked=True,
                        client_context=client_context,
                        proof_policy=ProofPolicy.strict_gopay(),
                        log=lambda message: self.log(job_id, message),
                    )
                    self.update(job_id, percent=90, text="正在生成 GoPay Midtrans 跳转链接")
                    started = start_custom_checkout_method(
                        chatgpt_http, token, session_id, custom_processor,
                        custom_method_id, device_id,
                        method_name="GoPay",
                        proxy=checkout_proxy,
                        did=did,
                        client_context=client_context,
                    )
                    action = started.get("next_action") or {}
                    normalized_gopay = require_gopay_midtrans_result({
                        **confirmed,
                        "started": started,
                    })
                    redirect_url = normalized_gopay["gopay_midtrans_url"]
                    result.update({
                        "link_type": "gopay",
                        "checkout_provider": "open_ai_oaics",
                        "checkout_ui_mode": "custom",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": custom_method_id,
                        "payment_method_type": str(action.get("paymentMethodType") or "gopay"),
                        "provider_redirect_url": redirect_url,
                        "gopay_midtrans_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "verification_url": str(confirmed.get("confirm_return_url") or ""),
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": (
                            "verified_discount_range" if gopay_promo_applied
                            else ("pending" if custom_amount is None else "nonzero")
                        ),
                        "promo_applied": gopay_promo_applied if promo_requested else None,
                        "expires_at": int(time.time()) + 1800,
                    })
                    self.update(job_id, percent=100, text="GoPay Midtrans 跳转链接生成完成", status="done", result=result)
                    return
                if provider == "gcash":
                    self.update(job_id, percent=58, text="正在读取 GCash 自定义支付方式")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=4,
                    )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or "PHP"
                    if promo_requested and custom_amount not in {None, 0}:
                        self.update(job_id, percent=66, text="正在应用优惠并刷新 GCash Checkout")
                        update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda m: self.log(job_id, m), device_id=device_id,
                        )
                        custom_state = fetch_custom_checkout_session(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                        )
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    gcash_billing = default_billing("PH", meta.get("email") or "", real_random=True)
                    gcash_address = gcash_billing.get("address") or {}
                    self.update(job_id, percent=72, text="正在提交 PH 账单地址")
                    self.log(
                        job_id,
                        "GCash PH 账单：name={}，city={}，state={}，postal={}，source={}，place={}".format(
                            gcash_billing.get("name") or "-",
                            gcash_address.get("city") or "-",
                            gcash_address.get("state") or "-",
                            gcash_address.get("postal_code") or "-",
                            gcash_billing.get("_address_source") or "fallback",
                            gcash_billing.get("_place_name") or "-",
                        ),
                    )
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        gcash_billing, custom_currency, device_id,
                    )
                    self.log(job_id, "GCash taxes 已提交，正在通过同一 PH 会话读取最新支付方式")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=4,
                        required_provider="gcash",
                    )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    if custom_amount is None and tax_checkout:
                        custom_amount = custom_checkout_amount_minor(tax_checkout)
                    custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    self.log(
                        job_id,
                        f"GCash taxes 后 Checkout 应付金额：{custom_amount if custom_amount is not None else '?'} {custom_currency}",
                    )
                    if promo_requested and custom_amount not in {None, 0}:
                        raise RuntimeError(
                            f"GCASH_ZERO_DUE_REQUIRED: GCash 优惠未生效，confirm 前应付金额为 {custom_amount} {custom_currency}"
                        )
                    custom_method_id = custom_payment_method_id_for(custom_state, "gcash")
                    if not custom_method_id:
                        raise RuntimeError("GCASH_METHOD_UNAVAILABLE: 当前 PH Checkout 尚未返回 GCash 支付方式，将更换代理重建")
                    self.update(job_id, percent=76, text="正在确认 GCash 支付方式")
                    try:
                        confirmed = confirm_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            custom_method_id, checkout_proxy, device_id, did,
                            use_sen=True, use_so=True, method_name="GCash",
                        )
                    except RuntimeError as confirm_error:
                        if "CUSTOM_CONFIRM_BLOCKED" not in str(confirm_error):
                            raise
                        self.log(job_id, "GCash confirm 首次被拦截，正在更新 SEN/SO 后重试")
                        time.sleep(1.2)
                        confirmed = confirm_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            custom_method_id, checkout_proxy, device_id, did,
                            use_sen=True, use_so=True, method_name="GCash",
                        )
                    self.update(job_id, percent=88, text="正在生成 GCash 跳转链接")
                    started = start_custom_checkout_method(
                        chatgpt_http, token, session_id, custom_processor,
                        custom_method_id, device_id,
                    )
                    action = started.get("next_action") or {}
                    adyen_redirect_url = str(action.get("url") or "").strip()
                    authorization_url = gcash_authorization_url(confirmed, started)
                    redirect_url = gcash_payment_url(confirmed, started)
                    if not authorization_url:
                        authorization_url = resolve_gcash_authorization_url(
                            chatgpt_http, adyen_redirect_url,
                            lambda message: self.log(job_id, message),
                        )
                    if authorization_url:
                        redirect_url = authorization_url
                    qr_payload = fetch_gcash_public_qr(
                        authorization_url,
                        checkout_proxy,
                        lambda message: self.log(job_id, message),
                    ) if authorization_url else {}
                    if qr_payload:
                        confirmed = {**confirmed, "gcash_qr_consult": qr_payload}
                    qr_data = gcash_qr_data(confirmed, started)
                    qr_data = qr_data or gcash_qr_data(qr_payload)
                    qr_expires_at = gcash_qr_expires_at(qr_payload, confirmed, started) if qr_data else 0
                    authorization_params = gcash_authorization_params(confirmed, started)
                    if not qr_data:
                        self.log(job_id, "GCash 上游未返回免登录二维码，仅保存 m.gcash.com 授权链接；不会将登录页链接编码为二维码")
                    if not redirect_url:
                        raise RuntimeError(
                            "GCASH_PAYMENT_LINK_MISSING: GCash 未返回 m.gcash.com 授权链接或有效 Adyen 跳转链接"
                        )
                    result.update({
                        "link_type": "gcash",
                        "checkout_provider": "open_ai",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": custom_method_id,
                        "payment_method_type": str(action.get("paymentMethodType") or "gcash"),
                        "provider_redirect_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "gcash_authorization_url": authorization_url,
                        "gcash_net_auth_id": authorization_params["net_auth_id"],
                        "gcash_client_id": authorization_params["client_id"],
                        # Do not synthesize a QR from the m.gcash.com login URL.
                        "qr_data": qr_data,
                        "qr_status": "ready" if qr_data else "unavailable",
                        "qr_expires_at": qr_expires_at,
                        "payment_status": "waiting_callback",
                        "adyen_redirect_url": adyen_redirect_url if is_valid_gcash_adyen_redirect_url(adyen_redirect_url) else "",
                        "verification_url": str(confirmed.get("confirm_return_url") or ""),
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": (
                            "verified_zero" if custom_amount == 0
                            else ("pending" if custom_amount is None else "nonzero")
                        ),
                        "promo_applied": (custom_amount == 0) if promo_requested else None,
                        "expires_at": int(time.time()) + 1800,
                    })
                    if promo_requested and custom_amount != 0:
                        raise RuntimeError(
                            f"GCASH_ZERO_DUE_REQUIRED: GCash 优惠未生效或金额未知：amount={custom_amount} {custom_currency}"
                        )
                    order_fields = self.register_gcash_order(
                        result,
                        {
                            "http": chatgpt_http,
                            "token": token,
                            "job_id": job_id,
                            "checkout_session_id": session_id,
                            "processor_entity": custom_processor,
                            "custom_payment_method_id": custom_method_id,
                            "device_id": device_id,
                            "did": did,
                            "proxy": checkout_proxy,
                            "log": lambda message: self.log(job_id, message),
                        },
                    )
                    http_sessions.release(chatgpt_http)
                    result.update(order_fields)
                    self.update(job_id, percent=100, text="GCash 跳转链接生成完成", status="done", result=result)
                    return
                if provider == "paypal":
                    self.update(job_id, percent=58, text="正在读取 OAICS PayPal 支付方式")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message),
                        require_paypal=True,
                    )
                    initial_methods = custom_state.get("custom_payment_methods") or []
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or options["currency"]
                    if promo_requested and custom_amount not in {None, 0}:
                        self.update(job_id, percent=66, text="正在为 OAICS PayPal 应用优惠")
                        update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda m: self.log(job_id, m), device_id=device_id,
                        )
                        custom_state = fetch_custom_checkout_session_with_retry(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                            log=lambda message: self.log(job_id, message),
                            require_paypal=True,
                        )
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    paypal_billing = default_billing(
                        country, meta.get("email") or "", geo=payment_geo,
                        real_random=(country in ROTATING_PAYPAL_ADDRESS_COUNTRIES),
                    )
                    self.update(job_id, percent=72, text="正在提交 OAICS PayPal 账单")
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        paypal_billing, custom_currency, device_id,
                    )
                    if tax_checkout:
                        custom_state = tax_checkout
                    else:
                        custom_state = fetch_custom_checkout_session_with_retry(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                            log=lambda message: self.log(job_id, message),
                            require_paypal=True,
                        )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    methods = list(custom_state.get("custom_payment_methods") or [])
                    if not methods:
                        methods = list(initial_methods)
                    methods = [item for item in methods if str((item or {}).get("id") or "").startswith("cpmt_")]
                    methods.sort(key=lambda item: 0 if "paypal" in json.dumps(item, ensure_ascii=False).lower() else 1)
                    if not methods:
                        raise RuntimeError("OAICS_PAYPAL_METHOD_UNAVAILABLE: OAICS Checkout 未返回 PayPal 自定义支付方式")
                    selected_method_id = ""
                    confirmed = {}
                    started = {}
                    redirect_url = ""
                    for method in methods:
                        method_id = str(method.get("id") or "")
                        try:
                            confirmed = confirm_custom_checkout_method(
                                chatgpt_http, token, session_id, custom_processor,
                                method_id, exit_proxy, device_id, did,
                                use_sen=bool(options.get("use_sen", True)),
                                use_so=bool(options.get("use_so", True)),
                                method_name="PayPal",
                                allow_sentinel_fallback=True,
                                log=lambda message: self.log(job_id, message),
                            )
                        except RuntimeError as confirm_error:
                            if "CUSTOM_CONFIRM_BLOCKED" not in str(confirm_error):
                                raise
                            self.log(job_id, "OAICS PayPal confirm 首次被拦截，更新 SEN/SO 后重试")
                            confirmed = confirm_custom_checkout_method(
                                chatgpt_http, token, session_id, custom_processor,
                                method_id, exit_proxy, device_id, did,
                                use_sen=True, use_so=True, method_name="PayPal",
                                allow_sentinel_fallback=True,
                                log=lambda message: self.log(job_id, message),
                            )
                        started = start_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            method_id, device_id,
                        )
                        action = started.get("next_action") or {}
                        candidate_url = str(action.get("url") or "").strip()
                        candidate_type = str(action.get("paymentMethodType") or "").lower()
                        if "paypal" in candidate_type or "paypal" in candidate_url.lower():
                            selected_method_id = method_id
                            redirect_url = candidate_url
                            break
                        self.log(job_id, f"OAICS 自定义支付 {method_id[:12]} 不是 PayPal，继续检查")
                    if not redirect_url:
                        raise RuntimeError("OAICS_PAYPAL_REDIRECT_MISSING: 自定义支付方式未返回 PayPal 跳转")
                    result.update({
                        "link_type": "paypal",
                        "checkout_provider": "open_ai_oaics",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": selected_method_id,
                        "payment_method_type": "paypal",
                        "paypal_link": redirect_url,
                        "provider_redirect_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "verification_url": str(confirmed.get("confirm_return_url") or ""),
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": "verified_zero" if custom_amount == 0 else ("pending" if custom_amount is None else "nonzero"),
                        "promo_applied": ((custom_amount == 0) if promo_requested and custom_amount is not None else None),
                        "oaics_paypal": True,
                        "expires_at": int(time.time()) + 1800,
                    })
                    if promo_requested and custom_amount not in {None, 0}:
                        raise RuntimeError(f"OAICS PayPal 优惠未生效：amount={custom_amount} {custom_currency}")
                    self.update(job_id, percent=100, text="OAICS PayPal 跳转链接生成完成", status="done", result=result)
                    return
                if provider == "ideal":
                    self.update(job_id, percent=58, text="正在读取 OAICS iDEAL 自定义支付方式")
                    custom_state = fetch_custom_checkout_session_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id,
                        log=lambda message: self.log(job_id, message), attempts=6,
                    )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    custom_currency = custom_checkout_currency(custom_state) or options["currency"]
                    if promo_requested and custom_amount != 0:
                        self.update(job_id, percent=66, text="正在为 OAICS iDEAL 应用优惠")
                        update_checkout_promo(
                            promo_chatgpt_http, token, session_id, custom_processor,
                            options.get("promo_campaign") or "plus-1-month-free",
                            lambda m: self.log(job_id, m), device_id=device_id,
                        )
                        custom_state = fetch_custom_checkout_session_with_retry(
                            chatgpt_http, token, session_id, custom_processor, device_id,
                            log=lambda message: self.log(job_id, message), attempts=6,
                        )
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    self.update(job_id, percent=72, text="正在提交 NL iDEAL 账单信息")
                    ideal_billing = default_billing("NL", meta.get("email") or "")
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        ideal_billing, custom_currency, device_id,
                    )
                    if tax_checkout:
                        custom_state = tax_checkout
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    if promo_requested and custom_amount != 0:
                        raise RuntimeError(
                            f"IDEAL_ZERO_DUE_REQUIRED: OAICS iDEAL 优惠未使金额归零：amount={custom_amount} {custom_currency}"
                        )
                    methods = custom_payment_methods_for(custom_state, "ideal")
                    if not methods:
                        raise RuntimeError(
                            "OAICS_IDEAL_METHOD_UNAVAILABLE: OAICS Checkout 未返回可确认的 iDEAL 自定义支付方式"
                        )
                    selected_method_id = ""
                    redirect_url = ""
                    confirmed: dict[str, Any] = {}
                    for method in methods:
                        method_id = str(method.get("id") or "")
                        try:
                            confirmed = confirm_custom_checkout_method(
                                chatgpt_http, token, session_id, custom_processor,
                                method_id, exit_proxy, device_id, did,
                                use_sen=bool(options.get("use_sen", True)),
                                use_so=bool(options.get("use_so", True)),
                                method_name="iDEAL",
                                log=lambda message: self.log(job_id, message),
                            )
                        except RuntimeError as confirm_error:
                            if "CUSTOM_CONFIRM_BLOCKED" not in str(confirm_error):
                                raise
                            self.log(job_id, "OAICS iDEAL confirm 首次被拦截，正在更新 SEN/SO 后重试")
                            confirmed = confirm_custom_checkout_method(
                                chatgpt_http, token, session_id, custom_processor,
                                method_id, exit_proxy, device_id, did,
                                use_sen=True, use_so=True,
                                method_name="iDEAL",
                                log=lambda message: self.log(job_id, message),
                            )
                        self.update(job_id, percent=88, text="正在提交 OAICS iDEAL Checkout approval")
                        approve_checkout(
                            token,
                            session_id,
                            custom_processor,
                            exit_proxy,
                            device_id,
                            did,
                            http=chatgpt_http,
                            log=lambda message: self.log(job_id, message),
                        )
                        started = start_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            method_id, device_id, method_name="iDEAL",
                        )
                        action = started.get("next_action") or {}
                        candidate_url = canonical_ideal_payment_url(str(action.get("url") or "").strip())
                        if is_valid_ideal_payment_url(candidate_url):
                            selected_method_id = method_id
                            redirect_url = candidate_url
                            break
                        self.log(job_id, f"OAICS 自定义支付 {method_id[:12]} 未返回有效 pay.ideal.nl 链接，继续检查")
                    if not redirect_url:
                        raise RuntimeError(
                            "OAICS_IDEAL_REDIRECT_MISSING: iDEAL 自定义支付方式未返回带签名的 pay.ideal.nl 链接"
                        )
                    ideal_details = enrich_ideal_redirect(
                        chatgpt_http, redirect_url, lambda message: self.log(job_id, message),
                    )
                    redirect_url = canonical_ideal_payment_url(
                        str(ideal_details.get("provider_redirect_url") or redirect_url)
                    )
                    if not is_valid_ideal_payment_url(redirect_url):
                        raise RuntimeError(
                            "IDEAL_PAYMENT_LINK_INVALID: OAICS iDEAL 返回的链接未通过 pay.ideal.nl 签名校验"
                        )
                    result.update({
                        "link_type": "ideal",
                        "checkout_provider": "open_ai_oaics",
                        "checkout_ui_mode": "custom",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": selected_method_id,
                        "payment_method_type": "ideal",
                        "provider_redirect_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "verification_url": str(confirmed.get("confirm_return_url") or ""),
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": "verified_zero" if custom_amount == 0 else "nonzero",
                        "promo_applied": custom_amount == 0 if promo_requested else None,
                        "expires_at": int(time.time()) + 1800,
                        **ideal_details,
                    })
                    if promo_requested and custom_amount != 0:
                        raise RuntimeError(
                            f"IDEAL_ZERO_DUE_REQUIRED: OAICS iDEAL 最终金额不是 0：amount={custom_amount} {custom_currency}"
                        )
                    self.update(job_id, percent=100, text="OAICS iDEAL 签名支付链接生成完成", status="done", result=result)
                    return
                if provider == "momo":
                    self.update(job_id, percent=58, text="正在读取 OAICS 原生 MoMo 支付方式")
                    custom_state = fetch_oaics_native_checkout_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id, "momo",
                        preserve_from=checkout_data,
                        log=lambda message: self.log(job_id, message), attempts=6,
                    )
                    native_methods = oaics_stage_native_payment_method_types(
                        custom_state, checkout_data,
                    )
                    custom_method_id = oaics_stage_custom_payment_method_id(
                        custom_state, checkout_data, "momo",
                    )
                    custom_amount = custom_checkout_amount_minor(custom_state)
                    if custom_amount is None:
                        custom_amount = custom_checkout_amount_minor(checkout_data)
                    custom_currency = (
                        custom_checkout_currency(custom_state)
                        or custom_checkout_currency(checkout_data)
                        or "VND"
                    )
                    if (
                        promo_requested
                        and not options.get("promo_on_create")
                        and ("momo" in native_methods or custom_method_id)
                    ):
                        custom_state = fetch_momo_checkout_stable_with_retry(
                            chatgpt_http,
                            token,
                            session_id,
                            custom_processor,
                            device_id,
                            custom_state,
                            attempts=3,
                            delay_seconds=0.9,
                            log=lambda message: self.log(job_id, message),
                        )
                        native_methods = oaics_stage_native_payment_method_types(
                            custom_state, checkout_data,
                        )
                        custom_method_id = oaics_stage_custom_payment_method_id(
                            custom_state, checkout_data, "momo",
                        )
                        stable_amount = custom_checkout_amount_minor(custom_state)
                        if stable_amount is not None:
                            custom_amount = stable_amount
                        custom_currency = (
                            custom_checkout_currency(custom_state)
                            or custom_currency
                        )
                    promo_state: dict[str, Any] = {}
                    promo_action = momo_promotion_action(
                        native_methods,
                        custom_method_id,
                        custom_amount,
                        custom_currency,
                        promo_requested,
                        bool(options.get("promo_on_create")),
                    )
                    if (
                        promo_action == "rebuild_late"
                        and promo_requested
                        and options.get("promo_on_create")
                    ):
                        settled_state = fetch_momo_discounted_checkout_with_retry(
                            chatgpt_http,
                            token,
                            session_id,
                            custom_processor,
                            device_id,
                            custom_state,
                            attempts=3,
                            delay_seconds=0.9,
                            log=lambda message: self.log(job_id, message),
                        )
                        settled_amount = custom_checkout_amount_minor(settled_state)
                        settled_currency = (
                            custom_checkout_currency(settled_state)
                            or custom_currency
                        )
                        settled_methods = oaics_stage_native_payment_method_types(
                            settled_state,
                            custom_state,
                        )
                        settled_method_id = oaics_stage_custom_payment_method_id(
                            settled_state,
                            custom_state,
                            "momo",
                        )
                        if settled_amount is not None:
                            custom_amount = settled_amount
                        custom_currency = settled_currency
                        native_methods = settled_methods
                        custom_method_id = settled_method_id
                        if (
                            ("momo" in native_methods or custom_method_id)
                            and is_momo_promo_amount(custom_amount, custom_currency)
                        ):
                            custom_state = settled_state
                            promo_action = "already_discounted"
                            self.log(
                                job_id,
                                "MoMo 创建时优惠已在短轮询窗口内生效；继续使用当前全新 OAICS",
                            )
                    self.log(
                        job_id,
                        "MoMo OAICS 初始状态：available={}，amount={} {}，campaign={}，action={}".format(
                            native_methods or custom_payment_methods_diagnostic(custom_state),
                            custom_amount if custom_amount is not None else "?",
                            custom_currency,
                            options.get("promo_campaign") or "plus-1-month-free",
                            promo_action,
                        ),
                    )
                    if promo_action == "rebuild":
                        raise RuntimeError(
                            "MOMO_CHECKOUT_REBUILD_REQUIRED: OAICS 首次响应和详情均未发布 "
                            "MoMo 支付方式，不能对不兼容的支付方式集合提交优惠；"
                            "available={}；custom={}".format(
                                native_methods or [], custom_payment_methods_diagnostic(custom_state),
                            )
                        )
                    if promo_action == "already_discounted":
                        self.log(
                            job_id,
                            "MoMo Checkout 今日应付已处于 0..50 VND；"
                            "跳过可能破坏支付方式兼容性的重复 checkout/update",
                        )
                    elif promo_action == "rebuild_late":
                        raise RuntimeError(
                            "MOMO_CREATE_PROMOTION_NOT_APPLIED_REBUILD_REQUIRED: "
                            "创建时携带优惠的 OAICS 已发布 MoMo，但金额未降至 0..50 VND；"
                            "将丢弃本会话并改用发布支付方式后的优惠更新时序"
                        )
                    elif promo_action == "refresh":
                        self.update(job_id, percent=66, text="正在刷新 OAICS MoMo 优惠与支付资格")
                        try:
                            promo_state = update_checkout_promo(
                                promo_chatgpt_http, token, session_id, custom_processor,
                                options.get("promo_campaign") or "plus-1-month-free",
                                lambda message: self.log(job_id, message), device_id=device_id,
                                is_coupon_from_query_param=bool(options.get("promo_from_query_param")),
                            )
                        except RuntimeError as exc:
                            if momo_promotion_is_payment_method_incompatible(exc):
                                strategy = (
                                    "create_with_promo"
                                    if options.get("promo_on_create")
                                    else "late_update"
                                )
                                raise RuntimeError(
                                    "MOMO_PROMOTION_INCOMPATIBLE_REBUILD_REQUIRED: 当前 OAICS 已发布 "
                                    "MoMo，但服务端拒绝将该优惠应用到当前支付方式集合；"
                                    f"strategy={strategy}，available={native_methods or []}；"
                                    "将丢弃本会话并使用另一优惠时序完整重建"
                                ) from exc
                            raise
                        custom_state = fetch_oaics_native_checkout_with_retry(
                            chatgpt_http, token, session_id, custom_processor, device_id, "momo",
                            preserve_from=promo_state or custom_state,
                            log=lambda message: self.log(job_id, message), attempts=6,
                        )
                        promo_amount = custom_checkout_amount_minor(custom_state)
                        if promo_amount is None:
                            promo_amount = custom_checkout_amount_minor(promo_state)
                        if promo_amount is not None:
                            custom_amount = promo_amount
                        custom_currency = (
                            custom_checkout_currency(custom_state)
                            or custom_checkout_currency(promo_state)
                            or custom_currency
                        )
                        native_methods = oaics_stage_native_payment_method_types(
                            custom_state, promo_state,
                        )
                        custom_method_id = oaics_stage_custom_payment_method_id(
                            custom_state, promo_state, "momo",
                        )
                        self.log(
                            job_id,
                            "MoMo Promotion refresh：available={}，amount={} {}（允许 0..50 VND）".format(
                                native_methods or custom_payment_methods_diagnostic(custom_state),
                                custom_amount if custom_amount is not None else "?",
                                custom_currency,
                            ),
                        )
                        if "momo" not in native_methods and not custom_method_id:
                            raise RuntimeError(
                                "MOMO_METHOD_REMOVED_REBUILD_REQUIRED: 优惠更新后的最新 OAICS "
                                "状态已撤下 MoMo 支付方式；available={}；custom={}".format(
                                    native_methods or [],
                                    custom_payment_methods_diagnostic(custom_state),
                                )
                            )
                        if not is_momo_promo_amount(custom_amount, custom_currency):
                            raise RuntimeError(
                                "MOMO_PROMO_AMOUNT_REQUIRED: MoMo 优惠更新后金额仍未进入 "
                                f"0..50 VND；实际 amount={custom_amount} {custom_currency}"
                            )
                    momo_billing = default_billing("VN", meta.get("email") or "")
                    momo_address = momo_billing.get("address") or {}
                    self.update(job_id, percent=72, text="正在提交 VN MoMo 账单地址")
                    self.log(
                        job_id,
                        "MoMo VN 账单：name={}，city={}，state={}，postal={}，source={}，place={}".format(
                            momo_billing.get("name") or "-",
                            momo_address.get("city") or "-",
                            momo_address.get("state") or "-",
                            momo_address.get("postal_code") or "-",
                            momo_billing.get("_address_source") or "fallback",
                            momo_billing.get("_place_name") or "-",
                        ),
                    )
                    tax_checkout = submit_custom_checkout_taxes(
                        chatgpt_http, token, session_id, custom_processor,
                        momo_billing, custom_currency, device_id,
                    )
                    self.log(job_id, "MoMo taxes 已提交，正在通过同一 VN Checkout 刷新原生支付方式")
                    custom_state = fetch_oaics_native_checkout_with_retry(
                        chatgpt_http, token, session_id, custom_processor, device_id, "momo",
                        preserve_from=tax_checkout or custom_state,
                        log=lambda message: self.log(job_id, message), attempts=6,
                    )
                    refreshed_amount = custom_checkout_amount_minor(custom_state)
                    if refreshed_amount is None and tax_checkout:
                        refreshed_amount = custom_checkout_amount_minor(tax_checkout)
                    if refreshed_amount is not None:
                        custom_amount = refreshed_amount
                    custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    native_methods = oaics_stage_native_payment_method_types(
                        custom_state, tax_checkout,
                    )
                    custom_method_id = oaics_stage_custom_payment_method_id(
                        custom_state, tax_checkout, "momo",
                    )
                    if "momo" not in native_methods and not custom_method_id:
                        raise RuntimeError(
                            "MOMO_METHOD_REMOVED_REBUILD_REQUIRED: VN taxes 后的最新 OAICS "
                            "状态未发布 MoMo 支付方式；available={}；custom={}".format(
                                native_methods or [], custom_payment_methods_diagnostic(custom_state),
                            )
                        )
                    self.log(
                        job_id,
                        "MoMo tax refresh：available={}，amount={} {}（允许 0..50 VND）".format(
                            native_methods or custom_payment_methods_diagnostic(custom_state),
                            custom_amount if custom_amount is not None else "?",
                            custom_currency,
                        ),
                    )
                    momo_discounted = is_momo_promo_amount(custom_amount, custom_currency)
                    if promo_requested and not momo_discounted:
                        raise RuntimeError(
                            "MOMO_PROMO_AMOUNT_REQUIRED: MoMo 优惠未生效或金额未知；"
                            f"要求 0 <= amount <= 50 VND，实际 amount={custom_amount} {custom_currency}"
                        )

                    payment_method_id = ""
                    confirmation_kind = "custom_payment_method"
                    intent_result: dict[str, Any] = {}
                    approved: dict[str, Any] = {}
                    if "momo" in native_methods:
                        self.update(job_id, percent=80, text="正在创建 OAICS 原生 MoMo confirmation_token")
                        publishable_key = (
                            _nested_scalar(custom_state, ("publishable_key", "stripe_publishable_key", "public_key"))
                            or _nested_scalar(tax_checkout, ("publishable_key", "stripe_publishable_key", "public_key"))
                            or _nested_scalar(promo_state, ("publishable_key", "stripe_publishable_key", "public_key"))
                            or _nested_scalar(checkout_data, ("publishable_key", "stripe_publishable_key", "public_key"))
                        )
                        stripe_http = http_sessions.track(sc.build_http(checkout_proxy))
                        momo_ctx = {
                            "checkout_amount": custom_amount,
                            "currency": str(custom_currency or "VND").lower(),
                            "payment_method_types": native_methods,
                            "runtime_version": _nested_scalar(
                                custom_state, ("runtime_version", "stripe_js_version"),
                            ) or sc.DEFAULT_STRIPE_RUNTIME_VERSION,
                            "config_id": _nested_scalar(custom_state, ("config_id", "checkout_config_id")),
                            "elements_session_id": _nested_scalar(
                                custom_state, ("elements_session_id", "elementsSessionId"),
                            ),
                            "elements_session_config_id": _nested_scalar(
                                custom_state,
                                ("elements_session_config_id", "elementsSessionConfigId"),
                            ),
                        }
                        payment_method_id = create_provider_payment_method(
                            stripe_http, publishable_key, session_id, "momo",
                            sc.STRIPE_VERSION_FULL, momo_ctx, momo_billing,
                            lambda message: self.log(job_id, message),
                        )
                        confirmation_token_id = create_oaics_confirmation_token(
                            stripe_http, publishable_key, payment_method_id,
                        )
                        self.log(job_id, "OAICS 原生 MoMo PaymentMethod 与 confirmation_token 已创建")
                        self.update(job_id, percent=88, text="正在提交 OAICS 原生 MoMo checkout/confirm")
                        confirmed = confirm_oaics_native_payment_method(
                            chatgpt_http, token, session_id, custom_processor, "momo",
                            confirmation_token_id, checkout_proxy, device_id, did,
                            use_sen=bool(options.get("use_sen", True)),
                            use_so=bool(options.get("use_so", True)),
                            allow_sentinel_fallback=False,
                            log=lambda message: self.log(job_id, message),
                        )
                        confirmation_kind = "momo_oaics_checkout"
                        redirect_url = momo_authorization_url(confirmed)
                        if not redirect_url:
                            self.log(job_id, "OAICS MoMo confirm 尚无 redirect action，继续确认返回的 Stripe Intent")
                            intent_result = confirm_oaics_momo_intent(
                                stripe_http, publishable_key, payment_method_id,
                                confirmed, session_id, custom_processor,
                            )
                            redirect_url = momo_authorization_url(intent_result, confirmed)
                        if not redirect_url:
                            self.log(job_id, "OAICS MoMo Intent 尚无 redirect action，提交 approval 后轮询")
                            approved = approve_checkout(
                                token, session_id, custom_processor, checkout_proxy, device_id, did,
                                http=chatgpt_http,
                                log=lambda message: self.log(job_id, message),
                                allow_sentinel_fallback=False,
                            )
                            redirect_url = momo_authorization_url(approved, intent_result, confirmed)
                            if not redirect_url:
                                polled_intent = poll_oaics_momo_intent(
                                    stripe_http, publishable_key,
                                    approved, intent_result, confirmed,
                                )
                                redirect_url = momo_authorization_url(
                                    polled_intent, approved, intent_result, confirmed,
                                )
                    else:
                        self.log(job_id, "OAICS 未发布原生 momo，回退已有 cpmt_* MoMo 协议")
                        self.update(job_id, percent=80, text="正在确认 OAICS 自定义 MoMo 支付方式")
                        confirmed = confirm_custom_checkout_method_with_retry(
                            chatgpt_http, token, session_id, custom_processor,
                            custom_method_id, checkout_proxy, device_id, did,
                            use_sen=bool(options.get("use_sen", True)),
                            use_so=bool(options.get("use_so", True)),
                            method_name="MoMo",
                            allow_sentinel_fallback=False,
                            max_retries=int(options.get("momo_confirm_retries") or 2),
                            rebuild_on_blocked=True,
                            log=lambda message: self.log(job_id, message),
                        )
                        started = start_custom_checkout_method(
                            chatgpt_http, token, session_id, custom_processor,
                            custom_method_id, device_id, method_name="MoMo",
                        )
                        redirect_url = momo_authorization_url(confirmed, started)
                    if not redirect_url:
                        raise RuntimeError(
                            "MOMO_REDIRECT_MISSING: MoMo OAICS confirmation_token/confirm 未返回有效的 "
                            "pm-redirects.stripe.com 授权链接"
                        )
                    result.update({
                        "link_type": "momo",
                        "checkout_provider": "open_ai_oaics",
                        "checkout_ui_mode": "custom",
                        "processor_entity": custom_processor,
                        "custom_payment_method_id": custom_method_id,
                        "payment_method_id": payment_method_id,
                        "payment_method_type": "momo",
                        "generation_kind": confirmation_kind,
                        "cs_count": 1,
                        "stripe_redirect_url": redirect_url,
                        "provider_redirect_url": redirect_url,
                        "long_url": redirect_url,
                        "short_link": redirect_url,
                        "checkout_url": redirect_url,
                        "verification_url": str(confirmed.get("confirm_return_url") or ""),
                        "checkout_amount": custom_amount,
                        "amount_currency": custom_currency,
                        "amount_verification": "verified_discounted" if momo_discounted else "nonzero",
                        "promo_applied": momo_discounted if promo_requested else None,
                        "expires_at": int(time.time()) + 600,
                    })
                    self.log(job_id, "OAICS 原生 MoMo 已返回 Stripe 授权长链；打开该页面后由上游展示支付二维码")
                    self.update(job_id, percent=100, text="MoMo Stripe 授权链接生成完成", status="done", result=result)
                    return
                if provider != "hosted":
                    raise RuntimeError(
                        f"CUSTOM_CHECKOUT_REBUILD_REQUIRED: received {session_id}; "
                        f"{provider} requires a Stripe cs_* checkout"
                    )
                custom_processor = (
                    str(checkout_data.get("processor_entity") or "").strip()
                    or ("openai_llc" if country == "US" else "openai_ie")
                )
                custom_url = (
                    str(checkout_data.get("checkout_url") or "").strip()
                    or f"https://chatgpt.com/checkout/{custom_processor}/{session_id}"
                )
                custom_update: dict[str, Any] = {}
                if promo_requested:
                    self.update(job_id, percent=68, text="正在为 OAICS Checkout 应用优惠")
                    custom_update = update_checkout_promo(
                        promo_chatgpt_http,
                        token,
                        session_id,
                        custom_processor,
                        options.get("promo_campaign") or "plus-1-month-free",
                        lambda m: self.log(job_id, m),
                        device_id=device_id,
                    )
                custom_amount = custom_checkout_amount_minor(custom_update)
                custom_currency = custom_checkout_currency(custom_update) or options["currency"]
                if custom_amount is None:
                    try:
                        custom_page = chatgpt_http.get(
                            custom_url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "User-Agent": sc.CHROME_UA,
                                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                "Referer": "https://chatgpt.com/",
                            },
                            timeout=35,
                        )
                        custom_state = custom_checkout_state_from_html(custom_page.text or "")
                        custom_amount = custom_checkout_amount_minor(custom_state)
                        custom_currency = custom_checkout_currency(custom_state) or custom_currency
                    except Exception as exc:
                        self.log(job_id, f"OAICS 金额页面读取提示：{type(exc).__name__}")
                custom_verification = (
                    "verified_zero" if custom_amount == 0
                    else ("pending" if custom_amount is None else "nonzero")
                )
                result.update({
                    "requested_link_type": str(options.get("requested_link_type") or provider),
                    "link_type": "oaics",
                    "checkout_provider": "open_ai",
                    "checkout_url": custom_url,
                    "short_link": custom_url,
                    "processor_entity": custom_processor,
                    "checkout_ui_mode": "custom",
                    "checkout_amount": custom_amount,
                    "amount_currency": custom_currency,
                    "amount_verification": custom_verification,
                    "promo_applied": (custom_amount == 0) if promo_requested and custom_amount is not None else None,
                })
                if promo_requested and custom_amount not in {None, 0}:
                    raise RuntimeError(
                        f"OAICS 优惠未生效：今日应付 amount={custom_amount} {custom_currency}"
                    )
                done_text = "OAICS Checkout 提链完成"
                if custom_amount is None:
                    done_text += "（金额待页面复核）"
                self.update(job_id, percent=100, text=done_text, status="done", result=result)
                return
            if provider == "hosted":
                transport_stage = "Stripe Hosted Checkout"
                self.update(job_id, percent=56, text="正在检测官方长链金额")
                if not session_id:
                    if promo_requested:
                        raise RuntimeError("官方长链未返回 Stripe Session ID，优惠金额校验失败")
                    self.update(job_id, percent=100, text="支付长链生成完成", status="done", result=result)
                    return

                hosted_stripe_http = http_sessions.track(sc.build_http(checkout_proxy))
                hosted_profile = sc._profile(country)
                hosted_pk = str(checkout_data.get("publishable_key") or "") or sc.verify_pk(
                    hosted_stripe_http, session_id, lambda m: self.log(job_id, m)
                )
                hosted_customer_session = str(
                    checkout_data.get("customer_session_client_secret") or ""
                ).strip()
                if hosted_customer_session.startswith("cuss_secret_"):
                    sc.CHECKOUT_CUSTOMER_SESSION_SECRETS[session_id] = hosted_customer_session
                    self.log(job_id, "???? CustomerSession ???")
                hosted_init, hosted_version, hosted_ctx = sc.init_checkout(
                    hosted_stripe_http, session_id, hosted_pk, hosted_profile, lambda m: self.log(job_id, m)
                )
                exact_hosted_url = normalize_hosted_checkout_url(
                    hosted_ctx.get("stripe_hosted_url") or "", session_id
                )
                hosted_processor = (
                    str(checkout_data.get("processor_entity") or "")
                    or sc._entity_from_return_url(hosted_ctx.get("return_url") or hosted_init.get("return_url") or "")
                    or "openai_llc"
                )
                if not exact_hosted_url or "#" not in exact_hosted_url:
                    raise RuntimeError("Stripe did not return a complete Hosted Checkout URL")
                source_hosted_url = str(checkout_data.get("checkout_url") or "").strip()
                if session_id in source_hosted_url and "#" in source_hosted_url:
                    result["checkout_url"] = source_hosted_url
                else:
                    result["checkout_url"] = exact_hosted_url
                result["stripe_hosted_url"] = exact_hosted_url
                if options["plan"] == "codex_low":
                    result["short_link"] = ""
                    result["checkout_ui_mode"] = "hosted"
                hosted_amount = hosted_ctx.get("checkout_amount")
                try:
                    hosted_zero = int(str(hosted_amount)) == 0
                except (TypeError, ValueError):
                    hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}

                if promo_requested and not hosted_zero:
                    self.update(job_id, percent=68, text="正在应用优惠并同步金额")
                    update_checkout_promo(
                        promo_chatgpt_http,
                        token,
                        session_id,
                        hosted_processor,
                        options.get("promo_campaign") or "plus-1-month-free",
                        lambda m: self.log(job_id, m),
                        device_id=device_id,
                    )
                    for sync_attempt in range(6):
                        time.sleep(1.5 if sync_attempt else 0.8)
                        hosted_init, hosted_version, hosted_ctx = sc.init_checkout(
                            hosted_stripe_http, session_id, hosted_pk, hosted_profile, lambda m: self.log(job_id, m)
                        )
                        hosted_amount = hosted_ctx.get("checkout_amount")
                        self.log(job_id, f"官方长链优惠同步检查 {sync_attempt + 1}/6：amount={hosted_amount}")
                        try:
                            hosted_zero = int(str(hosted_amount)) == 0
                        except (TypeError, ValueError):
                            hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}
                        if hosted_zero:
                            break

                hosted_elements = sc.fetch_elements_session(
                    hosted_stripe_http,
                    hosted_pk,
                    session_id,
                    hosted_ctx,
                    hosted_version,
                    hosted_profile,
                    lambda m: self.log(job_id, m),
                )

                # Keep saved-card availability for the UI, without enforcing
                # or exposing the saved card billing country.
                saved_method_count = 0
                for saved_source in (hosted_init, hosted_elements):
                    if not isinstance(saved_source, dict):
                        continue
                    saved_customer = saved_source.get("customer") or saved_source.get("legacy_customer") or {}
                    if not isinstance(saved_customer, dict):
                        continue
                    saved_methods = saved_customer.get("payment_methods") or []
                    if isinstance(saved_methods, list):
                        saved_method_count = max(saved_method_count, len(saved_methods))
                result["saved_payment_method_count"] = saved_method_count

                hosted_billing = default_billing(country, meta.get("email") or "")
                sc.update_tax_region(
                    hosted_stripe_http,
                    session_id,
                    hosted_pk,
                    hosted_version,
                    hosted_ctx,
                    hosted_billing,
                    hosted_profile,
                    lambda m: self.log(job_id, m),
                )
                hosted_amount = hosted_ctx.get("checkout_amount")
                try:
                    hosted_zero = int(str(hosted_amount)) == 0
                except (TypeError, ValueError):
                    hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}
                result.update({
                    "checkout_amount": hosted_amount,
                    "promo_applied": hosted_zero if promo_requested else None,
                    "payment_method_types": hosted_ctx.get("payment_method_types") or [],
                    "processor_entity": hosted_processor,
                    "stripe_publishable_key": hosted_pk,
                })
                if promo_requested and not hosted_zero:
                    raise RuntimeError(f"官方长链优惠未生效：Stripe 今日应付 amount={hosted_amount}")
                if promo_requested:
                    self.log(job_id, "官方长链金额校验通过：Stripe 今日应付 amount=0")
                else:
                    self.log(job_id, f"官方长链金额检测完成：Stripe 今日应付 amount={hosted_amount}")
                self.update(job_id, percent=100, text="支付长链生成完成", status="done", result=result)
                return

            stage3_text = "第 3/7 步：正在初始化 PIX" if provider == "pix" else (
                "第 3/7 步：正在初始化 PayPal" if provider == "paypal" and promo_requested else f"正在初始化 {provider.upper()}"
            )
            self.update(job_id, percent=56, text=stage3_text)
            billing_geo = None
            if provider == "paypal" and str(options.get("payment_proxy_country") or "").upper() == country:
                billing_geo = payment_geo
            billing = default_billing(
                country,
                meta.get("email") or "",
                options.get("pix_tax_id") or "",
                billing_geo,
                real_random=(provider == "paypal"),
            )
            if provider == "paypal":
                selected_address = billing.get("address") or {}
                self.log(
                    job_id,
                    "PayPal 本轮随机真实账单：source={}，城市={}，邮编={}，地点={}".format(
                        billing.get("_address_source") or "unknown",
                        selected_address.get("city") or "-",
                        selected_address.get("postal_code") or "-",
                        billing.get("_place_name") or "公开场所",
                    ),
                )
            if provider == "paypal":
                paypal_country = str(options.get("payment_proxy_country") or country).upper()
                if paypal_country != country:
                    self.log(
                        job_id,
                        f"PayPal 支付出口代理地区={paypal_country}；"
                        f"PaymentMethod 与 merchant approval 账单统一为 {country}/{options.get('currency')}",
                    )
            promotion_billing = None
            if provider == "paypal" and promo_requested:
                promotion_country = str(main_country or "BR").upper()
                promotion_billing = default_billing(
                    promotion_country,
                    meta.get("email") or "",
                )
                self.log(
                    job_id,
                    f"PayPal 地区：优惠更新={promotion_country}，Stripe/PayPal 账单与 merchant 快照={country}",
                )
            if provider == "pix":
                identity = options.get("pix_identity") or {}
                if identity:
                    billing["name"] = identity.get("name") or billing.get("name")
                    billing["email"] = identity.get("email") or billing.get("email")
                    address = billing.setdefault("address", {})
                    for key in ("line1", "city", "state", "postal_code"):
                        if identity.get(key):
                            address[key] = identity[key]
                    if identity.get("source") == "brasilapi_cnpj":
                        self.log(job_id, f"PIX 已匹配 CNPJ 登记主体：{billing.get('name')} / {address.get('state')}")
                    elif str(identity.get("source") or "").startswith("generated_"):
                        generated_kind = str(identity.get("source")).removeprefix("generated_").upper()
                        self.log(job_id, f"PIX 本轮已自动生成 {generated_kind}、持有人/企业名称及巴西地址")
            transport_stage = "Stripe/PayPal 支付处理"
            stripe_http = http_sessions.track(sc.build_http(exit_proxy))

            progress_mark = 62

            def advance_progress(percent: int, text: str):
                nonlocal progress_mark
                self.ensure_not_cancelled(job_id)
                if percent > progress_mark:
                    progress_mark = percent
                    self.update(job_id, percent=percent, text=text)

            def provider_log(message: str):
                self.log(job_id, message)
                lowered_message = message.lower()
                if "init ok" in lowered_message:
                    advance_progress(64, "支付方式初始化完成")
                elif "checkout/update" in lowered_message or "优惠更新完成" in message:
                    advance_progress(72, "优惠已应用，正在确认金额")
                elif "tax_region" in lowered_message:
                    advance_progress(78, "金额确认完成，正在提交账单信息")
                elif "snapshot billing" in lowered_message:
                    advance_progress(84, "账单信息已提交")
                elif "payment_method" in lowered_message:
                    advance_progress(88, "支付方式已创建")
                elif "manual_approval" in lowered_message or "approve:" in lowered_message:
                    advance_progress(92, "正在确认支付请求")
                elif "poll" in lowered_message:
                    advance_progress(96, "正在获取最终结果")

            def approve_cb(processor: str):
                self.ensure_not_cancelled(job_id)
                advance_progress(90, "正在确认支付请求")
                self.log(job_id, "提交 Checkout approval")
                approval_fn = approve_gopay_checkout_or_rebuild if provider == "gopay" else approve_checkout
                approval_kwargs = {
                    "http": provider_chatgpt_http,
                    "log": provider_log,
                    # GoPay must use a real approval proof. An empty fallback
                    # turns proof failures into misleading business blocks.
                    "allow_sentinel_fallback": provider == "paypal",
                }
                if provider == "gopay":
                    approval_kwargs["client_context"] = client_context
                    approval_kwargs["proof_policy"] = ProofPolicy.strict_gopay()
                approval_fn(
                    token,
                    session_id,
                    processor,
                    checkout_proxy,
                    device_id,
                    did,
                    **approval_kwargs,
                )
                self.ensure_not_cancelled(job_id)

            def apply_promo_cb(processor: str):
                self.ensure_not_cancelled(job_id)
                if provider == "pix":
                    self.log(job_id, "第 4/7 步：初始化已确认 PIX，开始应用优惠")
                elif provider == "paypal":
                    self.log(job_id, "PayPal 已确认可用，正在应用优惠")
                elif provider == "upi":
                    self.log(job_id, "UPI 已确认可用，正在应用优惠")
                elif provider == "momo":
                    self.log(job_id, "MoMo 已确认可用，正在应用优惠")
                elif provider == "ideal":
                    self.log(job_id, "iDEAL 已确认可用，正在通过 Promotion代理池提交优惠；最终以 Stripe 今日应付金额为准")
                elif provider == "twint":
                    self.log(job_id, "TWINT 已确认可用，正在应用首月优惠并校验 CHF 今日应付金额")
                elif provider == "gopay":
                    self.log(job_id, "GoPay 已确认可用，正在应用优惠并校验 IDR 今日应付金额")
                elif provider == "blik":
                    self.log(job_id, "BLIK 已确认可用，正在通过 Promotion代理池应用优惠并校验 PLN 今日应付金额")
                advance_progress(70, "正在应用优惠")
                campaign = options.get("promo_campaign") or "plus-1-month-free"
                if provider == "paypal":
                    self.log(
                        job_id,
                        "[promo] PayPal 更新上下文：campaign={}，session={}，Promotion={}，Checkout={}/{}".format(
                            campaign,
                            session_checkout_kind(session_id),
                            str(main_country or "?").upper(),
                            str(country or "?").upper(),
                            str(options.get("currency") or "?").upper(),
                        ),
                    )
                try:
                    response = update_checkout_promo(
                        promo_chatgpt_http,
                        token,
                        session_id,
                        processor,
                        campaign,
                        provider_log,
                        device_id=device_id,
                        is_coupon_from_query_param=bool(
                            provider == "gopay" and options.get("promo_from_query_param")
                        ),
                    )
                except RuntimeError as exc:
                    if provider == "paypal" and "promotion is not compatible with the checkout's payment methods" in str(exc).lower():
                        raise RuntimeError(
                            "PAYPAL_PROMOTION_INCOMPATIBLE: 当前地区的普通 Checkout 支持 PayPal，"
                            "但 plus-1-month-free 优惠不兼容该 Checkout 的 PayPal 支付方式"
                        ) from exc
                    if provider == "momo" and momo_promotion_is_payment_method_incompatible(exc):
                        raise RuntimeError(
                            "MOMO_PROMOTION_INCOMPATIBLE_REBUILD_REQUIRED: CS Live 已发布 MoMo，"
                            "但服务端拒绝将当前优惠应用到该支付方式集合；将完整重建 Checkout"
                        ) from exc
                    raise
                self.ensure_not_cancelled(job_id)
                return response

            self.update(job_id, percent=62, text="正在生成支付结果")
            provider_result = stripe_to_provider(
                stripe_http,
                session_id,
                provider,
                billing=billing,
                promotion_billing=promotion_billing,
                country=options.get("checkout_country") or country,
                chatgpt_http=provider_chatgpt_http,
                access_token=token,
                device_id=device_id,
                stage1=checkout_data,
                # PayPal 保持原协议的 Bearer approval；PIX/UPI 才使用带
                # Sentinel 的 callback。PayPal approval 返回 approved 后仍
                # 卡住时，额外 Sentinel 上下文会让批准结果与 Stripe
                # submission 不同步。
                approve_callback=None if provider == "paypal" else approve_cb,
                apply_promo_callback=apply_promo_cb if provider in {"pix", "momo", "gcash", "gopay", "blik", "paypal", "upi", "ideal", "twint"} and promo_requested else None,
                ideal_bank=options.get("ideal_bank", ""),
                require_zero_due=promo_requested,
                local_method_strategy=options.get("local_method_strategy") or "standalone",
                log=provider_log,
            )
            self.ensure_not_cancelled(job_id)
            if provider == "ideal":
                provider_amount = provider_result.get("checkout_amount")
                try:
                    provider_amount_zero = provider_amount is not None and float(provider_amount) == 0
                except (TypeError, ValueError):
                    provider_amount_zero = False
                if promo_requested and (
                    provider_result.get("promo_applied") is not True or not provider_amount_zero
                ):
                    raise RuntimeError(
                        "IDEAL_ZERO_DUE_VERIFICATION_FAILED: iDEAL 结果未通过金额为 0 且优惠已应用的校验"
                    )
                if not is_valid_ideal_payment_url(provider_result.get("provider_redirect_url") or ""):
                    raise RuntimeError(
                        "IDEAL_PAYMENT_LINK_INVALID: iDEAL 结果不是带签名的 pay.ideal.nl 交易链接"
                    )
            elif provider == "gopay":
                provider_result = require_gopay_midtrans_result(provider_result)
            self.update(job_id, percent=98, text="结果已生成，正在整理页面")
            result.update(provider_result)
            # Display the currency Stripe actually returned instead of only
            # echoing the requested currency.  This also makes automatic
            # proxy-region adaptation observable in the result panel/API.
            if provider_result.get("checkout_currency"):
                result["currency"] = str(provider_result["checkout_currency"]).upper()
                result["checkout_currency"] = result["currency"]
            if provider == "gopay" and client_context is not None:
                self.log(job_id, render_payment_diagnostic_event(
                    client_context,
                    phase="payment_result",
                    checkout_type=actual_checkout_kind or "unknown",
                    payment_method_source=str(
                        provider_result.get("payment_method_source") or "unknown"
                    ),
                    elapsed_ms=(time.monotonic() - flow_started_at) * 1000,
                    proxy_round=int(options.get("_proxy_round") or 1),
                ))
            done_text = "第 7/7 步：PIX 二维码生成完成" if provider == "pix" else (
                "第 7/7 步：MoMo 支付结果生成完成" if provider == "momo" else (
                "第 7/7 步：PayPal agreements/approve 链接生成完成" if provider == "paypal" else f"{provider.upper()} 提取完成"
                )
            )
            self.update(job_id, percent=100, text=done_text, status="done", result=result)
        except InterruptedError as exc:
            self.update(
                job_id,
                status="cancelled",
                percent=100,
                text=str(exc),
                error=str(exc),
                payment_error=None,
            )
        except Exception as exc:
            raw_error = str(exc)
            error_text = raw_error
            payment_error = exc.as_dict() if isinstance(exc, PaymentFlowError) else None
            lowered = raw_error.lower()
            if "token_invalidated" in lowered or "authentication token has been invalidated" in lowered:
                error_text = "Access Token 已失效，请重新登录 ChatGPT 获取新的 Session JSON 或 AT。"
            elif "token_expired" in lowered or "jwt expired" in lowered:
                error_text = "Access Token 已过期，请重新登录 ChatGPT 获取新的 Session JSON 或 AT。"
            elif "not_eligible" in lowered:
                error_text = "当前账号未开放所选套餐或支付通道。"
            elif "cannot combine currencies" in lowered:
                error_text = "该账号已有其他币种的活跃结账会话，请等待原会话释放，或更换账号后再生成当前币种链接。"
            elif "amount_too_small" in lowered:
                error_text = "当前地区换算后的结账金额低于支付提供商下限，请提高 Codex 积分数量后重试。"
            if _is_proxy_ssl_error(raw_error):
                entry_label = proxy_route_label(str(options.get("fixed_entry_proxy") or ""))
                exit_label = proxy_route_label(str(options.get("fixed_exit_proxy") or ""))
                diagnostic = (
                    f"传输诊断：阶段={transport_stage}；Promotion代理={entry_label}；"
                    f"Checkout代理={exit_label}；将切换代理并重建HTTP会话"
                )
                self.log(job_id, diagnostic)
                error_text = f"{error_text}（{diagnostic}）"
            if isinstance(exc, PaymentFlowError) and client_context is not None:
                self.log(job_id, render_payment_diagnostic_event(
                    client_context,
                    phase="failure",
                    failure=exc,
                    checkout_type=actual_checkout_kind or "unknown",
                    elapsed_ms=(time.monotonic() - flow_started_at) * 1000,
                    proxy_round=int(options.get("_proxy_round") or 1),
                ))
            self.log(job_id, f"错误：{type(exc).__name__}: {error_text}")
            if options.get("retry_wrapper"):
                self.update(
                    job_id,
                    status="running",
                    percent=8,
                    text="本次未成功，正在更换代理重试",
                    error=error_text[:1200],
                    payment_error=payment_error,
                )
            else:
                self.update(
                    job_id,
                    status="error",
                    percent=100,
                    text="任务失败",
                    error=error_text[:1200],
                    payment_error=payment_error,
                )
        finally:
            http_sessions.close()


    def _run_kakao_pidan(self, job_id: str, options: dict):
        """Run the dedicated pidan Kakao/Nicepay flow inside this worker."""
        stop_event = threading.Event()
        watcher_stop = threading.Event()
        watcher = None
        callback_token = None
        try:
            if str(options.get("plan") or "plus").lower() != "plus":
                raise RuntimeError("Kakao Pay 提链仅支持 Plus 计划")
            raw_token = str(options.get("token_raw") or "")
            token, meta = extract_access_token(raw_token)
            entry_pool = list(options.get("entry_proxies") or [])
            exit_pool = list(options.get("exit_proxies") or entry_pool)
            entry_proxy = str(options.get("fixed_entry_proxy") or (secrets.choice(entry_pool) if entry_pool else ""))
            exit_proxy = str(options.get("fixed_exit_proxy") or (secrets.choice(exit_pool) if exit_pool else entry_proxy))
            if not entry_proxy or not exit_proxy:
                raise RuntimeError("Kakao 提链缺少 Checkout 或 Promotion 代理")

            # Checkout is the primary route. Promotion follows it by default
            # and only separates when the user explicitly supplied a country.
            checkout_country = str(options.get("exit_proxy_country") or options.get("country") or "KR").upper()
            promotion_country = str(options.get("entry_proxy_country") or checkout_country).upper()
            if not re.fullmatch(r"[A-Z]{2}", checkout_country):
                checkout_country = "KR"
            if not re.fullmatch(r"[A-Z]{2}", promotion_country):
                promotion_country = checkout_country
            provider_country = checkout_country
            # Named pools already represent their target countries. When a
            # sticky country/region selector is present, derive all roles from
            # one Seed while keeping Provider/Approve equal to Checkout.
            checkout_proxy, promotion_proxy, provider_proxy = exit_proxy, entry_proxy, exit_proxy
            if re.search(r"(?i)(?:country|region)[-_=][a-z]{2}", entry_proxy):
                try:
                    checkout_proxy, promotion_proxy, provider_proxy = kakao.kakao_proxy_chain(
                        entry_proxy,
                        checkout_country=checkout_country,
                        promotion_country=promotion_country,
                        provider_country=provider_country,
                    )
                except Exception as exc:
                    self.log(job_id, f"Kakao Seed 地区派生失败，沿用已配置角色代理：{type(exc).__name__}")
            self.log(
                job_id,
                f"Kakao 专用链路：{checkout_country} Checkout/Bootstrap -> "
                f"{promotion_country} checkout/update -> "
                f"{provider_country} Stripe/taxes/Kakao/approve/redirect",
            )
            self.log(job_id, "Kakao 代理角色：Checkout={}；Promotion={}；Provider/Approve={}".format(
                kakao.proxy_label(checkout_proxy),
                kakao.proxy_label(promotion_proxy),
                kakao.proxy_label(provider_proxy),
            ))
            checked = set()
            for role, proxy in (
                ("checkout", checkout_proxy),
                ("promotion", promotion_proxy),
                ("provider", provider_proxy),
            ):
                if proxy in checked:
                    continue
                checked.add(proxy)
                expected = {
                    "checkout": checkout_country,
                    "promotion": promotion_country,
                    "provider": provider_country,
                }[role]
                ok, detail = kakao.preflight_proxy(proxy, role, expected)
                if not ok:
                    raise RuntimeError(f"Kakao {role} 代理预检失败：{detail}")
                self.log(job_id, f"Kakao {role} 代理出口预检通过：{detail}")

            def watch_cancel() -> None:
                while not watcher_stop.wait(0.25):
                    if self.cancelled(job_id):
                        stop_event.set()
                        return

            watcher = threading.Thread(target=watch_cancel, name=f"kakao-cancel-{job_id}", daemon=True)
            watcher.start()
            callback_token = kakao.set_log_callback(lambda message: self.log(job_id, message))
            self.update(job_id, status="running", percent=12, text="执行 Kakao/Nicepay 专用提链流程", error="")
            promo_requested = str(options.get("plan") or "plus").lower() == "plus" and bool(options.get("use_promo"))
            if promo_requested:
                self.update(job_id, percent=9, text="通过 Promotion 代理读取 Kakao 试用资格")
                preflight = preflight_trial_eligibility(
                    token,
                    meta.get("account_id") or "",
                    promotion_proxy,
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    lambda message: self.log(job_id, message),
                )
                detected_campaign = promo_campaign_from_payload(preflight)
                if detected_campaign:
                    self.log(job_id, f"Kakao 优惠预检已匹配账号活动：{detected_campaign}")
            extracted = kakao.kakao_link(
                token,
                checkout_proxy,
                promotion_proxy,
                provider_proxy,
                stop_event=stop_event,
                apply_promo=promo_requested,
            )
            final_url = str(extracted.get("provider_redirect_url") or "")
            host = urlsplit(final_url).netloc.lower()
            if "nicepay" not in host and "kakao" not in host:
                raise RuntimeError(f"Kakao/Nicepay 跳转域名不受支持：{final_url[:180]}")
            result = {
                "plan": options.get("plan") or "plus",
                "link_type": "kakao",
                "checkout_session_id": extracted.get("checkout_session_id") or "",
                "payment_method_id": extracted.get("payment_method_id") or "",
                "stripe_redirect_url": extracted.get("stripe_redirect_url") or "",
                "provider_redirect_url": final_url,
                "account_email": meta.get("email") or "",
                "account_id": meta.get("account_id") or "",
                "country": "KR",
                "currency": "KRW",
                "checkout_country": "KR",
                "checkout_currency": "KRW",
                "entry_country": promotion_country,
                "payment_proxy_country": checkout_country,
                "provider_proxy_country": provider_country,
                "proxy_mode": f"{checkout_country.lower()}_checkout_{promotion_country.lower()}_promotion_{provider_country.lower()}_provider",
                "entry_proxy_pool_size": len(entry_pool),
                "exit_proxy_pool_size": len(exit_pool),
                "promo_requested": promo_requested,
                "promo_applied": True if promo_requested else None,
                "extractor": "pidan_kakao_nicepay",
            }
            self.update(job_id, status="done", percent=100, text="Kakao/Nicepay 提取完成", error="", result=result)
        except (InterruptedError, kakao.TaskStopped) as exc:
            self.update(job_id, status="cancelled", percent=100, text="任务已停止", error=str(exc) or "任务已停止")
        except Exception as exc:
            error = str(exc)[:1200]
            if options.get("retry_wrapper"):
                self.update(job_id, status="running", percent=8, text="本轮未成功，正在更换代理重试", error=error)
            else:
                self.update(job_id, status="error", percent=100, text="任务失败", error=error)
        finally:
            if callback_token is not None:
                kakao.reset_log_callback(callback_token)
            watcher_stop.set()
            stop_event.set()
            if watcher is not None:
                watcher.join(timeout=1)


class IpTaskLimiter:
    def __init__(self, limit: int = 3, window_seconds: int = 60):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self.lock = threading.RLock()
        self.events: defaultdict[str, deque[float]] = defaultdict(deque)

    def acquire(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        with self.lock:
            bucket = self.events[ip]
            while bucket and now - bucket[0] >= self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0]) + 0.999))
                return False, retry_after
            bucket.append(now)
            if len(self.events) > 10000:
                stale = [key for key, values in self.events.items() if not values or now - values[-1] > self.window_seconds * 2]
                for key in stale[:2000]:
                    self.events.pop(key, None)
            return True, 0


def request_client_ip() -> str:
    remote = str(request.remote_addr or "").strip()
    if remote in {"127.0.0.1", "::1"}:
        return str(request.headers.get("X-Real-IP") or remote).strip()
    return remote or "unknown"


STORE = JobStore()
IP_TASK_LIMITER = IpTaskLimiter(
    limit=int(os.getenv("PAY153_IP_RPM", "3")),
    window_seconds=60,
)


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def _internal_key_valid(value: str) -> bool:
    expected = str(os.getenv("PAY153_INTERNAL_KEY") or "").strip()
    supplied = str(value or "").strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _private_page_key_valid(value: str) -> bool:
    expected = str(os.getenv("PAY153_PRIVATE_PAGE_KEY") or "").strip()
    supplied = str(value or "").strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _gcash_callback_token() -> str:
    header = str(request.headers.get("X-GCash-Callback-Token") or "").strip()
    if header:
        return header
    return str(request.args.get("callback_token") or "").strip()


@app.get("/private-checkout")
def private_checkout_page():
    bootstrap_key = str(request.args.get("key") or "").strip()
    if _private_page_key_valid(bootstrap_key):
        response = redirect("/private-checkout", code=302)
        response.set_cookie(
            "pay153_private_lane",
            bootstrap_key,
            max_age=30 * 24 * 60 * 60,
            secure=True,
            httponly=True,
            samesite="Strict",
        )
        return response
    if not _private_page_key_valid(request.cookies.get("pay153_private_lane") or ""):
        return "Not Found", 404
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "pay153", "time": int(time.time())})


@app.get("/api/config")
def config():
    return jsonify({
        "plans": list(PLANS),
        "link_types": ["hosted", "ph_short", "paypal", "ideal", "twint", "pix", "momo", "gcash", "gopay", "blik", "kakao"]
            + (["upi"] if UPI_ENABLED else []),
        "disabled_link_types": [] if UPI_ENABLED else ["upi"],
        "country_currency": COUNTRY_CURRENCY,
        "provider_defaults": PROVIDER_DEFAULTS,
        "proxy_policy": {
            "entry_required": True,
            "exit_required_for": ["ph_short", "paypal", "ideal", "twint", "upi", "gcash", "gopay", "blik", "kakao"],
            "single_chain_for": ["pix", "momo"],
            "max_per_pool": 500,
            "selection": "random_per_job",
        },
        "retry_policy": {"min": 0, "max": 50, "default_pix": 10, "default_other": 3, "meaning": "失败后的重试次数，首次提链不计入"},
        "pix_identity_policy": {"default": "cpf", "auto_kinds": ["cpf", "mixed", "cnpj"], "regenerate_each_attempt": True},
        "task_limits": {
            "global_rpm": STORE.global_rpm,
            "per_ip_rpm": IP_TASK_LIMITER.limit,
            "queue_enabled": True,
            "workers": STORE.worker_limit,
        },
    })


@app.post("/api/checkout")
def start_checkout():
    data = request.get_json(silent=True) or {}
    internal_request = bool(
        _internal_key_valid(request.headers.get("X-Pay153-Internal-Key") or "")
        or _private_page_key_valid(request.cookies.get("pay153_private_lane") or "")
    )
    plan = str(data.get("plan") or "plus").lower()
    link_type = str(data.get("link_type") or "hosted").lower()
    if plan not in PLANS:
        return jsonify({"error": "计划类型不正确"}), 400
    if link_type not in {"hosted", "ph_short", "paypal", "ideal", "twint", "upi", "pix", "momo", "gcash", "gopay", "blik", "kakao"}:
        return jsonify({"error": "提取方式不正确"}), 400
    if link_type == "upi" and not UPI_ENABLED:
        return jsonify({"error": "UPI 提链已暂停维护"}), 503
    if link_type == "ph_short" and plan != "plus":
        return jsonify({"error": "菲律宾短链仅支持 Plus 计划"}), 400
    defaults = PROVIDER_DEFAULTS.get(link_type, {})
    country = str(data.get("country") or defaults.get("country") or "US").upper()
    requested_currency = str(
        data.get("currency")
        or (COUNTRY_CURRENCY.get(country) if link_type == "paypal" else "")
        or defaults.get("currency")
        or COUNTRY_CURRENCY.get(country, "USD")
    ).upper()
    currency, _currency_source = normalize_checkout_currency(country, requested_currency)
    entry_raw = data.get("entry_proxies")
    if entry_raw is None:
        entry_raw = data.get("entry_proxy") or data.get("api_proxy") or data.get("proxy") or ""
    exit_raw = data.get("exit_proxies")
    if exit_raw is None:
        exit_raw = data.get("exit_proxy") or data.get("payment_proxy") or ""
    dynamic_proxy_api = bool(data.get("dynamic_proxy_api")) and internal_request
    if not entry_raw and not dynamic_proxy_api:
        return jsonify({"error": "请填写 Checkout 入口代理"}), 400
    if link_type not in {"hosted", "pix", "momo"} and not exit_raw and not dynamic_proxy_api:
        return jsonify({"error": "当前支付路径需要填写支付出口代理"}), 400
    try:
        entry_proxies = normalize_proxy_pool(entry_raw,  "入口代理") if entry_raw else []
        exit_proxies = normalize_proxy_pool(exit_raw, "出口代理") if exit_raw else []
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not entry_proxies and not dynamic_proxy_api:
        return jsonify({"error": "入口代理至少填写 1 条"}), 400
    if link_type not in {"hosted", "pix", "momo"} and not exit_proxies and not dynamic_proxy_api:
        return jsonify({"error": "出口代理至少填写 1 条"}), 400
    raw_pix_tax_id = re.sub(r"\D", "", str(data.get("pix_tax_id") or ""))[:14] if link_type == "pix" else ""
    try:
        default_retry_count = 10 if link_type in {"pix", "momo", "gcash"} else 3
        raw_retry_count = data.get("retry_count")
        if raw_retry_count is None or str(raw_retry_count).strip() == "":
            retry_count = default_retry_count
        else:
            retry_count = min(50, max(0, int(raw_retry_count)))
    except (TypeError, ValueError):
        return jsonify({"error": "失败重试次数需要填写 0-50 的整数"}), 400
    pix_identity: dict[str, str] = {}
    if link_type == "pix":
        manual_identity = {
            "name": str(data.get("pix_name") or "").strip()[:160],
            "email": str(data.get("pix_email") or "").strip()[:200],
            "line1": str(data.get("pix_line1") or "").strip()[:180],
            "city": str(data.get("pix_city") or "").strip()[:100],
            "state": str(data.get("pix_state") or "").strip()[:40],
            "postal_code": str(data.get("pix_postal_code") or "").strip()[:30],
        }
        if len(raw_pix_tax_id) == 14:
            try:
                pix_identity.update(lookup_cnpj_identity(raw_pix_tax_id))
            except Exception as exc:
                if not manual_identity["name"]:
                    return jsonify({"error": f"CNPJ 登记信息查询失败：{exc}"}), 400
        pix_identity.update({key: value for key, value in manual_identity.items() if value})
    options = {
        "token_raw": str(data.get("token") or ""),
        "plan": plan,
        "link_type": link_type,
        "country": country,
        "currency": currency,
        "checkout_country": country,
        "checkout_currency": currency,
        "entry_proxies": entry_proxies,
        "exit_proxies": (exit_proxies or entry_proxies) if link_type in {"pix", "momo"} else exit_proxies,
        "use_promo": bool(data.get("use_promo", True)) if plan == "plus" else False,
        "promo_campaign": str(data.get("promo_campaign") or "") if plan == "plus" else "",
        "promo_country": str(data.get("promo_country") or "").strip().upper()[:2],
        "oaics_paypal": bool(data.get("oaics_paypal")) and internal_request,
        "promo_code": str(data.get("promo_code") or "") if plan == "team" else "",
        "workspace_name": str(data.get("workspace_name") or "")[:80],
        "workspace_id": str(data.get("workspace_id") or "")[:120],
        "seat_quantity": min(999, max(2, int(data.get("seat_quantity") or 5))),
        "price_interval": "year" if data.get("price_interval") == "year" else "month",
        "credit_quantity": min(100000, max(1, int(data.get("credit_quantity") or 13))),
        "ideal_bank": str(data.get("ideal_bank") or "")[:40] if link_type == "ideal" else "",
        "pix_tax_id": raw_pix_tax_id,
        "pix_tax_id_auto": link_type == "pix" and not raw_pix_tax_id,
        "pix_auto_kind": str(data.get("pix_auto_kind") or "cpf").lower()
            if str(data.get("pix_auto_kind") or "cpf").lower() in {"mixed", "cpf", "cnpj"} else "cpf",
        "pix_identity": pix_identity,
        "retry_count": retry_count,
        "paired_proxy_rotation": bool(data.get("paired_proxy_rotation", False)),
        "use_sen": data.get("use_sen", True) is not False,
        "use_so": data.get("use_so", True) is not False,
        "dynamic_proxy_api": dynamic_proxy_api,
        "allow_missing_customer_session": bool(data.get("allow_missing_customer_session")) and internal_request,
        "entry_proxy_country": str(data.get("entry_proxy_country") or (str(data.get("promo_country") or country) if link_type == "kakao" else ("VN" if link_type == "gcash" else ("US" if link_type == "ph_short" and country == "PH" else country)))).upper(),
        "exit_proxy_country": str(data.get("exit_proxy_country") or ("PH" if link_type == "gcash" else ((str(data.get("promo_country") or ("TR" if country == "PH" else country))) if link_type == "ph_short" and bool(data.get("use_promo", True)) else country))).upper(),
        "proxy_session_time": min(120, max(1, int(data.get("proxy_session_time") or 10))),
    }
    if link_type == "ph_short":
        if country == "PH" and options["entry_proxy_country"] == "PH":
            options["entry_proxy_country"] = "US"
        if not options.get("use_promo"):
            options["exit_proxy_country"] = options["entry_proxy_country"]
        elif not str(data.get("exit_proxy_country") or "").strip() and not str(data.get("promo_country") or "").strip():
            options["exit_proxy_country"] = "TR" if country == "PH" else country
    if not options["token_raw"].strip():
        return jsonify({"error": "请填写 Access Token 或 Session JSON"}), 400
    if link_type == "pix" and options["pix_tax_id"] and len(options["pix_tax_id"]) not in {11, 14}:
        return jsonify({"error": "PIX 需要填写 11 位 CPF 或 14 位 CNPJ"}), 400
    if not internal_request:
        client_ip = request_client_ip()
        allowed, retry_after = IP_TASK_LIMITER.acquire(client_ip)
        if not allowed:
            response = jsonify({
                "error": f"当前 IP 每分钟最多创建 {IP_TASK_LIMITER.limit} 个任务，请在 {retry_after} 秒后重试。",
                "retry_after": retry_after,
                "limit": IP_TASK_LIMITER.limit,
            })
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
    job_id = STORE.create(options, internal=internal_request)
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "queue_position": STORE.queue_position(job_id),
        "global_rpm": STORE.global_rpm,
        "ip_rpm": IP_TASK_LIMITER.limit,
        "internal": internal_request,
    }), 202


@app.get("/api/checkout-progress")
def checkout_progress():
    job_id = str(request.args.get("job_id") or "")
    job = STORE.get(job_id, public=True)
    if not job:
        job = rust_job_public_snapshot(job_id)
    if not job:
        if LEGACY_SERVICE_BASE:
            try:
                legacy = requests.get(
                    f"{LEGACY_SERVICE_BASE}/api/checkout-progress",
                    params={"job_id": str(request.args.get("job_id") or "")},
                    timeout=8,
                )
                return app.response_class(
                    response=legacy.content,
                    status=legacy.status_code,
                    content_type=legacy.headers.get("content-type", "application/json"),
                )
            except Exception:
                pass
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)


@app.get("/api/gcash/orders/<order_id>")
def gcash_order_status(order_id: str):
    token = _gcash_callback_token()
    order = STORE.gcash_order(order_id, token)
    if not order:
        return jsonify({"error": "GCash 订单不存在或回调令牌无效"}), 404
    return jsonify(order)


@app.post("/api/gcash/orders/<order_id>/qr")
def gcash_order_qr_refresh(order_id: str):
    token = _gcash_callback_token()
    if not token:
        body = request.get_json(silent=True) or {}
        token = str(body.get("callback_token") or "").strip()
    if not token:
        return jsonify({"error": "缺少 GCash 回调令牌"}), 401
    order = STORE.refresh_gcash_order_qr(order_id, token)
    if not order:
        return jsonify({"error": "GCash 订单不存在、已过期或回调令牌无效"}), 404
    return jsonify(order), 200


@app.post("/api/gcash/orders/<order_id>/callback")
def gcash_order_callback(order_id: str):
    token = _gcash_callback_token()
    if not token:
        body = request.get_json(silent=True) or {}
        token = str(body.get("callback_token") or "").strip()
    if not token:
        return jsonify({"error": "缺少 GCash 回调令牌"}), 401
    body = request.get_json(silent=True)
    if body is None:
        body = request.form.to_dict(flat=True)
    callback = body.get("callback_url") or body.get("url") or body.get("redirect_url") or body
    order = STORE.complete_gcash_order(order_id, callback, token)
    if not order:
        return jsonify({"error": "GCash 订单不存在、已过期或回调令牌无效"}), 404
    return jsonify(order), 200


@app.post("/api/checkout-cancel")
def checkout_cancel():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "")
    ok = STORE.cancel(job_id)
    if not ok:
        alias = get_rust_job_alias(job_id)
        rust_base = str(os.getenv("PAY153_RUST_URL") or "").strip().rstrip("/")
        rust_job_id = str((alias or {}).get("rust_job_id") or "")
        if rust_base and rust_job_id:
            try:
                response = requests.post(
                    f"{rust_base}/api/v1/jobs/{rust_job_id}/cancel",
                    timeout=8,
                )
                ok = response.status_code in {200, 202}
            except Exception:
                ok = False
    if not ok and LEGACY_SERVICE_BASE:
        try:
            legacy = requests.post(
                f"{LEGACY_SERVICE_BASE}/api/checkout-cancel",
                json={"job_id": job_id},
                timeout=8,
            )
            return app.response_class(
                response=legacy.content,
                status=legacy.status_code,
                content_type=legacy.headers.get("content-type", "application/json"),
            )
        except Exception:
            pass
    return jsonify({"ok": ok}), 200 if ok else 404


if __name__ == "__main__":
    app.run(host=os.getenv("PAY153_HOST", "127.0.0.1"), port=int(os.getenv("PAY153_PORT", "18082")), threaded=True)
