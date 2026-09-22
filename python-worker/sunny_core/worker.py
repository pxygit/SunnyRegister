from __future__ import annotations

import json
import os
import random
import re
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import requests

from .access_token_probe import probe_access_token
from .agent_identity import AgentIdentityUnavailableError, create_agent_identity_auth
from .auth_resilience import classify_auth_failure, retry_allowed
from .browser_traffic import ProxyTrafficMeter, use_traffic_meter
from .db import SunnyDB, SunnyTaskCancelled, now_sql
from .domain_mail_cleanup import cleanup_failed_mailbox
from .firefox_sms import FIREFOX_RELEASE_DELAY_SECONDS, FireFoxSMSClient
from .luban_sms import LubanSMSClient
from .mailbox import MailboxAccessError, account_from_row, parse_account_line
from .openai_auth import TaskCancelledError, login_or_register, refresh_openai_access_token
from .phone_pool import read_sms_candidates, wait_sms_code
from .protocol_auth import ProtocolChallengeRequired, ProtocolRegistrationError, login_or_register_protocol
from .login_secret import setup_login_secret, setup_login_secret_protocol
from .proxy import build_proxy, proxy_target_tls_check, redact_proxy_url
from .proxy_scheduler import ProxyLease, TaskProxyScheduler
from .smsbower import SMSBowerClient
from .smspool import SMSPOOL_CODE_TIMEOUT_SECONDS, SMSPoolClient
from .rebind import rebind_one

REGISTER_ONLY = "register_only"
CODEX_PHONE_BIND = "codex_phone_bind"
IMPORT_REVERSE_PROXY = "import_reverse_proxy"
AGENT_IDENTITY_REVERSE_PROXY = "agent_identity_reverse_proxy"


class RemailOrderError(RuntimeError):
    def __init__(self, message: str, *, insufficient_balance: bool = False):
        super().__init__(message)
        self.insufficient_balance = insufficient_balance


class RemailMailboxProvisioner:
    """Purchase and persist one Remail mailbox for each available task slot."""

    def __init__(self, db: SunnyDB):
        self.db = db
        cfg = db.get_config("remail")
        self.base_url = str(cfg.get("base_url") or "https://remail.aishop6.com").strip().rstrip("/")
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.project_id = int(cfg.get("project_id") or 0)
        self.email_suffix = str(cfg.get("email_suffix") or "").strip()
        self.service_mode = str(cfg.get("service_mode") or "purchase").strip() or "purchase"
        if self.service_mode == "code" and cfg.get("service_mode_explicit") is not True:
            self.service_mode = "purchase"
        self.supply = str(cfg.get("supply") or "private_first").strip() or "private_first"
        if cfg.get("enabled") is not True or not self.base_url or not self.api_key or self.project_id <= 0:
            raise RemailOrderError("Remail 配置不完整或未启用")

    @staticmethod
    def _detail(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "detail"):
                if payload.get(key):
                    return str(payload[key])
        return fallback

    @staticmethod
    def _order(payload: Any) -> dict[str, Any]:
        current = payload if isinstance(payload, dict) else {}
        for key in ("data", "order", "result"):
            if isinstance(current.get(key), dict):
                current = current[key]
                break
        return current

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, idempotency_key: str = "") -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "SunnyRegister/1.0", "X-API-Key": self.api_key, "Authorization": f"Bearer {self.api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = requests.request(method, self.base_url + path, params=params, json=body, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise RemailOrderError(f"Remail 下单请求失败：{exc}") from exc
        try:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not response.ok:
                detail = self._detail(payload, response.text[:500])
                lower = detail.lower()
                insufficient = "余额不足" in detail or "insufficient" in lower and "balance" in lower
                raise RemailOrderError(f"Remail HTTP {response.status_code}：{detail}", insufficient_balance=insufficient)
            return payload if isinstance(payload, dict) else {}
        finally:
            response.close()

    def purchase(self, sequence: int) -> dict[str, Any]:
        self.db.ensure_not_cancelled()
        body: dict[str, Any] = {"projectId": self.project_id}
        if self.email_suffix:
            body["emailSuffix"] = self.email_suffix
        payload = self._request(
            "POST", "/v1/open/orders",
            params={"serviceMode": self.service_mode, "supply": self.supply},
            body=body,
            idempotency_key=f"sunny-{self.db.task_id}-{sequence}-{uuid.uuid4().hex[:8]}",
        )
        order = self._order(payload)
        order_no = str(order.get("orderNo") or "").strip()
        email = str(order.get("deliveryEmail") or "").strip()
        service_token = str(order.get("serviceToken") or "").strip()
        for _attempt in range(20):
            if email and service_token:
                break
            self.db.ensure_not_cancelled()
            if not order_no:
                break
            time.sleep(2)
            order = self._order(self._request("GET", f"/v1/open/orders/{quote(order_no, safe='')}"))
            email = str(order.get("deliveryEmail") or "").strip()
            service_token = str(order.get("serviceToken") or "").strip()
        if not email or not service_token:
            raise RemailOrderError("Remail 下单成功但未返回邮箱或 serviceToken")
        pickup_url = self.base_url + "/v1/pickup?" + urlencode({"email": email, "token": service_token}, safe="@")
        mailbox = self.db.create_remail_mailbox(email, pickup_url)
        self.db.event(
            f"[Remail] 已按需下单第 {sequence} 个邮箱：{email}",
            detail={"scope": "global", "provider": "remail", "mailbox_id": mailbox.get("id"), "sequence": sequence},
        )
        return mailbox

_REGISTRATION_PROGRESS_STEPS = {
    "initializing": 1,
    "proxy_ready": 2,
    "browser_started": 3,
    "protocol_started": 3,
    "email_submitted": 4,
    "email_verified": 5,
    "auth_completed": 6,
    "registered": 7,
    "phone_started": 8,
    "phone_code_received": 9,
    "phone_bound": 10,
    "reverse_importing": 11,
    "reverse_imported": 12,
    "agent_identity_importing": 8,
    "agent_identity_imported": 9,
    "login_secret_started": 1,
    "login_secret_password": 2,
    "login_secret_2fa": 3,
    "login_secret_at_refresh": 4,
    "login_secret_completed": 5,
    "login_secret_failed": 4,
}

_REGISTRATION_STAGE_TOTALS = {
    REGISTER_ONLY: 7,
    CODEX_PHONE_BIND: 10,
    IMPORT_REVERSE_PROXY: 12,
    AGENT_IDENTITY_REVERSE_PROXY: 9,
}

_MAILBOX_PROGRESS_RANK = {
    "未注册": 0,
    "已注册": 1,
    "registered": 1,
    "已接码": 2,
    "phone_bound": 2,
    "已反代": 3,
    "reverse_proxied": 3,
}


