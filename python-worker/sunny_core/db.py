from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def database_url() -> str:
    file_name = os.getenv("DATABASE_URL_FILE", "").strip()
    if file_name:
        try:
            value = open(file_name, encoding="utf-8").read().strip()
            if value:
                return value
        except OSError:
            pass
    raw = os.getenv("DATABASE_URL") or os.getenv("ACCOUNT_MANAGER_DATABASE_URL") or os.getenv("ACCOUNT_MANAGER_DB") or ""
    return raw.strip()


def is_postgres_url(value: str | None = None) -> bool:
    raw = (value if value is not None else database_url()).lower()
    return raw.startswith("postgres://") or raw.startswith("postgresql://")


def db_path() -> str:
    raw = database_url()
    if raw.startswith("sqlite:///"):
        return raw[10:]
    if raw.startswith("sqlite://"):
        return raw[9:]
    return raw


def database_identity() -> str:
    raw = database_url()
    if is_postgres_url(raw):
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        if parts.username:
            host = f"{parts.username}@{host}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
    return str(os.path.abspath(db_path())) if raw else ""


class _PostgresConnection:
    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires psycopg[binary]; reinstall python-worker requirements") from exc
        self._conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10, autocommit=True)
        self._transaction = None

    @staticmethod
    def _sql(statement: str) -> str:
        sql = statement.replace("?", "%s")
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        sql = sql.replace("enabled=1", "enabled=true").replace("enabled = 1", "enabled = true")
        sql = sql.replace("enabled=0", "enabled=false").replace("enabled = 0", "enabled = false")
        sql = sql.replace("last_check_ok=1", "last_check_ok=true").replace("last_check_ok = 1", "last_check_ok = true")
        sql = sql.replace("last_check_ok=0", "last_check_ok=false").replace("last_check_ok = 0", "last_check_ok = false")
        sql = sql.replace(
            "cooldown_until is null or cooldown_until='' or datetime(cooldown_until) <= datetime('now')",
            "cooldown_until is null or cooldown_until <= current_timestamp",
        )
        sql = sql.replace("cooldown_until='' or datetime(cooldown_until) <= datetime('now')", "cooldown_until <= current_timestamp")
        return sql

    def execute(self, statement: str, params: Any = None):
        sql = self._sql(statement)
        if sql.strip().upper() == "BEGIN":
            self._conn.autocommit = False
            return self._conn.cursor()
        return self._conn.execute(sql, params or ())

    def commit(self) -> None:
        self._conn.commit()
        self._conn.autocommit = True

    def rollback(self) -> None:
        self._conn.rollback()
        self._conn.autocommit = True

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        self._transaction = self._conn.transaction()
        self._transaction.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self._transaction.__exit__(exc_type, exc_value, traceback)
        finally:
            self._transaction = None


def app_timezone() -> ZoneInfo:
    tz_name = os.getenv("SUNNY_TIMEZONE") or os.getenv("TZ") or "Asia/Shanghai"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def sql_datetime(value: datetime | int | float | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(app_timezone())
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        parsed = datetime.fromtimestamp(int(value), timezone.utc)
    elif isinstance(value, str):
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=app_timezone())
    return parsed.astimezone(app_timezone()).isoformat(sep=" ", timespec="seconds")


def now_sql() -> str:
    return sql_datetime()


class SunnyTaskCancelled(RuntimeError):
    """Raised when the Go backend marks a SunnyRegister task as cancelled."""


_SENSITIVE_EVENT_KEYS = {
    "access_token", "refresh_token", "id_token", "openai_rt", "session_json",
    "password", "secret", "api_key", "admin_token", "authorization", "otp", "code",
}

_EVENT_BRACKET_EMAIL_RE = re.compile(r"^\s*\[([^\]\s]+@[^\]\s]+)\]")
_EVENT_INLINE_EMAIL_RE = re.compile(r"\b[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}\b", re.IGNORECASE)
_EVENT_MODULE_RE = re.compile(r"^\s*(?:\[[^\]\s]+@[^\]\s]+\]\s*)?\[([^\]]+)\]")
_EVENT_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/\-]{12,}")
_EVENT_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_EVENT_OTP_RE = re.compile(r"(?i)(OTP|verification code|received code|验证码)(\s*[:=]?\s*)\d{4,8}")
_EVENT_URL_CREDENTIAL_RE = re.compile(r"(?i)\b(https?|socks5h?)://[^/@\s]+@")


