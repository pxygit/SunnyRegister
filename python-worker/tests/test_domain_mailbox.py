import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from sunny_core import mailbox as mailbox_module
from sunny_core import domain_mail_cleanup as cleanup_module
from sunny_core import rebind as rebind_module
from sunny_core.db import SunnyDB
from sunny_core.mailbox import DomainMailReader, account_from_row
from sunny_core.protocol_auth import ProtocolChallengeRequired


def _credential():
    return json.dumps({"base_url": "https://mail.example", "auth_token": "token-1"})


def test_rebound_mailbox_credentials_are_used_for_bulk_mailbox_selection():
    row = {
        "email": "original@icloud.com",
        "access_key": "https://legacy.example/messages/original",
        "mailbox_type": "apple",
        "mailbox_channel": "url_api",
        "rebind_email": "replacement@example.com",
        "rebind_mailbox_api": "https://sunny.example/api/sunny/domain-mail/pickup?email=replacement%40example.com&token=dmsk_one",
    }

    effective = SunnyDB._apply_rebind_mailbox_credentials(row)

    assert effective["email"] == "replacement@example.com"
    assert effective["access_key"] == effective["rebind_mailbox_api"]
    assert effective["raw"] == f"replacement@example.com----{effective['access_key']}"
    assert effective["mailbox_type"] == "domain"
    assert effective["mailbox_channel"] == "domain_api"
    account = account_from_row(effective)
    assert account.email == "replacement@example.com"
    assert account.mailbox_type == "domain"


def test_rebound_email_only_changes_login_identity_and_keeps_original_mailbox_credentials():
    row = {
        "email": "original@example.com",
        "raw": "original@example.com----password----client-id----refresh-token",
        "password": "password",
        "client_id": "client-id",
        "refresh_token": "refresh-token",
        "mailbox_type": "microsoft",
        "mailbox_channel": "outlook",
        "rebind_email": "replacement@example.com",
    }
    effective = SunnyDB._apply_rebind_mailbox_credentials(row)
    assert effective["email"] == "replacement@example.com"
    assert effective.get("access_key", "") == ""
    assert effective["mailbox_type"] == "microsoft"
    account = account_from_row(effective)
    assert account.email == "replacement@example.com"
    assert account.client_id == "client-id"


def test_account_from_row_supports_domain_credentials():
    account = account_from_row({
        "email": "user@example.com",
        "mailbox_type": "domain",
        "mailbox_channel": "domain_api",
        "access_key": _credential(),
    })
    assert account.mailbox_type == "domain"
    assert account.mailbox_channel == "domain_api"
    assert json.loads(account.access_key)["auth_token"] == "token-1"


def test_domain_reader_uses_latest_message_and_extracts_code(monkeypatch):
    reader = DomainMailReader(
        account_from_row({"email": "user@example.com", "mailbox_type": "domain", "access_key": _credential()}),
        None,
    )
    monkeypatch.setattr(reader, "_request", lambda: {"items": [
        {"id": 1, "receivedAt": "2020-01-01T00:00:00Z", "verificationCode": "111111"},
        {"id": 2, "receivedAt": "2099-01-01T00:00:00Z", "bodyPreview": "ChatGPT code 978744"},
    ]})
    current = reader._latest()
    assert current["code"] == "978744"
    assert current["id"] == 2


def test_domain_reader_prefers_body_code_and_parses_cloudmail_utc(monkeypatch):
    reader = DomainMailReader(
        account_from_row({"email": "user@example.com", "mailbox_type": "domain", "access_key": _credential()}),
        None,
    )
    monkeypatch.setattr(reader, "_request", lambda: {"items": [{
        "id": 3,
        "receivedAt": "2026-08-24 07:34:15",
        "bodyPreview": "<style>.code{content:202123}</style><p>ChatGPT code 876769</p>",
        "verificationCode": "202123",
    }]})
    current = reader._latest()
    assert current["code"] == "876769"
    assert "<" not in current["body"]
    assert current["timestamp"] == datetime(2026, 8, 24, 7, 34, 15, tzinfo=timezone.utc).timestamp()


def test_domain_reader_filters_old_message(monkeypatch):
    reader = DomainMailReader(
        account_from_row({"email": "user@example.com", "mailbox_type": "domain", "access_key": _credential()}),
        None,
    )
    monkeypatch.setattr(reader, "_request", lambda: {"items": [
        {"id": 1, "receivedAt": "2020-01-01T00:00:00Z", "verificationCode": "111111"},
    ]})
    try:
        reader.wait_for_code(2000000000, timeout=0.05)
    except TimeoutError:
        pass
    else:
        raise AssertionError("old domain mailbox message must not satisfy a newer baseline")


