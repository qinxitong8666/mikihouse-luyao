from __future__ import annotations

import json
import importlib.util
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mikihouse_luyao.quote_assistant_authorization import (
    APP_BUNDLE_IDENTIFIER,
    APP_ENV_MARKER,
    OPERATION_CREATE_DAILY,
    OPERATION_RECOVER_FROZEN_PDF,
    OPERATION_REBUILD_MISSING_DAILY,
    QuoteAssistantAuthorizationError,
    issue_one_time_authorization,
    validate_and_consume_one_time_authorization,
)
from mikihouse_luyao.quote_assistant_runtime import check_quote_assistant_runtime
from mikihouse_luyao.wechat_favorite_runtime import PRODUCTION_CONFIRMATION


ROOT = Path(__file__).resolve().parents[1]


def make_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "config").mkdir()
    (root / "config" / "wechat_favorite_runtime.json").write_text(
        json.dumps({"production_save_enabled": False}) + "\n", encoding="utf-8"
    )
    (root / "marker.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "marker.txt", "config/wechat_favorite_runtime.json"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    return root


def issue(root: Path, monkeypatch: pytest.MonkeyPatch, *, now: datetime | None = None) -> dict:
    monkeypatch.setenv(APP_ENV_MARKER, APP_BUNDLE_IDENTIFIER)
    return issue_one_time_authorization(
        root,
        confirmation=PRODUCTION_CONFIRMATION,
        now=now,
    )


def test_one_time_authorization_is_private_bound_and_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    issued = issue(root, monkeypatch)
    path = Path(issued["authorization_file"])
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    consumed = validate_and_consume_one_time_authorization(
        path,
        root,
        confirmation=PRODUCTION_CONFIRMATION,
    )
    assert consumed["status"] == "CONSUMED"
    assert consumed["authorization_mode"] == "APP_ONE_TIME"
    assert consumed["reusable"] is False
    assert not path.exists()
    consumed_path = path.with_name(path.name.replace(".json", ".consumed.json"))
    payload = json.loads(consumed_path.read_text(encoding="utf-8"))
    assert payload["status"] == "CONSUMED"
    with pytest.raises(QuoteAssistantAuthorizationError, match="不存在或已被移除"):
        validate_and_consume_one_time_authorization(
            path,
            root,
            confirmation=PRODUCTION_CONFIRMATION,
        )


def test_authorization_requires_native_app_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repository(tmp_path)
    monkeypatch.delenv(APP_ENV_MARKER, raising=False)
    with pytest.raises(QuoteAssistantAuthorizationError, match="报价助手"):
        issue_one_time_authorization(root, confirmation=PRODUCTION_CONFIRMATION)


def test_authorization_rejects_wrong_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repository(tmp_path)
    monkeypatch.setenv(APP_ENV_MARKER, APP_BUNDLE_IDENTIFIER)
    with pytest.raises(QuoteAssistantAuthorizationError, match="确认内容"):
        issue_one_time_authorization(root, confirmation="wrong")


def test_rebuild_authorization_is_operation_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    monkeypatch.setenv(APP_ENV_MARKER, APP_BUNDLE_IDENTIFIER)
    issued = issue_one_time_authorization(
        root,
        confirmation=PRODUCTION_CONFIRMATION,
        operation=OPERATION_REBUILD_MISSING_DAILY,
    )
    path = Path(issued["authorization_file"])
    with pytest.raises(QuoteAssistantAuthorizationError, match="operation"):
        validate_and_consume_one_time_authorization(
            path,
            root,
            confirmation=PRODUCTION_CONFIRMATION,
        )
    assert path.is_file()
    consumed = validate_and_consume_one_time_authorization(
        path,
        root,
        confirmation=PRODUCTION_CONFIRMATION,
        expected_operation=OPERATION_REBUILD_MISSING_DAILY,
    )
    assert consumed["operation"] == OPERATION_REBUILD_MISSING_DAILY


def test_frozen_pdf_recovery_authorization_is_operation_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    monkeypatch.setenv(APP_ENV_MARKER, APP_BUNDLE_IDENTIFIER)
    issued = issue_one_time_authorization(
        root,
        confirmation=PRODUCTION_CONFIRMATION,
        operation=OPERATION_RECOVER_FROZEN_PDF,
    )
    consumed = validate_and_consume_one_time_authorization(
        Path(issued["authorization_file"]),
        root,
        confirmation=PRODUCTION_CONFIRMATION,
        expected_operation=OPERATION_RECOVER_FROZEN_PDF,
    )
    assert consumed["operation"] == OPERATION_RECOVER_FROZEN_PDF


def test_authorization_requires_tracked_gate_to_remain_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    monkeypatch.setenv(APP_ENV_MARKER, APP_BUNDLE_IDENTIFIER)
    (root / "config" / "wechat_favorite_runtime.json").write_text(
        json.dumps({"production_save_enabled": True}) + "\n", encoding="utf-8"
    )
    with pytest.raises(QuoteAssistantAuthorizationError, match="必须保持关闭"):
        issue_one_time_authorization(root, confirmation=PRODUCTION_CONFIRMATION)


def test_authorization_expires_and_cannot_survive_head_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    issued_at = datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc)
    expired = issue(root, monkeypatch, now=issued_at)
    with pytest.raises(QuoteAssistantAuthorizationError, match="已过期"):
        validate_and_consume_one_time_authorization(
            Path(expired["authorization_file"]),
            root,
            confirmation=PRODUCTION_CONFIRMATION,
            now=issued_at + timedelta(minutes=16),
        )

    current = issue(root, monkeypatch, now=issued_at)
    (root / "marker.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "marker.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "change"], check=True)
    with pytest.raises(QuoteAssistantAuthorizationError, match="仓库版本已变化"):
        validate_and_consume_one_time_authorization(
            Path(current["authorization_file"]),
            root,
            confirmation=PRODUCTION_CONFIRMATION,
            now=issued_at + timedelta(minutes=1),
        )


