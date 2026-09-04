from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_live_import import LiveImportError
from mikihouse_luyao.shijiu_writer_mutex import (
    build_retrospective_mutex_audit,
    mutex_evidence_satisfied,
    validate_writer_mutex_evidence,
)


def test_missing_or_in_workspace_mutex_evidence_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(LiveImportError, match="outside the Git workspace"):
        validate_writer_mutex_evidence(
            tmp_path / "missing.json",
            root=tmp_path,
            product_number="10-9332-796",
            stage_key="CREATE_CORE",
        )


def test_valid_external_mutex_evidence_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    current = datetime.now(timezone.utc)
    path = private / "mutex.json"
    path.write_text(json.dumps({
        "shijiu_writer_source": "MIKIHOUSE",
        "repository": "qinxitong8666/mikihouse-luyao",
        "branch": "main",
        "head_sha": "a" * 40,
        "product_number": "10-9332-796",
        "allowed_stage_keys": ["CREATE_CORE"],
        "concurrent_shijiu_writer_observed": False,
        "exclusive_window_confirmed": True,
        "confirmation_basis": "external_coordination",
        "issued_at": (current - timedelta(minutes=1)).isoformat(),
        "expires_at": (current + timedelta(minutes=10)).isoformat(),
        "private_note": "must not persist",
    }), encoding="utf-8")
    monkeypatch.setattr(
        "mikihouse_luyao.shijiu_writer_mutex.current_head_sha", lambda value: "a" * 40
    )
    result = validate_writer_mutex_evidence(
        path,
        root=root,
        product_number="10-9332-796",
        stage_key="CREATE_CORE",
    )
    assert result["status"] == "VERIFIED_BEFORE_WRITE"
    assert result["concurrent_shijiu_writer_observed"] is False
    assert "private_note" not in result


def test_retrospective_audit_does_not_invent_concurrency_proof() -> None:
    checkpoint = {
        "status": "COMPLETED",
        "product_number": "10-9332-796",
        "shijiu_product_id": "9358340",
        "mapping_persisted": True,
        "stages": [{
            "key": "CREATE_CORE",
            "state": "VERIFIED",
            "intent_at": "2026-09-04T12:43:07+00:00",
            "verified_at": "2026-09-04T12:43:12+00:00",
        }],
        "request_ledger": [],
    }
    assert mutex_evidence_satisfied(checkpoint) is False
    audit = build_retrospective_mutex_audit(checkpoint)
    assert audit["status"] == "FAIL_CLOSED_NO_FURTHER_WRITE_MUTEX_EVIDENCE_NOT_CAPTURED"
    assert audit["concurrent_shijiu_writer_observed"] == "NOT_CAPTURED"
    assert audit["production_governance_verified"] is False