def test_domain_reader_accepts_unix_millisecond_timestamp(monkeypatch):
    reader = DomainMailReader(
        account_from_row({"email": "user@example.com", "mailbox_type": "domain", "access_key": _credential()}),
        None,
    )
    monkeypatch.setattr(reader, "_request", lambda: {"items": [{
        "id": "m1",
        "timestamp": 4102444800000,
        "bodyPreview": "ChatGPT code 978744",
    }]})
    current = reader._latest()
    assert current["code"] == "978744"
    assert current["timestamp"] == 4102444800


def test_domain_reader_uses_individual_pickup_url(monkeypatch):
    pickup_url = "https://sunny.example/api/sunny/domain-mail/pickup?email=user%40example.com&token=dmsk_one"
    logs = []
    reader = DomainMailReader(
        account_from_row({"email": "user@example.com", "mailbox_type": "domain", "access_key": pickup_url}),
        logs.append,
    )

    class Response:
        status_code = 200
        ok = True

        @staticmethod
        def json():
            return {"items": [{"id": 3, "receivedAt": "2099-01-01T00:00:00Z", "verificationCode": "978744"}]}

        @staticmethod
        def close():
            return None

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(mailbox_module.requests, "get", fake_get)
    assert reader._latest()["code"] == "978744"
    assert calls[0][0] == pickup_url
    assert "Authorization" not in calls[0][1]["headers"]
    assert any("HTTP 200" in message and "识别到 1 封验证码邮件" in message for message in logs)


def test_rebind_domain_mailbox_creates_individual_pickup_credential(monkeypatch):
    class DB:
        @staticmethod
        def get_config(_key):
            return {
                "enabled_for_rebinding": True,
                "base_url": "https://cloudmail.example",
                "auth_token": "global-manager-token",
                "site_password": "site-password",
                "pickup_base_url": "https://sunny.example",
                "domain": "example.com",
                "random_local_length": 10,
            }

    class Response:
        ok = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"code": 0}

    monkeypatch.setattr(rebind_module.requests, "post", lambda *args, **kwargs: Response())
    logs = []
    email, credential, token_hash = rebind_module._domain_mailbox(DB(), logs.append)
    parsed = urlparse(credential)
    query = parse_qs(parsed.query)
    pickup_token = query["token"][0]
    assert parsed.netloc == "sunny.example"
    assert query["email"] == [email]
    assert pickup_token.startswith("dmsk_")
    assert token_hash == hashlib.sha256(pickup_token.encode("utf-8")).hexdigest()
    assert "global-manager-token" not in credential
    assert any(f"{email}----{credential}" in message for message in logs)


def test_rebind_resends_twice_after_otp_delivery_timeouts():
    calls = []
    logs = []

    class Reader:
        def wait_for_code(self, timestamp, timeout):
            calls.append((timestamp, timeout))
            if len(calls) < 3:
                raise TimeoutError("mailbox timeout")
            return "123456"

    class Client:
        def __init__(self):
            self.begin_calls = []

        def begin(self, email):
            self.begin_calls.append(email)

    client = Client()
    assert rebind_module._wait_for_rebind_code(Reader(), client, "new@example.com", 123.0, logs.append) == "123456"
    assert calls == [
        (123.0, 20),
        (123.0, 45),
        (123.0, 45),
    ]
    assert client.begin_calls == ["new@example.com", "new@example.com"]
    assert any("进行第 1 次重发" in message for message in logs)
    assert any("进行第 2 次重发" in message for message in logs)


def test_rebind_begin_retries_transient_network_error():
    calls = []
    logs = []

    class Client:
        def begin(self, email):
            calls.append(email)
            if len(calls) == 1:
                raise rebind_module.RebindError("换绑接口网络请求失败：curl timeout")
            return {"success": True}

    assert rebind_module._begin_with_retry(Client(), "new@example.com", logs.append) == {"success": True}
    assert calls == ["new@example.com", "new@example.com"]
    assert any("瞬时网络错误" in message for message in logs)


