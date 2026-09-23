from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .wechat_favorite_runtime import PRODUCTION_CONFIRMATION


AUTHORIZATION_KIND = "MIKIHOUSE_QUOTE_ASSISTANT_ONE_TIME_PRODUCTION_AUTHORIZATION"
AUTHORIZATION_SCOPE = "ONE_DAILY_QUOTE_TWO_WECHAT_FAVORITES"
OPERATION_CREATE_DAILY = "CREATE_DAILY_TWO_FAVORITES"
OPERATION_REBUILD_MISSING_DAILY = "REBUILD_MISSING_DAILY_TWO_FAVORITES"
OPERATION_RECOVER_FROZEN_PDF = "RECOVER_FROZEN_PDF_AND_CREATE_PENDING_TEXT"
ALLOWED_OPERATIONS = (
    OPERATION_CREATE_DAILY,
    OPERATION_REBUILD_MISSING_DAILY,
    OPERATION_RECOVER_FROZEN_PDF,
)
APP_BUNDLE_IDENTIFIER = "cn.luyao.mikihouse.quoteassistant"
APP_ENV_MARKER = "MIKIHOUSE_QUOTE_ASSISTANT_APP"
DEFAULT_TTL_SECONDS = 15 * 60


class QuoteAssistantAuthorizationError(RuntimeError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _repository_head(repository_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if len(value) != 40:
        raise QuoteAssistantAuthorizationError("无法读取当前仓库版本，单次授权已停止。")
    return value


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise QuoteAssistantAuthorizationError(f"单次授权缺少有效的 {field}。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QuoteAssistantAuthorizationError(f"单次授权的 {field} 格式无效。") from exc
    if parsed.tzinfo is None:
        raise QuoteAssistantAuthorizationError(f"单次授权的 {field} 必须包含时区。")
    return parsed.astimezone(timezone.utc)


def _require_tracked_gate_off(repository_root: Path) -> None:
    path = repository_root / "config" / "wechat_favorite_runtime.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuoteAssistantAuthorizationError("无法读取微信收藏安全配置，单次授权已停止。") from exc
    if config.get("production_save_enabled") is not False:
        raise QuoteAssistantAuthorizationError("仓库默认生产开关必须保持关闭，才能使用 App 单次授权。")


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".permit-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def issue_one_time_authorization(
    repository_root: Path,
    *,
    confirmation: str,
    output_dir: Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    operation: str = OPERATION_CREATE_DAILY,
) -> dict[str, Any]:
    root = repository_root.resolve()
    if confirmation != PRODUCTION_CONFIRMATION:
        raise QuoteAssistantAuthorizationError("确认内容不匹配，未生成单次生产授权。")
    if os.environ.get(APP_ENV_MARKER) != APP_BUNDLE_IDENTIFIER:
        raise QuoteAssistantAuthorizationError("单次生产授权只能由 MIKI HOUSE 报价助手.app 发起。")
    if ttl_seconds <= 0 or ttl_seconds > DEFAULT_TTL_SECONDS:
        raise QuoteAssistantAuthorizationError("单次授权有效期无效。")
    if operation not in ALLOWED_OPERATIONS:
        raise QuoteAssistantAuthorizationError("单次授权操作类型无效。")
    _require_tracked_gate_off(root)
    issued_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    nonce = str(uuid.uuid4())
    directory = (
        output_dir.resolve()
        if output_dir is not None
        else root / ".secrets" / "mikihouse_quote_assistant" / "authorizations"
    )
    path = directory / f"permit-{nonce}.json"
    payload = {
        "schema_version": 1,
        "kind": AUTHORIZATION_KIND,
        "scope": AUTHORIZATION_SCOPE,
        "operation": operation,
        "status": "ISSUED",
        "nonce": nonce,
        "repository_root": str(root),
        "repository_head": _repository_head(root),
        "app_bundle_identifier": APP_BUNDLE_IDENTIFIER,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(seconds=ttl_seconds)).isoformat(),
        "confirmation_sha256": _sha256_text(confirmation),
        "tracked_production_save_enabled": False,
        "reusable": False,
    }
    _atomic_private_json(path, payload)
    return {
        "status": "ISSUED",
        "authorization_file": str(path),
        "nonce": nonce,
        "expires_at": payload["expires_at"],
        "repository_head": payload["repository_head"],
        "operation": operation,
    }


def validate_and_consume_one_time_authorization(
    path: Path,
    repository_root: Path,
    *,
    confirmation: str,
    now: datetime | None = None,
    expected_operation: str = OPERATION_CREATE_DAILY,
) -> dict[str, Any]:
    root = repository_root.resolve()
    resolved = path.resolve()
    allowed = (root / ".secrets" / "mikihouse_quote_assistant" / "authorizations").resolve()
    if allowed not in resolved.parents:
        raise QuoteAssistantAuthorizationError("单次授权文件不在仓库私有授权目录中。")
    try:
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except FileNotFoundError as exc:
        raise QuoteAssistantAuthorizationError("单次授权文件不存在或已被移除。") from exc
    if mode & 0o077:
        raise QuoteAssistantAuthorizationError("单次授权文件权限不安全，已拒绝执行。")
    parent_mode = stat.S_IMODE(resolved.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise QuoteAssistantAuthorizationError("单次授权目录权限不安全，已拒绝执行。")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuoteAssistantAuthorizationError("无法读取单次授权文件。") from exc
    expected = {
        "schema_version": 1,
        "kind": AUTHORIZATION_KIND,
        "scope": AUTHORIZATION_SCOPE,
        "operation": expected_operation,
        "status": "ISSUED",
        "app_bundle_identifier": APP_BUNDLE_IDENTIFIER,
        "repository_root": str(root),
        "confirmation_sha256": _sha256_text(confirmation),
        "tracked_production_save_enabled": False,
        "reusable": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise QuoteAssistantAuthorizationError(f"单次授权字段 {key} 不匹配或已被使用。")
    if confirmation != PRODUCTION_CONFIRMATION:
        raise QuoteAssistantAuthorizationError("确认内容不匹配，已拒绝本次生产执行。")
    _require_tracked_gate_off(root)
    if payload.get("repository_head") != _repository_head(root):
        raise QuoteAssistantAuthorizationError("仓库版本已变化，请在 App 中重新确认本次执行。")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or resolved.name != f"permit-{nonce}.json":
        raise QuoteAssistantAuthorizationError("单次授权标识无效。")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued_at = _parse_time(payload.get("issued_at"), "issued_at")
    expires_at = _parse_time(payload.get("expires_at"), "expires_at")
    if issued_at > current + timedelta(seconds=30):
        raise QuoteAssistantAuthorizationError("单次授权的签发时间异常。")
    if expires_at <= current:
        raise QuoteAssistantAuthorizationError("单次授权已过期，请在 App 中重新确认。")
    if expires_at - issued_at > timedelta(seconds=DEFAULT_TTL_SECONDS):
        raise QuoteAssistantAuthorizationError("单次授权有效期超过安全上限。")

    # Atomically claim the permit before the caller can start any network or
    # GUI work. A concurrent/replayed consumer can no longer open the issued
    # path and therefore fails closed without reaching the production runner.
    consumed_path = resolved.with_name(f"permit-{nonce}.consumed.json")
    try:
        resolved.rename(consumed_path)
    except FileNotFoundError as exc:
        raise QuoteAssistantAuthorizationError("单次授权已被使用或移除。") from exc
    except FileExistsError as exc:
        raise QuoteAssistantAuthorizationError("单次授权已被使用。") from exc

    consumed = {
        **payload,
        "status": "CONSUMED",
        "consumed_at": current.isoformat(),
        "consumer_pid": os.getpid(),
    }
    _atomic_private_json(consumed_path, consumed)
    return {
        "status": "CONSUMED",
        "authorization_mode": "APP_ONE_TIME",
        "scope": AUTHORIZATION_SCOPE,
        "operation": payload["operation"],
        "nonce_sha256": _sha256_text(nonce),
        "repository_head": payload["repository_head"],
        "issued_at": payload["issued_at"],
        "expires_at": payload["expires_at"],
        "consumed_at": consumed["consumed_at"],
        "reusable": False,
    }
