from __future__ import annotations

import base64
import dataclasses
import email as email_pkg
import imaplib
import ipaddress
import json
import os
import random
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse

import requests

from .otp_candidates import extract_otp_candidates
from .proxy import normalize_proxy_url


OUTLOOK_IMAP_HOST = "outlook.office365.com"
OUTLOOK_IMAP_PORT = 993
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
XBOVO_API_BASE_URL = os.getenv("XBOVO_ICLOUD_API_BASE_URL", "https://icloud.xbovo.online").rstrip("/")


def _int_env(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


XBOVO_MAX_CONCURRENT_REQUESTS = _int_env("XBOVO_ICLOUD_MAX_CONCURRENCY", 3, 1, 20)
XBOVO_POOL_RETRIES = _int_env("XBOVO_ICLOUD_POOL_RETRIES", 4, 0, 10)
XBOVO_QUEUE_TIMEOUT = _int_env("XBOVO_ICLOUD_QUEUE_TIMEOUT", 120, 30, 600)
XBOVO_LONG_POLL_SECONDS = _int_env("XBOVO_ICLOUD_LONG_POLL_SECONDS", 10, 5, 20)
URL_API_REQUEST_TIMEOUT = max(35, int(os.getenv("URL_API_ICLOUD_REQUEST_TIMEOUT", "40") or 40))
URL_API_SPECIALIZED_FALLBACK_SECONDS = 45
URL_API_MAX_REDIRECTS = 3
URL_API_MAX_RESPONSE_BYTES = 1 << 20
URL_API_MAX_CONCURRENT_REQUESTS = _int_env("URL_API_ICLOUD_MAX_CONCURRENCY", 3, 1, 20)
URL_API_QUEUE_TIMEOUT = _int_env("URL_API_ICLOUD_QUEUE_TIMEOUT", 30, 5, 300)
URL_API_POLL_JITTER_SECONDS = 1.5
_XBOVO_REQUEST_GATE = threading.BoundedSemaphore(XBOVO_MAX_CONCURRENT_REQUESTS)
_URL_API_REQUEST_GATE = threading.BoundedSemaphore(URL_API_MAX_CONCURRENT_REQUESTS)


class MailboxAccessError(RuntimeError):
    def __init__(self, code: str, user_message: str, detail: str = "", terminal: bool = False):
        self.code = code
        self.user_message = user_message
        self.detail = detail
        self.terminal = terminal
        super().__init__(f"{user_message}: {detail}" if detail else user_message)


def _recipient_matches(expected: str, value: str) -> bool:
    target = str(expected or "").strip().casefold()
    if not target:
        return True
    recipients = {
        item.casefold()
        for item in re.findall(r"[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}", str(value or ""), flags=re.I)
    }
    return not recipients or target in recipients


def _outlook_token_error(status_code: int, payload: dict[str, Any]) -> MailboxAccessError:
    error_code = str(payload.get("error") or "").strip().lower()
    detail = str(payload.get("error_description") or payload.get("error") or f"HTTP {status_code}").strip()
    lower = detail.lower()
    if any(marker in lower for marker in ("grant is expired", "refresh token has expired", "token was revoked", "sign in again")):
        return MailboxAccessError(
            "mailbox_credential_expired",
            "邮箱 OAuth 凭证已过期或被撤销，请重新授权或更换 Refresh Token",
            detail,
            terminal=True,
        )
    if error_code == "invalid_client" or "client secret is invalid" in lower:
        return MailboxAccessError(
            "mailbox_client_invalid",
            "邮箱 OAuth 客户端配置无效，请检查 client_id 与凭证来源",
            detail,
            terminal=True,
        )
    if error_code == "invalid_grant" and ("malformed" in lower or "invalid refresh token" in lower):
        return MailboxAccessError(
            "mailbox_credential_invalid",
            "邮箱 OAuth 凭证无效，请检查 client_id 与 Refresh Token",
            detail,
            terminal=True,
        )
    if error_code == "invalid_scope" or ("scope" in lower and ("unauthorized" in lower or "consent" in lower)):
        return MailboxAccessError(
            "mailbox_scope_mismatch",
            "邮箱凭证权限类型不匹配，正在尝试 Graph 与 IMAP 兼容授权",
            detail,
        )
    return MailboxAccessError(
        "mailbox_auth_failed",
        "邮箱 OAuth 凭证验证失败，请检查凭证类型、授权范围与有效期",
        detail,
    )


def _aggregate_mailbox_error(errors: list[str]) -> MailboxAccessError:
    detail = " | ".join(errors)
    lower = detail.lower()
    if any(marker in lower for marker in ("timeout", "connection refused", "connection reset", "network is unreachable", "name resolution", "tls")):
        return MailboxAccessError(
            "mailbox_network_error",
            "邮箱服务网络连接失败，请检查服务器出网、代理与 Microsoft 服务连通性",
            detail,
        )
    if any(marker in lower for marker in ("scope", "permission", "audience")):
        return MailboxAccessError(
            "mailbox_scope_mismatch",
            "邮箱凭证权限类型不匹配，Graph 与 IMAP 均未获得可用授权",
            detail,
        )
    return MailboxAccessError(
        "mailbox_auth_failed",
        "邮箱凭证无法通过 Graph 或 IMAP 验证，请检查凭证类型、授权范围与有效期",
        detail,
    )


@dataclasses.dataclass
class MailAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str
    raw: str
    account_type: str = "free"
    openai_rt: str = ""
    mailbox_type: str = "microsoft"
    mailbox_channel: str = "outlook"
    access_key: str = ""
    chatgpt_password: str = ""
    totp_secret: str = ""

    @property
    def has_login_secret(self) -> bool:
        return bool(str(self.chatgpt_password or "").strip() and str(self.totp_secret or "").strip())

    @property
    def has_chatgpt_password(self) -> bool:
        """Whether the saved ChatGPT password can be used for authentication."""
        return bool(str(self.chatgpt_password or "").strip())


_MICROSOFT_CLIENT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _looks_like_microsoft_refresh_token(value: str) -> bool:
    token = str(value or "").strip()
    lowered = token.lower()
    if lowered.startswith(("m.c", "m.r", "0.a", "1.a")):
        return True
    return len(token) >= 80 and (any(char in token for char in "!*$") or token.count(".") >= 2)


def _normalize_microsoft_credentials(values: list[str]) -> tuple[str, str, str]:
    password, client_id, refresh_token = values[:3]
    client_index = next((index for index, value in enumerate(values[:3]) if _MICROSOFT_CLIENT_ID.fullmatch(value)), -1)
    refresh_index = next((index for index, value in enumerate(values[:3]) if _looks_like_microsoft_refresh_token(value)), -1)
    if client_index >= 0 and refresh_index >= 0 and client_index != refresh_index:
        password_index = next(index for index in range(3) if index not in {client_index, refresh_index})
        password = values[password_index]
        client_id = values[client_index]
        refresh_token = values[refresh_index]
    return password, client_id, refresh_token


def parse_account_line(line: str) -> MailAccount:
    parts = [p.strip() for p in str(line or "").strip().split("----")]
    if len(parts) < 4:
        raise ValueError("Invalid mailbox line; expected email----password----client_id----refresh_token")
    email = parts[0]
    password, client_id, refresh_token = _normalize_microsoft_credentials(parts[1:4])
    if not email or "@" not in email or not client_id or not refresh_token:
        raise ValueError("email / client_id / refresh_token must not be empty")
    openai_rt = ""
    for part in parts[4:]:
        low = part.lower()
        if low.startswith(("rt_token=", "openai_rt=")):
            openai_rt = part.split("=", 1)[1].strip()
    return MailAccount(
        email=email,
        password=password,
        client_id=client_id,
        refresh_token=refresh_token,
        raw="----".join((email, password, client_id, refresh_token)),
        account_type="plus" if openai_rt else "free",
        openai_rt=openai_rt,
    )


def account_from_row(row: dict[str, Any]) -> MailAccount:
    mailbox_type = str(row.get("mailbox_type") or "microsoft").strip().lower()
    if mailbox_type in {"domain", "domain_mailbox", "cloudmail", "cfworker"} or str(row.get("mailbox_channel") or "").strip().lower() == "domain_api":
        email = str(row.get("email") or "").strip()
        access_key = str(row.get("access_key") or "").strip()
        raw = str(row.get("raw") or "").strip()
        if raw and (not email or not access_key):
            parts = [part.strip() for part in raw.split("----", 1)]
            email = email or (parts[0] if parts else "")
            access_key = access_key or (parts[1] if len(parts) > 1 else "")
        if not email or "@" not in email or not access_key:
            raise ValueError("Invalid domain mailbox row; expected email and CloudMail credential")
        if access_key.startswith(("http://", "https://")):
            parsed = urlparse(access_key)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not query.get("token") or str(query.get("email") or "").strip().lower() != email.lower():
                raise ValueError("Invalid domain mailbox pickup URL")
        else:
            try:
                metadata = json.loads(access_key)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid domain mailbox credential JSON") from exc
            if not str(metadata.get("base_url") or "").strip() or not str(metadata.get("auth_token") or "").strip():
                raise ValueError("Domain mailbox credential is missing base_url or auth_token")
        return MailAccount(
            email=email, password="", client_id="", refresh_token="", raw=raw or f"{email}----{access_key}",
            account_type=str(row.get("account_type") or "free"), openai_rt=str(row.get("openai_rt") or ""),
            mailbox_type="domain", mailbox_channel="domain_api", access_key=access_key,
            chatgpt_password=str(row.get("chat_gpt_password") or row.get("chatgpt_password") or ""),
            totp_secret=str(row.get("totp_secret") or "").strip(),
        )
    if mailbox_type == "remail" or str(row.get("mailbox_channel") or "").strip().lower() == "remail_api":
        email = str(row.get("email") or "").strip()
        access_key = str(row.get("access_key") or "").strip()
        raw = str(row.get("raw") or "").strip()
        if raw and (not email or not access_key):
            parts = [part.strip() for part in raw.split("----", 1)]
            email = email or (parts[0] if parts else "")
            access_key = access_key or (parts[1] if len(parts) > 1 else "")
        if not email or "@" not in email or not access_key:
            raise ValueError("Invalid Remail mailbox row; expected delivery email and service token")
        return MailAccount(
            email=email, password="", client_id="", refresh_token="", raw=raw or f"{email}----{access_key}",
            account_type=str(row.get("account_type") or "free"), openai_rt=str(row.get("openai_rt") or ""),
            mailbox_type="remail", mailbox_channel="remail_api", access_key=access_key,
            chatgpt_password=str(row.get("chat_gpt_password") or row.get("chatgpt_password") or ""),
            totp_secret=str(row.get("totp_secret") or "").strip(),
        )
    if mailbox_type in {"apple", "icloud"}:
        email = str(row.get("email") or "").strip()
        access_key = str(row.get("access_key") or "").strip()
        raw = str(row.get("raw") or "").strip()
        mailbox_channel = str(row.get("mailbox_channel") or "xbovo").strip().lower()
        chatgpt_password = str(row.get("chat_gpt_password") or row.get("chatgpt_password") or "")
        totp_secret = str(row.get("totp_secret") or "").strip()
        if raw:
            parts = [part.strip() for part in raw.split("----")]
            if mailbox_channel == "url_api" and 1 <= len(parts) <= 4:
                email = email or parts[0]
                remaining = parts[1:]
                if remaining:
                    if remaining[0].lower().startswith(("http://", "https://")):
                        access_key = access_key or remaining[0]
                    else:
                        chatgpt_password = chatgpt_password or remaining[0]
                for value in remaining[1:]:
                    if value.lower().startswith(("http://", "https://")):
                        access_key = access_key or value
                    elif value:
                        totp_secret = totp_secret or value
            elif mailbox_channel != "url_api" and len(parts) == 2:
                email = email or parts[0]
                access_key = access_key or parts[1]
        if not email or "@" not in email or (mailbox_channel != "url_api" and not access_key):
            raise ValueError("Invalid Apple mailbox line; expected icloud_email----key")
        return MailAccount(
            email=email,
            password="",
            client_id="",
            refresh_token="",
            raw=raw or "----".join(part for part in (email, chatgpt_password, access_key, totp_secret) if part),
            account_type=str(row.get("account_type") or "free"),
            openai_rt=str(row.get("openai_rt") or ""),
            mailbox_type="apple",
            mailbox_channel=mailbox_channel,
            access_key=access_key,
            chatgpt_password=chatgpt_password,
            totp_secret=totp_secret,
        )
    raw = row.get("raw") or "----".join([
        row.get("email", ""),
        row.get("password", ""),
        row.get("client_id", ""),
        row.get("refresh_token", ""),
    ])
    account = parse_account_line(raw)
    account.openai_rt = row.get("openai_rt") or account.openai_rt
    account.account_type = row.get("account_type") or account.account_type
    account.mailbox_type = "microsoft"
    account.mailbox_channel = "outlook"
    account.chatgpt_password = str(row.get("chat_gpt_password") or row.get("chatgpt_password") or "")
    account.totp_secret = str(row.get("totp_secret") or "").strip()
    if str(row.get("rebind_email") or "").strip():
        account.email = str(row.get("rebind_email")).strip()
    return account


def extract_otp(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or ""))
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", normalized)
    return match.group(1) if match else ""