def test_rebind_begin_403_is_classified_as_rejected_mailbox():
    class Response:
        status_code = 403
        text = '{"error":"email is not eligible"}'
        url = "https://chatgpt.com/backend-api/accounts/change_email/begin"
        headers = {}

        @staticmethod
        def json():
            return {"error": "email is not eligible"}

    class Session:
        class Cookies:
            jar = []

        cookies = Cookies()

        @staticmethod
        def request(*_args, **_kwargs):
            return Response()

    flow = type("Flow", (), {"session": Session(), "device_id": "device", "_last_access_token": "access"})()
    client = rebind_module.ChangeEmailClient(flow, "account", lambda _message: None)
    with pytest.raises(rebind_module.RebindMailboxRejected):
        client.begin("candidate@example.com")


def test_failed_domain_mailbox_retention_defaults_to_enabled():
    assert cleanup_module.retain_failed_mailbox({}) is True
    assert cleanup_module.retain_failed_mailbox({"retain_failed_mailboxes": False}) is False
    assert cleanup_module.retain_failed_mailbox({"retain_failed_mailboxes": "off"}) is False


def test_cloudmail_failed_mailbox_delete_accepts_public_delete_extension(monkeypatch):
    calls = []

    class Response:
        ok = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"code": 200, "message": "success"}

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr(cleanup_module.requests, "request", request)
    cleanup_module.delete_cloudmail_user({"base_url": "https://cloudmail.example", "auth_token": "token", "site_password": "password"}, "failed@example.com")
    assert calls[0][0] == "DELETE"
    assert calls[0][1].endswith("/api/public/deleteUser")
    assert calls[0][2]["params"] == {"email": "failed@example.com"}
    assert calls[0][2]["headers"]["x-custom-auth"] == "password"