def test_authorization_rejects_unsafe_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    issued = issue(root, monkeypatch)
    path = Path(issued["authorization_file"])
    os.chmod(path, 0o644)
    with pytest.raises(QuoteAssistantAuthorizationError, match="权限不安全"):
        validate_and_consume_one_time_authorization(
            path,
            root,
            confirmation=PRODUCTION_CONFIRMATION,
        )


def test_authorization_rejects_unsafe_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repository(tmp_path)
    issued = issue(root, monkeypatch)
    path = Path(issued["authorization_file"])
    os.chmod(path.parent, 0o755)
    with pytest.raises(QuoteAssistantAuthorizationError, match="目录权限不安全"):
        validate_and_consume_one_time_authorization(
            path,
            root,
            confirmation=PRODUCTION_CONFIRMATION,
        )


def test_current_repository_runtime_reports_local_environment_and_default_gate() -> None:
    report = check_quote_assistant_runtime(ROOT)
    assert report["wechat_mutation_count"] == 0
    safety = next(row for row in report["checks"] if row["name"] == "safety_configuration")
    assert safety["passed"] is True
    assert "默认生产开关关闭" in safety["message"]
    python_check = next(row for row in report["checks"] if row["name"] == "python_environment")
    if (ROOT / ".venv" / "bin" / "python").is_file():
        assert report["status"] == "READY"
        assert report["errors"] == []
        assert python_check["passed"] is True
    else:
        # CI intentionally has no repository-local .venv. The App must report
        # that daily-use prerequisite in Chinese and fail closed.
        assert report["status"] == "BLOCKED"
        assert python_check["passed"] is False
        assert any("找不到可执行的项目 Python" in error for error in report["errors"])


def test_incomplete_repository_runtime_fails_with_chinese_reason(tmp_path: Path) -> None:
    report = check_quote_assistant_runtime(tmp_path)
    assert report["status"] == "BLOCKED"
    assert report["errors"]
    assert any("仓库文件不完整" in error for error in report["errors"])