def _validate_url_api_address(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MailboxAccessError(
            "mailbox_format_error",
            "url_api 邮箱凭证格式错误，应为 icloud_email----取码URL",
            terminal=True,
        )
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise MailboxAccessError("mailbox_url_forbidden", "url_api 取码地址不能指向本机或内部服务", terminal=True)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise MailboxAccessError("mailbox_url_forbidden", "url_api 取码地址不能指向私有网络", terminal=True)
    return raw


def _url_api_strategy(value: str) -> str:
    hostname = (urlparse(str(value or "")).hostname or "").lower().rstrip(".")
    if hostname == "mail.mczero.top" or hostname.endswith(".mail.mczero.top"):
        return "mczero"
    if hostname == "mail.ai1998.xyz" or hostname.endswith(".mail.ai1998.xyz"):
        return "ai1998"
    return "generic"


def _html_to_text(value: str) -> str:
    raw = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", str(value or ""))
    raw = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    lines = [re.sub(r"\s+", " ", unescape(line)).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


class _AI1998LatestMailParser(HTMLParser):
    """Extract the first mail card, which mail.ai1998.xyz defines as latest."""

    _FIELDS = ("subject", "date", "meta", "body")
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_card = False
        self.card_complete = False
        self.card_depth = 0
        self.field_stack: list[str] = []
        self.parts: dict[str, list[str]] = {field: [] for field in self._FIELDS}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if not self.in_card:
            if not self.card_complete and tag.lower() == "article" and "mail-card" in classes:
                self.in_card = True
                self.card_depth = 1
                self.field_stack.append("")
            return
        inherited = self.field_stack[-1] if self.field_stack else ""
        selected = next((field for field in self._FIELDS if field in classes), inherited)
        if tag.lower() in self._VOID_TAGS:
            if selected and attributes.get("value"):
                self.parts[selected].append(attributes["value"])
            return
        self.card_depth += 1
        self.field_stack.append(selected)

    def handle_startendtag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.in_card:
            return
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        selected = next((field for field in self._FIELDS if field in classes), self.field_stack[-1] if self.field_stack else "")
        if selected and attributes.get("value"):
            self.parts[selected].append(attributes["value"])

    def handle_endtag(self, _tag: str) -> None:
        if not self.in_card:
            return
        if self.field_stack:
            self.field_stack.pop()
        self.card_depth -= 1
        if self.card_depth <= 0:
            self.in_card = False
            self.card_complete = True

    def handle_data(self, data: str) -> None:
        if self.in_card and self.field_stack and self.field_stack[-1] and data.strip():
            self.parts[self.field_stack[-1]].append(data)


def _ai1998_latest_mail_html(raw_html: str) -> str:
    parser = _AI1998LatestMailParser()
    try:
        parser.feed(str(raw_html or ""))
        parser.close()
    except Exception:
        return str(raw_html or "")
    if not parser.card_complete:
        return str(raw_html or "")
    fields = {
        key: re.sub(r"\s+", " ", " ".join(value)).strip()
        for key, value in parser.parts.items()
    }
    if not any(fields.values()):
        return str(raw_html or "")
    return "".join(
        f'<div class="{field}">{escape(fields[field])}</div>'
        for field in parser._FIELDS
        if fields[field]
    )


TOKEN_ENDPOINTS = [
    {"name": "LIVE", "url": "https://login.live.com/oauth20_token.srf", "scope": ""},
    {"name": "LIVE+scope", "url": "https://login.live.com/oauth20_token.srf", "scope": IMAP_SCOPE},
    {"name": "V1-COMMON", "url": "https://login.microsoftonline.com/common/oauth2/token", "scope": "", "resource": "https://outlook.office.com/"},
    {"name": "V1-CONSUMERS", "url": "https://login.microsoftonline.com/consumers/oauth2/token", "scope": "", "resource": "https://outlook.office.com/"},
    {"name": "CONSUMERS", "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", "scope": IMAP_SCOPE},
    {"name": "CONSUMERS-noscope", "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", "scope": ""},
    {"name": "COMMON", "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token", "scope": IMAP_SCOPE},
    {"name": "COMMON-noscope", "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token", "scope": ""},
]

GRAPH_TOKEN_ENDPOINTS = [
    {"name": "GRAPH-LIVE", "url": "https://login.live.com/oauth20_token.srf", "scope": GRAPH_SCOPE},
    {"name": "GRAPH-CONSUMERS", "url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", "scope": GRAPH_SCOPE},
    {"name": "GRAPH-COMMON", "url": "https://login.microsoftonline.com/common/oauth2/v2.0/token", "scope": GRAPH_SCOPE},
]


def _request_outlook_access_token(account: MailAccount, endpoint: dict[str, str], proxies, log: Callable[[str], None] | None = None) -> str:
    data = {
        "client_id": account.client_id,
        "grant_type": "refresh_token",
        "refresh_token": account.refresh_token,
    }
    if endpoint.get("scope"):
        data["scope"] = endpoint["scope"]
    if endpoint.get("resource"):
        data["resource"] = endpoint["resource"]
    if log:
        log(f"[{account.email}] Try Outlook token endpoint {endpoint['name']}")
    try:
        resp = requests.post(endpoint["url"], data=data, headers={"Accept": "application/json"}, timeout=20, proxies=proxies)
    except requests.RequestException as exc:
        raise MailboxAccessError(
            "mailbox_network_error",
            "邮箱服务网络连接失败，请检查服务器出网、代理与 Microsoft 服务连通性",
            str(exc),
        ) from exc
    try:
        payload = resp.json() if resp.text else {}
    except ValueError as exc:
        raise MailboxAccessError(
            "mailbox_service_response_invalid",
            "Microsoft 邮箱服务返回了无法解析的响应，请稍后重试",
            f"HTTP {resp.status_code}",
        ) from exc
    if resp.ok and payload.get("access_token"):
        if log:
            log(f"[{account.email}] Outlook token endpoint {endpoint['name']} succeeded")
        return str(payload["access_token"])
    raise _outlook_token_error(resp.status_code, payload)


def refresh_hotmail_access_token(account: MailAccount, proxy_url: str = "", log: Callable[[str], None] | None = None) -> tuple[str, str]:
    errors: list[str] = []
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    for endpoint in TOKEN_ENDPOINTS:
        try:
            return _request_outlook_access_token(account, endpoint, proxies, log), str(endpoint["name"])
        except Exception as exc:
            errors.append(f"{endpoint['name']}: {exc}")
            if log:
                log(f"[{account.email}] Outlook token endpoint {endpoint['name']} failed: {exc}")
            if isinstance(exc, MailboxAccessError) and exc.terminal:
                raise
    raise _aggregate_mailbox_error(errors)


def decode_header_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def html_to_text(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", text))


def extract_message_text(msg) -> str:
    parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        parts.append(html_to_text(text) if part.get_content_type() == "text/html" else text)
    return "\n".join(parts)


class ProxiedIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, proxied_socket: socket.socket, timeout: float | None = None):
        self._proxied_socket = proxied_socket
        super().__init__(host=host, port=port, timeout=timeout)

    def open(self, host: str = "", port: int = 0, timeout: float | None = None):
        self.host = host
        self.port = port
        self.sock = self._proxied_socket
        self.file = self.sock.makefile("rb")


class HotmailReader:
    """Microsoft Outlook/Hotmail reader with Graph-first and IMAP fallback."""

    def __init__(self, account: MailAccount, log: Callable[[str], None] | None, proxy_url: str = ""):
        self.account = account
        self.log = log or (lambda _m: None)
        self.proxy_url = proxy_url
        self.imap: imaplib.IMAP4_SSL | None = None
        self.graph_access_token = ""
        self.graph_proxies: dict[str, str] | None = None
        self.seen: set[str] = set()

    def connect(self, access_token: str | None = None) -> None:
        self.log(f"[{self.account.email}] Connecting Outlook mailbox for OTP")
        if access_token is not None:
            self._connect_with_access_token_routes(access_token, "provided")
            return
        errors: list[str] = []
        request_routes = [None]
        if self.proxy_url:
            request_routes.append({"http": self.proxy_url, "https": self.proxy_url})
        if self._connect_graph_routes(request_routes, errors):
            return
        for endpoint in TOKEN_ENDPOINTS:
            for request_proxies in request_routes:
                route_name = "proxy" if request_proxies else "direct"
                try:
                    token = _request_outlook_access_token(self.account, endpoint, request_proxies, self.log)
                    self._connect_with_access_token_routes(token, f"{endpoint['name']} token-{route_name}")
                    return
                except Exception as exc:
                    errors.append(f"{endpoint['name']}/{route_name}: {exc}")
                    self.log(f"[{self.account.email}] Outlook IMAP connect via {endpoint['name']}/{route_name} failed: {exc}")
                    self.close()
                    if isinstance(exc, MailboxAccessError) and exc.terminal:
                        raise
                    time.sleep(0.5)
        raise _aggregate_mailbox_error(errors)

    def _connect_graph_routes(self, request_routes, errors: list[str]) -> bool:
        for endpoint in GRAPH_TOKEN_ENDPOINTS:
            for request_proxies in request_routes:
                route_name = "proxy" if request_proxies else "direct"
                try:
                    token = _request_outlook_access_token(self.account, endpoint, request_proxies, self.log)
                    self._graph_request(token, request_proxies, limit=1)
                    self.graph_access_token = token
                    self.graph_proxies = request_proxies
                    self.log(f"[{self.account.email}] Outlook Graph connected via {endpoint['name']}/{route_name}")
                    return True
                except Exception as exc:
                    errors.append(f"{endpoint['name']}/{route_name}: {exc}")
                    self.log(f"[{self.account.email}] Outlook Graph connect via {endpoint['name']}/{route_name} failed: {exc}")
                    if isinstance(exc, MailboxAccessError) and exc.terminal:
                        raise
        return False

    def _graph_request(self, access_token: str, proxies, limit: int) -> list[dict[str, Any]]:
        params = {
            "$top": str(max(1, min(50, limit))),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,isRead",
        }
        response = requests.get(
            GRAPH_MESSAGES_URL,
            params=params,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Prefer": 'outlook.body-content-type="html"',
            },
            timeout=25,
            proxies=proxies,
        )
        if not response.ok:
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message") or payload.get("error")
            except Exception:
                detail = response.text[:300]
            if response.status_code == 401:
                raise MailboxAccessError(
                    "mailbox_credential_expired",
                    "Outlook Graph 凭证已过期或被撤销",
                    f"Graph HTTP {response.status_code}: {detail}",
                    terminal=True,
                )
            if response.status_code == 403:
                raise MailboxAccessError(
                    "mailbox_scope_mismatch",
                    "Outlook Graph 权限不足，正在尝试 IMAP 兼容通道",
                    f"Graph HTTP 403: {detail}",
                )
            raise RuntimeError(f"Graph HTTP {response.status_code}: {detail}")
        payload = response.json()
        return list(payload.get("value") or [])

    def _graph_messages(self, limit: int) -> list[dict[str, Any]]:
        if not self.graph_access_token:
            return []
        messages = self._graph_request(self.graph_access_token, self.graph_proxies, limit)
        return [self._graph_message_item(message) for message in messages]

    def _graph_message_item(self, message: dict[str, Any]) -> dict[str, Any]:
        sender = message.get("from", {}).get("emailAddress", {}) or {}
        sender_text = str(sender.get("address") or "")
        if sender.get("name"):
            sender_text = f"{sender['name']} <{sender_text}>" if sender_text else str(sender["name"])
        recipients = []
        for recipient in message.get("toRecipients") or []:
            address = recipient.get("emailAddress", {}) or {}
            if address.get("address"):
                recipients.append(str(address["address"]))
        body_info = message.get("body") or {}
        body_raw = str(body_info.get("content") or message.get("bodyPreview") or "")
        body_text = html_to_text(body_raw) if str(body_info.get("contentType") or "").lower() == "html" else body_raw
        subject = str(message.get("subject") or "")
        return {
            "id": str(message.get("id") or ""),
            "email": self.account.email,
            "folder": "Graph",
            "subject": subject,
            "from": sender_text,
            "to": ", ".join(recipients),
            "date": str(message.get("receivedDateTime") or ""),
            "body": body_text,
            "body_preview": str(message.get("bodyPreview") or body_text)[:1200],
            "raw_html": body_raw,
            "otp": extract_otp(subject + "\n" + body_text),
            "source": "graph",
        }

    def _imap_proxy_candidates(self) -> list[str]:
        dedicated_proxy = os.getenv("OUTLOOK_IMAP_PROXY", "").strip()
        fallback_proxy = dedicated_proxy or self.proxy_url
        direct_first = os.getenv("OUTLOOK_IMAP_DIRECT_FIRST", "false").strip().lower() not in {"0", "false", "no", "off"}
        candidates = ["", fallback_proxy] if direct_first else [fallback_proxy, ""]
        return list(dict.fromkeys(candidate for candidate in candidates if candidate or candidate == ""))

    def _connect_with_access_token_routes(self, access_token: str, token_endpoint: str) -> None:
        errors: list[str] = []
        for proxy_url in self._imap_proxy_candidates():
            route_name = "IPv4 direct" if not proxy_url else ("dedicated proxy" if os.getenv("OUTLOOK_IMAP_PROXY", "").strip() else "task proxy")
            try:
                self._connect_with_access_token(access_token, token_endpoint, proxy_url)
                self.log(f"[{self.account.email}] Outlook IMAP route selected: {route_name}")
                return
            except Exception as exc:
                errors.append(f"{route_name}: {exc}")
                self.log(f"[{self.account.email}] Outlook IMAP {route_name} failed: {exc}")
                self.close()
        raise _aggregate_mailbox_error(errors)

    def _connect_with_access_token(self, access_token: str, token_endpoint: str, proxy_url: str = "") -> None:
        auth = f"user={self.account.email}\x01auth=Bearer {access_token}\x01\x01"
        if proxy_url:
            self.imap = self._connect_imap_via_proxy(proxy_url)
        else:
            self.imap = self._connect_imap_direct_ipv4()
        self.imap.authenticate("XOAUTH2", lambda _: auth.encode("utf-8"))
        try:
            self.imap.sock.settimeout(30)
        except Exception:
            pass
        self.log(f"[{self.account.email}] Outlook IMAP connected via {token_endpoint}")

    def _connect_imap_direct_ipv4(self) -> imaplib.IMAP4_SSL:
        errors: list[str] = []
        addresses = socket.getaddrinfo(OUTLOOK_IMAP_HOST, OUTLOOK_IMAP_PORT, socket.AF_INET, socket.SOCK_STREAM)
        for family, socktype, proto, _canonname, address in addresses:
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(20)
            try:
                raw.connect(address)
                tls_sock = ssl.create_default_context().wrap_socket(raw, server_hostname=OUTLOOK_IMAP_HOST)
                tls_sock.settimeout(20)
                return ProxiedIMAP4SSL(OUTLOOK_IMAP_HOST, OUTLOOK_IMAP_PORT, tls_sock, timeout=20)
            except Exception as exc:
                errors.append(f"{address[0]}:{address[1]}: {exc}")
                try:
                    raw.close()
                except Exception:
                    pass
        raise OSError("Outlook IMAP IPv4 connection failed -> " + " | ".join(errors))

    def _connect_imap_via_proxy(self, proxy_url: str) -> imaplib.IMAP4_SSL:
        proxy_url = normalize_proxy_url(proxy_url)
        parsed = urlparse(proxy_url)
        if parsed.scheme in {"socks5", "socks5h"}:
            return self._connect_imap_via_socks5(parsed)
        if parsed.scheme != "http" or not parsed.hostname:
            raise RuntimeError(f"IMAP proxy only supports HTTP CONNECT or SOCKS5: {proxy_url}")
        raw = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=30)
        target = f"{OUTLOOK_IMAP_HOST}:{OUTLOOK_IMAP_PORT}"
        request = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", "Proxy-Connection: keep-alive"]
        if parsed.username:
            token = base64.b64encode(f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode("utf-8")).decode("ascii")
            request.append(f"Proxy-Authorization: Basic {token}")
        raw.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin1"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 65536:
            chunk = raw.recv(4096)
            if not chunk:
                break
            response += chunk
        status = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if " 200 " not in f" {status} ":
            raw.close()
            raise RuntimeError(f"IMAP proxy CONNECT failed: {status}")
        tls_sock = ssl.create_default_context().wrap_socket(raw, server_hostname=OUTLOOK_IMAP_HOST)
        try:
            tls_sock.settimeout(20)
        except Exception:
            pass
        return ProxiedIMAP4SSL(OUTLOOK_IMAP_HOST, OUTLOOK_IMAP_PORT, tls_sock, timeout=20)

    def _connect_imap_via_socks5(self, parsed) -> imaplib.IMAP4_SSL:
        if not parsed.hostname:
            raise RuntimeError("SOCKS5 proxy host is empty")
        raw = socket.create_connection((parsed.hostname, parsed.port or 1080), timeout=30)
        try:
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            if username:
                raw.sendall(b"\x05\x02\x00\x02")
            else:
                raw.sendall(b"\x05\x01\x00")
            resp = raw.recv(2)
            if len(resp) != 2 or resp[0] != 5 or resp[1] == 0xFF:
                raise RuntimeError("SOCKS5 greeting failed")
            if resp[1] == 0x02:
                ub = username.encode("utf-8")
                pb = password.encode("utf-8")
                if len(ub) > 255 or len(pb) > 255:
                    raise RuntimeError("SOCKS5 username/password is too long")
                raw.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
                auth = raw.recv(2)
                if len(auth) != 2 or auth[1] != 0:
                    raise RuntimeError("SOCKS5 authentication failed")
            host = OUTLOOK_IMAP_HOST.encode("idna")
            port = OUTLOOK_IMAP_PORT.to_bytes(2, "big")
            raw.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + port)
            head = raw.recv(4)
            if len(head) != 4 or head[1] != 0:
                raise RuntimeError(f"SOCKS5 CONNECT failed: {head!r}")
            atyp = head[3]
            if atyp == 1:
                raw.recv(4)
            elif atyp == 3:
                size = raw.recv(1)[0]
                raw.recv(size)
            elif atyp == 4:
                raw.recv(16)
            raw.recv(2)
            tls_sock = ssl.create_default_context().wrap_socket(raw, server_hostname=OUTLOOK_IMAP_HOST)
            try:
                tls_sock.settimeout(20)
            except Exception:
                pass
            return ProxiedIMAP4SSL(OUTLOOK_IMAP_HOST, OUTLOOK_IMAP_PORT, tls_sock, timeout=20)
        except Exception:
            raw.close()
            raise

    def close(self) -> None:
        if self.imap:
            try:
                self.imap.logout()
            except Exception:
                pass
        self.imap = None
        self.graph_access_token = ""
        self.graph_proxies = None

    def _select_folder(self, folder: str) -> bool:
        assert self.imap is not None
        for name in (folder, f'"{folder}"'):
            try:
                status, _ = self.imap.select(name, readonly=True)
                if status == "OK":
                    return True
            except Exception:
                continue
        return False

    def latest_message(self) -> dict[str, Any]:
        if self.graph_access_token:
            items = self._graph_messages(1)
            return items[0] if items else {"email": self.account.email, "empty": True, "source": "graph"}
        assert self.imap is not None
        for folder in ("INBOX", "Junk", "Junk Email"):
            try:
                if not self._select_folder(folder):
                    continue
                status, data = self.imap.search(None, "ALL")
                if status != "OK" or not data or not data[0]:
                    continue
                msg_id = data[0].split()[-1]
                status, msg_data = self.imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue
                raw = next((item[1] for item in msg_data if isinstance(item, tuple)), None)
                if not raw:
                    continue
                msg = email_pkg.message_from_bytes(raw)
                subject = decode_header_text(msg.get("Subject"))
                body = extract_message_text(msg)
                return {
                    "email": self.account.email,
                    "folder": folder,
                    "subject": subject,
                    "from": decode_header_text(msg.get("From")),
                    "date": msg.get("Date"),
                    "body_preview": body[:1200],
                    "otp": extract_otp(subject + "\n" + body),
                }
            except Exception:
                continue
        return {"email": self.account.email, "empty": True}

    def wait_for_code(self, min_timestamp: float, timeout: int = 120) -> str:
        if self.imap is None and not self.graph_access_token:
            self.connect()
        started = time.time()
        last_notice = 0.0
        while time.time() - started < timeout:
            if self.graph_access_token:
                code = self._scan_graph(min_timestamp)
                if code:
                    return code
            for folder in ("INBOX", "Junk", "Junk Email"):
                if self.imap is None:
                    break
                code = self._scan_folder(folder, min_timestamp)
                if code:
                    return code
            if time.time() - last_notice >= 20:
                remain = max(0, int(timeout - (time.time() - started)))
                self.log(f"[{self.account.email}] Still waiting for OpenAI email OTP, about {remain}s left")
                last_notice = time.time()
            time.sleep(5)
        raise TimeoutError("Timed out waiting for OpenAI email OTP")

    def _scan_graph(self, min_timestamp: float) -> str:
        try:
            for item in self._graph_messages(30):
                key = f"graph:{item.get('id', '')}"
                if key in self.seen:
                    continue
                if not _recipient_matches(self.account.email, str(item.get("to") or "")):
                    continue
                try:
                    received = datetime.fromisoformat(str(item.get("date") or "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    received = time.time()
                if received + 30 < min_timestamp:
                    continue
                haystack = f"{item.get('subject', '')}\n{item.get('from', '')}\n{item.get('body', '')}"
                if not re.search(r"openai|chatgpt", haystack, flags=re.I):
                    continue
                self.seen.add(key)
                code = extract_otp(haystack)
                if code:
                    self.log(f"[{self.account.email}] Received OpenAI OTP from Graph ({len(code)} digits, redacted)")
                    return code
        except Exception as exc:
            self.log(f"[{self.account.email}] Outlook Graph OTP scan failed: {exc}")
            if isinstance(exc, MailboxAccessError) and exc.terminal:
                raise
        return ""

    def _scan_folder(self, folder: str, min_timestamp: float) -> str:
        assert self.imap is not None
        try:
            if not self._select_folder(folder):
                return ""
            status, data = self.imap.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return ""
            for msg_id in reversed(data[0].split()[-30:]):
                key = f"{folder}:{msg_id.decode(errors='ignore')}"
                if key in self.seen:
                    continue
                status, msg_data = self.imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue
                raw = next((item[1] for item in msg_data if isinstance(item, tuple)), None)
                if not raw:
                    continue
                msg = email_pkg.message_from_bytes(raw)
                try:
                    mail_time = parsedate_to_datetime(msg.get("Date")).timestamp() if msg.get("Date") else time.time()
                except Exception:
                    mail_time = time.time()
                if mail_time + 30 < min_timestamp:
                    continue
                subject = decode_header_text(msg.get("Subject"))
                sender = decode_header_text(msg.get("From"))
                recipient = decode_header_text(msg.get("To"))
                if not _recipient_matches(self.account.email, recipient):
                    continue
                body = extract_message_text(msg)
                haystack = f"{subject}\n{sender}\n{body}"
                if not re.search(r"openai|chatgpt", haystack, flags=re.I):
                    continue
                self.seen.add(key)
                code = extract_otp(haystack)
                if code:
                    self.log(f"[{self.account.email}] Received OpenAI OTP ({len(code)} digits, redacted)")
                    return code
        except Exception:
            return ""
        return ""


class XbovoICloudReader:
    """xbovo iCloud API adapter implementing the mailbox reader contract."""

    def __init__(self, account: MailAccount, log: Callable[[str], None] | None, proxy_url: str = ""):
        self.account = account
        self.log = log or (lambda _m: None)
        self.proxy_url = proxy_url
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.seen_codes: set[str] = set()

    def _request(self, path: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        query = dict(params)
        attempts = XBOVO_POOL_RETRIES + 1
        for attempt in range(attempts):
            acquired = _XBOVO_REQUEST_GATE.acquire(timeout=XBOVO_QUEUE_TIMEOUT)
            if not acquired:
                raise MailboxAccessError(
                    "mailbox_provider_busy",
                    "iCloud 邮箱渠道当前请求较多，请稍后重试",
                    f"local concurrency queue timed out after {XBOVO_QUEUE_TIMEOUT}s",
                )
            response = None
            payload: dict[str, Any] = {}
            request_error: Exception | None = None
            try:
                response = requests.get(
                    f"{XBOVO_API_BASE_URL}{path}",
                    params=query,
                    headers={"Accept": "application/json", "X-API-Key": self.account.access_key},
                    timeout=timeout,
                    proxies=self.proxies,
                )
                try:
                    payload = response.json() if response.text else {}
                except (ValueError, json.JSONDecodeError) as exc:
                    request_error = exc
            except requests.RequestException as exc:
                request_error = exc
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                _XBOVO_REQUEST_GATE.release()

            try:
                status_code = int(getattr(response, "status_code", 0) or 0)
            except (TypeError, ValueError):
                status_code = 0
            detail = str(payload.get("error") or payload.get("message") or request_error or f"HTTP {status_code}")
            lower = detail.lower()
            pool_busy = status_code in {429, 503} or any(
                marker in lower
                for marker in ("poolerror", "connection pool exhausted", "pool exhausted", "too many connections")
            )
            if pool_busy and attempt + 1 < attempts:
                delay = min(4.0, 0.5 * (2**attempt)) + random.uniform(0, 0.35)
                self.log(
                    f"[{self.account.email}] xbovo 连接池繁忙，等待 {delay:.1f}s 后重试 "
                    f"{attempt + 1}/{XBOVO_POOL_RETRIES}"
                )
                time.sleep(delay)
                continue
            if pool_busy:
                raise MailboxAccessError(
                    "mailbox_provider_busy",
                    "iCloud 邮箱渠道当前请求较多，请稍后重试",
                    detail,
                )
            if request_error is not None:
                if isinstance(request_error, requests.RequestException):
                    raise MailboxAccessError(
                        "mailbox_network_error",
                        "iCloud 邮箱渠道网络连接失败，请检查服务器出网与代理配置",
                        detail,
                    ) from request_error
                raise MailboxAccessError(
                    "mailbox_service_response_invalid",
                    "iCloud 邮箱渠道返回了无法解析的响应，请稍后重试",
                    f"HTTP {status_code}",
                ) from request_error
            if response is not None and response.ok and payload.get("ok") is True:
                return payload
            if "key" in lower or "密钥" in detail or "不正确" in detail or "无效" in detail:
                raise MailboxAccessError(
                    "mailbox_credential_invalid",
                    "iCloud 邮箱查询 Key 无效，请检查 xbovo 邮箱凭证",
                    detail,
                    terminal=True,
                )
            raise MailboxAccessError("mailbox_provider_failed", "iCloud 邮箱渠道请求失败，请稍后重试", detail)
        raise MailboxAccessError("mailbox_provider_busy", "iCloud 邮箱渠道当前请求较多，请稍后重试")

    def connect(self, access_token: str | None = None) -> None:
        if self.account.mailbox_channel != "xbovo":
            raise MailboxAccessError("mailbox_channel_unsupported", "暂不支持该 iCloud 邮箱渠道", self.account.mailbox_channel, terminal=True)
        self.log(f"[{self.account.email}] Connecting xbovo iCloud mailbox API for OTP")
        try:
            self._request("/api/v1/messages", {"email": self.account.email, "limit": 1}, timeout=10)
        except MailboxAccessError as exc:
            if exc.terminal:
                raise
            self.log(
                f"[{self.account.email}] xbovo iCloud 启动连通检查暂时失败，将在验证码阶段继续重试：{str(exc)[:180]}"
            )
            return
        self.log(f"[{self.account.email}] xbovo iCloud mailbox API connected")

    def close(self) -> None:
        return None

    def latest_message(self) -> dict[str, Any]:
        payload = self._request("/api/v1/messages", {"email": self.account.email, "limit": 1})
        messages = payload.get("messages") or []
        if not messages:
            return {"email": self.account.email, "empty": True, "source": "xbovo"}
        message = dict(messages[0])
        preview = str(message.get("preview") or "")
        otp = str(message.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", otp):
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", preview)
            otp = match.group(1) if match else ""
        return {
            "id": str(message.get("id") or ""),
            "email": self.account.email,
            "folder": "iCloud",
            "subject": str(message.get("subject") or ""),
            "from": str(message.get("from") or ""),
            "to": str(message.get("to") or message.get("alias_email") or self.account.email),
            "date": str(message.get("received_at") or ""),
            "body": preview,
            "body_preview": preview,
            "otp": otp,
            "source": "xbovo",
        }

    def wait_for_code(self, min_timestamp: float, timeout: int = 120) -> str:
        started = time.monotonic()
        last_notice = 0.0
        last_error_notice = 0.0
        while time.monotonic() - started < timeout:
            elapsed = time.monotonic() - started
            remaining = max(1, int(timeout - elapsed))
            chunk = min(XBOVO_LONG_POLL_SECONDS, remaining)
            params: dict[str, Any] = {
                "email": self.account.email,
                "timeout": chunk,
                "interval": 2,
                "after": int(min_timestamp),
            }
            if self.seen_codes:
                params["exclude"] = ",".join(sorted(self.seen_codes))
            try:
                payload = self._request("/api/v1/code/wait", params, timeout=chunk + 10)
            except MailboxAccessError as exc:
                if exc.terminal:
                    raise
                if time.monotonic() - last_error_notice >= 15:
                    self.log(f"[{self.account.email}] xbovo iCloud 临时不可用，将继续等待验证码：{str(exc)[:180]}")
                    last_error_notice = time.monotonic()
                time.sleep(min(1.0, max(0.0, timeout - (time.monotonic() - started))))
                continue
            code = str(payload.get("code") or "").strip()
            if re.fullmatch(r"\d{6}", code):
                self.seen_codes.add(code)
                self.log(f"[{self.account.email}] Received OpenAI OTP from xbovo iCloud API ({len(code)} digits, redacted)")
                return code
            if code:
                self.seen_codes.add(code)
                self.log(f"[{self.account.email}] xbovo returned a non-OpenAI code; ignored and continuing to wait")
            if time.monotonic() - last_notice >= 20:
                self.log(f"[{self.account.email}] Still waiting for OpenAI email OTP via xbovo, about {remaining}s left")
                last_notice = time.monotonic()
        raise TimeoutError("Timed out waiting for OpenAI email OTP")


class RemailReader:
    """Remail order/pickup adapter. Every Remail domain uses this API channel."""

    def __init__(self, account: MailAccount, log: Callable[[str], None] | None, proxy_url: str = ""):
        self.account = account
        self.log = log or (lambda _m: None)
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.pickup_url = ""
        self.pickup_email = ""
        try:
            parsed_access = urlparse(str(account.access_key or "").strip())
            if parsed_access.scheme in {"http", "https"} and parsed_access.netloc and parsed_access.path.rstrip("/").lower() == "/v1/pickup":
                self.pickup_url = parsed_access.geturl()
                query = dict(parse_qsl(parsed_access.query, keep_blank_values=True))
                self.pickup_email = str(query.get("email") or account.email).strip()
                metadata = {"base_url": f"{parsed_access.scheme}://{parsed_access.netloc}", "service_token": query.get("token", "")}
            else:
                metadata = json.loads(account.access_key)
        except (TypeError, ValueError):
            metadata = {"service_token": account.access_key}
        self.base_url = str(metadata.get("base_url") or "https://remail.aishop6.com").strip().rstrip("/")
        self.api_key = str(metadata.get("api_key") or "").strip()
        self.order_no = str(metadata.get("order_no") or "").strip()
        self.service_token = str(metadata.get("service_token") or account.access_key).strip()
        self.receive_until = str(metadata.get("receive_until") or "").strip()
        self.seen_keys: set[str] = set()
        self._latest_snapshot: dict[str, Any] = {}

    def _request(self, path: str, params: dict[str, str] | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "SunnyRegister/1.0"}
        if self.api_key:
            headers.update({"X-API-Key": self.api_key, "Authorization": f"Bearer {self.api_key}"})
        try:
            response = requests.get(self.base_url + path, params=params or {}, headers=headers, timeout=25, proxies=self.proxies)
        except requests.RequestException as exc:
            raise MailboxAccessError("remail_network_error", "Remail 邮箱接口连接失败", str(exc)) from exc
        try:
            if response.status_code in {401, 403}:
                raise MailboxAccessError("remail_credential_invalid", "Remail API Key 无效或已失效", f"HTTP {response.status_code}", terminal=True)
            if response.status_code == 404:
                return {}
            if not response.ok:
                raise MailboxAccessError("remail_provider_failed", "Remail 邮箱接口请求失败", f"HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                return json.loads(response.text or "{}")
        finally:
            response.close()

    @staticmethod
    def _nested(payload: Any) -> list[Any]:
        values: list[Any] = []
        if isinstance(payload, dict):
            values.append(payload)
            for key in ("data", "order", "message", "messages", "items", "result"):
                if key in payload:
                    values.extend(RemailReader._nested(payload[key]))
        elif isinstance(payload, list):
            for item in payload:
                values.extend(RemailReader._nested(item))
        return values

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if numeric > 1e14:
                numeric /= 1_000_000
            elif numeric > 1e11:
                numeric /= 1_000
            return numeric if numeric > 0 else 0.0
        raw = str(value or "").strip()
        if not raw:
            return 0.0
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return RemailReader._timestamp(float(raw))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _latest(self) -> dict[str, Any]:
        payloads: list[Any] = []
        if self.pickup_url:
            payloads.append(self._request("/v1/pickup", {"email": self.pickup_email or self.account.email, "token": self.service_token}))
        elif self.order_no:
            payloads.append(self._request(f"/v1/open/orders/{unquote(self.order_no)}"))
        else:
            payloads.append(self._request("/v1/pickup", {"email": self.account.email, "token": self.service_token}))
        candidates: list[dict[str, Any]] = []
        for payload in payloads:
            for item in self._nested(payload):
                code = ""
                for key in ("verificationCode", "verification_code", "otp", "code"):
                    candidate = str(item.get(key) or "").strip()
                    if re.fullmatch(r"\d{6}", candidate):
                        code = candidate
                        break
                if not code:
                    for key in ("bodyPreview", "body_preview", "body", "content", "html", "text", "preview", "snippet"):
                        match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(item.get(key) or ""))
                        if match:
                            code = match.group(1)
                            break
                if not code:
                    continue
                timestamp = 0.0
                for key in ("lastMailReceivedAt", "receivedAt", "received_at", "createdAt", "created_at", "date"):
                    timestamp = self._timestamp(item.get(key))
                    if timestamp:
                        break
                key = f"{item.get('id') or item.get('messageId') or timestamp}:{code}"
                body = str(item.get("body") or item.get("bodyPreview") or item.get("body_preview") or item.get("content") or item.get("html") or "")
                candidates.append({"code": code, "key": key, "timestamp": timestamp, "body": body, "id": item.get("id"), "sender": item.get("sender") or item.get("from"), "recipient": item.get("recipient") or item.get("to"), "subject": item.get("subject"), "date": item.get("receivedAt") or item.get("received_at") or item.get("date")})
        candidate = max(candidates, key=lambda item: (float(item.get("timestamp") or 0), str(item.get("key") or "")), default=None)
        self._latest_snapshot = candidate or {}
        return candidate or {}

    def connect(self, access_token: str | None = None) -> None:
        if not self.service_token:
            raise MailboxAccessError("remail_credential_invalid", "Remail serviceToken 缺失", terminal=True)
        try:
            current = self._latest()
            if current.get("key"):
                self.seen_keys.add(str(current["key"]))
        except MailboxAccessError as exc:
            if exc.terminal:
                raise
            self.log(f"[{self.account.email}] Remail 建立取件基线暂时失败，将在验证码阶段重试：{exc}")

    def close(self) -> None:
        return None

    def latest_message(self) -> dict[str, Any]:
        current = self._latest()
        return {"id": current.get("id") or current.get("key", "remail"), "email": self.account.email, "from": current.get("sender", ""), "to": current.get("recipient", self.account.email), "subject": current.get("subject") or "Remail", "date": current.get("date", ""), "body": current.get("body", ""), "body_preview": current.get("body", ""), "otp": current.get("code", ""), "source": "remail_api"}

    def wait_for_code(self, min_timestamp: float, timeout: int = 120) -> str:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            try:
                current = self._latest()
            except MailboxAccessError as exc:
                if exc.terminal:
                    raise
                time.sleep(2)
                continue
            timestamp = float(current.get("timestamp") or 0)
            key = str(current.get("key") or "")
            code = str(current.get("code") or "").strip()
            if code and re.fullmatch(r"\d{6}", code) and key not in self.seen_keys and (not timestamp or timestamp >= float(min_timestamp or 0)):
                self.seen_keys.add(key)
                self.log(f"[{self.account.email}] 已通过 Remail API 收到邮箱验证码（已脱敏）")
                return code
            time.sleep(2)
        raise TimeoutError("Timed out waiting for OpenAI email OTP via Remail API")


class DomainMailReader:
    """CloudMail/CF Worker-style self-hosted domain mailbox adapter."""

    def __init__(self, account: MailAccount, log: Callable[[str], None] | None, proxy_url: str = ""):
        self.account = account
        self.log = log or (lambda _m: None)
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.pickup_url = ""
        self.base_url = ""
        self.auth_token = ""
        self.site_password = ""
        access_key = str(account.access_key or "").strip()
        if access_key.startswith(("http://", "https://")):
            parsed = urlparse(access_key)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            credential_email = str(query.get("email") or "").strip().lower()
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not query.get("token") or credential_email != account.email.strip().lower():
                raise MailboxAccessError("domain_credential_invalid", "自建域名邮箱取件 URL 无效或与邮箱不匹配", terminal=True)
            self.pickup_url = access_key
        else:
            try:
                metadata = json.loads(access_key)
            except (TypeError, ValueError) as exc:
                raise MailboxAccessError("domain_credential_invalid", "自建域名邮箱凭证格式无效", terminal=True) from exc
            self.base_url = str(metadata.get("base_url") or "").strip().rstrip("/")
            self.auth_token = str(metadata.get("auth_token") or "").strip()
            self.site_password = str(metadata.get("site_password") or os.getenv("CLOUDMAIL_SITE_PASSWORD") or "").strip()
            if not self.base_url or not self.auth_token:
                raise MailboxAccessError("domain_credential_invalid", "自建域名邮箱凭证缺少 API 地址或 Authorization Token", terminal=True)
        self.seen_keys: set[str] = set()
        self.request_count = 0
        self.last_status = 0
        self.last_candidate_count = 0
        self.last_error = ""

    @staticmethod
    def _nested(payload: Any) -> list[Any]:
        values: list[Any] = []
        if isinstance(payload, dict):
            values.append(payload)
            for key in ("data", "items", "messages", "result", "list", "rows", "records"):
                if key in payload:
                    values.extend(DomainMailReader._nested(payload[key]))
        elif isinstance(payload, list):
            values.extend(payload)
        return values

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if numeric > 1e14:
                numeric /= 1_000_000
            elif numeric > 1e11:
                numeric /= 1_000
            return numeric if numeric > 0 else 0.0
        raw = str(value or "").strip()
        if not raw:
            return 0.0
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return DomainMailReader._timestamp(float(raw))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            # CloudMail emits timestamps such as "2026-08-24 07:34:15" in UTC without an offset.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _request(self) -> Any:
        self.request_count += 1
        try:
            if self.pickup_url:
                response = requests.get(
                    self.pickup_url,
                    headers={"Accept": "application/json", "User-Agent": "SunnyRegister/1.0"},
                    timeout=30,
                    proxies=self.proxies,
                )
            else:
                response = requests.post(
                    self.base_url + "/api/public/emailList",
                    json={"toEmail": self.account.email, "timeSort": "desc", "type": 0, "isDel": 0, "num": 1, "size": 20},
                    headers={"Authorization": self.auth_token, "X-Auth-Token": self.auth_token, "x-custom-auth": self.site_password, "Accept": "application/json", "User-Agent": "SunnyRegister/1.0"},
                    timeout=30,
                    proxies=self.proxies,
                )
        except requests.RequestException as exc:
            self.last_error = str(exc)
            if self.request_count == 1 or self.request_count % 10 == 0:
                self.log(f"[{self.account.email}] 自建域名邮箱取件 API 网络请求失败（第 {self.request_count} 次）：{str(exc)[:220]}")
            raise MailboxAccessError("domain_network_error", "自建域名邮箱接口连接失败", str(exc)) from exc
        try:
            self.last_status = int(response.status_code or 0)
            if response.status_code in {401, 403}:
                self.last_error = f"HTTP {response.status_code}"
                self.log(f"[{self.account.email}] 自建域名邮箱取件 API 返回 HTTP {response.status_code}，凭证或邮箱状态校验失败")
                raise MailboxAccessError("domain_credential_invalid", "自建域名邮箱取件凭证无效或邮箱已停用", f"HTTP {response.status_code}", terminal=True)
            if not response.ok:
                self.last_error = f"HTTP {response.status_code}"
                if self.request_count == 1 or self.request_count % 10 == 0:
                    self.log(f"[{self.account.email}] 自建域名邮箱取件 API 返回 HTTP {response.status_code}（第 {self.request_count} 次）")
                raise MailboxAccessError("domain_provider_failed", "自建域名邮箱接口请求失败", f"HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                self.last_error = "invalid_json"
                self.log(f"[{self.account.email}] 自建域名邮箱取件 API 返回内容不是有效 JSON（HTTP {response.status_code}）")
                raise MailboxAccessError("domain_response_invalid", "自建域名邮箱接口返回了无法解析的 JSON", str(exc), terminal=True) from exc
        finally:
            response.close()

    def _latest(self) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for order, item in enumerate(self._nested(self._request())):
            if not isinstance(item, dict):
                continue
            body_source = str(item.get("text") or item.get("body") or item.get("content") or item.get("html") or item.get("bodyPreview") or item.get("subject") or "")
            body = _html_to_text(body_source)
            code = ""
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", body)
            code = match.group(1) if match else ""
            if not code:
                for key in ("verificationCode", "verification_code", "otp", "code"):
                    candidate = str(item.get(key) or "").strip()
                    if re.fullmatch(r"\d{6}", candidate):
                        code = candidate
                        break
            if not code:
                continue
            timestamp = 0.0
            for key in ("createTime", "created_at", "receivedAt", "received_at", "timestamp", "time", "date"):
                timestamp = self._timestamp(item.get(key))
                if timestamp:
                    break
            message_id = item.get("emailId") or item.get("id") or item.get("messageId") or timestamp
            candidates.append({"code": code, "key": f"{message_id}:{code}", "timestamp": timestamp, "order": order, "body": body, "id": message_id, "sender": item.get("sendEmail") or item.get("sender") or item.get("from"), "recipient": item.get("toEmail") or item.get("recipient") or item.get("to"), "subject": item.get("subject"), "date": item.get("createTime") or item.get("receivedAt") or item.get("date")})
        self.last_candidate_count = len(candidates)
        if self.request_count == 1 or self.request_count % 10 == 0:
            self.log(f"[{self.account.email}] 自建域名邮箱取件 API：HTTP {self.last_status or '未知'}，第 {self.request_count} 次查询，识别到 {self.last_candidate_count} 封验证码邮件")
        return max(candidates, key=lambda item: (float(item.get("timestamp") or 0), -int(item.get("order") or 0)), default={})

    def connect(self, access_token: str | None = None) -> None:
        current = self._latest()
        if current.get("key"):
            self.seen_keys.add(str(current["key"]))

    def close(self) -> None:
        return None

    def latest_message(self) -> dict[str, Any]:
        current = self._latest()
        return {"id": current.get("id") or current.get("key", "domain"), "email": self.account.email, "from": current.get("sender", ""), "to": current.get("recipient", self.account.email), "subject": current.get("subject") or "Domain mailbox", "date": current.get("date", ""), "body": current.get("body", ""), "body_preview": current.get("body", ""), "otp": current.get("code", ""), "source": "domain_api"}

    def wait_for_code(self, min_timestamp: float, timeout: int = 120) -> str:
        started = time.monotonic()
        last_error_notice = 0.0
        while time.monotonic() - started < timeout:
            remaining = max(1, int(timeout - (time.monotonic() - started)))
            try:
                current = self._latest()
            except MailboxAccessError as exc:
                if exc.terminal:
                    raise
                if time.monotonic() - last_error_notice >= 20:
                    self.log(f"[{self.account.email}] 自建域名邮箱 API 暂时不可用，将继续重试：{str(exc)[:180]}")
                    last_error_notice = time.monotonic()
                time.sleep(min(3, remaining))
                continue
            timestamp = float(current.get("timestamp") or 0)
            key = str(current.get("key") or "")
            code = str(current.get("code") or "").strip()
            if code and re.fullmatch(r"\d{6}", code) and key not in self.seen_keys and (not timestamp or timestamp >= float(min_timestamp or 0)):
                self.seen_keys.add(key)
                self.log(f"[{self.account.email}] 已通过自建域名邮箱 API 收到验证码（已脱敏）")
                return code
            time.sleep(min(2, remaining))
        detail = f"HTTP {self.last_status or '未知'}，累计查询 {self.request_count} 次，最近识别到 {self.last_candidate_count} 封验证码邮件"
        if self.last_error:
            detail += f"，最近错误：{self.last_error[:180]}"
        raise TimeoutError(f"Timed out waiting for OpenAI email OTP via domain mailbox API（{detail}）")


class URLAPIICloudReader:
    """Slow URL-based iCloud adapter returning the newest mailbox message page."""

    def __init__(self, account: MailAccount, log: Callable[[str], None] | None, proxy_url: str = ""):
        self.account = account
        self.log = log or (lambda _m: None)
        self.proxy_url = proxy_url
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.url = _validate_url_api_address(account.access_key)
        self.strategy = _url_api_strategy(self.url)
        self.seen_candidate_keys: set[str] = set()
        self.candidate_counts: dict[str, int] = {}

    def _latest_generic(self, timeout: int = URL_API_REQUEST_TIMEOUT, *, latest_card_only: bool = False) -> dict[str, Any]:
        response = None
        target = self.url
        for redirect_count in range(URL_API_MAX_REDIRECTS + 1):
            target = _validate_url_api_address(target)
            response = self._request_url(
                target,
                headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8", "User-Agent": "Mozilla/5.0"},
                timeout=min(URL_API_REQUEST_TIMEOUT + 5, max(URL_API_REQUEST_TIMEOUT, int(timeout or 0))),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = str(response.headers.get("Location") or "").strip()
            response.close()
            if not location or redirect_count >= URL_API_MAX_REDIRECTS:
                raise MailboxAccessError("mailbox_redirect_invalid", "url_api 取码 URL 跳转次数过多或目标无效", terminal=True)
            target = urljoin(target, location)
        if response is None:
            raise MailboxAccessError("mailbox_network_error", "url_api 邮箱渠道未返回响应")
        if response.status_code in {401, 403, 404, 410}:
            response.close()
            raise MailboxAccessError(
                "mailbox_credential_invalid",
                "url_api 取码 URL 无效、已过期或无权访问",
                f"HTTP {response.status_code}",
                terminal=True,
            )
        if not response.ok:
            response.close()
            raise MailboxAccessError("mailbox_provider_failed", "url_api 邮箱渠道请求失败，请稍后重试", f"HTTP {response.status_code}")
        try:
            content_length = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > URL_API_MAX_RESPONSE_BYTES:
            response.close()
            raise MailboxAccessError("mailbox_response_too_large", "url_api 取码接口返回内容过大", terminal=True)
        chunks: list[bytes] = []
        size = 0
        try:
            iterator = response.iter_content(chunk_size=16 * 1024)
            for chunk in iterator:
                if not chunk:
                    continue
                size += len(chunk)
                if size > URL_API_MAX_RESPONSE_BYTES:
                    raise MailboxAccessError("mailbox_response_too_large", "url_api 取码接口返回内容过大", terminal=True)
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            raw_html = b"".join(chunks).decode(encoding, errors="replace")
        except TypeError:
            # Lightweight test doubles may only expose .text; real requests responses use iter_content.
            raw_html = str(response.text or "")
            if len(raw_html.encode("utf-8")) > URL_API_MAX_RESPONSE_BYTES:
                raise MailboxAccessError("mailbox_response_too_large", "url_api 取码接口返回内容过大", terminal=True)
        finally:
            response.close()
        candidate_html = _ai1998_latest_mail_html(raw_html) if latest_card_only else raw_html
        plain = _html_to_text(candidate_html)
        relevant = bool(re.search(r"openai|chatgpt", plain, flags=re.I))
        candidates = extract_otp_candidates(candidate_html)
        candidate = candidates[0] if candidates else None
        otp = str(candidate.get("code") or "") if candidate else ""
        heading = re.search(r"(?is)<h[1-4]\b[^>]*>(.*?)</h[1-4]>", candidate_html)
        subject = _html_to_text(heading.group(1)) if heading else ""
        if not re.search(r"openai|chatgpt", subject, flags=re.I) or "@" in subject:
            subject = next(
                (
                    line.strip()
                    for line in plain.splitlines()
                    if len(line.strip()) > len("ChatGPT")
                    and len(line.strip()) <= 160
                    and re.search(r"openai|chatgpt", line, flags=re.I)
                    and "@" not in line
                    and "url(" not in line.lower()
                    and "team" not in line.lower()
                ),
                "",
            )
        return {
            "id": f"url-api:{candidate.get('key') if candidate else abs(hash(raw_html))}",
            "email": self.account.email,
            "folder": "iCloud",
            "subject": subject or ("ChatGPT" if relevant else "Latest iCloud mail"),
            "from": "",
            "to": self.account.email,
            "date": "",
            "body": plain,
            "body_preview": plain[:500],
            "raw_html": raw_html,
            "otp": otp,
            "otp_key": str(candidate.get("key") or "") if candidate else "",
            "otp_candidates": candidates,
            "source": "url_api",
        }

    def _latest_mczero(self, timeout: int = URL_API_REQUEST_TIMEOUT) -> dict[str, Any]:
        parsed = urlparse(self.url)
        query = list(parse_qsl(parsed.query, keep_blank_values=True))
        query = [(key, value) for key, value in query if key.lower() not in {"format", "refresh"}]
        query.extend([("format", "json"), ("refresh", "1")])
        endpoint = parsed._replace(query=urlencode(query)).geturl()
        response = self._request_url(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=min(URL_API_REQUEST_TIMEOUT + 5, max(URL_API_REQUEST_TIMEOUT, int(timeout or 0))),
            allow_redirects=True,
        )
        response_url = getattr(response, "url", "")
        final_url = urlparse(response_url if isinstance(response_url, str) and response_url else endpoint)
        initial_url = urlparse(endpoint)
        if (final_url.scheme.lower(), final_url.netloc.lower()) != (initial_url.scheme.lower(), initial_url.netloc.lower()):
            response.close()
            raise MailboxAccessError("mailbox_url_forbidden", "url_api 取码地址跳转超出当前邮箱渠道域名", terminal=True)
        try:
            if response.status_code in {401, 403, 404, 410}:
                raise MailboxAccessError(
                    "mailbox_credential_invalid",
                    "url_api 取码 URL 无效、已过期或无权访问",
                    f"HTTP {response.status_code}",
                    terminal=True,
                )
            if not response.ok:
                raise MailboxAccessError("mailbox_provider_failed", "url_api 邮箱渠道请求失败，请稍后重试", f"HTTP {response.status_code}")
            try:
                raw_payload = getattr(response, "content", b"")
                if isinstance(raw_payload, (bytes, bytearray)):
                    payload = json.loads(bytes(raw_payload).decode("utf-8-sig", errors="replace"))
                else:
                    payload = response.json()
            except ValueError as exc:
                raise MailboxAccessError("mailbox_service_response_invalid", "url_api 邮箱渠道返回了无法解析的响应，请稍后重试", str(exc)) from exc
        finally:
            response.close()
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            message = {}
        raw_html = "\n".join(
            str(message.get(key) or "")
            for key in ("preview", "body", "content", "html", "text", "snippet")
            if message.get(key)
        )
        plain = _html_to_text(raw_html)
        candidates: list[dict[str, Any]] = []
        for index, value in enumerate(message.get("codes") or []):
            code = str(value or "").strip()
            if re.fullmatch(r"\d{6}", code):
                candidates.append({"code": code, "key": f"url-api:mczero:{message.get('id') or abs(hash(raw_html))}:{index}", "score": 240.0})
        if not candidates:
            candidates = extract_otp_candidates(raw_html)
        candidate = candidates[0] if candidates else None
        subject = str(message.get("subject") or "").strip()
        relevant = bool(re.search(r"openai|chatgpt", subject + "\n" + plain, flags=re.I))
        return {
            "id": f"url-api:{message.get('id') or abs(hash(raw_html))}",
            "email": self.account.email,
            "folder": "iCloud",
            "subject": subject or ("ChatGPT" if relevant else "Latest iCloud mail"),
            "from": str(message.get("from") or ""),
            "to": self.account.email,
            "date": str(message.get("date") or ""),
            "body": plain,
            "body_preview": plain[:500],
            "raw_html": raw_html,
            "otp": str(candidate.get("code") or "") if candidate else "",
            "otp_key": str(candidate.get("key") or "") if candidate else "",
            "otp_candidates": candidates,
            "source": "url_api",
        }

    def _latest(self, timeout: int = URL_API_REQUEST_TIMEOUT, strategy: str | None = None) -> dict[str, Any]:
        selected = strategy or getattr(self, "strategy", "generic")
        if selected == "mczero":
            return self._latest_mczero(timeout)
        return self._latest_generic(timeout, latest_card_only=selected == "ai1998")

    def _request_url(
        self,
        target: str,
        *,
        headers: dict[str, str],
        timeout: int,
        allow_redirects: bool,
        stream: bool = False,
    ):
        acquired = _URL_API_REQUEST_GATE.acquire(timeout=URL_API_QUEUE_TIMEOUT)
        if not acquired:
            raise MailboxAccessError(
                "mailbox_provider_busy",
                "url_api 邮箱渠道当前请求较多，请稍后重试",
                f"local concurrency queue timed out after {URL_API_QUEUE_TIMEOUT}s",
            )
        try:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    return requests.get(
                        target,
                        headers=headers,
                        timeout=timeout,
                        proxies=self.proxies,
                        allow_redirects=allow_redirects,
                        stream=stream,
                    )
                except requests.RequestException as exc:
                    last_error = exc
                    if attempt >= 2:
                        break
                    time.sleep(0.4 * (attempt + 1) + random.uniform(0, 0.4))
            raise MailboxAccessError(
                "mailbox_network_error",
                "url_api 邮箱渠道连接超时或网络不可达，请检查取码 URL、服务器出网与代理配置",
                str(last_error or "request failed"),
            ) from last_error
        finally:
            _URL_API_REQUEST_GATE.release()

    def connect(self, access_token: str | None = None) -> None:
        if self.account.mailbox_channel != "url_api":
            raise MailboxAccessError("mailbox_channel_unsupported", "暂不支持该 iCloud 邮箱渠道", self.account.mailbox_channel, terminal=True)
        self.log(f"[{self.account.email}] Connecting url_api iCloud mailbox URL for OTP")
        try:
            message = self._latest()
        except MailboxAccessError as exc:
            if getattr(self, "strategy", "generic") != "mczero" or exc.terminal:
                raise
            self.log(f"[{self.account.email}] url_api 专用域名接口暂时不可用，将在等待验证码期间保留通用解析兜底")
            try:
                message = self._latest(strategy="generic")
            except MailboxAccessError:
                message = {"otp_candidates": []}
        self.seen_candidate_keys.update(str(item.get("key") or "") for item in message.get("otp_candidates") or [] if item.get("key"))
        self.log(f"[{self.account.email}] url_api iCloud mailbox URL connected")

    def close(self) -> None:
        return None

    def latest_message(self) -> dict[str, Any]:
        return self._latest()

    def wait_for_code(self, min_timestamp: float, timeout: int = 120) -> str:
        started = time.monotonic()
        last_notice = 0.0
        last_error_notice = 0.0
        specialized = getattr(self, "strategy", "generic") == "mczero"
        fallback_at = min(float(timeout), URL_API_SPECIALIZED_FALLBACK_SECONDS) if specialized else 0
        time.sleep(min(random.uniform(0, URL_API_POLL_JITTER_SECONDS), max(0.0, float(timeout))))
        while time.monotonic() - started < timeout:
            remaining = max(1, int(timeout - (time.monotonic() - started)))
            use_specialized = specialized and time.monotonic() - started < fallback_at
            selected_strategy = "mczero" if use_specialized else ("generic" if specialized else getattr(self, "strategy", "generic"))
            try:
                message = self._latest(timeout=max(URL_API_REQUEST_TIMEOUT, remaining), strategy=selected_strategy)
            except MailboxAccessError as exc:
                if exc.terminal:
                    raise
                if time.monotonic() - last_error_notice >= 20:
                    route_label = "专用" if use_specialized else "通用"
                    self.log(f"[{self.account.email}] url_api {route_label}取码接口暂时不可用，将继续重试：{str(exc)[:180]}")
                    last_error_notice = time.monotonic()
                time.sleep(min(3, remaining))
                continue
            unseen = [item for item in message.get("otp_candidates") or [] if item.get("key") not in self.seen_candidate_keys]
            fresh = next((item for item in unseen if float(item.get("score") or 0) >= 40), None)
            if fresh is None:
                for item in unseen:
                    key = str(item.get("key") or "")
                    if key and float(item.get("score") or 0) >= 12:
                        self.candidate_counts[key] = self.candidate_counts.get(key, 0) + 1
                fresh = next(
                    (
                        item for item in unseen
                        if float(item.get("score") or 0) >= 12
                        and self.candidate_counts.get(str(item.get("key") or ""), 0) >= 2
                    ),
                    None,
                )
            code = str((fresh or {}).get("code") or "").strip()
            if re.fullmatch(r"\d{6}", code):
                key = str((fresh or {}).get("key") or "")
                if key:
                    self.seen_candidate_keys.add(key)
                    self.candidate_counts.pop(key, None)
                self.log(f"[{self.account.email}] Received OpenAI OTP from url_api iCloud URL ({len(code)} digits, redacted)")
                return code
            if time.monotonic() - last_notice >= 20:
                self.log(f"[{self.account.email}] Still waiting for OpenAI email OTP via url_api, about {remaining}s left")
                last_notice = time.monotonic()
            time.sleep(min(3, remaining))
        raise TimeoutError("Timed out waiting for OpenAI email OTP")


def create_mailbox_reader(account: MailAccount, log: Callable[[str], None] | None, proxy_url: str = ""):
    if account.mailbox_type == "domain" or account.mailbox_channel == "domain_api":
        return DomainMailReader(account, log, proxy_url)
    if account.mailbox_type == "remail" or account.mailbox_channel == "remail_api":
        return RemailReader(account, log, proxy_url)
    if account.mailbox_type == "apple":
        if account.mailbox_channel == "xbovo":
            return XbovoICloudReader(account, log, proxy_url)
        if account.mailbox_channel == "url_api":
            return URLAPIICloudReader(account, log, proxy_url)
        raise MailboxAccessError("mailbox_channel_unsupported", "暂不支持该 iCloud 邮箱渠道", account.mailbox_channel, terminal=True)
    return HotmailReader(account, log, proxy_url)


def latest_outlook_mail(email: str, client_id: str, refresh_token: str, proxy_url: str = "") -> dict[str, Any]:
    account = MailAccount(email, "", client_id, refresh_token, "")
    reader = HotmailReader(account, lambda _m: None, proxy_url)
    try:
        reader.connect()
        msg = reader.latest_message()
        msg["mail_protocol"] = "graph" if reader.graph_access_token else "imap"
        return msg
    finally:
        reader.close()
