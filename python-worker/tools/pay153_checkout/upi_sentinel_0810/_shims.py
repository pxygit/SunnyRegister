"""内联垫片：为搬入的 sentinel 子系统提供最小依赖。

原 sms_tool 从 auth_headers / phone_proxy 导入这几项；这里给出等价的最小实现，
让 sentinel 包自包含（不拖入整个 sms_tool 工具链）。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

_IMPERSONATE = "chrome136"


def auth_impersonate() -> str:
    return _IMPERSONATE


def auth_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )


def sentinel_fingerprint() -> dict:
    """返回一份可用的浏览器指纹（与 sms_tool sentinel_fingerprint 形状一致）。"""
    return {
        "impersonate": _IMPERSONATE,
        "user_agent": auth_user_agent(),
        "screen": "1920x1080",
        "lang": "en-US",
        "lang_full": "en-US,en;q=0.9",
        "timezone": "Asia/Kolkata",
        "timezone_name": "Asia/Kolkata",
        "timezone_offset_minutes": 330,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "device_pixel_ratio": 1.0,
        "navigator_platform": "Win32",
        "navigator_vendor": "Google Inc.",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "session_id": str(uuid.uuid4()),
    }


def normalize_proxy_url(proxy: str, default_scheme: str = "http") -> str:
    """把各种代理写法统一成 URL 形式。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    # user:pass@host:port
    if "@" in value:
        cred, _, hostpart = value.rpartition("@")
        return f"{default_scheme}://{cred}@{hostpart}"
    parts = value.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"{default_scheme}://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"{default_scheme}://{value}"
    return value


_TZ_OFFSETS = {
    "Asia/Kolkata": 5.5, "Asia/Shanghai": 8, "Asia/Tokyo": 9,
    "UTC": 0, "America/New_York": -5, "Europe/London": 0,
}


def now_in_timezone(tz_name: str = "UTC") -> datetime:
    """返回指定时区的当前时间（简化实现；未知时区回退 UTC）。"""
    hours = _TZ_OFFSETS.get(str(tz_name or "UTC"), 0)
    tz = timezone(timedelta(hours=hours))
    return datetime.now(tz)