def test_runner_accepts_consumed_app_permit_without_editing_tracked_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = ROOT / "scripts" / "run_mikihouse_daily_production.py"
    spec = importlib.util.spec_from_file_location("daily_production_runner", script)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    tracked_config = {
        "production_save_enabled": False,
        "runtime_validation_status": "PASS",
    }
    monkeypatch.setattr(runner, "_read_json", lambda _path: dict(tracked_config))
    consumed = {
        "status": "CONSUMED",
        "authorization_mode": "APP_ONE_TIME",
        "reusable": False,
    }
    authorization_call: dict[str, object] = {}

    def fake_consume(*_args: object, **kwargs: object) -> dict:
        authorization_call.update(kwargs)
        return dict(consumed)

    monkeypatch.setattr(
        runner,
        "validate_and_consume_one_time_authorization",
        fake_consume,
    )
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    monkeypatch.setattr(
        runner,
        "run_daily_quote",
        lambda **_kwargs: {"status": "SUCCESS", "output_dir": str(generated_dir)},
    )

    def fake_save(_daily: Path, config: dict, **_kwargs: object) -> dict:
        assert config["production_save_enabled"] is True
        assert config["production_authorization_mode"] == "APP_ONE_TIME"
        assert tracked_config["production_save_enabled"] is False
        return {"status": "PASS", "favorite_create_count": 2}

    monkeypatch.setattr(runner, "save_daily_production_favorites", fake_save)
    code = runner.main(
        [
            "--production-save",
            "--confirm",
            PRODUCTION_CONFIRMATION,
            "--app-authorization-file",
            str(tmp_path / "private-permit.json"),
            "--output-root", str(tmp_path / "output"),
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "SUCCESS"
    assert output["production_authorization"] == consumed
    assert authorization_call["expected_operation"] == OPERATION_CREATE_DAILY


def test_runner_binds_rebuild_flag_to_rebuild_permit_and_sink_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = ROOT / "scripts" / "run_mikihouse_daily_production.py"
    spec = importlib.util.spec_from_file_location("daily_production_rebuild_runner", script)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(
        runner,
        "_read_json",
        lambda _path: {"production_save_enabled": False},
    )

    def fake_consume(*_args: object, **kwargs: object) -> dict:
        assert kwargs["expected_operation"] == OPERATION_REBUILD_MISSING_DAILY
        return {
            "status": "CONSUMED",
            "authorization_mode": "APP_ONE_TIME",
            "operation": OPERATION_REBUILD_MISSING_DAILY,
        }

    monkeypatch.setattr(runner, "validate_and_consume_one_time_authorization", fake_consume)
    generated_dir = tmp_path / "2026-09-23"
    generated_dir.mkdir()
    (generated_dir / "wechat_daily_production_checkpoint.json").write_text('{"status":"FROZEN_RECONCILIATION_REQUIRED"}')
    monkeypatch.setattr(
        runner,
        "run_daily_quote",
        lambda **_kwargs: pytest.fail("rebuild must not regenerate a protected bundle"),
    )

    def fake_save(_daily: Path, _config: dict, **kwargs: object) -> dict:
        assert _daily == generated_dir
        assert kwargs["rebuild_missing_daily"] is True
        assert _config["production_authorization_operation"] == OPERATION_REBUILD_MISSING_DAILY
        return {"status": "PASS", "favorite_create_count": 2}

    monkeypatch.setattr(runner, "save_daily_production_favorites", fake_save)
    code = runner.main(
        [
            "--production-save",
            "--rebuild-missing-daily-favorites",
            "--confirm",
            PRODUCTION_CONFIRMATION,
            "--app-authorization-file",
            str(tmp_path / "private-rebuild-permit.json"),
            "--output-root", str(tmp_path), "--quote-date", "2026-09-23",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "SUCCESS"


@pytest.mark.parametrize("frozen", [False, True])
def test_ordinary_same_day_production_reuses_before_generation(tmp_path, monkeypatch, capsys, frozen):
    spec = importlib.util.spec_from_file_location("ordinary_replay", ROOT / "scripts/run_mikihouse_daily_production.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    daily = tmp_path / "2026-09-23"
    daily.mkdir()
    checkpoint = daily / "wechat_daily_production_checkpoint.json"
    checkpoint.write_text("protected checkpoint")
    monkeypatch.setattr(runner, "_read_json", lambda path: {"production_save_enabled": True})
    monkeypatch.setattr(runner, "run_daily_quote", lambda **kwargs: pytest.fail("protected bundle must not be generated"))
    def reuse(path, config, **kwargs):
        assert path == daily and kwargs["rebuild_missing_daily"] is False
        if frozen:
            raise runner.WeChatDailyProductionError("checkpoint manifest mismatch")
        return {"status": "PASS", "idempotent_replay": True}
    monkeypatch.setattr(runner, "save_daily_production_favorites", reuse)
    code = runner.main(["--output-root", str(tmp_path), "--quote-date", "2026-09-23",
                        "--production-save", "--confirm", PRODUCTION_CONFIRMATION])
    assert code == (1 if frozen else 0)
    assert checkpoint.read_text() == "protected checkpoint"
    output = json.loads(capsys.readouterr().out)
    if not frozen:
        assert output["daily_quote"]["website_crawl_started"] is False
        assert output["wechat_two_favorites"]["idempotent_replay"] is True


def test_runner_recovery_uses_dedicated_permit_without_new_crawl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = ROOT / "scripts" / "run_mikihouse_daily_production.py"
    spec = importlib.util.spec_from_file_location("daily_production_recovery_runner", script)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "_read_json", lambda _path: {"production_save_enabled": False})

    def fake_consume(*_args: object, **kwargs: object) -> dict:
        assert kwargs["expected_operation"] == OPERATION_RECOVER_FROZEN_PDF
        return {
            "status": "CONSUMED",
            "authorization_mode": "APP_ONE_TIME",
            "operation": OPERATION_RECOVER_FROZEN_PDF,
        }

    monkeypatch.setattr(runner, "validate_and_consume_one_time_authorization", fake_consume)
    monkeypatch.setattr(
        runner,
        "run_daily_quote",
        lambda **_kwargs: pytest.fail("recovery must not run a new crawl"),
    )
    monkeypatch.setattr(
        runner,
        "recover_frozen_pdf_and_complete_daily_favorites",
        lambda *_args, **_kwargs: {"status": "PASS", "favorite_create_count": 2},
    )
    code = runner.main(
        [
            "--production-save",
            "--recover-frozen-pdf",
            "--quote-date",
            "2026-09-23",
            "--output-root",
            str(tmp_path),
            "--confirm",
            PRODUCTION_CONFIRMATION,
            "--app-authorization-file",
            str(tmp_path / "private-recovery-permit.json"),
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "SUCCESS"
    assert output["daily_quote"]["website_crawl_started"] is False