def test_failed_mailbox_cleanup_removes_local_row_when_cloudmail_delete_fails(monkeypatch):
    class DB:
        deleted = False

        def delete_failed_domain_mailbox(self, email, pickup_token_hash):
            self.deleted = True
            return True

    monkeypatch.setattr(cleanup_module, "delete_cloudmail_user", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 404")))
    db = DB()
    try:
        cleanup_module.cleanup_failed_mailbox(db, {"retain_failed_mailboxes": False}, "failed@example.com", "hash", lambda _: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("CloudMail deletion failure must be surfaced")
    assert db.deleted is True


def test_rebind_failure_retention_policy_controls_persistence(monkeypatch):
    class DB:
        def __init__(self, retain):
            self.retain = retain
            self.persisted = False

        def get_config(self, key):
            assert key == "domain_mailbox"
            return {"retain_failed_mailboxes": self.retain}

        def persist_rebind_failure(self, *args):
            self.persisted = True

    cleanup_calls = []
    monkeypatch.setattr(rebind_module, "cleanup_failed_mailbox", lambda *args: cleanup_calls.append(args) or True)

    retained = DB(True)
    rebind_module._handle_failed_domain_mailbox(retained, "old@example.com", "new@example.com", "pickup", "hash", RuntimeError("failed"), lambda _: None)
    assert retained.persisted is True
    assert cleanup_calls == []

    discarded = DB(False)
    rebind_module._handle_failed_domain_mailbox(discarded, "old@example.com", "new@example.com", "pickup", "hash", RuntimeError("failed"), lambda _: None)
    assert discarded.persisted is False
    assert len(cleanup_calls) == 1


def test_rebind_client_observation_header_matches_web_format():
    class Session:
        cookies = type("Cookies", (), {"jar": []})()

    flow = type("Flow", (), {"session": Session(), "device_id": "device-id", "_last_access_token": "access-token"})()
    client = rebind_module.ChangeEmailClient(flow, "account-id")
    header = client._headers(rebind_module.BEGIN_PATH, json_body=True)["x-oai-is-client-observation"]
    assert re.fullmatch(r"v1\.r\.p\.[A-Za-z0-9_-]{16}", header)


def test_rebind_login_falls_back_to_headless_browser_after_sentinel_challenge(monkeypatch):
    account = account_from_row({
        "email": "original@example.com",
        "raw": "original@example.com----mail-password----client-id----refresh-token",
        "chatgpt_password": "chatgpt-password",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    challenge = ProtocolChallengeRequired("Sentinel 协议运行时初始化失败: TypedArray Xray")
    challenge.traffic = {"requests": 4, "total_bytes": 1024}
    browser_result = {
        "access_token": "browser-access-token",
        "session_json": {"accessToken": "browser-access-token", "account": {"id": "browser-account-id"}},
        "storage_state_json": {
            "cookies": [
                {"name": "oai-did", "value": "browser-device-id", "domain": ".chatgpt.com", "path": "/", "secure": True},
                {"name": "__Secure-next-auth.session-token", "value": "browser-session", "domain": "chatgpt.com", "path": "/", "secure": True},
            ],
            "origins": [],
        },
    }
    logs = []
    monkeypatch.setattr(rebind_module.ProtocolRegistrationFlow, "run", lambda _flow: (_ for _ in ()).throw(challenge))
    calls = []

    def browser_login(*args, **kwargs):
        calls.append((args, kwargs))
        return browser_result

    monkeypatch.setattr(rebind_module, "login_or_register", browser_login)

    flow, result = rebind_module._login_flow(account, "http://proxy.example:8080", logs.append, keep_session=True)

    assert result["access_token"] == "browser-access-token"
    assert result["execution_mode"] == "protocol_headless_fallback"
    assert result["protocol_traffic"] == challenge.traffic
    assert flow.device_id == "browser-device-id"
    assert flow._last_access_token == "browser-access-token"
    assert result["account_id"] == "browser-account-id"
    assert "__Secure-next-auth.session-token=browser-session" in rebind_module._cookie_header(flow.session)
    assert calls[0][1]["existing_account"] is True
    assert calls[0][1]["require_refresh_token"] is False
    assert calls[0][1]["execution_mode"] == "protocol_headless_fallback"
    assert any("自动切换 Camoufox" in message for message in logs)
    assert any("继续执行换绑接口" in message for message in logs)


def test_rebind_login_falls_back_to_browser_mailbox_after_browser_ls_failure(monkeypatch):
    account = account_from_row({
        "email": "original@example.com",
        "raw": "original@example.com----mail-password----client-id----refresh-token",
        "chatgpt_password": "chatgpt-password",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    challenge = ProtocolChallengeRequired("Sentinel challenge")
    created = []

    class Session:
        def __init__(self):
            self.closed = False
            self.cookies = type("Cookies", (), {"jar": [], "clear": lambda self: None, "set": lambda self, *args, **kwargs: None})()

        def close(self):
            self.closed = True

    class FakeFlow:
        def __init__(self, flow_account, proxy_url, log, **kwargs):
            self.account = flow_account
            self.proxy_url = proxy_url
            self.session = Session()
            self.device_id = "protocol-device"
            self._last_access_token = ""
            self.kwargs = kwargs
            created.append(self)

        def _new_session(self):
            return Session()

        def run(self):
            raise challenge

    browser_result = {
        "access_token": "mailbox-access-token",
        "account_id": "mailbox-account-id",
        "session_json": {"account": {"id": "mailbox-account-id"}},
        "storage_state_json": {
            "cookies": [
                {"name": "oai-did", "value": "mailbox-device-id", "domain": ".chatgpt.com", "path": "/", "secure": True},
                {"name": "session", "value": "mailbox-session", "domain": "chatgpt.com", "path": "/", "secure": True},
            ],
            "origins": [],
        },
    }
    calls = []

    def browser_login(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise RuntimeError("OpenAI 未提供邮箱验证码切换入口，且当前 LS 登录未完成")
        return browser_result

    logs = []
    monkeypatch.setattr(rebind_module, "ProtocolRegistrationFlow", FakeFlow)
    monkeypatch.setattr(rebind_module, "login_or_register", browser_login)

    flow, result = rebind_module._login_flow(account, "http://proxy.example:8080", logs.append, keep_session=True)

    assert len(created) == 2
    assert calls[1][0][0].has_login_secret is False
    assert calls[1][1]["existing_account"] is True
    assert calls[1][1]["require_refresh_token"] is False
    assert calls[1][1]["execution_mode"] == "protocol_headless_fallback"
    assert flow is created[1]
    assert result["access_token"] == "mailbox-access-token"
    assert result["execution_mode"] == "protocol_headless_fallback"
    assert result["protocol_fallback"] == "mailbox_browser"
    assert any("Camoufox 邮箱验证码登录" in message for message in logs)


def test_rebind_login_does_not_mailbox_retry_when_account_is_deactivated():
    error = rebind_module.LoginSecretAuthenticationError("account_deactivated: account is disabled")
    assert rebind_module._should_use_mailbox_browser_fallback(error) is False