class _ProtocolBatchPolicy:
    """Skip repeated protocol attempts after this batch proves they require a browser."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._protocol_attempts = 0
        self._browser_challenges = 0

    def record_challenge(self) -> None:
        with self._lock:
            self._protocol_attempts += 1
            self._browser_challenges += 1

    def record_success(self) -> None:
        with self._lock:
            self._protocol_attempts += 1

    def should_start_in_browser(self) -> bool:
        with self._lock:
            return (
                self._protocol_attempts >= 2
                and self._browser_challenges >= 2
                and self._browser_challenges * 4 >= self._protocol_attempts * 3
            )


def _is_retryable_protocol_transport_error(error: Exception) -> bool:
    return classify_auth_failure(error).category == "transient_transport"


_DEFAULT_SUB2API_MODELS = (
    "codex-auto-review", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6",
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-image-1.5", "gpt-image-2",
)


def _container_host_proxy(proxy_url: str) -> str:
    """Route localhost proxy settings to the Docker host when containerized."""
    if os.getenv("SUNNY_CONTAINERIZED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return proxy_url
    try:
        parsed = urlsplit(proxy_url)
        if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            return proxy_url
        auth = ""
        if "@" in parsed.netloc:
            auth = parsed.netloc.rsplit("@", 1)[0] + "@"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{auth}host.docker.internal{port}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return proxy_url


def _highest_mailbox_progress(current: str, candidate: str) -> str:
    current = str(current or "").strip()
    candidate = str(candidate or "").strip()
    current_rank = _MAILBOX_PROGRESS_RANK.get(current, -1)
    candidate_rank = _MAILBOX_PROGRESS_RANK.get(candidate, -1)
    return current if current_rank >= candidate_rank and current_rank >= 0 else candidate


def _account_status_for_mailbox(status: str) -> str:
    if status == "已反代":
        return "reverse_proxied"
    if status == "已接码":
        return "phone_bound"
    return "registered"


def _ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            n = int(item)
            if n > 0:
                out.append(n)
        except Exception:
            pass
    return out


def _stage(payload: dict[str, Any]) -> str:
    value = str(payload.get("registration_stage") or payload.get("stage") or REGISTER_ONLY).strip().lower()
    return value if value in {REGISTER_ONLY, CODEX_PHONE_BIND, IMPORT_REVERSE_PROXY, AGENT_IDENTITY_REVERSE_PROXY} else REGISTER_ONLY


def _stage_label(stage: str) -> str:
    return {
        REGISTER_ONLY: "仅注册ChatGPT",
        CODEX_PHONE_BIND: "Codex接码绑定",
        IMPORT_REVERSE_PROXY: "导入反代平台",
        AGENT_IDENTITY_REVERSE_PROXY: "绕过接码导入反代平台",
    }.get(stage, stage)


def _registration_stage_total(stage: str) -> int:
    return _REGISTRATION_STAGE_TOTALS.get(stage, _REGISTRATION_STAGE_TOTALS[REGISTER_ONLY])


def _emit_registration_progress(
    db: SunnyDB,
    email: str,
    stage: str,
    checkpoint: str,
    *,
    state: str = "running",
    error: str = "",
    setup_login_secret: bool = False,
) -> None:
    base_total = _registration_stage_total(stage)
    total = base_total + (5 if setup_login_secret else 0)
    if setup_login_secret and checkpoint.startswith("login_secret_"):
        current = base_total + min(5, max(0, _REGISTRATION_PROGRESS_STEPS.get(checkpoint, 0)))
    else:
        current = min(base_total, max(0, _REGISTRATION_PROGRESS_STEPS.get(checkpoint, 0)))
    db.event(
        f"[{email}] registration progress {current}/{total}: {checkpoint}",
        level="error" if state == "abnormal" else "info",
        typ="registration_progress",
        detail={
            "scope": "selected",
            "progress_type": "account_registration",
            "email": email,
            "stage": stage,
            "checkpoint": checkpoint,
            "current": current,
            "total": total,
            "state": state,
            "error": str(error or "")[:500],
        },
    )


def _emit_renewal_progress(
    db: SunnyDB,
    email: str,
    current: int,
    total: int,
    checkpoint: str,
    *,
    state: str = "running",
    error: str = "",
) -> None:
    safe_total = max(1, int(total or 1))
    safe_current = min(safe_total, max(0, int(current or 0)))
    db.event(
        f"[{email}] access token renewal progress {safe_current}/{safe_total}: {checkpoint}",
        level="error" if state == "failed" else "info",
        typ="renewal_progress",
        detail={
            "scope": "selected",
            "progress_type": "access_token_renewal",
            "email": email,
            "checkpoint": checkpoint,
            "current": safe_current,
            "total": safe_total,
            "state": state,
            "error": str(error or "")[:500],
        },
    )


def _account_event(
    db: SunnyDB,
    email: str,
    module: str,
    action: str,
    message: str,
    level: str = "info",
    detail: dict[str, Any] | None = None,
    *,
    account_id: int = 0,
    mailbox_id: int = 0,
    operation_id: str = "",
) -> None:
    writer = getattr(db, "account_event", None)
    if callable(writer):
        writer(
            email, module, action, message, level, detail,
            account_id=account_id, mailbox_id=mailbox_id, operation_id=operation_id,
        )
        return
    event_detail = dict(detail or {})
    event_detail.update({"email": email, "scope": "account", "module": module, "action": action})
    if account_id:
        event_detail["account_id"] = account_id
    if mailbox_id:
        event_detail["mailbox_id"] = mailbox_id
    if operation_id:
        event_detail["operation_id"] = operation_id
    db.event(message, level, detail=event_detail)


def _is_cancel_exception(exc: BaseException) -> bool:
    return isinstance(exc, (SunnyTaskCancelled, TaskCancelledError)) or "Task cancelled by user" in str(exc)


def _is_account_deactivated(error: Any) -> bool:
    text = str(error or "").strip().lower()
    return any(marker in text for marker in (
        "account_deactivated", "account disabled", "account has been disabled",
        "account deactivated", "account has been deactivated", "deleted or deactivated",
        "account suspended", "account has been suspended", "account is suspended",
        "account banned", "account has been banned", "account is banned", "account blocked",
        "account is disabled", "account is deactivated",
        "账户已停用", "账户被禁用", "账户已被禁用", "账户已禁用", "账号已封禁", "账号被封禁",
        "账号已被封禁", "账号已被禁用", "账户已封禁", "账户被暂停", "账户已被暂停", "アカウントが無効", "アカウントは無効",
        "アカウントが停止", "アカウントは停止", "利用停止", "계정이 비활성화",
        "계정이 정지", "계정이 차단",
    ))


def _raise_if_login_secret_account_deactivated(result: Any) -> None:
    """Promote a captured LS callback error so the account flow is terminated."""
    if not isinstance(result, dict):
        return
    errors = result.get("errors")
    if isinstance(errors, (list, tuple)):
        detail = "；".join(str(item) for item in errors if item)
    else:
        detail = str(errors or result.get("error") or "")
    if _is_account_deactivated(detail):
        raise RuntimeError(detail)


def _is_otp_security_context_failure(error: Any) -> bool:
    """Return true only for a rejected OTP request caused by auth proof context.

    A retry must start a new authorization session and obtain a new OTP. Reusing
    the old code is intentionally excluded because it can consume the upstream
    attempt limit.
    """
    text = str(error or "").strip().lower()
    if _is_account_deactivated(text):
        return False
    otp_request = "emailotpvalidate" in text or "email-otp/validate" in text
    security_rejection = any(
        marker in text
        for marker in (
            "http 403",
            "resp 403",
            "cloudflare",
            "sentinel_required",
            "proof_required",
            "challenge_required",
        )
    )
    return otp_request and security_rejection


def _raw_mailboxes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(str(payload.get("mailbox_lines") or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        acc = parse_account_line(line)
        rows.append({
            "id": 0 - idx,
            "email": acc.email,
            "password": acc.password,
            "client_id": acc.client_id,
            "refresh_token": acc.refresh_token,
            "openai_rt": acc.openai_rt,
            "raw": acc.raw,
            "account_type": acc.account_type,
            "status": "未注册",
        })
    return rows


def _proxy_pool_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_pool = payload.get("proxy_pool")
    pool_items = raw_pool if isinstance(raw_pool, list) else []
    raw_ids = payload.get("proxy_ids")
    proxy_ids = raw_ids if isinstance(raw_ids, list) else []
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(pool_items):
        stored_address = build_proxy("", str(item or "")).url
        if not stored_address:
            continue
        try:
            proxy_id = max(0, int(proxy_ids[index])) if index < len(proxy_ids) else 0
        except (TypeError, ValueError):
            proxy_id = 0
        candidates.append({
            "id": proxy_id,
            "address": stored_address,
            "register": _container_host_proxy(stored_address),
        })
    return candidates


def _proxy_snapshot(payload: dict[str, Any], slot: int = 0) -> dict[str, Any]:
    if payload.get("proxy_enabled") is False:
        system_proxy = str(payload.get("system_proxy") or "").strip()
        normalized_system_proxy = _container_host_proxy(build_proxy("", system_proxy).url)
        return {"register": normalized_system_proxy, "mode": "system_proxy" if normalized_system_proxy else "direct", "local_proxy": ""}
    lease = payload.get("_proxy_lease")
    if isinstance(lease, dict) and "register" in lease:
        register_proxy = str(lease.get("register") or "")
        return {
            "register": register_proxy,
            "mode": "task_proxy_lease" if register_proxy else "direct",
            "local_proxy": _container_host_proxy(build_proxy(str(payload.get("local_proxy") or ""), "").url),
            "proxy_id": int(lease.get("proxy_id") or 0),
            "proxy_slot": int(lease.get("slot") or -1),
            "proxy_latency_ms": int(lease.get("latency_ms") or 0),
        }
    base = str(payload.get("proxy") or "").strip()
    candidates = _proxy_pool_candidates(payload)
    if candidates:
        selected = candidates[max(0, int(slot)) % len(candidates)]
        register_proxy = selected["register"]
        proxy_id = selected["id"]
    else:
        register_proxy = _container_host_proxy(build_proxy("", str(payload.get("register_proxy") or base)).url)
        proxy_id = 0
    local_proxy = _container_host_proxy(build_proxy(str(payload.get("local_proxy") or ""), "").url)
    return {"register": register_proxy, "mode": "proxy_pool", "local_proxy": local_proxy, "proxy_id": proxy_id}


def _auxiliary_proxy(payload: dict[str, Any], proxies: dict[str, Any]) -> str:
    """Return the auxiliary route; empty means direct server egress."""
    if payload.get("proxy_all_traffic") is True:
        return str(proxies.get("register") or "")
    return ""


def _mailbox_proxy_for_task(
    payload: dict[str, Any],
    proxies: dict[str, Any],
    auxiliary_proxy: str,
    mailbox_type: str,
) -> str:
    if auxiliary_proxy:
        return auxiliary_proxy
    if payload.get("access_token_renewal") is True and str(mailbox_type or "").strip().lower() == "apple":
        return str(proxies.get("register") or "")
    return ""


def _prepare_register_proxy(db: SunnyDB, payload: dict[str, Any], email: str, slot: int = 0) -> dict[str, Any]:
    proxies = _proxy_snapshot(payload, slot)
    proxy = proxies.get("register", "")
    if not proxy or proxies.get("mode") != "proxy_pool":
        return proxies

    candidates = _proxy_pool_candidates(payload)
    excluded = {
        str(value or "").strip()
        for value in (payload.get("_excluded_register_proxies") or [])
        if str(value or "").strip()
    }
    if excluded:
        candidates = [candidate for candidate in candidates if str(candidate.get("register") or "").strip() not in excluded]
    if candidates:
        start = max(0, int(slot)) % len(candidates)
        candidates = candidates[start:] + candidates[:start]
        fallbacks = candidates[1:]
        random.SystemRandom().shuffle(fallbacks)
        candidates = candidates[:1] + fallbacks
    else:
        candidates = [{"id": 0, "address": proxy, "register": proxy}]

    failures: list[str] = []
    for attempt, candidate in enumerate(candidates, start=1):
        proxy_id = int(candidate.get("id") or 0)
        candidate_proxy = str(candidate.get("register") or "")
        if proxy_id > 0 and not db.proxy_is_usable(proxy_id):
            db.event(
                f"[{email}] [代理] 跳过已被其他任务标记为失效的代理：{redact_proxy_url(candidate_proxy)}",
                "warning",
                detail={"email": email, "scope": "selected", "proxy": candidate_proxy, "proxy_id": proxy_id, "proxy_mode": "proxy_pool", "proxy_skipped": True},
            )
            continue
        check = proxy_target_tls_check(candidate_proxy, timeout=10)
        if check.get("ok"):
            selected = {**proxies, "register": candidate_proxy, "proxy_id": proxy_id}
            db.event(
                f"[{email}] [代理] 代理 HTTPS 隧道预检通过：{redact_proxy_url(candidate_proxy)}，延迟 {check.get('latency_ms', 0)}ms",
                detail={"email": email, "scope": "selected", "proxy": candidate_proxy, "proxy_id": proxy_id, "proxy_mode": selected.get("mode"), "proxy_precheck": check, "proxy_attempt": attempt},
            )
            return selected
        err = str(check.get("error") or "unknown error")
        failures.append(f"{redact_proxy_url(candidate_proxy)}: {err}")
        transition = "仅本次跳过并切换下一条，不修改代理池状态"
        db.event(
            f"[{email}] [代理] 代理无法建立到 chatgpt.com:443 的 HTTPS 隧道，{transition}：{redact_proxy_url(candidate_proxy)}；原因：{err}",
            "warning",
            detail={"email": email, "scope": "selected", "proxy": candidate_proxy, "proxy_id": proxy_id, "proxy_mode": "proxy_pool", "proxy_precheck": check, "proxy_attempt": attempt, "proxy_pool_status_unchanged": True},
        )

    local_proxy = proxies.get("local_proxy", "")
    attempted_proxies = {str(candidate.get("register") or "") for candidate in candidates}
    if local_proxy and local_proxy not in attempted_proxies:
        local_check = proxy_target_tls_check(local_proxy, timeout=10)
        if local_check.get("ok"):
            db.event(
                f"[{email}] [代理] 代理池候选均不可用，已自动回退到本地代理出口：{redact_proxy_url(local_proxy)}。",
                "warning",
                detail={"email": email, "scope": "selected", "proxy": local_proxy, "proxy_mode": "local_proxy_fallback", "proxy_precheck": local_check},
            )
            return {"register": local_proxy, "mode": "local_proxy_fallback", "local_proxy": local_proxy}
        db.event(
            f"[{email}] [代理] 本地代理出口也未通过 HTTPS 隧道预检：{redact_proxy_url(local_proxy)}；原因：{local_check.get('error') or 'unknown error'}",
            "warning",
            detail={"email": email, "scope": "selected", "proxy": local_proxy, "proxy_mode": "local_proxy_fallback", "proxy_precheck": local_check},
        )
    failure_summary = "；".join(failures[-3:]) or "任务快照中的代理均已失效"
    raise RuntimeError(f"代理池中没有可用于 ChatGPT 注册链路的代理；{failure_summary}")


def _log_proxy_startup(db: SunnyDB, payload: dict[str, Any]) -> None:
    stats = payload.get("proxy_stats") if isinstance(payload.get("proxy_stats"), dict) else {}
    total = int(stats.get("total") or 0)
    enabled = int(stats.get("enabled") or 0)
    disabled = int(stats.get("disabled") or 0)
    invalid = int(stats.get("invalid") or 0)
    if payload.get("proxy_enabled") is False:
        system_proxy = _proxy_snapshot(payload).get("register", "")
        if payload.get("proxy_pool_fallback_confirmed") is True:
            db.event(
                f"[代理] 当前代理池未配置可用的注册/登录用途代理；用户已确认本次任务改用服务器系统出口"
                f"{'代理：' + system_proxy if system_proxy else '直连'}。代理池总数 {total}，启用 {enabled}，停用 {disabled}，失效 {invalid}",
                "warning",
                detail={"scope": "global", "proxy_enabled": False, "proxy_pool_fallback_confirmed": True, "proxy_stats": stats, "system_proxy": system_proxy},
            )
            return
        db.event(
            f"[代理] 代理池开关：关闭；注册机将使用服务器系统出口{'代理：' + system_proxy if system_proxy else '直连'}。代理池总数 {total}，启用 {enabled}，停用 {disabled}，失效 {invalid}",
            detail={"scope": "global", "proxy_enabled": False, "proxy_stats": stats, "system_proxy": system_proxy},
        )
        return
    proxy = _proxy_snapshot(payload).get("register", "")
    proxy_pool_size = len(payload.get("proxy_pool") or [])
    db.event(
        f"[代理] 代理开关：开启；代理池总数 {total}，启用 {enabled}，停用 {disabled}，失效 {invalid}；本任务快照可分配 {proxy_pool_size or (1 if proxy else 0)} 个代理",
        detail={"scope": "global", "proxy_enabled": True, "proxy_stats": stats, "proxy_pool_size": proxy_pool_size},
    )
    if proxy:
        redacted = redact_proxy_url(proxy)
        db.event(f"[代理] 注册/登录请求将按邮箱轮询使用代理池，首个出口：{redacted}", detail={"scope": "global", "proxy": redacted})
    else:
        db.event("[代理] 未获取到可用代理，注册任务将停止或回退到后端校验结果", "warning", detail={"scope": "global"})


def _choose_mailboxes(db: SunnyDB, payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _raw_mailboxes(payload)
    if raw:
        return raw
    ids = _ids(payload.get("mailbox_ids"))
    return db.fetch_mailboxes(ids or None, int(payload.get("count") or 0))


def _phone_provider(db: SunnyDB, email: str):
    active: dict[str, Any] = {}

    def provider(action: str, _email: str, payload: Any = None):
        nonlocal active
        if action == "next":
            phone = db.reserve_phone()
            if not phone:
                return None
            try:
                phone["seen_sms_keys"] = [item["key"] for item in read_sms_candidates(str(phone.get("sms_url") or ""))]
            except Exception as exc:
                if phone.get("id"):
                    db.mark_phone_error(int(phone["id"]), f"无法建立短信基线: {exc}")
                db.event(
                    f"[{email}] [接码] 自建收码接口基线读取失败，已拒绝该号码：{exc}",
                    "warning",
                    detail={"email": email, "scope": "selected", "sms_provider": "local"},
                )
                raise RuntimeError(f"自建收码接口无法建立短信基线: {exc}") from exc
            active = phone
            db.event(f"[{email}] [接码] 已从接码配置分配手机号 {phone.get('number')}", detail={"email": email, "scope": "selected"})
            return phone
        if action == "code":
            phone = payload or active
            return wait_sms_code(
                str(phone.get("number") or ""),
                str(phone.get("sms_url") or ""),
                timeout=180,
                log=lambda m: db.event(f"[{email}] {m}", detail={"email": email, "scope": "selected"}),
                seen_keys=set(phone.get("seen_sms_keys") or []),
            )
        if action == "success":
            phone = payload or active
            if phone and phone.get("id"):
                db.mark_phone_success(int(phone["id"]), str(phone.get("code") or ""))
            return True
        if action == "bad":
            phone = payload or active
            if phone and phone.get("id"):
                db.mark_phone_error(int(phone["id"]), str(phone.get("error") or "phone verification failed"))
            return True
        return None

    return provider


def _sms_country_metadata(db: SunnyDB, option: dict[str, Any] | None, country: str = "", dial_code: str = "") -> dict[str, str]:
    loader = getattr(db, "sms_provider_option_extra", None)
    extra = loader(option) if callable(loader) else {}
    extra = extra if isinstance(extra, dict) else {}
    title = str(extra.get("Country_Title") or "").strip()
    title_parts = [part.strip() for part in title.split("/") if part.strip()]
    country_name = str(
        extra.get("name")
        or extra.get("eng")
        or extra.get("country_name")
        or (title_parts[-1] if len(title_parts) > 1 else "")
        or (option or {}).get("label")
        or ""
    ).strip()
    country_iso = str(
        extra.get("short_name")
        or extra.get("iso2")
        or extra.get("iso")
        or extra.get("Country_ID")
        or country
    ).strip()
    country_code = str(
        dial_code
        or extra.get("cc")
        or extra.get("country_code")
        or extra.get("dial_code")
        or extra.get("Country_Area")
        or ""
    ).strip().lstrip("+")
    return {
        "country": str(country or "").strip(),
        "country_iso": country_iso,
        "country_name": country_name,
        "country_code": country_code,
    }


def _smsbower_provider(db: SunnyDB, email: str, proxy_url: str = "", country_override: str = ""):
    active: dict[str, Any] = {}
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    phone_cfg = db.get_config("phone")
    country_value = str(country_override or phone_cfg.get("smsbower_default_country") or "187").strip()
    country_option = db.resolve_sms_provider_option("smsbower", "country", country_value)
    resolved_country = str((country_option or {}).get("value") or country_value)
    phone_cfg = {**phone_cfg, "smsbower_default_country": resolved_country}
    country_metadata = _sms_country_metadata(db, country_option, resolved_country)
    client = SMSBowerClient(phone_cfg, proxies=proxies)

    def provider(action: str, _email: str, payload: Any = None):
        nonlocal active
        if action == "next":
            activation = client.get_number()
            active = {
                "provider": "smsbower",
                "activation_id": activation.activation_id,
                "number": activation.number,
                **country_metadata,
            }
            db.event(
                f"[{email}] [接码] 已从 SMSBower 获取手机号 {activation.number}，激活 ID {activation.activation_id}",
                detail={"email": email, "scope": "selected", "sms_provider": "smsbower"},
            )
            return active
        if action == "code":
            phone = payload or active
            activation_id = str(phone.get("activation_id") or "")
            return client.wait_code(
                activation_id,
                timeout=180,
                log=lambda m: db.event(f"[{email}] [接码] {m}", detail={"email": email, "scope": "selected", "sms_provider": "smsbower"}),
            )
        if action == "success":
            phone = payload or active
            activation_id = str(phone.get("activation_id") or "")
            client.finish(activation_id)
            db.event(f"[{email}] [接码] SMSBower 激活已完成", detail={"email": email, "scope": "selected", "sms_provider": "smsbower"})
            return True
        if action == "bad":
            phone = payload or active
            activation_id = str(phone.get("activation_id") or "")
            try:
                client.cancel(activation_id)
            finally:
                db.event(f"[{email}] [接码] SMSBower 激活已取消", "warning", detail={"email": email, "scope": "selected", "sms_provider": "smsbower"})
            return True
        return None

    return provider


def _luban_provider(db: SunnyDB, email: str, proxy_url: str = ""):
    active: dict[str, Any] = {}
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    client = LubanSMSClient(db.get_config("phone"), proxies=proxies)

    def provider(action: str, _email: str, payload: Any = None):
        nonlocal active
        if action == "next":
            activation = client.get_number()
            active = {"provider": "luban", "activation_id": activation.request_id, "number": activation.number}
            db.event(
                f"[{email}] [接码] 已从 LubanSMS 获取手机号 {activation.number}",
                detail={"email": email, "scope": "selected", "sms_provider": "luban"},
            )
            return active
        if action == "code":
            phone = payload or active
            return client.wait_code(
                str(phone.get("activation_id") or ""),
                timeout=180,
                log=lambda message: db.event(f"[{email}] [接码] {message}", detail={"email": email, "scope": "selected", "sms_provider": "luban"}),
            )
        if action == "success":
            db.event(f"[{email}] [接码] LubanSMS 接码完成", detail={"email": email, "scope": "selected", "sms_provider": "luban"})
            return True
        if action == "bad":
            phone = payload or active
            client.release(str(phone.get("activation_id") or ""))
            active = {}
            db.event(f"[{email}] [接码] LubanSMS 号码已拒绝释放", "warning", detail={"email": email, "scope": "selected", "sms_provider": "luban"})
            return True
        return None

    return provider


def _smspool_provider(db: SunnyDB, email: str, proxy_url: str = "", country_override: str = ""):
    active: dict[str, Any] = {}
    reuse_checked = False
    new_number_attempts = 0
    max_new_number_attempts = 3
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    phone_cfg = db.get_config("phone")
    country_value = str(country_override or phone_cfg.get("smspool_default_country") or "1").strip()
    country_option = db.resolve_sms_provider_option("smspool", "country", country_value)
    resolved_country = str((country_option or {}).get("value") or country_value or "1")
    service_value = str(phone_cfg.get("smspool_default_service") or "OpenAI").strip()
    service_option = db.resolve_sms_provider_option("smspool", "service", service_value, resolved_country)
    resolved_service = str((service_option or {}).get("value") or service_value)
    if resolved_country != country_value or resolved_service != service_value:
        phone_cfg = {
            **phone_cfg,
            "smspool_default_country": resolved_country,
            "smspool_default_service": resolved_service,
        }
        db.event(
            f"[{email}] [接码] 本次任务已将 SMSPool 配置解析为接口 ID：country={resolved_country}，service={resolved_service}",
            detail={"email": email, "scope": "selected", "sms_provider": "smspool", "country": resolved_country, "service": resolved_service},
        )
    country_metadata = _sms_country_metadata(db, country_option, resolved_country)
    client = SMSPoolClient(phone_cfg, proxies=proxies)

    def provider(action: str, _email: str, payload: Any = None):
        nonlocal active, reuse_checked, new_number_attempts
        if action == "next":
            db.event(
                f"[{email}] [接码] 准备向 SMSPool 申请手机号：country={client.country}，service={client.service}，pool={client.pool or '-'}，max_price={client.max_price}",
                detail={"email": email, "scope": "selected", "sms_provider": "smspool", "country": client.country, "service": client.service, "pool": client.pool, "max_price": client.max_price},
            )
            activation = None
            reused = False
            if not reuse_checked:
                reuse_checked = True
                try:
                    reusable = client.latest_reusable_order()
                except Exception as exc:
                    reusable = None
                    db.event(
                        f"[{email}] [接码] SMSPool orders_new 查询失败，本次跳过号码复用：{exc}",
                        "warning",
                        detail={"email": email, "scope": "selected", "sms_provider": "smspool", "reuse": True},
                    )
                if reusable:
                    db.event(
                        f"[{email}] [接码] SMSPool 尝试复用 orders_new 中最新订单（id={reusable.id}）的手机号 {reusable.number}",
                        detail={"email": email, "scope": "selected", "sms_provider": "smspool", "reuse": True, "orders_new_id": reusable.id},
                    )
                    try:
                        activation = client.get_number(preferred_number=reusable.number)
                        reused = True
                    except Exception as exc:
                        db.mark_sms_provider_number_error("smspool", reusable.number, str(exc))
                        db.event(
                            f"[{email}] [接码] SMSPool 最新手机号复用失败，将申请新号码：{exc}",
                            "warning",
                            detail={"email": email, "scope": "selected", "sms_provider": "smspool", "reuse": True, "orders_new_id": reusable.id},
                        )

            while activation is None and new_number_attempts < max_new_number_attempts:
                new_number_attempts += 1
                try:
                    activation = client.get_number()
                except Exception as exc:
                    db.event(
                        f"[{email}] [接码] SMSPool 第 {new_number_attempts}/{max_new_number_attempts} 次申请新号码失败：{exc}",
                        "warning",
                        detail={"email": email, "scope": "selected", "sms_provider": "smspool", "new_number_attempt": new_number_attempts},
                    )
            if activation is None:
                db.event(
                    f"[{email}] [接码] SMSPool 已用完 {max_new_number_attempts} 次新号码机会，停止使用该供应商",
                    "warning",
                    detail={"email": email, "scope": "selected", "sms_provider": "smspool", "exhausted": True},
                )
                return None
            active = {
                "provider": "smspool",
                "order_id": activation.order_id,
                "activation_id": activation.order_id,
                "number": activation.number,
                "token": activation.token,
                **country_metadata,
                "reused": reused,
                "new_number_attempt": 0 if reused else new_number_attempts,
            }
            db.record_sms_provider_number(
                "smspool",
                activation.number,
                country=client.country,
                service=client.service,
                pool=client.pool,
                order_id=activation.order_id,
                token=activation.token,
            )
            db.event(
                f"[{email}] [接码] 已从 SMSPool 获取手机号 {activation.number}，订单 ID {activation.order_id}"
                + ("（复用最新订单号码）" if reused else f"（新号码 {new_number_attempts}/{max_new_number_attempts}）"),
                detail={"email": email, "scope": "selected", "sms_provider": "smspool", "reuse": reused, "new_number_attempt": 0 if reused else new_number_attempts},
            )
            return active
        if action == "code":
            phone = payload or active
            order_id = str(phone.get("order_id") or phone.get("activation_id") or "")
            return client.wait_code(
                order_id,
                timeout=SMSPOOL_CODE_TIMEOUT_SECONDS,
                log=lambda m: db.event(f"[{email}] [接码] {m}", detail={"email": email, "scope": "selected", "sms_provider": "smspool"}),
            )
        if action == "success":
            phone = payload or active
            db.mark_sms_provider_number_success("smspool", str(phone.get("number") or ""), str(phone.get("code") or ""))
            db.event(f"[{email}] [接码] SMSPool 接码订单已完成，手机号进入 5 小时冷却后可复用", detail={"email": email, "scope": "selected", "sms_provider": "smspool"})
            return True
        if action == "bad":
            phone = payload or active
            order_id = str(phone.get("order_id") or phone.get("activation_id") or "")
            cancel_error = ""
            try:
                client.cancel(order_id)
            except Exception as exc:
                cancel_error = str(exc)
            db.mark_sms_provider_number_error("smspool", str(phone.get("number") or ""), str(phone.get("error") or "SMSPool order failed"))
            active = {}
            retry_same_provider = new_number_attempts < max_new_number_attempts
            if cancel_error:
                db.event(
                    f"[{email}] [接码] SMSPool 订单取消失败，但仍继续换号：{cancel_error}",
                    "warning",
                    detail={"email": email, "scope": "selected", "sms_provider": "smspool", "order_id": order_id},
                )
            else:
                db.event(
                    f"[{email}] [接码] SMSPool 接码订单已取消，将更换手机号",
                    "warning",
                    detail={"email": email, "scope": "selected", "sms_provider": "smspool", "order_id": order_id},
                )
            if not retry_same_provider:
                db.event(
                    f"[{email}] [接码] SMSPool 三次新号码均未完成接码，放弃该供应商",
                    "warning",
                    detail={"email": email, "scope": "selected", "sms_provider": "smspool", "exhausted": True},
                )
            return {"retry_same_provider": retry_same_provider}
        return None

    return provider


def _firefox_provider(db: SunnyDB, email: str, proxy_url: str = "", country_override: str = ""):
    active: dict[str, Any] = {}
    number_attempts = 0
    max_number_attempts = 3
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    phone_cfg = db.get_config("phone")
    country_value = str(country_override or phone_cfg.get("firefox_default_country") or "").strip()
    country_option = db.resolve_sms_provider_option("firefox", "country", country_value)
    resolved_country = str((country_option or {}).get("value") or country_value)
    country_metadata = _sms_country_metadata(db, country_option, resolved_country)
    service_value = str(phone_cfg.get("firefox_default_service") or "1096").strip()
    service_option = db.resolve_sms_provider_option("firefox", "service", service_value, resolved_country)
    resolved_service = str((service_option or {}).get("value") or service_value)
    phone_cfg = {
        **phone_cfg,
        "firefox_default_country": resolved_country,
        "firefox_default_service": resolved_service,
    }
    client = FireFoxSMSClient(phone_cfg, proxies=proxies)

    def provider(action: str, _email: str, payload: Any = None):
        nonlocal active, number_attempts
        if action == "next":
            if number_attempts >= max_number_attempts:
                return None
            number_attempts += 1
            db.event(
                f"[{email}] [接码] 准备向 FireFox 申请手机号：country={client.country}，service={client.service}，max_price={client.max_price}，quantity=1",
                detail={"email": email, "scope": "selected", "sms_provider": "firefox", "country": client.country, "service": client.service, "max_price": client.max_price, "quantity": 1},
            )
            activation = client.get_number()
            active = {
                "provider": "firefox",
                "pkey": activation.pkey,
                "activation_id": activation.pkey,
                "number": activation.number,
                **{
                    **country_metadata,
                    "country": activation.country or country_metadata["country"] or client.country,
                    "country_code": activation.country_code or country_metadata["country_code"],
                },
                "new_number_attempt": number_attempts,
            }
            db.record_sms_provider_number(
                "firefox",
                activation.number,
                country=activation.country or client.country,
                service=client.service,
                order_id=activation.pkey,
            )
            db.event(
                f"[{email}] [接码] 已从 FireFox 获取手机号 {activation.number}，pkey {activation.pkey}（{number_attempts}/{max_number_attempts}）",
                detail={"email": email, "scope": "selected", "sms_provider": "firefox", "pkey": activation.pkey, "number_attempt": number_attempts},
            )
            return active
        if action == "code":
            phone = payload or active
            pkey = str(phone.get("pkey") or phone.get("activation_id") or "")
            return client.wait_code(
                pkey,
                timeout=180,
                log=lambda message: db.event(
                    f"[{email}] [接码] {message}",
                    detail={"email": email, "scope": "selected", "sms_provider": "firefox"},
                ),
            )
        if action == "success":
            phone = payload or active
            db.mark_sms_provider_number_success("firefox", str(phone.get("number") or ""), str(phone.get("code") or ""))
            db.event(f"[{email}] [接码] FireFox 手机号接码完成", detail={"email": email, "scope": "selected", "sms_provider": "firefox"})
            return True
        if action == "bad":
            phone = payload or active
            pkey = str(phone.get("pkey") or phone.get("activation_id") or "")
            client.release_later(pkey, FIREFOX_RELEASE_DELAY_SECONDS)
            db.mark_sms_provider_number_error("firefox", str(phone.get("number") or ""), str(phone.get("error") or "FireFox phone verification failed"))
            active = {}
            retry_same_provider = number_attempts < max_number_attempts
            db.event(
                f"[{email}] [接码] FireFox 当前号码不可用，已安排 {FIREFOX_RELEASE_DELAY_SECONDS} 秒后异步释放，"
                + ("立即申请下一个号码" if retry_same_provider else "三次号码均未完成接码，放弃该供应商"),
                "warning",
                detail={"email": email, "scope": "selected", "sms_provider": "firefox", "pkey": pkey, "retry_same_provider": retry_same_provider},
            )
            return {"retry_same_provider": retry_same_provider}
        return None

    return provider


def _combined_phone_provider(db: SunnyDB, email: str, proxy_url: str = "", execution_mode: str = "protocol"):
    background_us_only = str(execution_mode or "").strip().lower() == "background"
    candidates: list[tuple[str, Any]] = []
    if _provider_is_available(db, "luban"):
        candidates.append(("LubanSMS", lambda: _luban_provider(db, email, proxy_url)))
    if _provider_is_available(db, "smsbower"):
        candidates.append(("SMSBower", lambda: _smsbower_provider(db, email, proxy_url, "187" if background_us_only else "")))
    if _provider_is_available(db, "smspool"):
        candidates.append(("SMSPool", lambda: _smspool_provider(db, email, proxy_url, "1" if background_us_only else "")))
    if _provider_is_available(db, "firefox"):
        candidates.append(("FireFox", lambda: _firefox_provider(db, email, proxy_url, "usa" if background_us_only else "")))
    random.shuffle(candidates)
    if db.usable_phone_count() > 0:
        candidates.append(("自建手机号池", lambda: _phone_provider(db, email)))
    if not candidates:
        return None

    remaining = list(candidates)
    active_provider = None
    active_name = ""
    active_phone: dict[str, Any] = {}

    db.event(
        f"[{email}] [接码] 本次接码候选顺序：{' → '.join(name for name, _ in remaining)}（外部供应商随机，自建手机号池兜底）",
        detail={"email": email, "scope": "selected", "sms_provider": "combined", "candidate_order": [name for name, _ in remaining]},
    )
    if background_us_only:
        db.event(
            f"[{email}] [接码] 后台无头浏览器模式仅使用美国 +1 手机号；本次任务将外部供应商国家临时设为美国，不修改已保存配置",
            detail={"email": email, "scope": "selected", "sms_provider": "combined", "execution_mode": "background", "country_code": "1"},
        )

    def provider(action: str, _email: str, payload: Any = None):
        nonlocal active_provider, active_name, active_phone
        if action == "next":
            if active_provider:
                try:
                    phone = active_provider("next", _email, payload)
                    if phone:
                        active_phone = dict(phone)
                        active_phone["provider_name"] = active_name
                        if background_us_only and not str(active_phone.get("number") or "").strip().startswith("+1"):
                            active_provider("bad", _email, {**active_phone, "error": "后台无头浏览器模式只允许美国 +1 手机号"})
                            raise RuntimeError("后台无头浏览器模式只允许美国 +1 手机号")
                        return active_phone
                except Exception as exc:
                    db.event(
                        f"[{email}] [接码] {active_name} 无法继续获取手机号，切换下一个接码资源：{exc}",
                        "warning",
                        detail={"email": email, "scope": "selected", "sms_provider": active_name, "error": str(exc)},
                    )
                active_provider = None
                active_name = ""
                active_phone = {}
            while remaining:
                name, factory = remaining.pop(0)
                candidate_provider = None
                db.event(
                    f"[{email}] [接码] 正在尝试接码资源：{name}",
                    detail={"email": email, "scope": "selected", "sms_provider": name},
                )
                try:
                    candidate_provider = factory()
                    phone = candidate_provider("next", _email, payload)
                    if not phone:
                        raise RuntimeError("未获取到可用手机号")
                    active_provider = candidate_provider
                    active_name = name
                    active_phone = dict(phone)
                    active_phone["provider_name"] = name
                    if background_us_only and not str(active_phone.get("number") or "").strip().startswith("+1"):
                        candidate_provider("bad", _email, {**active_phone, "error": "后台无头浏览器模式只允许美国 +1 手机号"})
                        raise RuntimeError("后台无头浏览器模式只允许美国 +1 手机号")
                    return active_phone
                except Exception as exc:
                    if candidate_provider is not None and active_provider is candidate_provider:
                        active_provider = None
                        active_name = ""
                        active_phone = {}
                    db.event(
                        f"[{email}] [接码] {name} 无法获取手机号，继续尝试下一个接码资源：{exc}",
                        "warning",
                        detail={"email": email, "scope": "selected", "sms_provider": name, "error": str(exc)},
                    )
            db.event(
                f"[{email}] [接码] 所有外部接码供应商及自建手机号池均不可用，停止手机号绑定",
                "warning",
                detail={"email": email, "scope": "selected", "sms_provider": "combined", "exhausted": True},
            )
            return None
        if not active_provider:
            return None
        result = None
        try:
            result = active_provider(action, _email, payload or active_phone)
            return result
        finally:
            if action == "bad":
                retry_same_provider = isinstance(result, dict) and result.get("retry_same_provider") is True
                db.event(
                    f"[{email}] [接码] {active_name} 本次号码失败，"
                    + ("继续使用该供应商申请下一个号码" if retry_same_provider else "切换到下一个接码资源"),
                    "warning",
                    detail={"email": email, "scope": "selected", "sms_provider": active_name, "retry_same_provider": retry_same_provider},
                )
                active_phone = {}
                if not retry_same_provider:
                    active_provider = None
                    active_name = ""

    return provider


def _sub2api_group_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            n = int(item)
            if n > 0:
                out.append(n)
        except Exception:
            pass
    return out


def _sub2api_config(db: SunnyDB) -> tuple[dict[str, Any], str, str]:
    cfg = db.get_config("sub2api")
    if cfg.get("enabled") is False:
        raise RuntimeError("反代配置中的 sub2api 未启用")
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    token = str(cfg.get("admin_token") or "").strip()
    if not base_url or not token:
        raise RuntimeError("请先在反代配置中填写 sub2api Base URL 和 Admin Token")
    return cfg, base_url, token


def _provider_is_available(db: SunnyDB, provider: str) -> bool:
    checker = getattr(db, f"{provider}_available", None)
    return bool(checker()) if callable(checker) else False


def _sub2api_secret_key(db: SunnyDB, email: str, session: dict[str, Any]) -> str:
    fetch_mailbox = getattr(db, "fetch_mailbox_by_email", None)
    if callable(fetch_mailbox):
        mailbox = fetch_mailbox(email)
        if isinstance(mailbox, dict):
            try:
                secret_key = str(account_from_row(mailbox).raw or "").strip()
            except (TypeError, ValueError):
                secret_key = str(mailbox.get("raw") or "").strip()
            if secret_key:
                return secret_key
    return str(session.get("raw_mailbox_line") or session.get("mailbox_raw") or "").strip()


def _sub2api_notes(db: SunnyDB, email: str, session: dict[str, Any], cfg: dict[str, Any]) -> str:
    lines: list[str] = []
    if cfg.get("notes_include_sk") is True:
        secret_key = _sub2api_secret_key(db, email, session)
        if secret_key:
            lines.append(f"邮箱凭证：{secret_key}")
    if cfg.get("notes_include_ls") is True:
        login_secret = _sub2api_login_secret(db, email, session)
        if login_secret:
            lines.append(f"密码2FA：{login_secret}")
    if cfg.get("notes_include_custom") is True:
        custom_text = str(cfg.get("notes_custom_text") or "").strip()
        if custom_text:
            lines.append(custom_text)
    return "\n".join(lines)


def _import_sub2api(db: SunnyDB, email: str, account_id: int, session: dict[str, Any], proxy_url: str = "") -> dict[str, Any]:
    cfg, base_url, token = _sub2api_config(db)
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or session.get("openai_rt") or "").strip()
    if not access_token or not refresh_token:
        raise RuntimeError("当前账号缺少 Access Token 或 Refresh Token，无法导入 sub2api")
    token_record = session.get("token_record")
    if not isinstance(token_record, dict):
        token_record = {}
    credentials = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": session.get("id_token", ""),
        "email": email,
        "client_id": session.get("client_id") or token_record.get("client_id") or "app_EMoamEEZ73f0CkXaXp7hrann",
    }
    optional_credentials = {
        "chatgpt_account_id": session.get("chatgpt_account_id") or token_record.get("account_id"),
        "chatgpt_user_id": session.get("chatgpt_user_id") or token_record.get("chatgpt_user_id"),
        "organization_id": session.get("organization_id") or token_record.get("organization_id"),
        "plan_type": session.get("plan_type") or token_record.get("plan_type"),
        "expires_at": session.get("expires_at") or token_record.get("expires_at"),
    }
    credentials.update({key: value for key, value in optional_credentials.items() if value not in (None, "", 0)})
    model_mapping = session.get("model_mapping")
    if not isinstance(model_mapping, dict) or not model_mapping:
        configured_models = [model for model in (cfg.get("model_whitelist") or []) if isinstance(model, str) and model.strip()]
        model_mapping = {model: model for model in (configured_models or _DEFAULT_SUB2API_MODELS)}
    elif cfg.get("model_whitelist"):
        model_mapping = {str(model): str(model) for model in cfg.get("model_whitelist") if str(model).strip()}
    if model_mapping:
        credentials["model_mapping"] = model_mapping
    account_payload = {
        "name": f"{str(cfg.get('name_prefix') or '')}{email}",
        "notes": _sub2api_notes(db, email, session, cfg),
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {"import_source": "sunnyregister_oauth_code", "email": email},
        "group_ids": _sub2api_group_ids(cfg.get("group_ids")),
        "concurrency": int(cfg.get("concurrency") or 3),
        "priority": int(cfg.get("priority") or 50),
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }
    if int(cfg.get("proxy_id") or 0) > 0:
        account_payload["proxy_id"] = int(cfg["proxy_id"])
    if int(cfg.get("load_factor") or 0) > 0:
        account_payload["load_factor"] = int(cfg["load_factor"])
    request_headers = {"x-api-key": token, "Idempotency-Key": f"sunny-{db.task_id}-{account_id}-{uuid.uuid4().hex[:8]}"}
    resp = None
    for attempt in range(2):
        try:
            resp = requests.post(
                f"{base_url}/api/v1/admin/accounts/batch",
                headers=request_headers,
                json={"accounts": [account_payload]},
                timeout=90,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
            )
        except requests.RequestException:
            if attempt == 0:
                continue
            raise
        if attempt == 0 and (resp.status_code == 429 or resp.status_code >= 500):
            continue
        break
    if resp is None:
        raise RuntimeError("sub2api 导入请求未返回响应")
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"sub2api 导入失败: HTTP {resp.status_code} {resp.text[:500]}")
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    response_data = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    if not isinstance(response_data, dict):
        response_data = {}
    succeeded = int(response_data.get("success") or response_data.get("succeeded") or response_data.get("created") or 0)
    failed = int(response_data.get("failed") or 0)
    remote_id = str(response_data.get("id") or "")
    confirmed = succeeded == 1 and failed == 0
    results = response_data.get("results")
    if not confirmed and isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            nested = item.get("account") if isinstance(item.get("account"), dict) else {}
            item_email = str(item.get("email") or item.get("account_email") or item.get("name") or nested.get("email") or nested.get("account_email") or nested.get("name") or "").strip().lower()
            item_status = str(item.get("status") or item.get("state") or "").strip().lower()
            item_ok = item.get("success") is True or item_status in {"success", "succeeded", "created", "imported"}
            if item_ok and item_email in {email.lower(), f"{str(cfg.get('name_prefix') or '')}{email}".lower()}:
                confirmed = True
                remote_id = str(item.get("id") or item.get("account_id") or item.get("remote_id") or nested.get("id") or nested.get("account_id") or nested.get("remote_id") or "")
                break
    if failed > 0 or not confirmed:
        raise RuntimeError(f"sub2api 批量导入未确认成功: {json.dumps(data, ensure_ascii=False)[:500]}")
    db.set_account_sub2api_status(email, "imported", remote_id)
    db.event(f"[{email}] [反代] 已根据反代配置导入 sub2api", detail={"email": email, "scope": "selected", "account_id": account_id})
    return data


def _sub2api_login_secret(db: SunnyDB, email: str, session: dict[str, Any]) -> str:
    mailbox = db.fetch_mailbox_by_email(email) or {}
    password = str(mailbox.get("chat_gpt_password") or mailbox.get("chatgpt_password") or "").strip()
    totp = str(mailbox.get("totp_secret") or "").strip()
    if password and totp:
        return f"{email}----{password}----{totp}"
    return ""


def _login_secret_result_message(result: dict[str, Any]) -> str:
    errors = "；".join(str(item) for item in (result.get("errors") or []) if str(item).strip()) or "未知原因"
    password_complete = bool(result.get("password"))
    totp_complete = bool(result.get("totp_secret"))
    access_token_refreshed = bool(result.get("access_token_refreshed"))
    if result.get("complete"):
        return "ChatGPT 密码、2FA 与最新 Access Token 已全部完成"
    if password_complete and totp_complete:
        at_status = "已更新" if access_token_refreshed else "更新未完成"
        return f"ChatGPT 密码与 2FA 已成功并保存，Access Token {at_status}：{errors}"
    if password_complete:
        return f"ChatGPT 密码已成功并保存，2FA 未完成，Access Token 未刷新：{errors}"
    if totp_complete:
        return f"ChatGPT 2FA 已保存，但密码未完成，Access Token 未刷新：{errors}"
    return f"ChatGPT 密码与 2FA 均未完成，Access Token 未刷新：{errors}"


def _import_sub2api_agent_identity(
    db: SunnyDB,
    email: str,
    account_id: int,
    session: dict[str, Any],
    proxy_url: str,
) -> dict[str, Any]:
    cfg, base_url, token = _sub2api_config(db)
    access_token = str(session.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("当前账号没有 Access Token，无法创建 Agent Identity")
    try:
        auth_json = create_agent_identity_auth(
            access_token,
            email=email,
            plan_type=str(session.get("plan_type") or "free"),
            proxy_url=proxy_url,
            should_cancel=db.cancel_requested,
            log=lambda message: db.event(message, detail={"email": email, "scope": "selected"}),
        )
    except AgentIdentityUnavailableError as exc:
        refresh_token = str(session.get("refresh_token") or session.get("openai_rt") or "").strip()
        if refresh_token:
            db.event(
                f"[{email}] [反代] 当前账号未开放 Agent Identity，已使用现有 Refresh Token 回退到标准 sub2api OAuth 导入",
                "warning",
                detail={"email": email, "scope": "selected", "fallback": "oauth_refresh_token"},
            )
            data = _import_sub2api(db, email, account_id, session, proxy_url=proxy_url)
            if isinstance(data, dict):
                data = {**data, "_sunny_import_mode": "oauth_refresh_token"}
            return data
        raise AgentIdentityUnavailableError(
            f"{exc}；当前账号没有 Refresh Token，无法回退到标准 OAuth 导入。"
            "请改用“Codex 接码绑定”获取 Refresh Token 后，再执行“导入反代平台”"
        ) from exc
    except Exception as exc:
        if _is_cancel_exception(exc):
            raise
        raise RuntimeError(f"Agent Identity 凭证创建失败: {exc}") from exc
    auth_json["notes"] = _sub2api_notes(db, email, session, cfg)
    auth_content = json.dumps(auth_json, ensure_ascii=False, separators=(",", ":"))
    payload = {
        "contents": [auth_content],
        "update_existing": True,
    }
    db.ensure_not_cancelled()
    endpoint = _sub2api_codex_import_url(base_url)
    headers = {
        "X-API-Key": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "SunnyRegister/1.0",
    }
    resp = _post_sub2api_agent_identity(db, endpoint, headers, payload, proxy_url=proxy_url)
    if resp.status_code in {400, 404, 422} and "content" in str(resp.text or "").lower():
        # Older Sub2API builds accepted a single content field. Only retry
        # schema-level rejections, so a successful import is never duplicated.
        legacy_payload = {**payload, "content": auth_content}
        legacy_payload.pop("contents", None)
        resp = _post_sub2api_agent_identity(db, endpoint, headers, legacy_payload, proxy_url=proxy_url)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(
            f"sub2api Agent Identity 导入失败: {_sub2api_response_diagnostic(resp, endpoint)}"
        )
    response_json = _sub2api_response_json(resp, endpoint)
    result = response_json.get("data") if isinstance(response_json, dict) and isinstance(response_json.get("data"), dict) else response_json
    if not isinstance(result, dict):
        raise RuntimeError("sub2api Agent Identity 导入结果格式无效")
    failed = int(result.get("failed") or 0)
    created = int(result.get("created") or 0)
    updated = int(result.get("updated") or 0)
    if failed > 0 or (created + updated <= 0 and int(result.get("skipped") or 0) <= 0):
        errors = result.get("errors") or result.get("items") or []
        raise RuntimeError(f"sub2api Agent Identity 导入未成功: {json.dumps(errors, ensure_ascii=False)[:500]}")
    remote_id = ""
    items = result.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        remote_id = str(items[0].get("account_id") or "")
    db.set_account_sub2api_status(email, "imported", remote_id)
    db.event(
        f"[{email}] [反代] 已使用 Agent Identity auth.json 导入 sub2api，后续请求由平台动态签名",
        detail={"email": email, "scope": "selected", "account_id": account_id, "auth_mode": "agentIdentity"},
    )
    return result


def _post_sub2api_agent_identity(
    db: SunnyDB,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    proxy_url: str = "",
):
    """Post one import payload, retrying only transient gateway failures."""
    response = None
    for attempt in range(3):
        db.ensure_not_cancelled()
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=90,
                allow_redirects=False,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
            )
        except requests.RequestException as exc:
            if attempt >= 2:
                raise RuntimeError(f"sub2api Agent Identity 导入请求失败: {exc}") from exc
            db.event(
                f"[反代] sub2api 导入请求异常，准备重试 {attempt + 1}/2",
                "warning",
                detail={"scope": "global", "attempt": attempt + 1},
            )
            time.sleep(1.5 * (attempt + 1))
            continue
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in {429, 502, 503, 504} or attempt >= 2:
            return response
        db.event(
            f"[反代] sub2api 网关暂时不可用，准备重试 {attempt + 1}/2（HTTP {status}）",
            "warning",
            detail={"scope": "global", "attempt": attempt + 1, "status": status},
        )
        time.sleep(1.5 * (attempt + 1))
    if response is None:
        raise RuntimeError("sub2api Agent Identity 导入请求未返回响应")
    return response


def _sub2api_response_diagnostic(response: Any, endpoint: str) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type") or headers.get("content-type") or "unknown")
    location = str(headers.get("Location") or headers.get("location") or "").strip()
    final_url = str(getattr(response, "url", "") or endpoint)
    body = str(getattr(response, "text", "") or "").strip()
    lowered = body.lower()
    if 300 <= status < 400:
        target = location or "未提供 Location"
        return f"HTTP {status}，接口发生重定向到 {target}；请检查 Base URL、Cloudflare 与鉴权配置"
    if "<html" in lowered or "<!doctype" in lowered:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "HTML 页面"
        return (
            f"HTTP {status}，服务返回 HTML（{title}，Content-Type={content_type}，URL={final_url}）；"
            "请检查 sub2api API 路径、Cloudflare 回源状态及 Admin Token"
        )
    summary = re.sub(r"\s+", " ", body)[:500] or "空响应"
    return f"HTTP {status}，Content-Type={content_type}，URL={final_url}，响应={summary}"


def _sub2api_response_json(response: Any, endpoint: str) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception:
        body = str(getattr(response, "text", "") or "").strip()
        try:
            value = json.loads(body)
        except Exception as exc:
            raise RuntimeError(
                f"sub2api Agent Identity 导入返回非 JSON 内容: {_sub2api_response_diagnostic(response, endpoint)}"
            ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("sub2api Agent Identity 导入结果必须是 JSON 对象")
    return value


def _sub2api_codex_import_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("sub2api Base URL 必须是完整的 http:// 或 https:// 地址")
    endpoint = "/api/v1/admin/accounts/import/codex-session"
    if cleaned.endswith(endpoint):
        return cleaned
    for suffix in ("/api/v1/admin", "/api/v1"):
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)] + endpoint
    return cleaned + endpoint


def _persist_registration_checkpoint(
    db: SunnyDB,
    mailbox: dict[str, Any],
    account,
    checkpoint: str,
    snapshot: dict[str, Any],
    original_status: str,
) -> None:
    mailbox_id = max(0, int(mailbox.get("id") or 0))
    email = str(mailbox.get("_original_email_for_auth") or mailbox.get("email") or account.email or "")
    if mailbox_id <= 0 or not email:
        return
    candidate = "已接码" if checkpoint == "phone_bound" else "已注册"
    current_status = db.mailbox_status(mailbox_id)
    completed_status = _highest_mailbox_progress(
        _highest_mailbox_progress(original_status, current_status),
        candidate,
    )
    refresh_token = str(snapshot.get("refresh_token") or snapshot.get("openai_rt") or "").strip()
    access_token = str(snapshot.get("access_token") or "").strip()
    fields: dict[str, Any] = {
        "mailbox_id": mailbox_id,
        "status": _account_status_for_mailbox(completed_status),
        "account_type": snapshot.get("plan_type") or account.account_type,
        "last_error": "",
        "metadata_json": json.dumps(
            {"task_id": db.task_id, "source": "sunny_register", "checkpoint": checkpoint, "completed_status": completed_status},
            ensure_ascii=False,
        ),
    }
    if refresh_token:
        fields["openai_rt"] = refresh_token
    if access_token:
        fields["access_token"] = access_token
    if snapshot.get("phone_number"):
        fields["phone_number"] = str(snapshot.get("phone_number") or "")
    account_id = db.upsert_account(email, **fields)
    # Registration flows generate/set the ChatGPT password before the first
    # durable session checkpoint. Persist it here as well so an interruption
    # between password submission and the next stage cannot lose the secret.
    chatgpt_password = str(getattr(account, "chatgpt_password", "") or "").strip()
    if chatgpt_password:
        db.save_chatgpt_password(mailbox_id, chatgpt_password)
    if access_token or snapshot.get("session_json"):
        db.upsert_session(email, account_id, snapshot, account.raw)
    db.mark_mailbox(mailbox_id, completed_status, openai_rt=refresh_token)
    db.event(
        f"[{email}] [系统] 已保存任务阶段检查点：{completed_status}",
        detail={"email": email, "scope": "selected", "checkpoint": checkpoint, "completed_status": completed_status},
    )


def _persist_authenticated_login(
    db: SunnyDB,
    email: str,
    mailbox_id: int,
    session: dict[str, Any],
    raw_line: str = "",
) -> int:
    """Persist a successful login before any optional post-login stage runs."""
    persist = getattr(db, "persist_authenticated_session", None)
    if callable(persist):
        return int(persist(email, mailbox_id, session, raw_line))
    access_token = str(session.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("successful login did not return an access token")
    fields: dict[str, Any] = {"access_token": access_token, "last_error": ""}
    if mailbox_id > 0:
        fields["mailbox_id"] = mailbox_id
    refresh_token = str(session.get("refresh_token") or session.get("openai_rt") or "").strip()
    if refresh_token:
        fields["openai_rt"] = refresh_token
    account_id = int(db.upsert_account(email, **fields))
    db.upsert_session(email, account_id, session, raw_line)
    return account_id


def _run_one_impl(
    db: SunnyDB,
    task_type: str,
    payload: dict[str, Any],
    mailbox: dict[str, Any],
    index: int,
    total: int,
    protocol_batch_policy: _ProtocolBatchPolicy | None = None,
) -> tuple[bool, dict[str, Any] | str]:
    db.ensure_not_cancelled()
    email = mailbox.get("email") or f"mailbox-{index}"
    identity_email = str(mailbox.get("_original_email_for_auth") or email)
    mailbox_id = max(0, int(mailbox.get("id") or 0))
    current_mailbox_status = db.mailbox_status(mailbox_id) if mailbox_id else str(mailbox.get("status") or "")
    if not mailbox_id and email:
        current = db.fetch_mailbox_by_email(str(email))
        if current:
            current_mailbox_status = str(current.get("status") or current_mailbox_status)
    if str(current_mailbox_status or "").strip().lower() in {"已封禁", "banned", "account_deactivated"}:
        message = f"[{email}] account_deactivated: 账户已封禁，已停止后续任务并释放当前账号资源"
        db.event(message, "warning", detail={"email": email, "scope": "selected", "account_deactivated": True, "skipped": True})
        return False, message
    stage = _stage(payload)
    setup_login_secret_enabled = payload.get("setup_login_secret") is True
    explicit_rt_acquire = task_type == "sunny_acquire_rt"
    _emit_registration_progress(db, str(email), stage, "initializing", setup_login_secret=setup_login_secret_enabled)
    try:
        proxies = _prepare_register_proxy(db, payload, str(email), index - 1)
    except Exception as exc:
        if _is_cancel_exception(exc):
            raise
        mailbox_id = max(0, int(mailbox.get("id") or 0))
        err_text = str(exc)
        err = f"[{email}] {err_text}"
        mailbox_status = str(mailbox.get("status") or "")
        completed_status = mailbox_status if _MAILBOX_PROGRESS_RANK.get(mailbox_status, -1) > 0 else ""
        if completed_status:
            db.mark_mailbox(mailbox_id, completed_status, err_text)
            db.upsert_account(identity_email, mailbox_id=mailbox_id, status=_account_status_for_mailbox(completed_status), last_error=err_text)
        else:
            db.mark_mailbox(mailbox_id, "失败", err_text)
            db.upsert_account(identity_email, mailbox_id=mailbox_id, status="failed", last_error=err_text)
            if str(payload.get("identity") or "").strip().lower() in {"domain", "domain_mailbox", "自建域名邮箱"}:
                try:
                    cleanup_failed_mailbox(db, db.get_config("domain_mailbox"), str(email), str(mailbox.get("pickup_token_hash") or ""), db.event)
                except Exception as cleanup_exc:
                    db.event(f"[{email}] 失败域名邮箱清理失败：{cleanup_exc}", "warning", detail={"email": email, "scope": "selected"})
        db.event(
            err,
            "error",
            detail={"email": email, "scope": "selected", "proxy_pool_exhausted": True, "traceback": traceback.format_exc()[-3000:]},
        )
        _emit_registration_progress(db, str(email), stage, "failed", state="abnormal", error=err_text, setup_login_secret=setup_login_secret_enabled)
        return False, err
    auxiliary_proxy = _auxiliary_proxy(payload, proxies)
    chatgpt_proxy_label = redact_proxy_url(str(proxies.get("register") or ""))
    auxiliary_proxy_label = redact_proxy_url(auxiliary_proxy)
    db.event(
        f"[{email}] [代理] ChatGPT 官方流量使用{chatgpt_proxy_label or '系统直连'}；其他流程使用{auxiliary_proxy_label or '系统直连'}",
        detail={"email": email, "scope": "selected", "chatgpt_proxy": chatgpt_proxy_label, "auxiliary_proxy": auxiliary_proxy_label, "proxy_all_traffic": payload.get("proxy_all_traffic") is True},
    )
    execution_mode = str(payload.get("execution_mode") or payload.get("mode") or "protocol").strip().lower()
    if execution_mode not in {"background", "visible", "protocol"}:
        execution_mode = "background"
    headless = execution_mode == "background"
    protocol_challenge_strategy = str(payload.get("protocol_challenge_strategy") or "sentinel_protocol").strip().lower()
    if protocol_challenge_strategy not in {"native_headless", "sentinel_protocol"}:
        protocol_challenge_strategy = "native_headless"
    account = account_from_row(mailbox)
    mailbox_proxy_url = _mailbox_proxy_for_task(payload, proxies, auxiliary_proxy, account.mailbox_type)
    if mailbox_proxy_url and not auxiliary_proxy:
        db.event(
            f"[{email}] [邮箱] AT续期的 iCloud 邮箱 API 将复用当前认证代理，避免服务器直连不可达",
            detail={"email": email, "scope": "selected", "mailbox_proxy": redact_proxy_url(mailbox_proxy_url), "renewal_mailbox_proxy_fallback": True},
        )
    mailbox_id = max(0, int(mailbox.get("id") or 0))
    is_registered_mailbox = bool(account.openai_rt) or str(mailbox.get("status") or "") in {"registered", "已注册", "phone_bound", "已接码", "已反代", "reverse_proxied", "登录刷新"}
    traffic_meter = ProxyTrafficMeter(
        proxy_url=str(proxies.get("register") or ""),
        tracked_proxy=str(proxies.get("mode") or "") == "proxy_pool",
        email=str(email),
        operation=task_type,
    )
    traffic_scope = use_traffic_meter(traffic_meter)
    traffic_scope.__enter__()
    traffic_finished = False

    def finalize_traffic(registration_succeeded: bool) -> dict[str, Any]:
        nonlocal traffic_finished
        if traffic_finished:
            return traffic_meter.snapshot()
        traffic_finished = True
        snapshot = traffic_meter.snapshot()
        try:
            db.record_proxy_traffic(
                str(email),
                mailbox_id,
                int(snapshot.get("total_bytes") or 0),
                registration_attempt=task_type == "sunny_register" and not is_registered_mailbox,
                registration_succeeded=registration_succeeded,
            )
            host_summary = ", ".join(
                f"{host}={int((details or {}).get('bytes') or 0)}"
                for host, details in list((snapshot.get("by_host") or {}).items())[:3]
            )
            db.event(
                f"[{email}] [流量] 本次代理池 HTTP 应用层流量估算 {snapshot.get('total_bytes', 0)} bytes"
                f"（缓存回放已排除，不含 TLS/TCP 开销）"
                + (f"；主要域名 {host_summary}" if host_summary else ""),
                detail={"email": email, "scope": "selected", "proxy_traffic": snapshot},
            )
        except Exception as exc:
            db.event(f"[{email}] [流量] 保存代理池流量统计失败，已保留任务结果: {exc}", "warning", detail={"email": email, "scope": "selected"})
        finally:
            traffic_scope.__exit__(None, None, None)
        return snapshot
    original_mailbox_status = str(mailbox.get("status") or ("已注册" if is_registered_mailbox else "未注册"))
    original_completed_status = original_mailbox_status if _MAILBOX_PROGRESS_RANK.get(original_mailbox_status, -1) > 0 else ""
    db.upsert_account(
        identity_email,
        mailbox_id=mailbox_id,
        status=_account_status_for_mailbox(original_completed_status) if original_completed_status else "pending",
        metadata_json=json.dumps(
            {
                "task_id": db.task_id,
                "source": "sunny_register",
                "checkpoint": "task_started",
                "completed_status": original_completed_status,
            },
            ensure_ascii=False,
        ),
    )
    db.event(f"[{email}] [系统] 开始注册/登录 {index}/{total}，阶段={_stage_label(stage)}", detail={"email": email, "scope": "selected", "stage": stage})
    if execution_mode == "protocol":
        mode_label = (
            "协议注册（Sentinel 协议运行时，仅证明生成使用窄范围 Camoufox）"
            if protocol_challenge_strategy == "sentinel_protocol"
            else "协议注册（本项目原生后台浏览器挑战接管）"
        )
    elif headless:
        mode_label = (
            "无头浏览器注册（直接使用协议降级 Camoufox 流程，不预执行协议请求）"
            if task_type == "sunny_register"
            else "后台浏览器自动（Camoufox Headless，无窗口）"
        )
    else:
        mode_label = "可视浏览器自动（Chromium Visible，有窗口）"
    db.event(f"[{email}] [认证] 执行方式：{mode_label}", detail={"email": email, "scope": "selected", "execution_mode": execution_mode, "headless": headless})
    if proxies.get("register"):
        proxy_label = redact_proxy_url(proxies["register"])
        if proxies.get("mode") == "system_proxy":
            db.event(f"[{email}] [代理] 注册/登录流量使用服务器系统出口代理: {proxy_label}", detail={"email": email, "scope": "selected", "proxy": proxy_label, "proxy_mode": "system_proxy"})
        elif proxies.get("mode") == "local_proxy_fallback":
            db.event(f"[{email}] [代理] 注册/登录流量已切换为本地代理链路: {proxy_label}", detail={"email": email, "scope": "selected", "proxy": proxy_label, "proxy_mode": "local_proxy_fallback"})
        else:
            db.event(f"[{email}] [代理] 注册/登录流量使用代理池代理: {proxy_label}（代理池检测为轻量 TCP 连通检测，不等同于目标站点可访问）", detail={"email": email, "scope": "selected", "proxy": proxy_label, "proxy_mode": "proxy_pool"})
    else:
        db.event(f"[{email}] [代理] 注册/登录流量使用服务器系统网络直连出口", detail={"email": email, "scope": "selected", "proxy": "", "proxy_mode": "direct"})
    _emit_registration_progress(db, str(email), stage, "proxy_ready", setup_login_secret=setup_login_secret_enabled)
    db.mark_mailbox(mailbox_id, "登录刷新" if is_registered_mailbox else "注册中")

    def save_progress(checkpoint: str, snapshot: dict[str, Any]) -> None:
        _emit_registration_progress(db, str(email), stage, checkpoint, setup_login_secret=setup_login_secret_enabled)
        if checkpoint in {"password_submitted", "password_created"}:
            generated_password = str(snapshot.get("generated_chatgpt_password") or "").strip()
            if generated_password:
                account.chatgpt_password = generated_password
                db.save_chatgpt_password(mailbox_id, generated_password)
                db.event(
                    f"[{email}] [登录密钥] 注册密码步骤完成后已立即保存 ChatGPT 密码",
                    detail={
                        "email": email,
                        "scope": "selected",
                        "credential": "chatgpt_password",
                        "checkpoint": checkpoint,
                        "checkpoint_persisted": True,
                    },
                )
        if checkpoint in {"registered", "phone_bound"}:
            _persist_registration_checkpoint(
                db,
                mailbox,
                account,
                checkpoint,
                snapshot,
                original_mailbox_status,
            )

    def save_login_secret_credential(kind: str, value: str) -> None:
        if kind == "password":
            db.save_chatgpt_password(mailbox_id, value)
            db.event(
                f"[{email}] [登录密钥] ChatGPT 密码已立即保存",
                detail={"email": email, "scope": "selected", "credential": "chatgpt_password", "checkpoint_persisted": True},
            )
        elif kind == "totp_secret":
            db.save_totp_secret(mailbox_id, value)
            db.event(
                f"[{email}] [登录密钥] ChatGPT 2FA 已立即保存",
                detail={"email": email, "scope": "selected", "credential": "totp_secret", "checkpoint_persisted": True},
            )

    def save_login_secret_session(session_snapshot: dict[str, Any]) -> None:
        if not isinstance(session_snapshot, dict):
            return
        # /api/auth/session returns accessToken while the persistence layer
        # stores the normalized access_token field. Keep the source response
        # as session_json so it remains authoritative and cannot be mistaken
        # for a missing token during an interruption-safe checkpoint.
        normalized_session = dict(session_snapshot)
        access_token = str(
            normalized_session.get("access_token")
            or normalized_session.get("accessToken")
            or ""
        ).strip()
        if not access_token:
            return
        normalized_session["access_token"] = access_token
        normalized_session.setdefault("session_json", dict(session_snapshot))
        _persist_authenticated_login(db, identity_email, mailbox_id, normalized_session, account.raw)
        db.event(
            f"[{email}] [登录密钥] AT 刷新成功后已立即保存最新 Session",
            detail={"email": email, "scope": "selected", "credential": "access_token", "checkpoint_persisted": True},
        )

    def setup_login_secret_in_browser(context, page, base_session: dict[str, Any]) -> dict[str, Any]:
        """Run optional LS setup inside the registration browser session.

        The browser flow owns this context and keeps its fingerprint/cookies
        alive until this callback returns; no second Camoufox instance is
        created for a freshly registered account.
        """
        if not setup_login_secret_enabled:
            return {}
        db.event(
            f"[{email}] [登录密钥] 在当前注册/登录浏览器中补充缺失的 ChatGPT 密码与 2FA",
            detail={"email": email, "scope": "selected", "setup_login_secret": True, "browser_reused": True},
        )
        return setup_login_secret(
            account,
            base_session,
            proxies["register"],
            lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
            should_cancel=db.cancel_requested,
            mailbox_proxy_url=mailbox_proxy_url,
            traffic_meter=traffic_meter,
            recent_email_code=str(base_session.get("recent_email_code") or ""),
            recent_email_code_at=float(base_session.get("recent_email_code_at") or 0.0),
            browser_page=page,
            browser_context=context,
            on_credential_saved=save_login_secret_credential,
            on_session_saved=save_login_secret_session,
            on_progress=lambda checkpoint: _emit_registration_progress(
                db, str(email), stage, checkpoint, setup_login_secret=True,
            ),
        )

    def setup_login_secret_in_protocol(protocol_client, base_session: dict[str, Any]) -> dict[str, Any]:
        """Run LS setup through the protocol registration cookie jar."""
        if not setup_login_secret_enabled:
            return {}
        db.event(
            f"[{email}] [登录密钥] 在当前协议登录态中补充缺失的 ChatGPT 密码与 2FA",
            detail={"email": email, "scope": "selected", "setup_login_secret": True, "protocol_session_reused": True},
        )
        return setup_login_secret_protocol(
            account,
            base_session,
            protocol_client,
            lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
            should_cancel=db.cancel_requested,
            mailbox_proxy_url=mailbox_proxy_url,
            on_credential_saved=save_login_secret_credential,
            on_session_saved=save_login_secret_session,
            recent_email_code=str(base_session.get("recent_email_code") or ""),
            recent_email_code_at=float(base_session.get("recent_email_code_at") or 0.0),
            on_progress=lambda checkpoint: _emit_registration_progress(
                db, str(email), stage, checkpoint, setup_login_secret=True,
            ),
        )

    wants_rt = stage in {CODEX_PHONE_BIND, IMPORT_REVERSE_PROXY} or explicit_rt_acquire
    phone_provider = None
    require_refresh_token = False
    phone_skipped_reason = ""
    if wants_rt:
        sms_cfg = db.get_config("phone")
        db.event(
            f"[{email}] [接码] 接码资源检查：自建号池可用 {db.usable_phone_count()} 个，LubanSMS={'启用' if _provider_is_available(db, 'luban') else '不可用'}，SMSBower={'启用' if _provider_is_available(db, 'smsbower') else '不可用'}，SMSPool={'启用' if _provider_is_available(db, 'smspool') else '不可用'}，FireFox={'启用' if _provider_is_available(db, 'firefox') else '不可用'}",
            detail={"email": email, "scope": "selected", "sms_provider": "resource_check", "phone_config": {"pool_enabled": sms_cfg.get("pool_enabled"), "luban_enabled": sms_cfg.get("luban_enabled"), "smsbower_enabled": sms_cfg.get("smsbower_enabled"), "smspool_enabled": sms_cfg.get("smspool_enabled"), "firefox_enabled": sms_cfg.get("firefox_enabled")}},
        )
        if account.openai_rt:
            require_refresh_token = True
            db.event(f"[{email}] [接码] 邮箱记录已有 OpenAI RT，将直接刷新 Session", detail={"email": email, "scope": "selected"})
        else:
            phone_provider = _combined_phone_provider(db, email, auxiliary_proxy, execution_mode)
        if phone_provider:
            require_refresh_token = True
            db.event(f"[{email}] [接码] 已启用组合接码策略：外部供应商随机尝试，自建手机号池作为兜底", detail={"email": email, "scope": "selected", "sms_provider": "combined"})
        elif explicit_rt_acquire:
            require_refresh_token = True
            db.event(
                f"[{email}] [Session] 账户没有已保存 RT，将通过已有账户登录态发起 Codex OAuth 授权；若上游要求手机号验证，则联动当前接码配置",
                detail={"email": email, "scope": "selected", "explicit_rt_acquire": True},
            )
        elif not account.openai_rt:
            phone_skipped_reason = "无可用手机号：自建手机号池无可用号码，且 LubanSMS/SMSBower/SMSPool/FireFox 均未启用或未完成配置。本账号只执行 ChatGPT 注册/登录，不进行接码，也不会获取 Refresh Token。"
            db.event(f"[{email}] [接码] {phone_skipped_reason}", "warning", detail={"email": email, "scope": "selected"})
    elif stage == AGENT_IDENTITY_REVERSE_PROXY:
        db.event(
            f"[{email}] [接码] 当前任务选择 Agent Identity 导入，将跳过手机号绑定并使用 Access Token 生成动态签名凭证",
            detail={"email": email, "scope": "selected", "stage": stage},
        )
    else:
        db.event(
            f"[{email}] [接码] 当前任务阶段为“仅注册 ChatGPT”，不会调用接码供应商，也不会获取 Refresh Token",
            detail={"email": email, "scope": "selected", "stage": stage},
        )

    def run_protocol_headless_fallback(existing_session: dict[str, Any] | None = None) -> dict[str, Any]:
        """Use the single Camoufox path shared by direct and protocol fallback registration."""
        return login_or_register(
            account,
            proxies["register"],
            True,
            lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
            phone_provider=phone_provider,
            existing_account=is_registered_mailbox or task_type == "sunny_login",
            require_refresh_token=require_refresh_token,
            should_cancel=db.cancel_requested,
            execution_mode="protocol_headless_fallback",
            on_progress=save_progress,
            mailbox_proxy_url=mailbox_proxy_url,
            existing_session=existing_session,
            traffic_meter=traffic_meter,
            traffic_config=payload.get("browser_traffic_optimization"),
            post_registration_callback=setup_login_secret_in_browser if setup_login_secret_enabled else None,
        )

    try:
        db.ensure_not_cancelled()
        if execution_mode == "protocol":
            try:
                session = login_or_register_protocol(
                    account,
                    proxies["register"],
                    lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
                    existing_account=is_registered_mailbox or task_type == "sunny_login",
                    should_cancel=db.cancel_requested,
                    on_progress=save_progress,
                    challenge_strategy=protocol_challenge_strategy,
                    mailbox_proxy_url=mailbox_proxy_url,
                    traffic_meter=traffic_meter,
                    post_registration_callback=setup_login_secret_in_protocol if setup_login_secret_enabled else None,
                )
            except (ProtocolChallengeRequired, ProtocolRegistrationError) as protocol_error:
                is_challenge = isinstance(protocol_error, ProtocolChallengeRequired)
                retryable_transport_error = _is_retryable_protocol_transport_error(protocol_error)
                if not is_challenge and not retryable_transport_error:
                    raise
                if protocol_batch_policy is not None and protocol_challenge_strategy == "native_headless" and is_challenge:
                    protocol_batch_policy.record_challenge()
                db.ensure_not_cancelled()
                protocol_traffic = getattr(protocol_error, "traffic", None)
                handoff_session = getattr(protocol_error, "browser_handoff", None)
                native_handoff = (
                    handoff_session
                    if is_challenge
                    and protocol_challenge_strategy == "native_headless"
                    and isinstance(handoff_session, dict)
                    else None
                )
                fallback_reason = "浏览器挑战" if is_challenge else "可恢复的网络传输错误"
                if native_handoff:
                    db.event(
                        f"[{email}] [认证] 协议模式遇到浏览器挑战，已保存当前认证 Cookie 与步骤；"
                        "由 Camoufox 原生控件从该断点继续，不重新创建登录/注册会话",
                        "warning",
                        detail={
                            "email": email,
                            "scope": "selected",
                            "execution_mode": "protocol_native_challenge_takeover",
                            "protocol_fallback": "native_challenge_handoff",
                            "challenge_flow": native_handoff.get("protocol_challenge_flow"),
                            "email_verified": native_handoff.get("protocol_email_verified") is True,
                        },
                    )
                else:
                    db.event(
                        f"[{email}] [认证] 协议模式遇到{fallback_reason}，没有可恢复的认证断点，"
                        "切换到后台无头浏览器重新建立会话",
                        "warning",
                        detail={
                            "email": email,
                            "scope": "selected",
                            "execution_mode": "protocol_headless_fallback",
                            "protocol_error": str(protocol_error),
                            "protocol_traffic": protocol_traffic if isinstance(protocol_traffic, dict) else {},
                        },
                    )
                handoff_failed = False
                try:
                    session = run_protocol_headless_fallback(native_handoff)
                except Exception as handoff_error:
                    if native_handoff is None or _is_cancel_exception(handoff_error) or _is_account_deactivated(handoff_error):
                        raise
                    handoff_failed = True
                    db.event(
                        f"[{email}] [认证] 协议断点无头接管未完成，将清除失效断点并使用新的隔离无头会话兜底一次："
                        f"{str(handoff_error)[:300]}",
                        "warning",
                        detail={
                            "email": email,
                            "scope": "selected",
                            "execution_mode": "protocol_headless_fallback",
                            "protocol_fallback": "native_handoff_failed",
                        },
                    )
                    session = run_protocol_headless_fallback()
                session["requested_execution_mode"] = "protocol"
                session["execution_mode"] = "protocol_headless_fallback"
                session["protocol_fallback"] = (
                    "headless_after_handoff_failure"
                    if handoff_failed
                    else "native_challenge_handoff" if native_handoff else "headless"
                )
                if isinstance(protocol_traffic, dict):
                    session["protocol_traffic"] = protocol_traffic
                db.event(
                    f"[{email}] [认证] 协议模式的后台无头浏览器接管已完成"
                    + ("，未重复执行已完成的协议认证步骤" if native_handoff and not handoff_failed else ""),
                    detail={
                        "email": email,
                        "scope": "selected",
                        "execution_mode": "protocol_headless_fallback",
                        "protocol_fallback": session["protocol_fallback"],
                    },
                )
            else:
                if protocol_batch_policy is not None:
                    protocol_batch_policy.record_success()
                protocol_session = session
                if wants_rt and require_refresh_token:
                    db.event(
                        f"[{email}] [认证] 协议注册/登录已完成，复用当前登录态进入后台 OAuth 续段以完成接码和 Refresh Token 获取",
                        detail={"email": email, "scope": "selected", "execution_mode": "protocol_post_stage"},
                    )
                    try:
                        session = login_or_register(
                            account,
                            proxies["register"],
                            True,
                            lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
                            phone_provider=phone_provider,
                            existing_account=True,
                            require_refresh_token=True,
                            should_cancel=db.cancel_requested,
                            execution_mode="protocol_post_stage",
                            on_progress=save_progress,
                            mailbox_proxy_url=mailbox_proxy_url,
                            existing_session=protocol_session,
                            traffic_meter=traffic_meter,
                            traffic_config=payload.get("browser_traffic_optimization"),
                            post_registration_callback=setup_login_secret_in_browser if setup_login_secret_enabled else None,
                        )
                        session["requested_execution_mode"] = "protocol"
                        session["execution_mode"] = "protocol_post_stage"
                        if isinstance(protocol_session.get("protocol_traffic"), dict):
                            session["protocol_traffic"] = protocol_session["protocol_traffic"]
                    except Exception as exc:
                        if _is_cancel_exception(exc):
                            raise
                        session = protocol_session
                        session["post_registration_error"] = f"协议注册已完成，但后续接码/OAuth 阶段失败: {exc}"
                        db.event(
                            f"[{email}] [接码] 协议注册已完成，后续接码/OAuth 阶段失败，账号保留为已注册: {exc}",
                            "warning",
                            detail={"email": email, "scope": "selected", "execution_mode": "protocol_post_stage"},
                        )
        elif execution_mode == "background" and task_type == "sunny_register":
            db.event(
                f"[{email}] [认证] 无头浏览器注册直接使用协议模式的 Camoufox 降级流程；不预执行协议注册请求",
                detail={
                    "email": email,
                    "scope": "selected",
                    "execution_mode": "protocol_headless_fallback",
                    "requested_execution_mode": "background",
                    "protocol_fallback": "direct_headless",
                },
            )
            session = run_protocol_headless_fallback()
            session["requested_execution_mode"] = "background"
            session["execution_mode"] = "protocol_headless_fallback"
            session["protocol_fallback"] = "direct_headless"
        else:
            session = login_or_register(
                account,
                proxies["register"],
                headless,
                lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
                phone_provider=phone_provider,
                existing_account=is_registered_mailbox or task_type == "sunny_login",
                require_refresh_token=require_refresh_token,
                should_cancel=db.cancel_requested,
                execution_mode=execution_mode,
                on_progress=save_progress,
                mailbox_proxy_url=mailbox_proxy_url,
                traffic_meter=traffic_meter,
                traffic_config=payload.get("browser_traffic_optimization"),
                post_registration_callback=setup_login_secret_in_browser if setup_login_secret_enabled else None,
            )
        db.ensure_not_cancelled()
        _persist_authenticated_login(db, identity_email, mailbox_id, session, account.raw)
        persisted_access_token = str(session.get("access_token") or "").strip()
        persisted_refresh_token = str(session.get("refresh_token") or session.get("openai_rt") or "").strip()
        persisted_id_token = str(session.get("id_token") or "").strip()
        persisted_session_json = session.get("session_json")
        persisted_storage_state = session.get("storage_state_json")
        db.event(
            f"[{email}] [Session] 登录成功后已立即同步最新 Access Token",
            detail={"email": email, "scope": "selected", "access_token_synced": True},
        )
        generated_password = str(session.pop("generated_chatgpt_password", "") or "")
        if generated_password:
            db.save_chatgpt_password(mailbox_id, generated_password)
            account.chatgpt_password = generated_password
            db.event(
                f"[{email}] [认证] 已保存本次注册生成的 ChatGPT 密码",
                detail={"email": email, "scope": "selected", "credential": "chatgpt_password"},
            )
        login_secret_result: dict[str, Any] | None = session.pop("login_secret_result", None)
        login_secret_from_browser = login_secret_result is not None
        _raise_if_login_secret_account_deactivated(login_secret_result)
        recent_email_code = str(session.get("recent_email_code") or "").strip()
        try:
            recent_email_code_at = float(session.get("recent_email_code_at") or 0.0)
        except (TypeError, ValueError):
            recent_email_code_at = 0.0
        session.pop("recent_email_code", None)
        session.pop("recent_email_code_at", None)
        if (
            login_secret_result is not None
            and login_secret_result.get("browser_challenge_required") is True
            and execution_mode == "protocol"
        ):
            protocol_login_secret_result = login_secret_result
            if isinstance(protocol_login_secret_result.get("session"), dict):
                session = protocol_login_secret_result["session"]
            db.event(
                f"[{email}] [登录密钥] 协议登录密钥流程遇到浏览器挑战，将携带当前协议 Cookie 登录态由 Camoufox 后台接管",
                "warning",
                detail={"email": email, "scope": "selected", "protocol_login_secret_browser_takeover": True},
            )
            try:
                browser_result = setup_login_secret(
                    account,
                    session,
                    proxies["register"],
                    lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
                    should_cancel=db.cancel_requested,
                    mailbox_proxy_url=mailbox_proxy_url,
                    traffic_meter=traffic_meter,
                    recent_email_code=recent_email_code,
                    recent_email_code_at=recent_email_code_at,
                    force_access_token_refresh=True,
                    on_credential_saved=save_login_secret_credential,
                    on_session_saved=save_login_secret_session,
                    on_progress=lambda checkpoint: _emit_registration_progress(
                        db, str(email), stage, checkpoint, setup_login_secret=True,
                    ),
                )
                _raise_if_login_secret_account_deactivated(browser_result)
                for key in ("password_added", "totp_added"):
                    if protocol_login_secret_result.get(key):
                        browser_result[key] = True
                for key in ("password", "totp_secret"):
                    if not browser_result.get(key) and protocol_login_secret_result.get(key):
                        browser_result[key] = protocol_login_secret_result[key]
                login_secret_result = browser_result
                login_secret_from_browser = True
                if isinstance(browser_result.get("session"), dict):
                    session = browser_result["session"]
            except Exception as exc:
                if _is_cancel_exception(exc):
                    raise
                if _is_account_deactivated(exc):
                    raise
                errors = list(protocol_login_secret_result.get("errors") or [])
                errors.append(f"浏览器挑战接管失败: {exc}")
                login_secret_result = {**protocol_login_secret_result, "complete": False, "errors": errors}
        if payload.get("setup_login_secret") is True and login_secret_result is None:
            db.event(
                f"[{email}] [登录密钥] 开始补充缺失的 ChatGPT 密码与 2FA",
                detail={"email": email, "scope": "selected", "setup_login_secret": True},
            )
            try:
                login_secret_result = setup_login_secret(
                    account,
                    session,
                    proxies["register"],
                    lambda m: db.event(m, detail={"email": email, "scope": "selected"}),
                    should_cancel=db.cancel_requested,
                    mailbox_proxy_url=mailbox_proxy_url,
                    traffic_meter=traffic_meter,
                    recent_email_code=recent_email_code,
                    recent_email_code_at=recent_email_code_at,
                    on_credential_saved=save_login_secret_credential,
                    on_session_saved=save_login_secret_session,
                    on_progress=lambda checkpoint: _emit_registration_progress(
                        db, str(email), stage, checkpoint, setup_login_secret=True,
                    ),
                )
                _raise_if_login_secret_account_deactivated(login_secret_result)
                if login_secret_result.get("password_added"):
                    db.save_chatgpt_password(mailbox_id, str(login_secret_result.get("password") or ""))
                if login_secret_result.get("totp_added"):
                    db.save_totp_secret(mailbox_id, str(login_secret_result.get("totp_secret") or ""))
                if isinstance(login_secret_result.get("session"), dict):
                    session = login_secret_result["session"]
                if login_secret_result.get("complete"):
                    db.event(
                        f"[{email}] [登录密钥] {_login_secret_result_message(login_secret_result)}",
                        detail={"email": email, "scope": "selected", "login_secret_complete": True},
                    )
                else:
                    db.event(
                        f"[{email}] [登录密钥] {_login_secret_result_message(login_secret_result)}",
                        "warning",
                        detail={
                            "email": email,
                            "scope": "selected",
                            "login_secret_complete": False,
                            "password_complete": bool(login_secret_result.get("password")),
                            "totp_complete": bool(login_secret_result.get("totp_secret")),
                            "access_token_refreshed": bool(login_secret_result.get("access_token_refreshed")),
                        },
                    )
            except Exception as exc:
                if _is_cancel_exception(exc):
                    raise
                if _is_account_deactivated(exc):
                    raise
                login_secret_result = {"complete": False, "errors": [str(exc)]}
                db.event(f"[{email}] [登录密钥] 账户已注册，但添加密码与 2FA 失败: {exc}", "warning", detail={"email": email, "scope": "selected", "login_secret_complete": False})
        if login_secret_from_browser and login_secret_result is not None:
            if login_secret_result.get("password_added"):
                db.save_chatgpt_password(mailbox_id, str(login_secret_result.get("password") or ""))
            if login_secret_result.get("totp_added"):
                db.save_totp_secret(mailbox_id, str(login_secret_result.get("totp_secret") or ""))
            if isinstance(login_secret_result.get("session"), dict):
                session = login_secret_result["session"]
            if login_secret_result.get("complete"):
                db.event(
                    f"[{email}] [登录密钥] {_login_secret_result_message(login_secret_result)}",
                    detail={"email": email, "scope": "selected", "login_secret_complete": True},
                )
            else:
                db.event(
                    f"[{email}] [登录密钥] {_login_secret_result_message(login_secret_result)}",
                    "warning",
                    detail={
                        "email": email,
                        "scope": "selected",
                        "login_secret_complete": False,
                        "password_complete": bool(login_secret_result.get("password")),
                        "totp_complete": bool(login_secret_result.get("totp_secret")),
                        "access_token_refreshed": bool(login_secret_result.get("access_token_refreshed")),
                    },
                )
        if session.get("phone_binding_skipped_reason"):
            phone_skipped_reason = str(session.get("phone_binding_skipped_reason") or "")
        rt_value = session.get("refresh_token") or session.get("openai_rt") or account.openai_rt
        has_rt = bool(rt_value)
        phone_bound = bool(session.get("phone_bound")) or has_rt
        candidate_status = "已接码" if phone_bound else "已注册"
        mailbox_status = _highest_mailbox_progress(original_mailbox_status, candidate_status)
        account_id = db.upsert_account(
            identity_email,
            mailbox_id=mailbox_id,
            status=_account_status_for_mailbox(mailbox_status),
            account_type=session.get("plan_type") or account.account_type,
            openai_rt=rt_value,
            access_token=session.get("access_token", ""),
            last_error="",
            metadata_json=json.dumps({"task_id": db.task_id, "source": "sunny_register", "stage": stage, "checkpoint": "flow_completed", "completed_status": mailbox_status, "phone_skipped_reason": phone_skipped_reason}, ensure_ascii=False),
        )
        current_access_token = str(session.get("access_token") or "").strip()
        current_refresh_token = str(session.get("refresh_token") or session.get("openai_rt") or "").strip()
        current_id_token = str(session.get("id_token") or "").strip()
        if (
            current_access_token != persisted_access_token
            or current_refresh_token != persisted_refresh_token
            or current_id_token != persisted_id_token
            or session.get("session_json") != persisted_session_json
            or session.get("storage_state_json") != persisted_storage_state
        ):
            account_fields: dict[str, Any] = {
                "access_token": current_access_token,
                "last_error": "",
            }
            if current_refresh_token:
                account_fields["openai_rt"] = current_refresh_token
            account_id = db.upsert_account(identity_email, **account_fields)
            db.upsert_session(identity_email, account_id, session, account.raw)
        action = str(session.get("auth_action") or "login")
        action_label = "注册" if action == "register" else "登录"
        db.mark_mailbox(mailbox_id, mailbox_status, openai_rt=rt_value)
        post_registration_error = str(session.get("post_registration_error") or "").strip()
        result: dict[str, Any] = {
            "email": email,
            "account_id": account_id,
            "auth_action": action,
            "execution_mode": str(session.get("execution_mode") or execution_mode),
            "stage": stage,
            "access_token": session.get("access_token", ""),
            "refresh_token": rt_value,
            "has_session": bool(session.get("access_token")),
            "phone_bound": phone_bound,
            "completed_status": mailbox_status,
            "stage_complete": stage == REGISTER_ONLY or (stage == CODEX_PHONE_BIND and has_rt),
            "phone_skipped_reason": phone_skipped_reason,
        }
        base_stage_complete = bool(result["stage_complete"])
        if login_secret_result is not None:
            result["login_secret_complete"] = bool(login_secret_result.get("complete"))
            result["login_secret_errors"] = list(login_secret_result.get("errors") or [])
            if not result["login_secret_complete"]:
                login_secret_error = "；".join(result["login_secret_errors"] or ["密码与 2FA 未全部完成"])
                result["stage_error"] = "; ".join(filter(None, [str(result.get("stage_error") or ""), login_secret_error]))
        if isinstance(session.get("protocol_traffic"), dict):
            result["protocol_traffic"] = session["protocol_traffic"]
        if session.get("protocol_fallback"):
            result["protocol_fallback"] = str(session["protocol_fallback"])
        result["proxy_traffic"] = traffic_meter.snapshot()
        if post_registration_error:
            result["stage_error"] = post_registration_error
        db.event(f"[{email}] [认证] 识别为{action_label}成功，已保存 ChatGPT Session" + (" 和 Refresh Token" if result["refresh_token"] else ""), detail={"email": email, "scope": "selected", **result})
        if stage == IMPORT_REVERSE_PROXY:
            if not result["refresh_token"]:
                result["sub2api_skipped_reason"] = "没有 Refresh Token，已停止导入反代平台"
                result["stage_complete"] = False
                result.setdefault("stage_error", post_registration_error or result["sub2api_skipped_reason"])
                db.upsert_account(identity_email, mailbox_id=mailbox_id, status=_account_status_for_mailbox(mailbox_status), last_error=result["stage_error"])
                db.mark_mailbox(mailbox_id, mailbox_status, result["stage_error"], openai_rt=rt_value)
                db.event(
                    f"[{email}] [反代] 没有 Refresh Token，已停止导入 sub2api；OAuth 原因：{result['stage_error']}",
                    "warning",
                    detail={"email": email, "scope": "selected", "oauth_error": result["stage_error"]},
                )
            else:
                try:
                    _emit_registration_progress(db, str(email), stage, "reverse_importing", setup_login_secret=setup_login_secret_enabled)
                    result["sub2api"] = _import_sub2api(db, email, account_id, session, proxy_url=auxiliary_proxy)
                    mailbox_status = _highest_mailbox_progress(mailbox_status, "已反代")
                    db.mark_mailbox(mailbox_id, mailbox_status, openai_rt=rt_value)
                    db.upsert_account(identity_email, mailbox_id=mailbox_id, status="reverse_proxied", last_error="")
                    result["completed_status"] = mailbox_status
                    result["stage_complete"] = True
                    _emit_registration_progress(db, str(email), stage, "reverse_imported", setup_login_secret=setup_login_secret_enabled)
                except Exception as exc:
                    stage_error = str(exc)
                    result["stage_complete"] = False
                    result["stage_error"] = stage_error
                    result["sub2api_error"] = stage_error
                    db.set_account_sub2api_status(email, "failed", error=stage_error)
                    db.mark_mailbox(mailbox_id, mailbox_status, stage_error, openai_rt=rt_value)
                    db.event(f"[{email}] [反代] 导入 sub2api 失败，账号保留为{mailbox_status}: {stage_error}", "error", detail={"email": email, "scope": "selected", "completed_status": mailbox_status})
        elif stage == AGENT_IDENTITY_REVERSE_PROXY:
            try:
                _emit_registration_progress(db, str(email), stage, "agent_identity_importing", setup_login_secret=setup_login_secret_enabled)
                import_result = _import_sub2api_agent_identity(
                    db,
                    email,
                    account_id,
                    session,
                    auxiliary_proxy,
                )
                import_mode = str(import_result.pop("_sunny_import_mode", "agent_identity")) if isinstance(import_result, dict) else "agent_identity"
                result["sub2api"] = import_result
                mailbox_status = _highest_mailbox_progress(mailbox_status, "已反代")
                db.mark_mailbox(mailbox_id, mailbox_status, openai_rt=rt_value)
                db.upsert_account(identity_email, mailbox_id=mailbox_id, status="reverse_proxied", last_error="")
                result["completed_status"] = mailbox_status
                result["stage_complete"] = True
                result["agent_identity"] = import_mode == "agent_identity"
                result["agent_identity_fallback"] = import_mode != "agent_identity"
                _emit_registration_progress(db, str(email), stage, "agent_identity_imported", setup_login_secret=setup_login_secret_enabled)
            except Exception as exc:
                if _is_cancel_exception(exc):
                    raise
                stage_error = str(exc)
                result["stage_complete"] = False
                result["stage_error"] = stage_error
                result["sub2api_error"] = stage_error
                db.set_account_sub2api_status(email, "failed", error=stage_error)
                db.mark_mailbox(mailbox_id, mailbox_status, stage_error, openai_rt=rt_value)
                db.upsert_account(identity_email, mailbox_id=mailbox_id, status=_account_status_for_mailbox(mailbox_status), last_error=stage_error)
                db.event(
                    f"[{email}] [反代] 绕过接码导入反代平台未完成，账号保留为{mailbox_status}: {stage_error}",
                    "error",
                    detail={"email": email, "scope": "selected", "completed_status": mailbox_status},
                )
        elif wants_rt and not result["stage_complete"]:
            stage_error = post_registration_error or phone_skipped_reason or "接码/Refresh Token 阶段未完成"
            result["stage_error"] = stage_error
            db.upsert_account(identity_email, mailbox_id=mailbox_id, status=_account_status_for_mailbox(mailbox_status), last_error=stage_error)
            db.mark_mailbox(mailbox_id, mailbox_status, stage_error, openai_rt=rt_value)
            db.event(f"[{email}] [接码] 后续接码阶段未完成，账号保留为{mailbox_status}: {stage_error}", "warning", detail={"email": email, "scope": "selected", "completed_status": mailbox_status})
        if login_secret_result is not None:
            # LS is an optional post-registration phase. Keep the account and its
            # base registration result, but mark the task progress partial when
            # either password or TOTP setup did not finish.
            base_stage_complete = bool(result.get("stage_complete"))
            result["stage_complete"] = bool(result.get("stage_complete") and login_secret_result.get("complete"))
        elif setup_login_secret_enabled:
            result["stage_complete"] = False
        result["has_access_token"] = bool(result.pop("access_token", ""))
        result["has_refresh_token"] = bool(result.pop("refresh_token", ""))
        terminal_checkpoint = {
            REGISTER_ONLY: "registered",
            CODEX_PHONE_BIND: "phone_bound",
            IMPORT_REVERSE_PROXY: "reverse_imported",
            AGENT_IDENTITY_REVERSE_PROXY: "agent_identity_imported",
        }.get(stage, "registered")
        terminal_checkpoint = (
            "login_secret_completed"
            if setup_login_secret_enabled and result.get("stage_complete")
            else "login_secret_failed"
            if setup_login_secret_enabled and base_stage_complete
            else terminal_checkpoint
        )
        _emit_registration_progress(
            db,
            str(email),
            stage,
            terminal_checkpoint
            if result.get("stage_complete") or (setup_login_secret_enabled and base_stage_complete)
            else "stage_incomplete",
            state="completed" if result.get("stage_complete") else "abnormal",
            error=str(result.get("stage_error") or ""),
            setup_login_secret=setup_login_secret_enabled,
        )
        result["proxy_traffic"] = finalize_traffic(True)
        return True, result
    except Exception as exc:
        if _is_cancel_exception(exc):
            finalize_traffic(False)
            current_status = db.mailbox_status(mailbox_id)
            completed_status = _highest_mailbox_progress(original_mailbox_status, current_status)
            if _MAILBOX_PROGRESS_RANK.get(completed_status, -1) > 0:
                db.mark_mailbox(mailbox_id, completed_status)
                db.event(f"[{email}] [系统] 用户已停止任务，账号保留在上一个完成状态：{completed_status}", "warning", detail={"email": email, "scope": "selected", "cancelled": True, "completed_status": completed_status})
            else:
                db.mark_mailbox(mailbox_id, "失败", "任务已由用户停止，当前邮箱尚未完成 ChatGPT 注册")
                db.event(f"[{email}] [系统] 用户已停止任务，当前邮箱尚未完成 ChatGPT 注册并已标记为失败", "warning", detail={"email": email, "scope": "selected", "cancelled": True})
            raise
        err_text = str(exc)
        err = f"[{email}] {err_text}"
        traffic_snapshot = finalize_traffic(False)
        traffic = getattr(exc, "traffic", None)
        failure = classify_auth_failure(exc)
        if isinstance(exc, MailboxAccessError) and exc.terminal or failure.category == "mailbox_credential_invalid":
            marker = getattr(db, "mark_mailbox_credential_invalid", None)
            if callable(marker):
                marker(mailbox_id, err_text)
            db.event(
                f"[{email}] [邮箱] 邮箱凭证已确认失效，已停用该邮箱，避免后续任务重复取件",
                "warning",
                detail={"email": email, "scope": "selected", "mailbox_id": mailbox_id, "credential_invalid": True},
            )
        if _is_account_deactivated(err_text):
            db.mark_account_deactivated(email, err_text)
            db.event(
                f"[{email}] [认证] OpenAI 返回 account_deactivated，账户已标记为已封禁",
                "warning",
                detail={"email": email, "scope": "selected", "account_deactivated": True},
            )
        elif "Phone verification required" in err_text or "phone verification" in err_text.lower():
            db.mark_mailbox(mailbox_id, "需二验", err_text)
            db.event(f"[{email}] [接码] 账号需要手机号二次验证，但当前没有可用接码配置，本账号流程已停止", "warning", detail={"email": email, "scope": "selected"})
        elif original_completed_status:
            db.mark_mailbox(mailbox_id, original_completed_status, err_text)
            db.upsert_account(identity_email, mailbox_id=mailbox_id, status=_account_status_for_mailbox(original_completed_status), last_error=err_text)
            db.event(f"[{email}] [系统] 后续操作失败，账号保留在已完成状态：{original_completed_status}", "warning", detail={"email": email, "scope": "selected", "completed_status": original_completed_status})
        else:
            db.mark_mailbox(mailbox_id, "失败", err_text)
            db.upsert_account(identity_email, mailbox_id=mailbox_id, status="failed", last_error=err_text)
            if str(payload.get("identity") or "").strip().lower() in {"domain", "domain_mailbox", "自建域名邮箱"}:
                try:
                    cleanup_failed_mailbox(db, db.get_config("domain_mailbox"), str(email), str(mailbox.get("pickup_token_hash") or ""), db.event)
                except Exception as cleanup_exc:
                    db.event(f"[{email}] 失败域名邮箱清理失败：{cleanup_exc}", "warning", detail={"email": email, "scope": "selected"})
        error_detail = {"email": email, "scope": "selected", "traceback": traceback.format_exc()[-3000:]}
        if isinstance(traffic, dict):
            error_detail["protocol_traffic"] = traffic
        error_detail["proxy_traffic"] = traffic_snapshot
        db.event(err, "error", detail=error_detail)
        _emit_registration_progress(db, str(email), stage, "failed", state="abnormal", error=err_text, setup_login_secret=setup_login_secret_enabled)
        return False, err


def _run_one(
    db: SunnyDB,
    task_type: str,
    payload: dict[str, Any],
    mailbox: dict[str, Any],
    index: int,
    total: int,
    protocol_batch_policy: _ProtocolBatchPolicy | None = None,
) -> tuple[bool, dict[str, Any] | str]:
    """Serialize operations that consume OTPs from the same mailbox."""
    mailbox_id = max(0, int(mailbox.get("id") or 0))
    acquire = getattr(db, "acquire_mailbox_lease", None)
    release = getattr(db, "release_mailbox_lease", None)
    owner = f"{getattr(db, 'task_id', 'inline')}:{mailbox_id}:{index}:{uuid.uuid4().hex}"
    acquired = mailbox_id <= 0 or not callable(acquire)
    if callable(acquire) and mailbox_id > 0:
        for _attempt in range(16):
            db.ensure_not_cancelled()
            if acquire(mailbox_id, owner, ttl_seconds=900):
                acquired = True
                break
            time.sleep(1)
    if not acquired:
        email = str(mailbox.get("email") or "")
        message = f"[{email}] 邮箱正在被另一个登录/注册任务使用，等待租约超时"
        db.event(message, "warning", detail={"email": email, "scope": "selected", "mailbox_id": mailbox_id, "mailbox_lease_busy": True})
        return False, message
    try:
        return _run_one_impl(db, task_type, payload, mailbox, index, total, protocol_batch_policy)
    finally:
        if callable(release) and mailbox_id > 0:
            release(mailbox_id, owner)


def _run_one_isolated(
    task_id: str,
    task_type: str,
    payload: dict[str, Any],
    mailbox: dict[str, Any],
    index: int,
    total: int,
    protocol_batch_policy: _ProtocolBatchPolicy | None = None,
) -> tuple[int, bool, dict[str, Any] | str]:
    """Run one mailbox in its own DB connection/thread.

    Each browser flow owns exactly one mailbox/account object, one Outlook reader,
    one browser context and one SQLite connection. This keeps concurrent OTP reads
    and mailbox state updates isolated from other mailboxes.
    """
    worker_db = SunnyDB(task_id, ensure_schema=False)
    try:
        ok, result = _run_one(worker_db, task_type, payload, mailbox, index, total, protocol_batch_policy)
        return index, ok, result
    finally:
        worker_db.close()


def _interruptible_delay(db: SunnyDB, seconds: float) -> None:
    remaining = max(0.0, float(seconds or 0))
    while remaining > 0:
        db.ensure_not_cancelled()
        chunk = min(1.0, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _refresh_with_retry(db: SunnyDB, refresh_token: str, proxy_url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        db.ensure_not_cancelled()
        try:
            return refresh_openai_access_token(refresh_token, proxy_url)
        except Exception as exc:
            last_error = exc
            decision = retry_allowed(exc, attempt, operation="token_refresh")
            if not decision.retryable:
                raise
            _interruptible_delay(db, decision.delay_seconds)
    raise RuntimeError(str(last_error or "Refresh Token 续期失败"))


def _verify_access_token(access_token: str, proxy_url: str) -> dict[str, Any]:
    result = probe_access_token(access_token, proxy_url)
    if result.get("status") != "valid":
        error = str(result.get("error") or "AT 二次验活未得到有效响应")
        marker = "token_invalid: " if result.get("status") == "invalid" else ""
        raise RuntimeError(f"{marker}AT 二次验活失败[{result.get('status') or 'unknown'}]: {error}")
    return result


def _verify_persisted_access_token(db: SunnyDB, email: str, access_token: str, proxy_url: str) -> dict[str, Any]:
    try:
        return _verify_access_token(access_token, proxy_url)
    except Exception as exc:
        discard = getattr(db, "discard_unverified_access_token", None)
        if callable(discard):
            discard(email, access_token, str(exc))
        raise


def _refresh_sessions_sequential(db: SunnyDB, payload: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    accounts = db.fetch_accounts(_ids(payload.get("account_ids")) or None)
    index_offset = max(0, int(payload.get("_renewal_index_offset") or 0))
    total_accounts = max(1, int(payload.get("_renewal_total") or len(accounts) or 1))
    parallel = bool(payload.get("_renewal_parallel"))
    ok = 0
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    for idx, acc in enumerate(accounts, start=index_offset + 1):
        db.ensure_not_cancelled()
        email = acc.get("email") or ""
        renewal_current = 3
        renewal_total = 10
        _emit_renewal_progress(db, email, renewal_current, renewal_total, "recovery_preparing")
        try:
            mailbox = db.fetch_mailbox_by_email(email)
            rt = acc.get("openai_rt") or ""
            if not rt:
                sess = db.fetch_session_by_email(email) or {}
                rt = sess.get("refresh_token") or ""
            renewal_current = 4
            _emit_renewal_progress(db, email, renewal_current, renewal_total, "credentials_loaded")
            refresh_error = ""
            if rt:
                try:
                    renewal_current = 5
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "refresh_token_ready")
                    proxy_url = _proxy_snapshot(payload, idx - 1)["register"]
                    token = _refresh_with_retry(db, rt, proxy_url)
                    db.ensure_not_cancelled()
                    renewal_current = 6
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "token_received")
                    new_access_token = str(token.get("access_token") or "").strip()
                    renewal_current = 7
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "secondary_probe")
                    probe = _verify_access_token(new_access_token, proxy_url)
                    account_id = int(acc.get("id") or db.upsert_account(email))
                    payload2 = {"access_token": new_access_token, "refresh_token": token.get("refresh_token") or rt, "id_token": token.get("id_token", ""), "expires_at": token.get("expires_at"), "session_json": token}
                    renewal_current = 8
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "saving_session")
                    db.upsert_session(email, account_id, payload2)
                    refreshed_status = "已接码" if payload2["refresh_token"] else "已注册"
                    current_status = str((mailbox or {}).get("status") or acc.get("status") or "")
                    completed_status = _highest_mailbox_progress(current_status, refreshed_status)
                    db.upsert_account(email, status=_account_status_for_mailbox(completed_status), access_token=payload2["access_token"], openai_rt=payload2["refresh_token"])
                    db.mark_mailbox_by_email(email, completed_status, openai_rt=payload2["refresh_token"])
                    marker = getattr(db, "mark_access_token_probe", None)
                    if callable(marker):
                        marker(email, "valid")
                    renewal_current = 9
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "session_saved")
                    items.append({"email": email, "status": "valid", "has_access_token": True, "has_refresh_token": bool(payload2["refresh_token"]), "refresh_method": "refresh_token", "secondary_probe": probe.get("status"), "verified": True})
                    ok += 1
                    _account_event(db, email, "session", "access_token.renewed", f"[{email}] [Session] 已通过 Refresh Token 完成 AT 续期并通过二次验活", account_id=account_id)
                    renewal_current = 10
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "completed", state="succeeded")
                    if not parallel:
                        db.update_task(progress_current=idx, success_count=ok, error_count=len(errors))
                    continue
                except Exception as exc:
                    if _is_cancel_exception(exc):
                        raise
                    if _is_account_deactivated(exc):
                        raise
                    refresh_error = str(exc)
                    renewal_current = 5
                    _emit_renewal_progress(db, email, renewal_current, renewal_total, "refresh_token_unavailable")
                    _account_event(db, email, "session", "refresh_token.unavailable", f"[{email}] [Session] Refresh Token 续期不可用，改用后台无头登录更新 AT：{refresh_error}", "warning", account_id=int(acc.get("id") or 0))
            else:
                renewal_current = 5
                _emit_renewal_progress(db, email, renewal_current, renewal_total, "refresh_token_missing")
                _account_event(db, email, "session", "refresh_token.missing", f"[{email}] [Session] 账户没有可用 Refresh Token，改用后台无头登录更新 AT", "warning", account_id=int(acc.get("id") or 0))

            if not mailbox:
                raise RuntimeError("找不到该账户对应的邮箱凭证，无法回退登录更新 AT")
            renewal_current = 5
            _emit_renewal_progress(db, email, renewal_current, renewal_total, "mailbox_ready")
            fallback_payload = dict(payload)
            fallback_payload.update(
                {
                    "execution_mode": "protocol",
                    "protocol_challenge_strategy": "sentinel_protocol",
                    "registration_stage": "register_only",
                    "access_token_renewal": True,
                    "mailbox_ids": [int(mailbox.get("id") or 0)],
                }
            )
            renewal_current = 6
            _emit_renewal_progress(db, email, renewal_current, renewal_total, "protocol_login_started")
            db.event(
                f"[{email}] [Session] 复用注册机登录链路更新 AT：协议登录优先，遇到挑战时先由窄范围 Sentinel 生成证明，失败再由无头浏览器接管",
                detail={"email": email, "scope": "selected", "renewal_login_mode": "protocol_sentinel_headless_fallback"},
            )
            succeeded, result = _run_one(db, "sunny_login", fallback_payload, mailbox, idx, total_accounts)
            if not succeeded and _is_account_deactivated(result):
                raise RuntimeError(str(result).strip())
            if not succeeded:
                db.ensure_not_cancelled()
                decision = retry_allowed(result, 0, operation="protocol_login")
                if decision.terminal:
                    raise RuntimeError(str(result).strip())
                wait_seconds = decision.delay_seconds or (15 if _is_otp_security_context_failure(result) else 2)
                db.event(
                    f"[{email}] [认证] 协议/原生挑战登录链路未完成，将建立新的隔离无痕后台浏览器上下文重试一次：{result}",
                    "warning",
                    detail={"email": email, "scope": "selected", "renewal_fallback": "background_headless"},
                )
                _emit_renewal_progress(db, email, 7, renewal_total, "headless_login_fallback")
                _interruptible_delay(db, wait_seconds)
                background_payload = dict(fallback_payload)
                background_payload.update({"execution_mode": "background", "renewal_retry_fresh_context": True})
                succeeded, result = _run_one(db, "sunny_login", background_payload, mailbox, idx, total_accounts)
            if not succeeded and _is_account_deactivated(result):
                raise RuntimeError(str(result).strip())
            retry_decision = retry_allowed(result, 0, operation="protocol_login") if not succeeded else None
            if not succeeded and retry_decision and retry_decision.fresh_context and not retry_decision.terminal:
                db.ensure_not_cancelled()
                db.event(
                    f"[{email}] [认证] 后台登录的邮箱验证码请求被认证证明层拒绝；"
                    "已停止使用旧验证码，将建立新的隔离无痕后台浏览器上下文并等待新验证码后重试一次",
                    "warning",
                    detail={"email": email, "scope": "selected", "renewal_fallback": "fresh_headless_context"},
                )
                _emit_renewal_progress(db, email, 7, renewal_total, "sentinel_login_retry")
                # The next reader filters mail by timestamp. Let the rejected OTP
                # fall outside that window so the retry cannot consume it again.
                _interruptible_delay(db, retry_decision.delay_seconds or 15)
                retry_payload = dict(fallback_payload)
                retry_payload["execution_mode"] = "background"
                retry_payload["renewal_retry_fresh_context"] = True
                succeeded, result = _run_one(db, "sunny_login", retry_payload, mailbox, idx, total_accounts)
            if not succeeded:
                result_text = str(result).strip()
                email_prefix = f"[{email}] "
                if result_text.startswith(email_prefix):
                    result_text = result_text[len(email_prefix):].strip()
                raise RuntimeError(result_text)
            fetch_session_by_account = getattr(db, "fetch_session_by_account_id", None)
            refreshed_session = (
                fetch_session_by_account(int(acc.get("id") or 0))
                if callable(fetch_session_by_account)
                else None
            ) or db.fetch_session_by_email(email) or {}
            refreshed_token = str(refreshed_session.get("access_token") or "").strip()
            if not refreshed_token:
                raw_session_json = refreshed_session.get("session_json")
                if isinstance(raw_session_json, str):
                    try:
                        raw_session_json = json.loads(raw_session_json)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raw_session_json = {}
                if isinstance(raw_session_json, dict):
                    refreshed_token = str(raw_session_json.get("accessToken") or raw_session_json.get("access_token") or "").strip()
            renewal_current = 8
            _emit_renewal_progress(db, email, renewal_current, renewal_total, "secondary_probe")
            probe = _verify_persisted_access_token(db, email, refreshed_token, _proxy_snapshot(payload, idx - 1)["register"])
            marker = getattr(db, "mark_access_token_probe", None)
            if callable(marker):
                marker(email, "valid")
            renewal_current = 9
            _emit_renewal_progress(db, email, renewal_current, renewal_total, "session_refreshed")
            items.append({"email": email, "status": "valid", "has_access_token": True, "has_refresh_token": bool(refreshed_session.get("refresh_token")), "refresh_method": "login", "refresh_token_error": refresh_error, "secondary_probe": probe.get("status"), "verified": True, "login_required": True, "login_succeeded": True})
            ok += 1
            _account_event(db, email, "session", "access_token.renewed", f"[{email}] [Session] 已通过登录完成 AT 续期并通过二次验活", account_id=int(acc.get("id") or 0))
            renewal_current = 10
            _emit_renewal_progress(db, email, renewal_current, renewal_total, "completed", state="succeeded")
        except Exception as exc:
            if _is_cancel_exception(exc):
                raise
            errors.append(f"[{email}] {exc}")
            failure = classify_auth_failure(exc)
            items.append({"email": email, "status": "failed", "error": str(exc), "error_category": failure.category, "retryable": failure.retryable, "login_required": True, "login_succeeded": False})
            if _is_account_deactivated(exc):
                db.mark_account_deactivated(email, str(exc))
                db.event(
                    f"[{email}] [认证] AT 续期确认账户已停用，已归类为已封禁并更新最近测活时间",
                    "warning",
                    detail={"email": email, "scope": "selected", "account_deactivated": True},
                )
                _emit_renewal_progress(db, email, renewal_current, renewal_total, "account_deactivated", state="failed", error=str(exc))
            else:
                db.mark_access_token_renewal_failed(email, str(exc))
                _account_event(db, email, "session", "access_token.renewal_failed", errors[-1], "error", account_id=int(acc.get("id") or 0), detail={"error": str(exc), "error_category": failure.category, "retryable": failure.retryable})
                _emit_renewal_progress(db, email, renewal_current, renewal_total, "failed", state="failed", error=str(exc))
        if not parallel:
            db.update_task(progress_current=idx, success_count=ok, error_count=len(errors))
    return ok, errors, items


def _refresh_sessions_isolated(
    task_id: str,
    payload: dict[str, Any],
    account_id: int,
    index: int,
    total: int,
) -> tuple[int, int, list[str], list[dict[str, Any]]]:
    """Refresh one account with an isolated DB connection and auth context."""
    worker_db = SunnyDB(task_id, ensure_schema=False)
    single_payload = dict(payload)
    single_payload.update(
        {
            "account_ids": [account_id],
            "_renewal_index_offset": index - 1,
            "_renewal_total": total,
            "_renewal_parallel": True,
        }
    )
    try:
        ok, errors, items = _refresh_sessions_sequential(worker_db, single_payload)
        return index, ok, errors, items
    finally:
        worker_db.close()


def _refresh_sessions(db: SunnyDB, payload: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    accounts = db.fetch_accounts(_ids(payload.get("account_ids")) or None)
    requested = int(payload.get("concurrency") or os.getenv("SUNNY_AT_RENEWAL_CONCURRENCY") or 3)
    concurrency = max(1, min(requested, 6, len(accounts)))
    if not accounts:
        return 0, [], []
    candidates = _proxy_pool_candidates(payload) if payload.get("proxy_enabled") is not False else []
    scheduler = TaskProxyScheduler(candidates, lambda proxy: proxy_target_tls_check(proxy, timeout=10))
    leases: dict[int, ProxyLease] = {}
    probe_payloads: dict[int, dict[str, Any]] = {}
    for index, account in enumerate(accounts):
        account_id = int(account.get("id") or 0)
        lease = scheduler.acquire(str(account.get("email") or account_id), index)
        leases[account_id] = lease
        current = dict(payload)
        if lease.address or not candidates:
            current["_proxy_lease"] = lease.payload()
        else:
            current["_proxy_unavailable"] = True
        probe_payloads[account_id] = current
    db.event(
        f"[系统] AT续期采用两阶段并发：先验活 {len(accounts)} 个账户，再仅恢复确认失效账户；并发数 {concurrency}",
        detail={"scope": "global", "concurrency": concurrency, "total": len(accounts), "operation": "access_token_renewal", "strategy": "probe_then_recover_then_verify"},
    )
    success = 0
    completed = 0
    errors: list[str] = []
    items: list[dict[str, Any]] = []

    probe_specs: list[dict[str, Any]] = []
    fetch_saved_session = getattr(db, "fetch_session_by_email", None)
    for index, account in enumerate(accounts):
        email = str(account.get("email") or "")
        session = fetch_saved_session(email) if callable(fetch_saved_session) else {}
        session = session or {}
        probe_specs.append({
            "account": account,
            "index": index,
            "token": str(account.get("access_token") or session.get("access_token") or "").strip(),
            "previous_status": str(session.get("access_token_status") or "").strip().lower(),
        })

    def probe_existing(spec: dict[str, Any]) -> dict[str, Any]:
        account = spec["account"]
        account_id = int(account.get("id") or 0)
        if probe_payloads[account_id].get("_proxy_unavailable") is True:
            return {**spec, "probe": {"status": "probe_failed", "error": "代理池脉冲预检未找到可用 ChatGPT HTTPS 出口"}}
        proxy_url = _proxy_snapshot(probe_payloads[account_id], int(spec["index"]))["register"]
        token = str(spec.get("token") or "")
        result = probe_access_token(token, proxy_url)
        return {**spec, "probe": result}

    probe_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-at-probe") as pool:
        futures = {pool.submit(probe_existing, spec): spec for spec in probe_specs}
        for future in futures:
            ensure_active = getattr(db, "ensure_not_cancelled", None)
            if callable(ensure_active):
                ensure_active()
            probe_results.append(future.result())

    recovery: list[dict[str, Any]] = []
    for outcome in probe_results:
        account = outcome["account"]
        email = str(account.get("email") or "")
        account_id = int(account.get("id") or 0)
        probe = outcome["probe"]
        status = str(probe.get("status") or "probe_failed")
        _emit_renewal_progress(db, email, 1, 10, "precheck_started")
        marker = getattr(db, "mark_access_token_probe", None)
        if status == "valid":
            if callable(marker):
                marker(email, "valid")
            success += 1
            completed += 1
            scheduler.record(leases[account_id], success=True)
            items.append({"email": email, "status": "valid", "refresh_method": "existing_access_token", "login_required": False, "verified": True})
            _emit_renewal_progress(db, email, 10, 10, "precheck_valid", state="succeeded")
            db.update_task(progress_current=completed, success_count=success, error_count=len(errors))
            continue
        confirmed_invalid = status == "invalid" or not outcome["token"] or outcome["previous_status"] == "invalid"
        if confirmed_invalid:
            if callable(marker):
                marker(email, "invalid", str(probe.get("error") or ""))
            recovery.append(outcome)
            _emit_renewal_progress(db, email, 2, 10, "precheck_invalid")
            continue
        error = str(probe.get("error") or "AT 验活结果不确定")
        if callable(marker):
            marker(email, status, error)
        failure = classify_auth_failure(error, http_status=int(probe.get("http_status") or 0))
        errors.append(f"[{email}] {error}")
        items.append({"email": email, "status": status, "error": error, "error_category": failure.category, "login_required": False})
        completed += 1
        scheduler.record(leases[account_id], success=False, error=error)
        _emit_renewal_progress(db, email, 2, 10, "precheck_unconfirmed", state="failed", error=error)
        db.update_task(progress_current=completed, success_count=success, error_count=len(errors))

    db.event(
        f"[系统] AT 第一阶段验活完成：有效 {success}，需要恢复 {len(recovery)}，未确认 {len(errors)}",
        detail={"scope": "global", "operation": "access_token_renewal", "phase": "probe_complete", "valid": success, "login_required": len(recovery), "unconfirmed": len(errors), "proxy_scheduler": scheduler.snapshot()},
    )
    if not hasattr(db, "task_id"):
        for outcome in recovery:
            account_id = int(outcome["account"].get("id") or 0)
            single_payload = dict(probe_payloads[account_id])
            single_payload.update({"account_ids": [account_id], "_renewal_index_offset": int(outcome["index"]), "_renewal_total": len(accounts), "_renewal_parallel": True})
            account_ok, account_errors, account_items = _refresh_sessions_sequential(db, single_payload)
            completed += 1
            success += account_ok
            errors.extend(account_errors)
            items.extend(account_items)
            scheduler.record(leases[account_id], success=bool(account_ok), error=account_errors[0] if account_errors else "")
            db.update_task(progress_current=completed, success_count=success, error_count=len(errors))
    elif recovery:
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-renewal")
        try:
            futures = {
                pool.submit(
                    _refresh_sessions_isolated,
                    db.task_id,
                    probe_payloads[int(outcome["account"].get("id") or 0)],
                    int(outcome["account"].get("id") or 0),
                    int(outcome["index"]) + 1,
                    len(accounts),
                ): outcome
                for outcome in recovery
            }
            pending = set(futures)
            while pending:
                if db.cancel_requested():
                    for future in pending:
                        future.cancel()
                    raise SunnyTaskCancelled("Task cancelled by user")
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    outcome = futures[future]
                    email = str(outcome["account"].get("email") or "")
                    try:
                        _index, ok, account_errors, account_items = future.result()
                    except Exception as exc:
                        if _is_cancel_exception(exc):
                            raise
                        ok = 0
                        account_items = []
                        account_errors = [f"[{email}] AT续期并行 Worker 失败: {exc}"]
                    completed += 1
                    success += ok
                    errors.extend(account_errors)
                    items.extend(account_items)
                    account_id = int(outcome["account"].get("id") or 0)
                    scheduler.record(leases.get(account_id, ProxyLease("", 0, "", -1)), success=bool(ok), error=account_errors[0] if account_errors else "")
                    db.update_task(progress_current=completed, success_count=success, error_count=len(errors))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    db.event(
        f"[系统] AT 第二阶段恢复完成：成功 {success}，失败 {len(errors)}；新 AT 均已执行二次验活",
        detail={"scope": "global", "operation": "access_token_renewal", "phase": "recovery_complete", "success": success, "failed": len(errors), "proxy_scheduler": scheduler.snapshot()},
    )
    return success, errors, items


def _persist_maintenance_tokens(
    db: SunnyDB,
    account: dict[str, Any],
    token: dict[str, Any],
    refresh_token: str,
) -> dict[str, Any]:
    email = str(account.get("email") or "")
    mailbox = db.fetch_mailbox_by_email(email) or {}
    account_id = int(account.get("id") or db.upsert_account(email))
    normalized = {
        "access_token": str(token.get("access_token") or "").strip(),
        "refresh_token": str(token.get("refresh_token") or refresh_token).strip(),
        "id_token": str(token.get("id_token") or "").strip(),
        "expires_at": token.get("expires_at"),
        "session_json": token,
    }
    if not normalized["access_token"] or not normalized["refresh_token"]:
        raise RuntimeError("令牌响应缺少 Access Token 或 Refresh Token")
    db.upsert_session(email, account_id, normalized)
    current_status = str(mailbox.get("status") or account.get("status") or "")
    completed_status = _highest_mailbox_progress(current_status, "已接码")
    db.upsert_account(email, status=_account_status_for_mailbox(completed_status), access_token=normalized["access_token"], openai_rt=normalized["refresh_token"], last_error="")
    db.mark_mailbox_by_email(email, completed_status, openai_rt=normalized["refresh_token"])
    marker = getattr(db, "mark_access_token_probe", None)
    if callable(marker):
        marker(email, "valid")
    return normalized


def _acquire_refresh_token_recovery(
    db: SunnyDB,
    payload: dict[str, Any],
    account: dict[str, Any],
    index: int,
    total: int,
) -> tuple[bool, str, dict[str, Any]]:
    email = str(account.get("email") or "")
    account_id = int(account.get("id") or 0)
    try:
        mailbox = db.fetch_mailbox_by_email(email)
        if not mailbox:
            raise RuntimeError("找不到该账户对应的邮箱凭证")
        acquire_payload = dict(payload)
        acquire_payload.update({
            "execution_mode": "protocol",
            "protocol_challenge_strategy": "sentinel_protocol",
            "registration_stage": CODEX_PHONE_BIND,
            "mailbox_ids": [int(mailbox.get("id") or 0)],
        })
        succeeded, result = _run_one(db, "sunny_acquire_rt", acquire_payload, mailbox, index, total)
        result_map = result if isinstance(result, dict) else {}
        if not succeeded or not result_map.get("has_refresh_token"):
            detail = str(result_map.get("stage_error") or result_map.get("phone_skipped_reason") or result)
            raise RuntimeError(detail if detail and detail != "{}" else "无法获取该账户RT")
        saved = db.fetch_session_by_email(email) or {}
        access_token = str(saved.get("access_token") or "").strip()
        refresh_token = str(saved.get("refresh_token") or "").strip()
        probe = _verify_persisted_access_token(db, email, access_token, _proxy_snapshot(payload, index - 1)["register"])
        if not refresh_token:
            raise RuntimeError("Codex OAuth 登录完成但数据库中没有新的 Refresh Token")
        marker = getattr(db, "mark_access_token_probe", None)
        if callable(marker):
            marker(email, "valid")
        item = {"email": email, "status": "valid", "has_refresh_token": True, "has_access_token": True, "acquire_method": "codex_oauth", "secondary_probe": probe.get("status"), "verified": True, "login_required": True, "login_succeeded": True}
        _account_event(db, email, "session", "refresh_token.acquired", f"[{email}] [Session] 已通过 Codex OAuth 获取 Refresh Token，新 AT 已通过二次验活", account_id=account_id)
        return True, "", item
    except Exception as exc:
        if _is_cancel_exception(exc):
            raise
        failure = classify_auth_failure(exc)
        if failure.category == "account_deactivated":
            db.mark_account_deactivated(email, str(exc))
        message = str(exc).strip()
        error = f"[{email}] 无法获取该账户RT" + (f"：{message}" if message else "")
        _account_event(db, email, "session", "refresh_token.acquire_failed", error, "error", account_id=account_id, detail={"error": message, "error_category": failure.category, "retryable": failure.retryable})
        return False, error, {"email": email, "status": "failed", "error": message, "error_category": failure.category, "retryable": failure.retryable, "login_required": True, "login_succeeded": False}


def _acquire_refresh_token_isolated(task_id: str, payload: dict[str, Any], account_id: int, index: int, total: int) -> tuple[int, bool, str, dict[str, Any]]:
    worker_db = SunnyDB(task_id, ensure_schema=False)
    try:
        accounts = worker_db.fetch_accounts([account_id])
        if not accounts:
            return index, False, f"账户 {account_id} 不存在", {"account_id": account_id, "status": "failed"}
        ok, error, item = _acquire_refresh_token_recovery(worker_db, payload, accounts[0], index, total)
        return index, ok, error, item
    finally:
        worker_db.close()


def _acquire_refresh_tokens(db: SunnyDB, payload: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    accounts = db.fetch_accounts(_ids(payload.get("account_ids")) or None)
    if not accounts:
        return 0, [], []
    requested = int(payload.get("concurrency") or os.getenv("SUNNY_RT_ACQUIRE_CONCURRENCY") or 3)
    concurrency = max(1, min(requested, 6, len(accounts)))
    candidates = _proxy_pool_candidates(payload) if payload.get("proxy_enabled") is not False else []
    scheduler = TaskProxyScheduler(candidates, lambda proxy: proxy_target_tls_check(proxy, timeout=10))
    specs: list[dict[str, Any]] = []
    for index, account in enumerate(accounts):
        email = str(account.get("email") or "")
        session = db.fetch_session_by_email(email) or {}
        rt = str(account.get("openai_rt") or session.get("refresh_token") or "").strip()
        lease = scheduler.acquire(email, index)
        account_payload = dict(payload)
        if lease.address or not candidates:
            account_payload["_proxy_lease"] = lease.payload()
        else:
            account_payload["_proxy_unavailable"] = True
        specs.append({"account": account, "index": index + 1, "refresh_token": rt, "lease": lease, "payload": account_payload})

    db.event(
        f"[系统] RT任务采用两阶段并发：先校验已有 RT，再仅对缺失或确认失效账户执行 Codex OAuth；并发数 {concurrency}",
        detail={"scope": "global", "operation": "refresh_token_acquire", "strategy": "refresh_probe_then_oauth", "concurrency": concurrency, "total": len(accounts)},
    )

    def inspect_existing(spec: dict[str, Any]) -> dict[str, Any]:
        if spec["payload"].get("_proxy_unavailable") is True:
            failure = classify_auth_failure("代理池脉冲预检未找到可用 ChatGPT HTTPS 出口")
            return {**spec, "status": "unconfirmed", "error": "代理池脉冲预检未找到可用 ChatGPT HTTPS 出口", "failure": failure}
        rt = str(spec.get("refresh_token") or "")
        if not rt:
            return {**spec, "status": "missing"}
        proxy_url = _proxy_snapshot(spec["payload"], int(spec["index"]) - 1)["register"]
        try:
            token = refresh_openai_access_token(rt, proxy_url)
            probe = _verify_access_token(str(token.get("access_token") or ""), proxy_url)
            return {**spec, "status": "valid", "token": token, "probe": probe}
        except Exception as exc:
            failure = classify_auth_failure(exc)
            return {**spec, "status": "invalid" if failure.category == "token_invalid" else "unconfirmed", "error": str(exc), "failure": failure}

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-rt-probe") as pool:
        inspected = list(pool.map(inspect_existing, specs))

    ok = 0
    completed = 0
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    recovery: list[dict[str, Any]] = []
    for outcome in inspected:
        db.ensure_not_cancelled()
        account = outcome["account"]
        email = str(account.get("email") or "")
        if outcome["status"] == "valid":
            normalized = _persist_maintenance_tokens(db, account, outcome["token"], str(outcome["refresh_token"]))
            item = {"email": email, "status": "valid", "has_refresh_token": True, "has_access_token": True, "acquire_method": "refresh_token_validated", "secondary_probe": "valid", "verified": True, "refresh_token_rotated": normalized["refresh_token"] != outcome["refresh_token"], "login_required": False}
            items.append(item)
            ok += 1
            completed += 1
            scheduler.record(outcome["lease"], success=True)
            _account_event(db, email, "session", "refresh_token.valid", f"[{email}] [Session] 已有 Refresh Token 可用，已刷新并验活最新 AT", account_id=int(account.get("id") or 0))
            db.update_task(progress_current=completed, success_count=ok, error_count=len(errors))
        elif outcome["status"] in {"missing", "invalid"}:
            recovery.append(outcome)
        else:
            error = str(outcome.get("error") or "Refresh Token 校验结果不确定")
            failure = outcome.get("failure") or classify_auth_failure(error)
            errors.append(f"[{email}] {error}")
            items.append({"email": email, "status": "unconfirmed", "error": error, "error_category": failure.category, "login_required": False})
            completed += 1
            scheduler.record(outcome["lease"], success=False, error=error)
            db.update_task(progress_current=completed, success_count=ok, error_count=len(errors))

    if concurrency == 1 and not hasattr(db, "task_id"):
        for outcome in recovery:
            succeeded, error, item = _acquire_refresh_token_recovery(db, outcome["payload"], outcome["account"], int(outcome["index"]), len(accounts))
            ok += int(succeeded)
            completed += 1
            if error:
                errors.append(error)
            items.append(item)
            scheduler.record(outcome["lease"], success=succeeded, error=error)
            db.update_task(progress_current=completed, success_count=ok, error_count=len(errors))
    elif recovery:
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-rt-acquire")
        try:
            futures = {
                pool.submit(_acquire_refresh_token_isolated, db.task_id, outcome["payload"], int(outcome["account"].get("id") or 0), int(outcome["index"]), len(accounts)): outcome
                for outcome in recovery
            }
            pending = set(futures)
            while pending:
                db.ensure_not_cancelled()
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    outcome = futures[future]
                    try:
                        _index, succeeded, error, item = future.result()
                    except Exception as exc:
                        if _is_cancel_exception(exc):
                            raise
                        email = str(outcome["account"].get("email") or "")
                        succeeded, error = False, f"[{email}] RT 获取并行 Worker 失败：{exc}"
                        item = {"email": email, "status": "failed", "error": str(exc)}
                    ok += int(succeeded)
                    completed += 1
                    if error:
                        errors.append(error)
                    items.append(item)
                    scheduler.record(outcome["lease"], success=succeeded, error=error)
                    db.update_task(progress_current=completed, success_count=ok, error_count=len(errors))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    db.event(
        f"[系统] RT 两阶段任务完成：成功 {ok}，失败 {len(errors)}；所有新 AT 均已二次验活",
        detail={"scope": "global", "operation": "refresh_token_acquire", "success": ok, "failed": len(errors), "proxy_scheduler": scheduler.snapshot()},
    )
    return ok, errors, items


def _sub2_import_rt_eligible(account: dict[str, Any], mailbox: dict[str, Any] | None) -> bool:
    """Only retry login for accounts that have already completed phone binding."""
    statuses = {
        str(account.get("status") or "").strip().lower(),
        str((mailbox or {}).get("status") or "").strip().lower(),
    }
    return bool(statuses & {"已接码", "phone_bound", "已反代", "reverse_proxied", "登录刷新"})


def _sub2_import_one(
    db: SunnyDB,
    payload: dict[str, Any],
    account: dict[str, Any],
    index: int,
    total: int,
) -> tuple[int, list[str], dict[str, Any]]:
    """Import one account, recovering a missing RT through the existing login flow."""
    email = str(account.get("email") or "").strip()
    account_id = int(account.get("id") or 0)
    stage = IMPORT_REVERSE_PROXY
    _emit_registration_progress(db, email, stage, "initializing")
    mailbox = db.fetch_mailbox_by_email(email)
    if not mailbox:
        raise RuntimeError("找不到该账户对应的邮箱凭证")
    session = db.fetch_session_by_email(email) or {}
    access_token = str(account.get("access_token") or session.get("access_token") or "").strip()
    refresh_token = str(account.get("openai_rt") or session.get("refresh_token") or "").strip()
    if not refresh_token:
        if not _sub2_import_rt_eligible(account, mailbox):
            reason = "账户尚未完成接码，缺少 Refresh Token，已跳过反代导入"
            db.event(f"[{email}] [反代] {reason}", "warning", detail={"email": email, "scope": "selected", "skipped": True})
            _emit_registration_progress(db, email, stage, "stage_incomplete", state="completed", error=reason)
            return 0, [], {"email": email, "status": "skipped", "reason": reason}
        db.event(
            f"[{email}] [反代] 检测到缺少 Refresh Token，账户状态允许登录恢复，开始获取 RT",
            detail={"email": email, "scope": "selected", "account_id": account_id, "refresh_token_recovery": True},
        )
        acquire_payload = dict(payload)
        acquire_payload.update(
            {
                "account_ids": [account_id],
                "mailbox_ids": [int(mailbox.get("id") or 0)],
                "execution_mode": "protocol",
                "protocol_challenge_strategy": "sentinel_protocol",
                "registration_stage": CODEX_PHONE_BIND,
            }
        )
        succeeded, result = _run_one(db, "sunny_acquire_rt", acquire_payload, mailbox, index, total)
        result_map = result if isinstance(result, dict) else {}
        if not succeeded or not result_map.get("has_refresh_token"):
            detail = str(result_map.get("stage_error") or result_map.get("phone_skipped_reason") or result)
            raise RuntimeError(detail if detail and detail != "{}" else "登录成功但未获取到 Refresh Token")
        account = (db.fetch_accounts([account_id]) or [account])[0]
        session = db.fetch_session_by_email(email) or session
        access_token = str(account.get("access_token") or session.get("access_token") or result_map.get("access_token") or "").strip()
        refresh_token = str(account.get("openai_rt") or session.get("refresh_token") or result_map.get("refresh_token") or "").strip()
    if not access_token and refresh_token:
        _emit_registration_progress(db, email, stage, "auth_completed")
        token = refresh_openai_access_token(refresh_token, _proxy_snapshot(payload, max(0, index - 1))["register"])
        access_token = str(token.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Refresh Token 已获取，但未返回有效 Access Token")
        session = {
            "access_token": access_token,
            "refresh_token": str(token.get("refresh_token") or refresh_token),
            "id_token": token.get("id_token", ""),
            "expires_at": token.get("expires_at"),
            "session_json": token,
        }
        refresh_token = str(session["refresh_token"] or refresh_token)
        account_id = db.upsert_account(email, access_token=access_token, openai_rt=refresh_token, last_error="")
        db.upsert_session(email, account_id, session)
    if not access_token or not refresh_token:
        raise RuntimeError("当前账号缺少 Access Token 或 Refresh Token，无法导入 sub2api")
    session = dict(session)
    session.update({"access_token": access_token, "refresh_token": refresh_token})
    _emit_registration_progress(db, email, stage, "reverse_importing")
    # Sub2API itself can use its configured upstream proxy; importing an
    # already-authenticated account must not require a registration proxy.
    _import_sub2api(db, email, account_id, session, proxy_url="")
    db.upsert_account(email, status="reverse_proxied", access_token=access_token, openai_rt=refresh_token, last_error="")
    db.mark_mailbox_by_email(email, "已反代", openai_rt=refresh_token)
    _emit_registration_progress(db, email, stage, "reverse_imported", state="completed")
    return 1, [], {"email": email, "status": "success", "has_access_token": True, "has_refresh_token": True}


def _sub2_import_one_isolated(
    task_id: str,
    payload: dict[str, Any],
    account_id: int,
    index: int,
    total: int,
) -> tuple[int, int, list[str], dict[str, Any]]:
    worker_db = SunnyDB(task_id, ensure_schema=False)
    try:
        accounts = worker_db.fetch_accounts([account_id])
        if not accounts:
            return index, 0, [f"账户 {account_id} 不存在"], {"email": str(account_id), "status": "failed"}
        try:
            ok, errors, item = _sub2_import_one(worker_db, payload, accounts[0], index, total)
            return index, ok, errors, item
        except Exception as exc:
            if _is_cancel_exception(exc):
                raise
            email = str(accounts[0].get("email") or account_id)
            message = f"[{email}] {exc}"
            worker_db.event(f"[{email}] [反代] 任务失败：{exc}", "error", detail={"email": email, "scope": "selected"})
            _emit_registration_progress(worker_db, email, IMPORT_REVERSE_PROXY, "failed", state="abnormal", error=str(exc))
            return index, 0, [message], {"email": email, "status": "failed", "error": str(exc)}
    finally:
        worker_db.close()


def _sub2_import(db: SunnyDB, payload: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    accounts = db.fetch_accounts(_ids(payload.get("account_ids")) or None)
    if not accounts:
        raise RuntimeError("未找到需要导入的账户")
    requested = int(payload.get("concurrency") or 0)
    default_concurrency = max(1, (int(os.cpu_count() or 1) * 3 + 1) // 2)
    concurrency = max(1, min(requested or default_concurrency, 6, len(accounts)))
    db.event(
        f"[系统] 反代导入并发数：{concurrency}（CPU {int(os.cpu_count() or 1)} 核，默认并发 {default_concurrency}）",
        detail={"scope": "global", "concurrency": concurrency, "total": len(accounts), "operation": "sub2_import"},
    )
    success = 0
    completed = 0
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    db.update_task(progress_total=len(accounts), progress_current=0, success_count=0, error_count=0)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-sub2-import") as pool:
        futures = {
            pool.submit(_sub2_import_one_isolated, db.task_id, payload, int(account.get("id") or 0), offset, len(accounts)): str(account.get("email") or "")
            for offset, account in enumerate(accounts, start=1)
        }
        pending = set(futures)
        while pending:
            if db.cancel_requested():
                for future in pending:
                    future.cancel()
                raise SunnyTaskCancelled("Task cancelled by user")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    _index, account_success, account_errors, item = future.result()
                except Exception as exc:
                    if _is_cancel_exception(exc):
                        raise
                    account_success, account_errors, item = 0, [f"[{futures[future]}] 反代并行 Worker 失败：{exc}"], {"email": futures[future], "status": "failed", "error": str(exc)}
                completed += 1
                success += account_success
                errors.extend(account_errors)
                items.append(item)
                db.update_task(progress_current=completed, success_count=success, error_count=len(errors))
    return success, errors, items


def _add_login_secret_account(
    db: SunnyDB,
    payload: dict[str, Any],
    account_row: dict[str, Any],
    index: int,
    total: int,
) -> tuple[int, list[str], dict[str, Any]]:
    db.ensure_not_cancelled()
    email = str(account_row.get("email") or "").strip()
    mailbox = db.fetch_mailbox_by_email(email)
    if not mailbox:
        error = f"[{email}] 找不到对应的邮箱凭证"
        db.event(error, "error", detail={"email": email, "scope": "selected"})
        return 0, [error], {"email": email, "status": "failed", "login_secret_complete": False, "error": error}
    if str(mailbox.get("status") or "").strip().lower() in {"已封禁", "banned", "account_deactivated"}:
        db.event(
            f"[{email}] [登录密钥] 账户已封禁，跳过密码与 2FA 设置并终止该账户任务",
            "warning",
            detail={"email": email, "scope": "selected", "account_deactivated": True, "skipped": True},
        )
        return 0, [], {"email": email, "status": "skipped", "login_secret_complete": False, "reason": "account_deactivated"}
    # The mailbox table uses chat_gpt_password; accept the legacy alias as
    # well so accounts imported from older records retain their password.
    chatgpt_password = str(mailbox.get("chat_gpt_password") or mailbox.get("chatgpt_password") or "").strip()
    totp_secret = str(mailbox.get("totp_secret") or "").strip()
    if chatgpt_password and totp_secret:
        db.event(f"[{email}] [登录密钥] 已存在完整 LS，跳过重复设置", detail={"email": email, "scope": "selected"})
        return 0, [], {"email": email, "status": "skipped", "login_secret_complete": True}

    account_payload = dict(payload)
    account_payload.update(
        {
            "account_ids": [int(account_row.get("id") or 0)],
            "mailbox_ids": [int(mailbox.get("id") or 0)],
            "execution_mode": "protocol",
            "protocol_challenge_strategy": "sentinel_protocol",
            "registration_stage": REGISTER_ONLY,
            "setup_login_secret": True,
        }
    )
    db.event(
        f"[{email}] [登录密钥] 使用工作台协议模式 Sentinel 运行时补充密码与 2FA",
        detail={"email": email, "scope": "selected", "execution_mode": "protocol", "protocol_challenge_strategy": "sentinel_protocol"},
    )
    try:
        succeeded, result = _run_one(db, "sunny_login", account_payload, mailbox, index, total)
        result_map = result if isinstance(result, dict) else {}
        complete = bool(succeeded and result_map.get("login_secret_complete"))
        if complete:
            db.event(f"[{email}] [登录密钥] LS 添加完成", detail={"email": email, "scope": "selected"})
            return 1, [], {"email": email, "status": "success", "login_secret_complete": True}
        detail = "；".join(str(value) for value in result_map.get("login_secret_errors") or [] if str(value).strip())
        detail = detail or str(result_map.get("stage_error") or result or "登录密钥未完整设置")
        error = f"[{email}] {detail}"
        status = "partial" if succeeded and result_map else "failed"
        db.event(f"[{email}] [登录密钥] 密码与 2FA 未完整设置: {detail}", "warning", detail={"email": email, "scope": "selected"})
        return 0, [error], {"email": email, "status": status, "login_secret_complete": False, "error": detail}
    except Exception as exc:
        if _is_cancel_exception(exc):
            raise
        if _is_account_deactivated(exc):
            db.mark_account_deactivated(email, str(exc))
        error = f"[{email}] 添加 LS 失败: {exc}"
        db.event(error, "error", detail={"email": email, "scope": "selected"})
        return 0, [error], {"email": email, "status": "failed", "login_secret_complete": False, "error": str(exc)}


def _add_login_secret_isolated(
    task_id: str,
    payload: dict[str, Any],
    account_id: int,
    index: int,
    total: int,
) -> tuple[int, int, list[str], dict[str, Any]]:
    worker_db = SunnyDB(task_id, ensure_schema=False)
    try:
        accounts = worker_db.fetch_accounts([account_id])
        if not accounts:
            error = f"账户 {account_id} 不存在"
            return index, 0, [error], {"email": "", "status": "failed", "login_secret_complete": False, "error": error}
        success, errors, item = _add_login_secret_account(worker_db, payload, accounts[0], index, total)
        return index, success, errors, item
    finally:
        worker_db.close()


def _add_login_secrets(db: SunnyDB, payload: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    account_ids = _ids(payload.get("account_ids"))
    accounts = db.fetch_accounts(account_ids) if account_ids else ([] if payload.get("account_ids_explicit") is True else db.fetch_accounts(None))
    raw_skipped = payload.get("prefiltered_login_secret_items")
    items = [dict(item) for item in raw_skipped if isinstance(item, dict)] if isinstance(raw_skipped, list) else []
    success = 0
    errors: list[str] = []
    prefiltered_count = len(items)
    completed = prefiltered_count
    total = completed + len(accounts)
    db.update_task(progress_total=max(1, total), progress_current=completed, success_count=0, error_count=0)
    if items:
        db.event(
            f"[系统] 已在任务提交阶段过滤 {len(items)} 个具有完整 LS 的账户",
            detail={"scope": "global", "skipped": len(items), "operation": "login_secret"},
        )
    if not accounts:
        return success, errors, items

    cpu_count = max(1, int(os.cpu_count() or 1))
    default_concurrency = max(1, (cpu_count * 3 + 1) // 2)
    try:
        requested_concurrency = int(payload.get("concurrency") or default_concurrency)
    except (TypeError, ValueError):
        requested_concurrency = default_concurrency
    concurrency = max(1, min(requested_concurrency, len(accounts)))
    db.event(
        f"[系统] 添加 LS 并发数：{concurrency}（CPU {cpu_count} 核，默认并发 {default_concurrency}）",
        detail={"scope": "global", "concurrency": concurrency, "cpu_count": cpu_count, "default_concurrency": default_concurrency, "total": total, "operation": "login_secret"},
    )
    if concurrency <= 1:
        for offset, account in enumerate(accounts, start=1):
            index = completed + offset
            account_success, account_errors, item = _add_login_secret_account(db, payload, account, index, total)
            success += account_success
            errors.extend(account_errors)
            items.append(item)
            db.update_task(progress_current=index, success_count=success, error_count=len(errors))
        return success, errors, items

    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-add-ls")
    try:
        futures = {
            pool.submit(
                _add_login_secret_isolated,
                db.task_id,
                payload,
                int(account.get("id") or 0),
                prefiltered_count + offset,
                total,
            ): str(account.get("email") or "")
            for offset, account in enumerate(accounts, start=1)
        }
        pending = set(futures)
        while pending:
            if db.cancel_requested():
                for future in pending:
                    future.cancel()
                raise SunnyTaskCancelled("Task cancelled by user")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    _index, account_success, account_errors, item = future.result()
                except Exception as exc:
                    if _is_cancel_exception(exc):
                        raise
                    email = futures[future]
                    account_success = 0
                    account_errors = [f"[{email}] 添加 LS 并行 Worker 失败: {exc}"]
                    item = {"email": email, "status": "failed", "login_secret_complete": False, "error": str(exc)}
                completed += 1
                success += account_success
                errors.extend(account_errors)
                items.append(item)
                db.update_task(progress_current=completed, success_count=success, error_count=len(errors))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return success, errors, items


def _rebind_one_isolated(
    task_id: str,
    payload: dict[str, Any],
    account_id: int,
    index: int,
    total: int,
) -> tuple[int, dict[str, Any]]:
    worker_db = SunnyDB(task_id, ensure_schema=False)
    try:
        accounts = worker_db.fetch_accounts([account_id])
        if not accounts:
            raise RuntimeError(f"账户 {account_id} 不存在")
        email = str(accounts[0].get("email") or "").strip()
        worker_db.event(
            f"[{email}] 开始邮箱协议换绑",
            detail={"email": email, "module": "auth", "action": "rebind.start", "current": index - 1, "total": total},
        )
        result = _rebind_with_proxy_rotation(worker_db, payload, accounts[0], index - 1)
        return index, result
    finally:
        worker_db.close()


def _rebind_with_proxy_rotation(
    db: SunnyDB,
    payload: dict[str, Any],
    account: dict[str, Any],
    slot: int,
) -> dict[str, Any]:
    email = str(account.get("email") or "").strip()
    candidates = _proxy_pool_candidates(payload) if payload.get("proxy_enabled") is not False else []
    max_attempts = min(3, len(candidates)) if candidates else 1
    excluded: set[str] = set()
    log = lambda message: db.event(
        message,
        detail={"email": email, "module": "auth", "action": "rebind.progress"},
    )
    for attempt in range(max_attempts):
        current_payload = dict(payload)
        if excluded:
            current_payload["_excluded_register_proxies"] = sorted(excluded)
        proxy = _prepare_register_proxy(db, current_payload, email, slot + attempt).get("register", "")
        try:
            return rebind_one(db, account, proxy, log)
        except Exception as exc:
            failure = classify_auth_failure(exc)
            can_rotate = (
                attempt + 1 < max_attempts
                and str(getattr(exc, "rebind_phase", "")) == "login"
                and failure.rotate_proxy
                and bool(proxy)
            )
            if not can_rotate:
                raise
            excluded.add(proxy)
            db.event(
                f"[{email}] [代理] 当前代理在换绑登录阶段发生{failure.category}，"
                f"将排除该代理并切换下一条（{attempt + 2}/{max_attempts}）：{redact_proxy_url(proxy)}；"
                f"原因：{str(exc)[:260]}",
                "warning",
                detail={
                    "email": email,
                    "module": "auth",
                    "action": "rebind.proxy_rotated",
                    "proxy": proxy,
                    "proxy_error_category": failure.category,
                    "proxy_attempt": attempt + 1,
                    "proxy_max_attempts": max_attempts,
                },
            )
    raise RuntimeError("邮箱换绑代理轮换未返回执行结果")


def _rebind_sessions(db: SunnyDB, payload: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    account_ids = _ids(payload.get("account_ids"))
    accounts = db.fetch_accounts(account_ids or None)
    if not accounts:
        return 0, ["未找到需要换绑的账户"], []
    success = 0
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    db.update_task(progress_total=len(accounts))
    cpu_count = max(1, int(os.cpu_count() or 1))
    default_concurrency = max(1, (cpu_count * 3 + 1) // 2)
    try:
        requested_concurrency = int(payload.get("concurrency") or default_concurrency)
    except (TypeError, ValueError):
        requested_concurrency = default_concurrency
    concurrency = max(1, min(requested_concurrency, len(accounts)))
    db.event(
        f"[系统] 邮箱换绑并发数：{concurrency}（CPU {cpu_count} 核，默认并发 {default_concurrency}）",
        detail={"scope": "global", "concurrency": concurrency, "cpu_count": cpu_count, "default_concurrency": default_concurrency, "total": len(accounts), "operation": "rebind"},
    )

    def handle_result(index: int, result: dict[str, Any]) -> None:
        nonlocal success
        email = str(result.get("email") or "").strip()
        items.append(result)
        if result.get("status") == "success":
            success += 1
        elif result.get("status") == "skipped":
            db.event(f"[{email}] 已跳过邮箱换绑：{result.get('reason') or '不满足条件'}", "warning", detail={"email": email, "module": "auth", "action": "rebind.skipped"})

    if concurrency <= 1:
        for index, account in enumerate(accounts, start=1):
            db.ensure_not_cancelled()
            email = str(account.get("email") or "").strip()
            db.event(f"[{email}] 开始邮箱协议换绑", detail={"email": email, "module": "auth", "action": "rebind.start", "current": index - 1, "total": len(accounts)})
            try:
                result = _rebind_with_proxy_rotation(db, payload, account, index - 1)
                handle_result(index, result)
            except Exception as exc:
                if db.cancel_requested():
                    raise SunnyTaskCancelled("Task cancelled by user") from exc
                message = f"[{email}] 邮箱换绑失败：{exc}"
                errors.append(message)
                items.append({"email": email, "status": "failed", "error": str(exc)})
                db.event(message, "error", detail={"email": email, "module": "auth", "action": "rebind.failed"})
            db.update_task(progress_current=index, success_count=success, error_count=len(errors))
        return success, errors, items

    completed = 0
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-rebind")
    try:
        futures = {
            pool.submit(
                _rebind_one_isolated,
                db.task_id,
                payload,
                int(account.get("id") or 0),
                offset,
                len(accounts),
            ): str(account.get("email") or "")
            for offset, account in enumerate(accounts, start=1)
        }
        pending = set(futures)
        while pending:
            if db.cancel_requested():
                for future in pending:
                    future.cancel()
                raise SunnyTaskCancelled("Task cancelled by user")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                email = futures[future]
                try:
                    index, result = future.result()
                    handle_result(index, result)
                except Exception as exc:
                    if _is_cancel_exception(exc):
                        raise
                    message = f"[{email}] 邮箱换绑并行 Worker 失败：{exc}"
                    errors.append(message)
                    items.append({"email": email, "status": "failed", "error": str(exc)})
                    db.event(message, "error", detail={"email": email, "module": "auth", "action": "rebind.failed"})
                completed += 1
                db.update_task(progress_current=completed, success_count=success, error_count=len(errors))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return success, errors, items


def _token_maintenance_result(success: int, errors: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "selected": len(items),
        "success": success,
        "failed": len(errors),
        "valid": sum(1 for item in items if item.get("status") == "valid"),
        "verified": sum(1 for item in items if item.get("verified") is True),
        "already_valid": sum(1 for item in items if item.get("refresh_method") == "existing_access_token"),
        "refreshed": sum(1 for item in items if item.get("refresh_method") in {"refresh_token", "login"} or item.get("acquire_method") in {"refresh_token_validated", "codex_oauth"}),
        "login_required": sum(1 for item in items if item.get("login_required") is True),
        "login_succeeded": sum(1 for item in items if item.get("login_succeeded") is True),
        "login_failed": sum(1 for item in items if item.get("login_required") is True and item.get("login_succeeded") is False),
        "banned": sum(1 for item in items if item.get("error_category") == "account_deactivated"),
        "unconfirmed": sum(1 for item in items if item.get("status") in {"blocked", "probe_failed", "unconfirmed"}),
        "errors": errors,
        "items": items,
    }


def run_sunny_task(task_id: str) -> None:
    db = SunnyDB(task_id)
    try:
        task = db.task()
        task_type = task.get("type") or "sunny_register"
        payload = json.loads(task.get("payload_json") or "{}")
        if db.cancel_requested():
            db.mark_cancelled("用户已中断注册任务")
            return
        db.update_task(status="running", started_at=now_sql())
        db.ensure_not_cancelled()
        db.event(f"========= SunnyRegister 注册任务开始 {now_sql()} =========", level="separator", detail={"scope": "global", "separator": True})
        db.event("SunnyRegister Worker accepted register task", typ="state")
        if task_type == "sunny_refresh_session":
            ok, errors, items = _refresh_sessions(db, payload)
            db.ensure_not_cancelled()
            status = "succeeded" if ok else "failed"
            result = _token_maintenance_result(ok, errors, items)
            db.update_task(status=status, success_count=ok, error_count=len(errors), result_json=json.dumps(result, ensure_ascii=False), error="; ".join(errors[:3]) if not ok else "", finished_at=now_sql())
            db.event(f"AT 续期任务总结：有效 {result['valid']}，登录恢复 {result['login_succeeded']}，未确认 {result['unconfirmed']}，失败 {result['failed']}", "info" if status == "succeeded" else "error", detail={"scope": "global", **result})
            return
        if task_type == "sunny_acquire_rt":
            ok, errors, items = _acquire_refresh_tokens(db, payload)
            db.ensure_not_cancelled()
            status = "succeeded" if ok else "failed"
            result = _token_maintenance_result(ok, errors, items)
            db.update_task(status=status, success_count=ok, error_count=len(errors), result_json=json.dumps(result, ensure_ascii=False), error="; ".join(errors[:3]) if not ok else "", finished_at=now_sql())
            db.event(f"RT 获取任务总结：有效 {result['valid']}，OAuth 登录 {result['login_succeeded']}，未确认 {result['unconfirmed']}，失败 {result['failed']}", "info" if status == "succeeded" else "error", detail={"scope": "global", **result})
            return
        if task_type == "sunny_sub2_import":
            ok, errors, items = _sub2_import(db, payload)
            db.ensure_not_cancelled()
            skipped = len([item for item in items if item.get("status") == "skipped"])
            failed = len(errors)
            status = "succeeded" if not errors else "failed"
            result = {"selected": len(items), "uploaded": ok, "confirmed": ok, "success": ok, "failed": failed, "skipped": skipped, "errors": errors, "items": items}
            db.update_task(status=status, success_count=ok, error_count=failed, result_json=json.dumps(result, ensure_ascii=False), error="; ".join(errors[:3]) if errors else "", finished_at=now_sql())
            db.event(f"反代导入任务总结：选中 {len(items)}，成功 {ok}，失败 {failed}，跳过 {skipped}", "info" if status == "succeeded" else "error", detail={"scope": "global", **result})
            return
        if task_type == "sunny_add_ls":
            ok, errors, items = _add_login_secrets(db, payload)
            db.ensure_not_cancelled()
            skipped = len([item for item in items if item.get("status") == "skipped"])
            partial = len([item for item in items if item.get("status") == "partial"])
            status = "succeeded" if not errors and (ok > 0 or skipped > 0) else "failed"
            result = {"success": ok, "failed": len(errors), "skipped": skipped, "partial": partial, "errors": errors, "items": items}
            db.update_task(status=status, success_count=ok, error_count=len(errors), result_json=json.dumps(result, ensure_ascii=False), error="; ".join(errors[:3]), finished_at=now_sql())
            db.event(f"添加 LS 任务总结：成功 {ok}，跳过 {skipped}，部分完成 {partial}，失败 {len(errors)}", "info" if status == "succeeded" else "error", detail={"scope": "global", **result})
            return
        if task_type == "sunny_rebind":
            ok, errors, items = _rebind_sessions(db, payload)
            db.ensure_not_cancelled()
            skipped = len([item for item in items if item.get("status") == "skipped"])
            status = "succeeded" if not errors else "failed"
            result = {"success": ok, "failed": len(errors), "skipped": skipped, "errors": errors, "items": items}
            db.update_task(status=status, success_count=ok, error_count=len(errors), result_json=json.dumps(result, ensure_ascii=False), error="; ".join(errors[:3]) if errors else "", finished_at=now_sql())
            db.event(f"邮箱换绑任务总结：成功 {ok}，跳过 {skipped}，失败 {len(errors)}", "info" if not errors else "error", detail={"scope": "global", **result})
            return

        is_remail_task = str(payload.get("identity") or "").strip().lower() == "remail"
        requested_total = max(1, int(payload.get("count") or 1))
        existing_remail_ids = _ids(payload.get("mailbox_ids")) if is_remail_task else []
        mailboxes = db.fetch_mailboxes(existing_remail_ids) if existing_remail_ids else ([] if is_remail_task else _choose_mailboxes(db, payload))
        if not is_remail_task and not mailboxes:
            raise RuntimeError("邮箱配置不可用：请先导入并启用 Outlook 邮箱池")
        total = requested_total if is_remail_task else len(mailboxes)
        stage = _stage(payload)
        provider_stop_reason = str(payload.get("provider_stop_reason") or "").strip()
        db.update_task(progress_total=total)
        db.event(f"[系统] 本次任务阶段：{_stage_label(stage)}，账号数量：{total}", detail={"scope": "global", "stage": stage, "total": total})
        if provider_stop_reason:
            db.event(
                f"[Remail] {provider_stop_reason}",
                "warning",
                detail={"scope": "global", "provider": "remail", "provider_stop_reason": provider_stop_reason},
            )
        _log_proxy_startup(db, payload)
        db.ensure_not_cancelled()
        if payload.get("proxy_enabled") is not False and not _proxy_snapshot(payload).get("register"):
            raise RuntimeError("代理开关已开启，但没有可用于注册机的启用代理；请在代理配置中新增并启用代理，或关闭代理开关后再开始任务")
        requested_concurrency = int(payload.get("concurrency") or 1)
        concurrency = max(1, min(requested_concurrency, total))
        db.event(
            f"[系统] 注册任务并发数：{concurrency}，每个邮箱使用独立 Worker/浏览器上下文/邮箱验证码读取器",
            detail={"scope": "global", "concurrency": concurrency, "total": total},
        )
        success = 0
        completed = 0
        errors: list[str] = []
        items: list[dict[str, Any]] = []
        protocol_batch_policy = _ProtocolBatchPolicy()

        def record_result(ok: bool, result: dict[str, Any] | str) -> None:
            nonlocal success, completed
            completed += 1
            if ok:
                success += 1
                assert isinstance(result, dict)
                items.append(result)
            else:
                errors.append(str(result))
            db.update_task(progress_current=completed, success_count=success, error_count=len(errors))

        provisioner = RemailMailboxProvisioner(db) if is_remail_task and len(mailboxes) < total and not provider_stop_reason else None

        def purchase(sequence: int) -> dict[str, Any] | None:
            nonlocal provider_stop_reason
            if provisioner is None:
                return None
            try:
                return provisioner.purchase(sequence)
            except RemailOrderError as exc:
                if exc.insufficient_balance:
                    provider_stop_reason = f"Remail 余额不足：已下单 {sequence - 1}/{total} 个邮箱；当前正在注册的邮箱将继续处理，后续下单已停止"
                else:
                    provider_stop_reason = f"Remail 第 {sequence} 个邮箱下单失败，后续下单已停止：{exc}"
                db.event(f"[Remail] {provider_stop_reason}", "error", detail={"scope": "global", "provider": "remail", "sequence": sequence})
                return None

        if concurrency <= 1:
            source = range(1, total + 1) if is_remail_task else enumerate(mailboxes, start=1)
            for entry in source:
                db.ensure_not_cancelled()
                if is_remail_task:
                    idx = int(entry)
                    if idx <= len(mailboxes):
                        mailbox = mailboxes[idx - 1]
                    else:
                        mailbox = purchase(idx)
                        if mailbox is None:
                            break
                else:
                    idx, mailbox = entry
                ok, result = _run_one(db, task_type, payload, mailbox, idx, total, protocol_batch_policy)
                db.ensure_not_cancelled()
                record_result(ok, result)
        else:
            pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sunny-register")
            try:
                pending = set()
                next_sequence = 1

                def submit_next() -> bool:
                    nonlocal next_sequence
                    if next_sequence > total or (provider_stop_reason and next_sequence > len(mailboxes)):
                        return False
                    if is_remail_task and next_sequence <= len(mailboxes):
                        mailbox = mailboxes[next_sequence - 1]
                    elif is_remail_task:
                        mailbox = purchase(next_sequence)
                        if mailbox is None:
                            return False
                    else:
                        mailbox = mailboxes[next_sequence - 1]
                    pending.add(pool.submit(_run_one_isolated, db.task_id, task_type, payload, mailbox, next_sequence, total, protocol_batch_policy))
                    next_sequence += 1
                    return True

                while len(pending) < concurrency and submit_next():
                    pass
                while pending:
                    if db.cancel_requested():
                        for future in pending:
                            future.cancel()
                        db.mark_cancelled("用户已中断注册任务")
                        return
                    done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in done:
                        try:
                            _idx, ok, result = future.result()
                        except Exception as exc:
                            if _is_cancel_exception(exc):
                                db.mark_cancelled("用户已中断注册任务")
                                return
                            ok, result = False, f"parallel worker failed: {exc}"
                        db.ensure_not_cancelled()
                        record_result(ok, result)
                        submit_next()
            finally:
                # Do not let browser/OTP threads outlive the task. Running flows
                # observe the cancelled task state through should_cancel and then
                # close their own browser, mailbox reader and DB connection.
                pool.shutdown(wait=True, cancel_futures=True)
        db.ensure_not_cancelled()
        registered = len([x for x in items if x.get("auth_action") == "register"])
        logged_in = len([x for x in items if x.get("auth_action") != "register"])
        skipped_phone = len([x for x in items if x.get("phone_skipped_reason")])
        imported = len([x for x in items if x.get("sub2api")])
        partial = len([x for x in items if x.get("stage_complete") is False])
        status = "failed" if provider_stop_reason or not success else "succeeded"
        summary = {"success": success, "failed": len(errors), "partial": partial, "registered": registered, "logged_in": logged_in, "skipped_phone": skipped_phone, "imported": imported, "stage": stage, "errors": errors, "items": items, "provider_stop_reason": provider_stop_reason}
        task_error = provider_stop_reason or ("; ".join(errors[:3]) if not success else "")
        db.update_task(status=status, error=task_error, result_json=json.dumps(summary, ensure_ascii=False), finished_at=now_sql())
        summary_message = f"注册任务总结：成功 {success}，失败 {len(errors)}，阶段未完成 {partial}，新注册 {registered}，登录更新 {logged_in}，跳过接码 {skipped_phone}，导入反代 {imported}"
        if provider_stop_reason:
            summary_message += f"；任务因供应商余额不足停止：{provider_stop_reason}"
        db.event(summary_message, "error" if status == "failed" else "info", detail={"scope": "global", **summary})
    except Exception as exc:
        if _is_cancel_exception(exc):
            db.mark_cancelled("用户已中断注册任务")
            return
        db.update_task(status="failed", error=f"SunnyRegister Worker failed: {exc}", result_json=json.dumps({"traceback": traceback.format_exc()[-4000:]}, ensure_ascii=False), finished_at=now_sql())
        db.event(f"SunnyRegister Worker failed: {exc}", "error", detail={"traceback": traceback.format_exc()[-4000:]})
    finally:
        db.close()