def _normalize_event_module(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {
        "认证": "auth", "登录": "auth", "注册": "auth", "oauth": "auth", "auth": "auth",
        "邮箱": "mailbox", "邮件": "mailbox", "mail": "mailbox", "email": "mailbox", "mailbox": "mailbox",
        "接码": "sms", "phone": "sms", "mobile": "sms", "sms": "sms",
        "session": "session", "token": "session", "at": "session", "rt": "session",
        "代理": "proxy", "proxy": "proxy", "反代": "sub2api", "sub2api": "sub2api",
        "试用": "trial", "trial": "trial", "checkout": "checkout", "提链": "checkout",
        "订阅": "subscription", "subscription": "subscription", "测活": "health", "health": "health",
        "系统": "system", "system": "system", "": "system",
    }
    return aliases.get(normalized, normalized)


def _sanitize_event_message(value: Any) -> str:
    message = str(value or "")
    message = _EVENT_BEARER_RE.sub(r"\1[REDACTED]", message)
    message = _EVENT_JWT_RE.sub("[REDACTED_JWT]", message)
    message = _EVENT_OTP_RE.sub(r"\1\2[REDACTED]", message)
    return _EVENT_URL_CREDENTIAL_RE.sub(r"\1://[REDACTED]@", message)


def _event_metadata(message: str, typ: str, detail: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(context or {})
    email = str(metadata.get("email") or detail.get("email") or "").strip()
    if not email:
        match = _EVENT_BRACKET_EMAIL_RE.search(message) or _EVENT_INLINE_EMAIL_RE.search(message)
        email = str(match.group(1) if match and match.lastindex else match.group(0) if match else "").strip()
    module = str(metadata.get("module") or detail.get("module") or "").strip()
    if not module:
        match = _EVENT_MODULE_RE.search(message)
        module = str(match.group(1) if match else "")
    module = _normalize_event_module(module)
    if module == "system":
        lowered = message.lower()
        for candidate, words in (
            ("sub2api", ("sub2api", "反代")), ("trial", ("试用", "trial")),
            ("checkout", ("checkout", "支付方式", "提链")), ("health", ("测活", "封禁")),
            ("mailbox", ("邮箱", "邮件", "mail", "otp")), ("sms", ("接码", "手机号", "phone", "sms")),
            ("session", ("session", "access token", "refresh token")), ("proxy", ("代理", "proxy")),
            ("auth", ("登录", "注册", "认证", "oauth", "login", "register")),
        ):
            if any(word in lowered for word in words):
                module = candidate
                break
    scope = str(metadata.get("scope") or detail.get("scope") or "").strip().lower()
    if email:
        scope = "account"
    elif scope == "selected":
        scope = "account"
    else:
        scope = scope or "global"
    subject_type = str(metadata.get("subject_type") or ("account" if email else "system"))
    return {
        "scope": scope,
        "subject_type": subject_type,
        "subject_key": email.lower(),
        "email": email,
        "account_id": int(metadata.get("account_id") or detail.get("account_id") or 0),
        "mailbox_id": int(metadata.get("mailbox_id") or detail.get("mailbox_id") or 0),
        "module": module,
        "action": str(metadata.get("action") or detail.get("action") or f"{module}.event"),
        "operation_id": str(metadata.get("operation_id") or detail.get("operation_id") or ""),
    }


def _sanitize_event_detail(value: Any, key: str = "") -> Any:
    normalized = key.lower().strip()
    if normalized in _SENSITIVE_EVENT_KEYS or normalized.endswith(("_password", "_secret", "_token", "_api_key")):
        return "[REDACTED]" if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(k): _sanitize_event_detail(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_event_detail(item, key) for item in value]
    return value


class SunnyDB:
    def __init__(self, task_id: str, *, ensure_schema: bool = True):
        self.task_id = task_id
        target = db_path()
        self.postgres = is_postgres_url(target)
        if self.postgres:
            self.conn = _PostgresConnection(target)
        else:
            self.conn = sqlite3.connect(target, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("pragma busy_timeout=30000")
            self.conn.execute("pragma foreign_keys=on")
        if ensure_schema:
            self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        """Keep the Python worker compatible with databases created by older builds."""
        if self.postgres:
            required = ("tasks", "task_events", "sunny_accounts", "sunny_mailboxes", "sunny_sessions")
            for table in required:
                row = self.conn.execute("select to_regclass(?) as name", (f"public.{table}",)).fetchone()
                if not row or not row["name"]:
                    raise RuntimeError(f"PostgreSQL schema is not initialized: missing table {table}; start the Go backend first")
            return
        wanted = {
            "task_events": {
                "scope": "text DEFAULT 'global'",
                "subject_type": "text DEFAULT 'system'",
                "subject_key": "text DEFAULT ''",
                "email": "text DEFAULT ''",
                "account_id": "integer DEFAULT 0",
                "mailbox_id": "integer DEFAULT 0",
                "module": "text DEFAULT ''",
                "action": "text DEFAULT ''",
                "operation_id": "text DEFAULT ''",
            },
            "sunny_accounts": {
                "mailbox_id": "integer DEFAULT 0",
                "group_name": "text DEFAULT ''",
                "status": "text DEFAULT 'pending'",
                "account_type": "text DEFAULT 'free'",
                "openai_rt": "text DEFAULT ''",
                "access_token": "text DEFAULT ''",
                "phone_number": "text DEFAULT ''",
                "sub2api_status": "text DEFAULT ''",
                "sub2api_id": "text DEFAULT ''",
                "last_error": "text DEFAULT ''",
                "metadata_json": "text DEFAULT '{}'",
                "rebind_email": "text DEFAULT ''",
                "rebind_mailbox_api": "text DEFAULT ''",
                "last_health_checked_at": "datetime",
                "status_changed_at": "datetime",
                "created_at": "datetime",
                "updated_at": "datetime",
            },
            "sunny_mailboxes": {
                "mailbox_type": "text DEFAULT 'microsoft'",
                "mailbox_channel": "text DEFAULT 'outlook'",
                "rebind_email": "text DEFAULT ''",
                "rebind_mailbox_api": "text DEFAULT ''",
                "access_key": "text DEFAULT ''",
                "pickup_token_hash": "text DEFAULT ''",
                "chat_gpt_password": "text DEFAULT ''",
                "totp_secret": "text DEFAULT ''",
                "openai_rt": "text DEFAULT ''",
                "registered_at": "datetime",
                "chatgpt_register_traffic_bytes": "integer DEFAULT 0",
                "proxy_traffic_bytes": "integer DEFAULT 0",
                "registration_traffic_finalized_at": "datetime",
                "last_error": "text DEFAULT ''",
                "last_health_checked_at": "datetime",
                "status_changed_at": "datetime",
            },
            "sunny_sessions": {
                "refresh_token": "text DEFAULT ''",
                "id_token": "text DEFAULT ''",
                "session_json": "text DEFAULT '{}'",
                "storage_state_json": "text DEFAULT '{}'",
                "raw_mailbox_line": "text DEFAULT ''",
                "access_token_status": "text DEFAULT 'unknown'",
                "access_token_error": "text DEFAULT ''",
                "access_token_checked_at": "datetime",
                "health_check_status": "text DEFAULT 'unknown'",
                "health_check_error": "text DEFAULT ''",
                "expires_at": "datetime",
                "last_refresh_at": "datetime",
            },
        }
        for table, columns in wanted.items():
            try:
                existing = {str(row["name"]) for row in self.conn.execute(f"pragma table_info({table})").fetchall()}
            except Exception:
                existing = set()
            if not existing:
                continue
            for name, ddl in columns.items():
                if name in existing:
                    continue
                self.conn.execute(f"alter table {table} add column {name} {ddl}")
            refreshed = {str(row["name"]) for row in self.conn.execute(f"pragma table_info({table})").fetchall()}
            if table in {"sunny_accounts", "sunny_mailboxes"} and "open_airt" in refreshed and "openai_rt" in refreshed:
                self.conn.execute(f"update {table} set openai_rt=open_airt where coalesce(openai_rt,'')='' and coalesce(open_airt,'')<>''")

        self._normalize_datetime_storage()
        for table in ("sunny_accounts", "sunny_mailboxes"):
            columns = {str(row["name"]) for row in self.conn.execute(f"pragma table_info({table})").fetchall()}
            if "status_changed_at" not in columns:
                continue
            self.conn.execute(f"update {table} set status_changed_at=updated_at where status_changed_at is null")
            self.conn.execute(f"drop trigger if exists trg_{table}_status_changed")
            self.conn.execute(
                f"""create trigger if not exists trg_{table}_status_changed
                after update of status on {table}
                when old.status is not new.status
                begin
                    update {table}
                    set status_changed_at=case
                        when new.updated_at is not old.updated_at then new.updated_at
                        else strftime('%Y-%m-%d %H:%M:%S','now','+8 hours') || '+08:00'
                    end
                    where id=new.id;
                end"""
            )
            self.conn.execute(f"drop trigger if exists trg_{table}_status_created")
            self.conn.execute(
                f"""create trigger if not exists trg_{table}_status_created
                after insert on {table}
                when new.status_changed_at is null
                begin
                    update {table}
                    set status_changed_at=coalesce(new.created_at,new.updated_at,strftime('%Y-%m-%d %H:%M:%S','now','+8 hours') || '+08:00')
                    where id=new.id;
                end"""
            )
        self.conn.execute("update sunny_mailboxes set status='已接码' where status='PLUS试用中'")
        self.conn.execute("update sunny_accounts set status='phone_bound' where status='PLUS试用中'")
        self.conn.execute(
            """
            create table if not exists sunny_sms_provider_numbers (
                id integer primary key autoincrement,
                provider text not null,
                phone_number text not null,
                country text default '',
                service text default '',
                pool text default '',
                last_order_id text default '',
                token text default '',
                status text default 'available',
                success_count integer default 0,
                max_success integer default 3,
                cooldown_until datetime,
                last_error text default '',
                last_used_at datetime,
                created_at datetime,
                updated_at datetime
            )
            """
        )
        self.conn.execute(
            "create unique index if not exists idx_sunny_sms_provider_number on sunny_sms_provider_numbers(provider, phone_number, country, service)"
        )
        self.conn.execute(
            """
            create table if not exists sunny_mailbox_leases (
                id integer primary key autoincrement,
                mailbox_id integer not null unique,
                owner text not null,
                expires_at datetime not null,
                created_at datetime,
                updated_at datetime
            )
            """
        )
        self.conn.execute("create index if not exists idx_sunny_mailbox_leases_expires on sunny_mailbox_leases(expires_at)")
        self.conn.commit()

    def _normalize_datetime_storage(self) -> None:
        migration_key = "timezone_storage_asia_shanghai_v1"
        try:
            if self.conn.execute("select 1 from sunny_configs where key=?", (migration_key,)).fetchone():
                return
        except sqlite3.Error:
            pass
        tables = self.conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        ).fetchall()
        for table_row in tables:
            table = str(table_row["name"])
            quoted_table = '"' + table.replace('"', '""') + '"'
            for column in self.conn.execute(f"pragma table_info({quoted_table})").fetchall():
                column_type = str(column["type"] or "").lower()
                if "date" not in column_type and "time" not in column_type:
                    continue
                name = str(column["name"])
                quoted_column = '"' + name.replace('"', '""') + '"'
                self.conn.execute(
                    f"""update {quoted_table} set {quoted_column}=trim({quoted_column}) || '+08:00'
                    where typeof({quoted_column})='text' and length(trim({quoted_column}))>=16
                      and substr(trim({quoted_column}),11,1) in (' ','T')
                      and upper(substr(trim({quoted_column}),-1,1))<>'Z'
                      and instr(substr(trim({quoted_column}),11),'+')=0
                      and instr(substr(trim({quoted_column}),11),'-')=0"""
                )
        try:
            timestamp = now_sql()
            self.conn.execute(
                "insert or ignore into sunny_configs(key,value_json,created_at,updated_at) values(?,?,?,?)",
                (migration_key, '{"timezone":"Asia/Shanghai"}', timestamp, timestamp),
            )
        except sqlite3.Error:
            pass

    def task(self) -> dict[str, Any]:
        row = self.conn.execute("select * from tasks where id=?", (self.task_id,)).fetchone()
        if not row:
            raise RuntimeError(f"task not found: {self.task_id}")
        return dict(row)

    def event(
        self,
        message: str,
        level: str = "info",
        typ: str = "log",
        detail: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        created_at = now_sql()
        raw_message = str(message)
        sanitized_message = _sanitize_event_message(raw_message)
        event_detail = _sanitize_event_detail(dict(detail or {}))
        event_detail.setdefault("local_created_at", created_at)
        metadata = _event_metadata(raw_message, typ, event_detail, context)
        if not metadata["operation_id"] and metadata["email"]:
            metadata["operation_id"] = f"{self.task_id}:{metadata['subject_key']}:{metadata['module']}"
        event_detail.update({key: value for key, value in metadata.items() if key in {"scope", "email", "module", "action", "operation_id"} and value not in (None, "")})
        event_detail = _sanitize_event_detail(event_detail)
        values = (
            self.task_id, typ, level, sanitized_message, metadata["scope"], metadata["subject_type"],
            metadata["subject_key"], metadata["email"], metadata["account_id"], metadata["mailbox_id"],
            metadata["module"], metadata["action"], metadata["operation_id"],
            json.dumps(event_detail, ensure_ascii=False), created_at,
        )
        if self.postgres or self._task_event_structured_columns_available():
            self.conn.execute(
                "insert into task_events(task_id,type,level,message,scope,subject_type,subject_key,email,account_id,mailbox_id,module,action,operation_id,detail_json,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        else:
            self.conn.execute(
                "insert into task_events(task_id,type,level,message,detail_json,created_at) values(?,?,?,?,?,?)",
                (self.task_id, typ, level, sanitized_message, json.dumps(event_detail, ensure_ascii=False), created_at),
            )
        self.conn.commit()

    def _task_event_structured_columns_available(self) -> bool:
        cached = getattr(self, "_structured_task_events", None)
        if cached is None:
            columns = {str(row["name"]) for row in self.conn.execute("pragma table_info(task_events)").fetchall()}
            cached = {"email", "module", "action", "scope", "subject_key"}.issubset(columns)
            self._structured_task_events = cached
        return bool(cached)

    def account_event(
        self,
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
        typ: str = "log",
    ) -> None:
        self.event(
            message,
            level,
            typ,
            detail,
            context={
                "email": email, "module": module, "action": action, "scope": "account",
                "subject_type": "account", "account_id": account_id, "mailbox_id": mailbox_id,
                "operation_id": operation_id,
            },
        )

    def update_task(self, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields and str(fields.get("status") or "") not in {"cancelled", "interrupted"}:
            row = self.conn.execute("select status from tasks where id=?", (self.task_id,)).fetchone()
            current = str(row["status"] if row else "")
            if current in {"cancel_requested", "cancelled", "interrupted"}:
                fields["status"] = "cancelled"
                fields.setdefault("error", "用户已中断注册任务")
                fields.setdefault("finished_at", now_sql())
        fields["updated_at"] = now_sql()
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"update tasks set {sets} where id=?", [*fields.values(), self.task_id])
        self.conn.commit()

    def task_status(self) -> str:
        row = self.conn.execute("select status from tasks where id=?", (self.task_id,)).fetchone()
        return str(row["status"] if row else "")

    def cancel_requested(self) -> bool:
        return self.task_status() in {"cancel_requested", "cancelled", "interrupted"}

    def ensure_not_cancelled(self) -> None:
        if self.cancel_requested():
            raise SunnyTaskCancelled("Task cancelled by user")

    def mark_cancelled(self, message: str = "用户已停止注册任务") -> dict[str, Any]:
        current = self.task_status()
        task = self.task()
        task_type = str(task.get("type") or "")
        if task_type in {"sunny_register", "sunny_login"}:
            summary = self.fail_unfinished_mailboxes(message)
        else:
            summary = {
                "completed": int(task.get("success_count") or 0),
                "failed": int(task.get("error_count") or 0),
                "completed_mailbox_ids": [],
                "failed_mailbox_ids": [],
            }
        try:
            result = json.loads(task.get("result_json") or "{}")
            if not isinstance(result, dict):
                result = {}
        except Exception:
            result = {}
        result.update({"cancelled": True, **summary})
        error_count = int(task.get("error_count") or 0)
        if task_type in {"sunny_register", "sunny_login"}:
            error_count = max(error_count, summary["failed"])
        self.update_task(
            status="cancelled",
            error=message,
            progress_current=max(int(task.get("progress_current") or 0), summary["completed"] + summary["failed"]),
            success_count=max(int(task.get("success_count") or 0), summary["completed"]),
            error_count=error_count,
            result_json=json.dumps(result, ensure_ascii=False),
            finished_at=now_sql(),
        )
        if current not in {"cancelled", "interrupted"}:
            if task_type in {"sunny_register", "sunny_login"}:
                event_message = f"{message}；已完成 {summary['completed']} 个，未完成并标记失败 {summary['failed']} 个"
            else:
                event_message = f"{message}；当前操作已停止，尚未执行的账户不再继续处理"
            self.event(event_message, "warning", detail={"scope": "global", "cancelled": True, **summary})
        return summary

    def fail_unfinished_mailboxes(self, reason: str = "任务已由用户停止，当前邮箱未完成本次注册流程") -> dict[str, Any]:
        """Fail selected mailboxes that did not complete successfully in this task."""
        task = self.task()
        try:
            payload = json.loads(task.get("payload_json") or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        mailbox_ids: list[int] = []
        for raw in payload.get("mailbox_ids") or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in mailbox_ids:
                mailbox_ids.append(value)
        if not mailbox_ids:
            account_ids: list[int] = []
            for raw in payload.get("account_ids") or []:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    account_ids.append(value)
            if account_ids:
                marks = ",".join("?" for _ in account_ids)
                rows = self.conn.execute(
                    f"select mailbox_id from sunny_accounts where id in ({marks})",
                    account_ids,
                ).fetchall()
                mailbox_ids = [int(row["mailbox_id"] or 0) for row in rows if int(row["mailbox_id"] or 0) > 0]

        completed_statuses: dict[int, str] = {}
        progress_rank = {"已注册": 1, "已接码": 2, "已反代": 3}

        def remember_completed(mailbox_id: int, status: str) -> None:
            current = completed_statuses.get(mailbox_id, "")
            if progress_rank.get(status, 0) >= progress_rank.get(current, 0):
                completed_statuses[mailbox_id] = status

        if mailbox_ids:
            marks = ",".join("?" for _ in mailbox_ids)
            rows = self.conn.execute(
                f"select mailbox_id,status,sub2api_status,metadata_json from sunny_accounts where mailbox_id in ({marks})",
                mailbox_ids,
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict) or str(metadata.get("task_id") or "") != self.task_id:
                    continue
                completed_status = str(metadata.get("completed_status") or "").strip()
                if not completed_status:
                    account_status = str(row["status"] or "").lower()
                    completed_status = {
                        "registered": "已注册",
                        "phone_bound": "已接码",
                        "reverse_proxied": "已反代",
                    }.get(account_status, "")
                if completed_status in {"已注册", "已接码", "已反代"}:
                    remember_completed(int(row["mailbox_id"] or 0), completed_status)

                if str(row["sub2api_status"] or "").lower() in {"imported", "success", "succeeded"}:
                    remember_completed(int(row["mailbox_id"] or 0), "已反代")

        mailbox_marks = ",".join("?" for _ in mailbox_ids)
        if mailbox_marks:
            mailbox_rows = self.conn.execute(
                f"select id,status from sunny_mailboxes where id in ({mailbox_marks})",
                mailbox_ids,
            ).fetchall()
            for row in mailbox_rows:
                current_status = str(row["status"] or "").strip()
                if current_status in {"已注册", "已接码", "已反代"}:
                    remember_completed(int(row["id"]), current_status)

        for mailbox_id, completed_status in completed_statuses.items():
            self.conn.execute(
                "update sunny_mailboxes set status=?,last_error='',updated_at=? where id=?",
                (completed_status, now_sql(), mailbox_id),
            )
        failed_ids = [mailbox_id for mailbox_id in mailbox_ids if mailbox_id not in completed_statuses]
        if failed_ids:
            marks = ",".join("?" for _ in failed_ids)
            self.conn.execute(
                f"update sunny_mailboxes set status='失败',last_error=?,updated_at=? where id in ({marks})",
                [reason, now_sql(), *failed_ids],
            )
        if completed_statuses or failed_ids:
            self.conn.commit()
        return {
            "completed": len(completed_statuses),
            "failed": len(failed_ids),
            "completed_mailbox_ids": sorted(completed_statuses),
            "completed_mailbox_statuses": {str(key): value for key, value in completed_statuses.items()},
            "failed_mailbox_ids": failed_ids,
        }

    def fetch_mailboxes(self, ids: list[int] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = self.conn.execute(f"select * from sunny_mailboxes where id in ({marks}) order by id asc", ids).fetchall()
        else:
            sql = "select * from sunny_mailboxes where enabled=1 and coalesce(status,'') not in ('disabled','已封禁','banned','换绑中') order by id asc"
            if limit:
                sql += f" limit {int(limit)}"
            rows = self.conn.execute(sql).fetchall()
        items = [self._apply_rebind_mailbox_credentials(dict(r)) for r in rows]
        for item in items:
            self._hydrate_mailbox_auth(item)
        return items

    def create_remail_mailbox(self, email: str, pickup_url: str) -> dict[str, Any]:
        group_name = f"rm-api-{datetime.now(app_timezone()).strftime('%m-%d')}"
        stamp = now_sql()
        self.conn.execute(
            "insert into sunny_mailbox_groups(name,description,created_at,updated_at) values(?,?,?,?) on conflict(name) do nothing",
            (group_name, "", stamp, stamp),
        )
        group = self.conn.execute("select id from sunny_mailbox_groups where name=?", (group_name,)).fetchone()
        if not group:
            raise RuntimeError("Remail 邮箱分组创建失败")
        row = self.conn.execute("select id from sunny_mailboxes where lower(email)=lower(?)", (email,)).fetchone()
        raw = f"{email}----{pickup_url}"
        if row:
            mailbox_id = int(row["id"])
            self.conn.execute(
                "update sunny_mailboxes set group_id=?,mailbox_type='remail',mailbox_channel='remail_api',access_key=?,raw=?,status='未注册',enabled=1,last_error='',updated_at=? where id=?",
                (int(group["id"]), pickup_url, raw, stamp, mailbox_id),
            )
        else:
            values = (int(group["id"]), email, "remail", "remail_api", pickup_url, raw, "free", "unknown", "未注册", True, "{}", stamp, stamp)
            sql = "insert into sunny_mailboxes(group_id,email,mailbox_type,mailbox_channel,access_key,raw,account_type,trial_eligibility,status,enabled,latest_mail_json,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            if self.postgres:
                mailbox_id = int(self.conn.execute(sql + " returning id", values).fetchone()["id"])
            else:
                mailbox_id = int(self.conn.execute(sql, values).lastrowid)
        task = self.task()
        try:
            payload = json.loads(task.get("payload_json") or "{}")
        except Exception:
            payload = {}
        ids = [int(value) for value in payload.get("mailbox_ids") or [] if int(value or 0) > 0]
        if mailbox_id not in ids:
            ids.append(mailbox_id)
            payload["mailbox_ids"] = ids
            self.conn.execute("update tasks set payload_json=?,updated_at=? where id=?", (json.dumps(payload, ensure_ascii=False), stamp, self.task_id))
        self.conn.commit()
        mailbox = self.conn.execute("select * from sunny_mailboxes where id=?", (mailbox_id,)).fetchone()
        item = dict(mailbox)
        self._hydrate_mailbox_auth(item)
        return item

    def _hydrate_mailbox_auth(self, mailbox: dict[str, Any]) -> None:
        """Fill mailbox OpenAI RT from account/session tables when the mailbox row is stale."""
        original_email = str(mailbox.pop("_original_email_for_auth", "") or "").strip()
        if mailbox.get("openai_rt"):
            return
        email = str(mailbox.get("email") or "")
        if not email:
            return
        row = self.conn.execute(
            "select openai_rt from sunny_accounts where (lower(email)=lower(?) or lower(email)=lower(?) or lower(rebind_email)=lower(?)) and coalesce(openai_rt,'')<>'' limit 1",
            (email, original_email or email, email),
        ).fetchone()
        if row and row["openai_rt"]:
            mailbox["openai_rt"] = row["openai_rt"]
            return
        row = self.conn.execute(
            "select refresh_token from sunny_sessions where (lower(email)=lower(?) or lower(email)=lower(?)) and coalesce(refresh_token,'')<>'' limit 1",
            (email, original_email or email),
        ).fetchone()
        if row and row["refresh_token"]:
            mailbox["openai_rt"] = row["refresh_token"]

    @staticmethod
    def _apply_rebind_mailbox_credentials(mailbox: dict[str, Any]) -> dict[str, Any]:
        """Switch later operations to the replacement domain mailbox after rebind."""
        rebind_email = str(mailbox.get("rebind_email") or "").strip()
        rebind_api = str(mailbox.get("rebind_mailbox_api") or "").strip()
        if not rebind_email:
            return mailbox
        mailbox["_original_email_for_auth"] = str(mailbox.get("email") or "").strip()
        mailbox["email"] = rebind_email
        if rebind_api:
            mailbox["access_key"] = rebind_api
            mailbox["raw"] = f"{rebind_email}----{rebind_api}"
            mailbox["mailbox_type"] = "domain"
            mailbox["mailbox_channel"] = "domain_api"
        return mailbox

    def fetch_accounts(self, ids: list[int] | None = None) -> list[dict[str, Any]]:
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = self.conn.execute(f"select * from sunny_accounts where id in ({marks}) order by id asc", ids).fetchall()
        else:
            rows = self.conn.execute("select * from sunny_accounts order by id asc").fetchall()
        return [dict(r) for r in rows]

    def fetch_mailbox_by_email(self, email: str) -> dict[str, Any] | None:
        requested = str(email or "").strip()
        row = self.conn.execute("select * from sunny_mailboxes where lower(email)=lower(?) or lower(rebind_email)=lower(?) limit 1", (requested, requested)).fetchone()
        if not row:
            return None
        item = dict(row)
        self._apply_rebind_mailbox_credentials(item)
        self._hydrate_mailbox_auth(item)
        return item

    def fetch_session_by_email(self, email: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "select * from sunny_sessions where lower(trim(email))=lower(trim(?)) order by updated_at desc,id desc limit 1",
            (email,),
        ).fetchone()
        return dict(row) if row else None

    def fetch_session_by_account_id(self, account_id: int) -> dict[str, Any] | None:
        if int(account_id or 0) <= 0:
            return None
        row = self.conn.execute(
            "select * from sunny_sessions where account_id=? order by updated_at desc,id desc limit 1",
            (int(account_id),),
        ).fetchone()
        return dict(row) if row else None

    def persist_rebind(self, old_email: str, new_email: str, new_mailbox_api: str, pickup_token_hash: str, session: dict[str, Any]) -> None:
        """Persist rebind metadata while keeping the original mailbox identity."""
        old_email = str(old_email or '').strip()
        new_email = str(new_email or '').strip()
        if not old_email or not new_email or '@' not in new_email:
            raise ValueError('换绑邮箱地址无效')
        timestamp = now_sql()
        access_token = str(session.get('access_token') or '')
        refresh_token = str(session.get('refresh_token') or session.get('openai_rt') or '')
        id_token = str(session.get('id_token') or access_token)
        session_json = session.get('session_json', session)
        if not isinstance(session_json, str):
            session_json = json.dumps(session_json, ensure_ascii=False)
        storage_state = session.get('storage_state_json', {})
        if not isinstance(storage_state, str):
            storage_state = json.dumps(storage_state, ensure_ascii=False)
        raw = f"{new_email}----{new_mailbox_api}"
        with self.conn:
            pending = self.conn.execute(
                "select id from sunny_mailboxes where lower(email)=lower(?) and pickup_token_hash=? limit 1",
                (new_email, pickup_token_hash),
            ).fetchone()
            pending_id = int(pending["id"]) if pending else 0
            conflict = self.conn.execute(
                "select email from sunny_mailboxes where (lower(email)=lower(?) or lower(rebind_email)=lower(?)) and lower(email)<>lower(?)" + (" and id<>?" if pending_id else "") + " limit 1",
                (new_email, new_email, old_email, pending_id) if pending_id else (new_email, new_email, old_email),
            ).fetchone()
            if conflict:
                raise ValueError('换绑邮箱已存在于邮箱池中')
            conflict = self.conn.execute(
                "select email from sunny_accounts where lower(email)=lower(?) and lower(email)<>lower(?) limit 1",
                (new_email, old_email),
            ).fetchone()
            if conflict:
                raise ValueError('换绑邮箱已被其他账户使用')
            conflict = self.conn.execute(
                "select email from sunny_sessions where lower(email)=lower(?) and lower(email)<>lower(?) limit 1",
                (new_email, old_email),
            ).fetchone()
            if conflict:
                raise ValueError('换绑邮箱已被其他会话使用')
            mailbox = self.conn.execute("select id from sunny_mailboxes where lower(email)=lower(?) limit 1", (old_email,)).fetchone()
            account = self.conn.execute("select id from sunny_accounts where lower(email)=lower(?) limit 1", (old_email,)).fetchone()
            current_session = self.conn.execute("select id from sunny_sessions where lower(email)=lower(?) limit 1", (old_email,)).fetchone()
            if not mailbox or not account or not current_session:
                raise ValueError('换绑账户关联的邮箱、账户或会话记录不完整')
            self.conn.execute(
                """update sunny_mailboxes set rebind_email=?,rebind_mailbox_api=?,mailbox_type='domain',mailbox_channel='domain_api',access_key=?,pickup_token_hash=?,raw=?,last_error='',updated_at=? where id=?""",
                (new_email, new_mailbox_api, new_mailbox_api, pickup_token_hash, raw, timestamp, mailbox['id']),
            )
            self.conn.execute(
                """update sunny_accounts set access_token=?,openai_rt=?,rebind_email=?,rebind_mailbox_api=?,last_error='',updated_at=? where id=?""",
                (access_token, refresh_token, new_email, new_mailbox_api, timestamp, account['id']),
            )
            self.conn.execute(
                """update sunny_sessions set access_token=?,refresh_token=?,id_token=?,session_json=?,storage_state_json=?,raw_mailbox_line=?,access_token_status=?,access_token_error='',access_token_checked_at=?,last_refresh_at=?,updated_at=? where id=?""",
                (access_token, refresh_token, id_token, session_json, storage_state, raw, 'valid' if access_token else 'invalid', timestamp, timestamp, timestamp, current_session['id']),
            )
            if pending_id:
                self.conn.execute("delete from sunny_mailboxes where id=?", (pending_id,))

    def persist_rebind_pending(self, new_email: str, new_mailbox_api: str, pickup_token_hash: str) -> None:
        """Pre-register a generated pickup URL so the public endpoint can validate it."""
        new_email = str(new_email or '').strip()
        if not new_email or '@' not in new_email:
            raise ValueError('换绑邮箱地址无效')
        timestamp = now_sql()
        raw = f"{new_email}----{new_mailbox_api}"
        with self.conn:
            group_name = f"domain-api-{datetime.now(app_timezone()).strftime('%m-%d')}"
            self.conn.execute(
                "insert into sunny_mailbox_groups(name,description,created_at,updated_at) values(?,?,?,?) on conflict(name) do nothing",
                (group_name, "自建域名邮箱 API 换绑邮箱", timestamp, timestamp),
            )
            group = self.conn.execute("select id from sunny_mailbox_groups where name=?", (group_name,)).fetchone()
            if not group:
                raise RuntimeError('自建域名邮箱分组创建失败')
            existing = self.conn.execute("select id from sunny_mailboxes where lower(email)=lower(?) limit 1", (new_email,)).fetchone()
            if existing:
                current = self.conn.execute("select pickup_token_hash from sunny_mailboxes where id=?", (int(existing['id']),)).fetchone()
                if current and str(current['pickup_token_hash'] or '') not in {'', pickup_token_hash}:
                    raise ValueError('换绑邮箱已存在于邮箱池中')
                self.conn.execute(
                    "update sunny_mailboxes set group_id=?,mailbox_type='domain',mailbox_channel='domain_api',access_key=?,pickup_token_hash=?,raw=?,status='换绑中',enabled=?,last_error='',updated_at=? where id=?",
                    (int(group['id']), new_mailbox_api, pickup_token_hash, raw, True, timestamp, int(existing['id'])),
                )
                return
            values = (int(group['id']), new_email, 'domain', 'domain_api', new_mailbox_api, pickup_token_hash, raw, '换绑中', True, '', '{}', timestamp, timestamp)
            sql = "insert into sunny_mailboxes(group_id,email,mailbox_type,mailbox_channel,access_key,pickup_token_hash,raw,status,enabled,last_error,latest_mail_json,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            self.conn.execute(sql, values)

    def persist_rebind_failure(self, email: str, new_email: str, new_mailbox_api: str, pickup_token_hash: str, error: str) -> None:
        """Keep a generated replacement mailbox visible when the rebind flow fails later."""
        email = str(email or '').strip()
        new_email = str(new_email or '').strip()
        if not new_email or '@' not in new_email:
            return
        timestamp = now_sql()
        raw = f"{new_email}----{new_mailbox_api}"
        with self.conn:
            pending = self.conn.execute(
                "select id from sunny_mailboxes where lower(email)=lower(?) and pickup_token_hash=? limit 1",
                (new_email, pickup_token_hash),
            ).fetchone()
            if pending:
                self.conn.execute(
                    "update sunny_mailboxes set status='失败',enabled=?,last_error=?,updated_at=? where id=?",
                    (True, error, timestamp, int(pending['id'])),
                )
                return
            group_name = f"domain-api-{datetime.now(app_timezone()).strftime('%m-%d')}"
            self.conn.execute(
                "insert into sunny_mailbox_groups(name,description,created_at,updated_at) values(?,?,?,?) on conflict(name) do nothing",
                (group_name, "自建域名邮箱 API 换绑失败邮箱", timestamp, timestamp),
            )
            group = self.conn.execute("select id from sunny_mailbox_groups where name=?", (group_name,)).fetchone()
            if not group:
                return
            exists = self.conn.execute("select id from sunny_mailboxes where lower(email)=lower(?)", (new_email,)).fetchone()
            if exists:
                return
            self.conn.execute(
                """insert into sunny_mailboxes(group_id,email,mailbox_type,mailbox_channel,access_key,pickup_token_hash,raw,status,enabled,last_error,latest_mail_json,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (group['id'], new_email, 'domain', 'domain_api', new_mailbox_api, pickup_token_hash, raw, '失败', True, error, '{}', timestamp, timestamp),
            )

    def delete_failed_domain_mailbox(self, email: str, pickup_token_hash: str = "") -> bool:
        """Delete only a generated, unfinished domain mailbox from the current flow."""
        email = str(email or '').strip()
        if not email or '@' not in email:
            return False
        query = "select id from sunny_mailboxes where lower(email)=lower(?) and mailbox_type='domain' and status in ('失败','换绑中')"
        params: list[Any] = [email]
        if pickup_token_hash:
            query += " and pickup_token_hash=?"
            params.append(pickup_token_hash)
        row = self.conn.execute(query + " limit 1", params).fetchone()
        if not row:
            return False
        self.conn.execute("delete from sunny_mailboxes where id=?", (int(row['id']),))
        self.conn.commit()
        return True

    def reserve_phone(self) -> dict[str, Any] | None:
        phone_cfg = self.get_config("phone")
        if phone_cfg and phone_cfg.get("pool_enabled") is False:
            return None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            lock_clause = " for update skip locked" if self.postgres else ""
            row = self.conn.execute(
                """
                select * from sunny_phones
                where enabled=1 and coalesce(status,'available') not in ('disabled','full','in_use')
                  and coalesce(success_count,0) < coalesce(max_success,3)
                  and (cooldown_until is null or cooldown_until='' or datetime(cooldown_until) <= datetime('now'))
                order by success_count asc, id asc limit 1
                """ + lock_clause
            ).fetchone()
            if not row:
                self.conn.rollback()
                return None
            phone = dict(row)
            self.conn.execute("update sunny_phones set status=?, updated_at=? where id=?", ("in_use", now_sql(), phone["id"]))
            self.conn.commit()
            return phone
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def mark_phone_success(self, phone_id: int, code: str = "") -> None:
        until = sql_datetime(datetime.now(app_timezone()) + timedelta(hours=5))
        self.conn.execute(
            "update sunny_phones set success_count=coalesce(success_count,0)+1, status=case when coalesce(success_count,0)+1>=coalesce(max_success,3) then 'full' else 'cooldown' end, cooldown_until=?, last_code=?, last_used_at=?, updated_at=? where id=?",
            (until, code, now_sql(), now_sql(), phone_id),
        )
        self.conn.commit()

    def mark_phone_error(self, phone_id: int, error: str) -> None:
        self.conn.execute("update sunny_phones set status='available', last_error=?, updated_at=? where id=?", (error, now_sql(), phone_id))
        self.conn.commit()

    def usable_phone_count(self) -> int:
        phone_cfg = self.get_config("phone")
        if phone_cfg and phone_cfg.get("pool_enabled") is False:
            return 0
        row = self.conn.execute(
            """
            select count(*) as n from sunny_phones
            where enabled=1 and coalesce(status,'available') not in ('disabled','full','in_use')
              and coalesce(success_count,0) < coalesce(max_success,3)
              and (cooldown_until is null or cooldown_until='' or datetime(cooldown_until) <= datetime('now'))
            """
        ).fetchone()
        return int(row["n"] if row else 0)

    def smsbower_available(self) -> bool:
        phone_cfg = self.get_config("phone")
        return bool(phone_cfg.get("smsbower_enabled") and str(phone_cfg.get("smsbower_api_key") or "").strip())

    def luban_available(self) -> bool:
        phone_cfg = self.get_config("phone")
        return bool(
            phone_cfg.get("luban_enabled")
            and str(phone_cfg.get("luban_api_key") or "").strip()
            and str(phone_cfg.get("luban_service_id") or "").strip()
        )

    def smspool_available(self) -> bool:
        phone_cfg = self.get_config("phone")
        return bool(phone_cfg.get("smspool_enabled") and str(phone_cfg.get("smspool_api_key") or "").strip())

    def firefox_available(self) -> bool:
        phone_cfg = self.get_config("phone")
        try:
            max_price = float(phone_cfg.get("firefox_max_price") or 0)
        except (TypeError, ValueError):
            max_price = 0
        return bool(
            phone_cfg.get("firefox_enabled")
            and str(phone_cfg.get("firefox_api_token") or phone_cfg.get("firefox_password") or "").strip()
            and str(phone_cfg.get("firefox_default_country") or "").strip()
            and str(phone_cfg.get("firefox_default_service") or "").strip()
            and max_price > 0
        )

    def resolve_sms_provider_option(self, provider: str, kind: str, value: str, parent: str = "") -> dict[str, Any] | None:
        value = str(value or "").strip()
        if not value:
            return None
        params: list[Any] = [provider, kind]
        parent_clause = ""
        if parent:
            parent_clause = " and parent_value=?"
            params.append(parent)
        rows = self.conn.execute(
            f"select * from sunny_sms_provider_options where provider=? and kind=?{parent_clause}",
            params,
        ).fetchall()
        normalized = value.casefold()
        contains: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if str(item.get("value") or "").strip().casefold() == normalized:
                return item
            label = str(item.get("label") or "").strip()
            if label.casefold() == normalized:
                return item
            if normalized in label.casefold():
                contains.append(item)
        return min(contains, key=lambda item: len(str(item.get("label") or ""))) if contains else None

    def sms_provider_option_extra(self, option: dict[str, Any] | None) -> dict[str, Any]:
        if not option:
            return {}
        try:
            value = json.loads(str(option.get("extra_json") or "{}"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def reserve_sms_provider_number(self, provider: str, country: str = "", service: str = "") -> dict[str, Any] | None:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            lock_clause = " for update skip locked" if self.postgres else ""
            row = self.conn.execute(
                """
                select * from sunny_sms_provider_numbers
                where provider=?
                  and (?='' or country=?)
                  and (?='' or service=?)
                  and coalesce(status,'available') not in ('disabled','in_use','full')
                  and coalesce(success_count,0) < coalesce(max_success,3)
                  and (cooldown_until is null or cooldown_until='' or datetime(cooldown_until) <= datetime('now'))
                order by success_count asc, last_used_at asc, id asc
                limit 1
                """ + lock_clause,
                (provider, country, country, service, service),
            ).fetchone()
            if not row:
                self.conn.rollback()
                return None
            item = dict(row)
            self.conn.execute(
                "update sunny_sms_provider_numbers set status='in_use', updated_at=? where id=?",
                (now_sql(), item["id"]),
            )
            self.conn.commit()
            return item
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def record_sms_provider_number(self, provider: str, phone_number: str, country: str = "", service: str = "", pool: str = "", order_id: str = "", token: str = "") -> None:
        if not phone_number:
            return
        row = self.conn.execute(
            "select id from sunny_sms_provider_numbers where provider=? and phone_number=? and country=? and service=?",
            (provider, phone_number, country, service),
        ).fetchone()
        values = {
            "provider": provider,
            "phone_number": phone_number,
            "country": country,
            "service": service,
            "pool": pool,
            "last_order_id": order_id,
            "token": token,
            "status": "in_use",
            "last_error": "",
            "last_used_at": now_sql(),
            "updated_at": now_sql(),
        }
        if row:
            sets = ",".join(f"{k}=?" for k in values)
            self.conn.execute(f"update sunny_sms_provider_numbers set {sets} where id=?", [*values.values(), row["id"]])
        else:
            values["created_at"] = now_sql()
            cols = ",".join(values)
            self.conn.execute(f"insert into sunny_sms_provider_numbers({cols}) values({','.join('?' for _ in values)})", list(values.values()))
        self.conn.commit()

    def mark_sms_provider_number_success(self, provider: str, phone_number: str, code: str = "") -> None:
        if not phone_number:
            return
        until = sql_datetime(datetime.now(app_timezone()) + timedelta(hours=5))
        self.conn.execute(
            """
            update sunny_sms_provider_numbers
            set success_count=coalesce(success_count,0)+1,
                status=case when coalesce(success_count,0)+1>=coalesce(max_success,3) then 'full' else 'cooldown' end,
                cooldown_until=?,
                last_error='',
                last_used_at=?,
                updated_at=?
            where provider=? and phone_number=?
            """,
            (until, now_sql(), now_sql(), provider, phone_number),
        )
        self.conn.commit()

    def mark_sms_provider_number_error(self, provider: str, phone_number: str, error: str) -> None:
        if not phone_number:
            return
        self.conn.execute(
            "update sunny_sms_provider_numbers set status='available', last_error=?, updated_at=? where provider=? and phone_number=?",
            (error, now_sql(), provider, phone_number),
        )
        self.conn.commit()

    def get_config(self, key: str) -> dict[str, Any]:
        row = self.conn.execute("select value_json from sunny_configs where key=?", (key,)).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["value_json"] or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def set_account_sub2api_status(self, email: str, status: str, sub2api_id: str = "", error: str = "") -> None:
        self.conn.execute("update sunny_accounts set sub2api_status=?, sub2api_id=?, last_error=?, updated_at=? where email=?", (status, sub2api_id, error, now_sql(), email))
        self.conn.commit()

    def upsert_account(self, email: str, **fields: Any) -> int:
        email = str(email or "").strip()
        if not email:
            raise ValueError("account email is required")
        row = self.conn.execute(
            "select id,status,email from sunny_accounts where lower(trim(email))=lower(trim(?)) order by updated_at desc,id desc limit 1",
            (email,),
        ).fetchone()
        base = {"updated_at": now_sql(), **fields}
        if row:
            if "status" in base and str(base["status"] or "") != str(row["status"] or ""):
                base["status_changed_at"] = base["updated_at"]
            sets = ",".join(f"{k}=?" for k in base)
            self.conn.execute(f"update sunny_accounts set {sets} where id=?", [*base.values(), row["id"]])
            account_id = int(row["id"])
        else:
            base["email"] = email
            base.setdefault("created_at", now_sql())
            cols = ",".join(base)
            marks = ",".join("?" for _ in base)
            if getattr(self, "postgres", False):
                updates = ",".join(f"{key}=excluded.{key}" for key in base if key not in {"email", "created_at"})
                cur = self.conn.execute(
                    f"insert into sunny_accounts({cols}) values({marks}) "
                    f"on conflict (lower(btrim(email))) where btrim(email)<>'' do update set {updates} returning id",
                    list(base.values()),
                )
                account_id = int(cur.fetchone()["id"])
            else:
                cur = self.conn.execute(f"insert into sunny_accounts({cols}) values({marks})", list(base.values()))
                account_id = int(cur.lastrowid)
        self.conn.commit()
        return account_id

    def upsert_session(self, email: str, account_id: int, session: dict[str, Any], raw_line: str = "") -> None:
        email = str(email or "").strip()
        if not email:
            raise ValueError("session email is required")
        row = None
        if account_id > 0:
            row = self.conn.execute(
                "select * from sunny_sessions where account_id=? order by updated_at desc,id desc limit 1",
                (account_id,),
            ).fetchone()
        if not row:
            row = self.conn.execute(
                "select * from sunny_sessions where lower(trim(email))=lower(trim(?)) order by updated_at desc,id desc limit 1",
                (email,),
            ).fetchone()
        expires_at = session.get("expires_at")
        if not expires_at:
            token = str(session.get("access_token") or "")
            try:
                payload = token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                expires_at = json.loads(base64.urlsafe_b64decode(payload.encode()).decode()).get("exp")
            except Exception:
                expires_at = None
        if isinstance(expires_at, (int, float)) or (isinstance(expires_at, str) and expires_at.isdigit()):
            expires_at = sql_datetime(expires_at)
        elif isinstance(expires_at, (datetime, str)):
            expires_at = sql_datetime(expires_at)
        values = {
            "account_id": account_id,
            "access_token": session.get("access_token", "") or (row["access_token"] if row else ""),
            "refresh_token": session.get("refresh_token", "") or session.get("openai_rt", "") or (row["refresh_token"] if row else ""),
            "id_token": session.get("id_token", "") or (row["id_token"] if row else ""),
            "session_json": json.dumps(session.get("session_json", session), ensure_ascii=False) if not isinstance(session.get("session_json"), str) else session.get("session_json"),
            "storage_state_json": json.dumps(session.get("storage_state_json", {}), ensure_ascii=False) if not isinstance(session.get("storage_state_json"), str) else session.get("storage_state_json"),
            "raw_mailbox_line": raw_line or (row["raw_mailbox_line"] if row else ""),
            "access_token_status": "valid" if session.get("access_token") or (row and row["access_token"]) else "invalid",
            "access_token_error": "",
            "access_token_checked_at": now_sql(),
            "expires_at": expires_at or None,
            "last_refresh_at": now_sql(),
            "updated_at": now_sql(),
        }
        if row:
            sets = ",".join(f"{k}=?" for k in values)
            self.conn.execute(f"update sunny_sessions set {sets} where id=?", [*values.values(), row["id"]])
        else:
            values["email"] = email
            values["created_at"] = now_sql()
            cols = ",".join(values)
            marks = ",".join("?" for _ in values)
            if getattr(self, "postgres", False):
                updates = ",".join(f"{key}=excluded.{key}" for key in values if key not in {"email", "created_at"})
                self.conn.execute(
                    f"insert into sunny_sessions({cols}) values({marks}) "
                    f"on conflict (lower(btrim(email))) where btrim(email)<>'' do update set {updates}",
                    list(values.values()),
                )
            else:
                self.conn.execute(f"insert into sunny_sessions({cols}) values({marks})", list(values.values()))
        self.conn.commit()

    def persist_authenticated_session(self, email: str, mailbox_id: int, session: dict[str, Any], raw_line: str = "") -> int:
        """Immediately synchronize a successful login's AT to account and session rows."""
        session_json = session.get("session_json")
        parsed_session_json = session_json
        if isinstance(session_json, str):
            try:
                parsed_session_json = json.loads(session_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_session_json = {}
        access_token = ""
        if isinstance(parsed_session_json, dict):
            # /api/auth/session is authoritative after login. Do not let a
            # stale token carried by an earlier authentication step win.
            access_token = str(parsed_session_json.get("accessToken") or parsed_session_json.get("access_token") or "").strip()
        if not access_token:
            access_token = str(session.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("successful login did not return an access token")
        session["access_token"] = access_token
        refresh_token = str(session.get("refresh_token") or session.get("openai_rt") or "").strip()
        account_fields: dict[str, Any] = {"access_token": access_token, "last_error": ""}
        if mailbox_id > 0:
            account_fields["mailbox_id"] = mailbox_id
        if refresh_token:
            account_fields["openai_rt"] = refresh_token
        account_id = self.upsert_account(email, **account_fields)
        normalized = dict(session)
        normalized["access_token"] = access_token
        self.upsert_session(email, account_id, normalized, raw_line)
        if mailbox_id > 0:
            mailbox = self.conn.execute(
                "select rebind_email from sunny_mailboxes where id=? limit 1",
                (mailbox_id,),
            ).fetchone()
            rebind_email = str(mailbox["rebind_email"] or "").strip() if mailbox else ""
            if rebind_email and rebind_email.lower() != email.lower():
                duplicates = self.conn.execute(
                    "select id from sunny_accounts where mailbox_id=? and id<>? and lower(trim(email))=lower(trim(?))",
                    (mailbox_id, account_id, rebind_email),
                ).fetchall()
                duplicate_ids = [int(row["id"]) for row in duplicates]
                if duplicate_ids:
                    marks = ",".join("?" for _ in duplicate_ids)
                    self.conn.execute(f"delete from sunny_sessions where account_id in ({marks})", duplicate_ids)
                    self.conn.execute(f"delete from sunny_accounts where id in ({marks})", duplicate_ids)
                    self.conn.commit()
        return account_id

    def mark_access_token_renewal_failed(self, email: str, error: str = "") -> None:
        self.conn.execute(
            "update sunny_sessions set access_token_status='renewal_failed', access_token_checked_at=?, access_token_error=?, updated_at=? where email=?",
            (now_sql(), str(error or "")[:1000], now_sql(), email),
        )
        self.conn.commit()

    def mark_account_deactivated(self, email: str, error: str = "") -> None:
        if not email:
            return
        timestamp = now_sql()
        detail = str(error or "account_deactivated")[:2000]
        with self.conn:
            self.conn.execute(
                """update sunny_mailboxes
                set status='已封禁', last_error=?, last_health_checked_at=?, status_changed_at=?, updated_at=?
                where email=?""",
                (detail, timestamp, timestamp, timestamp, email),
            )
            self.conn.execute(
                """update sunny_accounts
                set status='banned', last_error=?, last_health_checked_at=?, status_changed_at=?, updated_at=?
                where email=?""",
                (detail, timestamp, timestamp, timestamp, email),
            )
            self.conn.execute(
                """update sunny_sessions
                set access_token_status='invalid', access_token_checked_at=?, access_token_error=?, updated_at=?
                where email=?""",
                (timestamp, detail, timestamp, email),
            )

    def mark_mailbox(self, mailbox_id: int, status: str, error: str = "", openai_rt: str = "") -> None:
        if mailbox_id <= 0:
            return
        success_statuses = {"已注册", "已接码", "已反代"}
        sets = ["status=?", "last_error=?", "updated_at=?"]
        values: list[Any] = [status, error, now_sql()]
        if openai_rt:
            sets.append("openai_rt=?")
            values.append(openai_rt)
        if status in success_statuses:
            sets.append("registered_at=coalesce(registered_at, ?)")
            values.append(now_sql())
        values.append(mailbox_id)
        self.conn.execute(f"update sunny_mailboxes set {','.join(sets)} where id=?", values)
        self.conn.commit()

    def mailbox_status(self, mailbox_id: int) -> str:
        if mailbox_id <= 0:
            return ""
        row = self.conn.execute("select status from sunny_mailboxes where id=?", (mailbox_id,)).fetchone()
        return str(row["status"] if row else "")

    def proxy_is_usable(self, proxy_id: int) -> bool:
        if proxy_id <= 0:
            return True
        row = self.conn.execute(
            "select status,enabled,last_check_ok from sunny_proxies where id=?",
            (proxy_id,),
        ).fetchone()
        return bool(
            row
            and str(row["status"] or "").strip().lower() == "enabled"
            and int(row["enabled"] or 0) == 1
            and int(row["last_check_ok"] or 0) == 1
        )

    def acquire_mailbox_lease(self, mailbox_id: int, owner: str, ttl_seconds: int = 600) -> bool:
        if mailbox_id <= 0:
            return True
        owner = str(owner or "").strip()
        if not owner:
            raise ValueError("mailbox lease owner is required")
        now = datetime.now(app_timezone())
        expires_at = sql_datetime(now + timedelta(seconds=max(30, int(ttl_seconds or 600))))
        timestamp = sql_datetime(now)
        with self.conn:
            self.conn.execute("delete from sunny_mailbox_leases where expires_at<=?", (timestamp,))
            self.conn.execute(
                "insert into sunny_mailbox_leases(mailbox_id,owner,expires_at,created_at,updated_at) values(?,?,?,?,?) on conflict(mailbox_id) do nothing",
                (mailbox_id, owner, expires_at, timestamp, timestamp),
            )
            row = self.conn.execute(
                "select owner from sunny_mailbox_leases where mailbox_id=?",
                (mailbox_id,),
            ).fetchone()
            if not row or str(row["owner"] or "") != owner:
                return False
            self.conn.execute(
                "update sunny_mailbox_leases set expires_at=?,updated_at=? where mailbox_id=? and owner=?",
                (expires_at, timestamp, mailbox_id, owner),
            )
        return True

    def release_mailbox_lease(self, mailbox_id: int, owner: str) -> None:
        if mailbox_id <= 0 or not str(owner or "").strip():
            return
        self.conn.execute(
            "delete from sunny_mailbox_leases where mailbox_id=? and owner=?",
            (mailbox_id, str(owner).strip()),
        )
        self.conn.commit()

    def mark_mailbox_credential_invalid(self, mailbox_id: int, error: str) -> None:
        if mailbox_id <= 0:
            return
        self.conn.execute(
            "update sunny_mailboxes set enabled=0,last_error=?,updated_at=? where id=?",
            (str(error or "")[:2000], now_sql(), mailbox_id),
        )
        self.conn.commit()

    def mark_access_token_probe(self, email: str, status: str, error: str = "") -> None:
        if not email:
            return
        self.conn.execute(
            "update sunny_sessions set access_token_status=?,access_token_error=?,access_token_checked_at=?,updated_at=? where lower(email)=lower(?)",
            (str(status or "unknown"), str(error or "")[:1000], now_sql(), now_sql(), email),
        )
        self.conn.commit()

    def discard_unverified_access_token(self, email: str, access_token: str, error: str) -> None:
        email = str(email or "").strip()
        access_token = str(access_token or "").strip()
        if not email or not access_token:
            return
        timestamp = now_sql()
        detail = str(error or "AT 二次验活失败")[:1000]
        with self.conn:
            self.conn.execute(
                "update sunny_accounts set access_token='',last_error=?,updated_at=? where lower(email)=lower(?) and access_token=?",
                (detail, timestamp, email, access_token),
            )
            self.conn.execute(
                "update sunny_sessions set access_token='',access_token_status='invalid',access_token_error=?,access_token_checked_at=?,updated_at=? where lower(email)=lower(?) and access_token=?",
                (detail, timestamp, timestamp, email, access_token),
            )

    def save_chatgpt_password(self, mailbox_id: int, password: str) -> None:
        if mailbox_id <= 0 or not password:
            return
        self.conn.execute(
            "update sunny_mailboxes set chat_gpt_password=?, updated_at=? where id=?",
            (password, now_sql(), mailbox_id),
        )
        self.conn.commit()

    def save_totp_secret(self, mailbox_id: int, secret: str) -> None:
        if mailbox_id <= 0 or not secret:
            return
        self.conn.execute(
            "update sunny_mailboxes set totp_secret=?, updated_at=? where id=?",
            (secret, now_sql(), mailbox_id),
        )
        self.conn.commit()

    def record_proxy_traffic(
        self,
        email: str,
        mailbox_id: int,
        total_bytes: int,
        *,
        registration_attempt: bool = False,
        registration_succeeded: bool = False,
    ) -> None:
        """Persist proxy-pool traffic without mixing in direct auxiliary traffic."""
        total = max(0, int(total_bytes or 0))
        if mailbox_id <= 0 or total <= 0:
            if registration_attempt and registration_succeeded and mailbox_id > 0:
                self.conn.execute(
                    "update sunny_mailboxes set registration_traffic_finalized_at=coalesce(registration_traffic_finalized_at,?), updated_at=? where id=?",
                    (now_sql(), now_sql(), mailbox_id),
                )
                self.conn.commit()
            return
        self.conn.execute(
            "update sunny_mailboxes set proxy_traffic_bytes=coalesce(proxy_traffic_bytes,0)+?, updated_at=? where id=?",
            (total, now_sql(), mailbox_id),
        )
        if registration_attempt:
            self.conn.execute(
                "update sunny_mailboxes set chatgpt_register_traffic_bytes=case when registration_traffic_finalized_at is null then coalesce(chatgpt_register_traffic_bytes,0)+? else chatgpt_register_traffic_bytes end where id=?",
                (total, mailbox_id),
            )
            if registration_succeeded:
                self.conn.execute(
                    "update sunny_mailboxes set registration_traffic_finalized_at=coalesce(registration_traffic_finalized_at,?), updated_at=? where id=?",
                    (now_sql(), now_sql(), mailbox_id),
                )
        self.conn.commit()

    def mark_mailbox_by_email(self, email: str, status: str, error: str = "", openai_rt: str = "") -> None:
        if not email:
            return
        success_statuses = {"已注册", "已接码", "已反代"}
        sets = ["status=?", "last_error=?", "updated_at=?"]
        values: list[Any] = [status, error, now_sql()]
        if openai_rt:
            sets.append("openai_rt=?")
            values.append(openai_rt)
        if status in success_statuses:
            sets.append("registered_at=coalesce(registered_at, ?)")
            values.append(now_sql())
        values.append(email)
        self.conn.execute(f"update sunny_mailboxes set {','.join(sets)} where email=?", values)
        self.conn.commit()
