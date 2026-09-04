from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .shijiu_import import now
from .shijiu_live_import import LiveImportError


WRITER_SOURCE = "MIKIHOUSE"
REPOSITORY = "qinxitong8666/mikihouse-luyao"
BRANCH = "main"
GLOBAL_MUTEX_PATH = Path("/private/tmp/shijiu-production-write.lock")
ALLOWED_CONFIRMATION_BASES = {
    "external_coordination",
    "shared_scheduler",
    "operator_confirmed_global_window",
}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise LiveImportError("writer mutex timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def current_head_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def validate_writer_mutex_evidence(
    path: Path,
    *,
    root: Path,
    product_number: str,
    stage_key: str,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if root.resolve() == resolved or root.resolve() in resolved.parents:
        raise LiveImportError("writer mutex evidence must remain outside the Git workspace")
    if not resolved.is_file():
        raise LiveImportError("writer mutex evidence is missing")
    raw_bytes = resolved.read_bytes()
    raw = json.loads(raw_bytes)
    head = current_head_sha(root)
    required = {
        "shijiu_writer_source": WRITER_SOURCE,
        "repository": REPOSITORY,
        "branch": BRANCH,
        "head_sha": head,
        "product_number": product_number,
        "concurrent_shijiu_writer_observed": False,
        "exclusive_window_confirmed": True,
    }
    for key, expected in required.items():
        if raw.get(key) != expected:
            raise LiveImportError(f"writer mutex evidence field mismatch: {key}")
    allowed_stages = raw.get("allowed_stage_keys") or []
    if stage_key not in allowed_stages:
        raise LiveImportError("writer mutex evidence does not authorize the current stage")
    if raw.get("confirmation_basis") not in ALLOWED_CONFIRMATION_BASES:
        raise LiveImportError("writer mutex evidence lacks an accepted external confirmation basis")
    issued_at = _utc(str(raw.get("issued_at") or ""))
    expires_at = _utc(str(raw.get("expires_at") or ""))
    current = datetime.now(timezone.utc)
    if not issued_at <= current < expires_at:
        raise LiveImportError("writer mutex evidence is not currently valid")
    if (expires_at - issued_at).total_seconds() > 4 * 60 * 60:
        raise LiveImportError("writer mutex evidence window exceeds four hours")
    return {
        "status": "VERIFIED_BEFORE_WRITE",
        "shijiu_writer_source": WRITER_SOURCE,
        "repository": REPOSITORY,
        "branch": BRANCH,
        "head_sha": head,
        "product_number": product_number,
        "stage_key": stage_key,
        "confirmation_basis": raw["confirmation_basis"],
        "concurrent_shijiu_writer_observed": False,
        "exclusive_window_confirmed": True,
        "evidence_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "sensitive_values_included": False,
    }


@contextmanager
def production_write_window(
    evidence_path: Path,
    *,
    root: Path,
    product_number: str,
    stage_key: str,
) -> Iterator[dict[str, Any]]:
    evidence = validate_writer_mutex_evidence(
        evidence_path,
        root=root,
        product_number=product_number,
        stage_key=stage_key,
    )
    GLOBAL_MUTEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(GLOBAL_MUTEX_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LiveImportError("another local Shijiu production writer holds the global mutex") from exc
        evidence["production_write_window_started_at"] = now()
        yield evidence
    finally:
        evidence["production_write_window_ended_at"] = now()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def mutex_evidence_satisfied(checkpoint: dict[str, Any]) -> bool:
    stages = checkpoint.get("stages") or []
    windows = checkpoint.get("production_write_windows") or []
    verified = [row for row in stages if row.get("state") == "VERIFIED"]
    if len(windows) != len(verified) or not verified:
        return False
    by_stage = {str(row.get("stage_key")): row for row in windows}
    return all(
        (window := by_stage.get(str(stage.get("key")))) is not None
        and window.get("status") == "VERIFIED_BEFORE_WRITE"
        and window.get("shijiu_writer_source") == WRITER_SOURCE
        and window.get("concurrent_shijiu_writer_observed") is False
        and window.get("exclusive_window_confirmed") is True
        and bool(window.get("production_write_window_started_at"))
        and bool(window.get("production_write_window_ended_at"))
        for stage in verified
    )


def build_retrospective_mutex_audit(checkpoint: dict[str, Any]) -> dict[str, Any]:
    stages = checkpoint.get("stages") or []
    ledger = checkpoint.get("request_ledger") or []
    started = min(
        (str(row.get("intent_at")) for row in stages if row.get("intent_at")),
        default=None,
    )
    ended = max(
        (str(row.get("verified_at")) for row in stages if row.get("verified_at")),
        default=None,
    )
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": (
            "MUTEX_EVIDENCE_VERIFIED"
            if mutex_evidence_satisfied(checkpoint)
            else "FAIL_CLOSED_NO_FURTHER_WRITE_MUTEX_EVIDENCE_NOT_CAPTURED"
        ),
        "rule_source": "AGENTS.md#14",
        "rule_introduced_remote_commit": "26e30330c25987743be6fe7c424dd3528c6f83c0",
        "shijiu_writer_source": WRITER_SOURCE,
        "repository": REPOSITORY,
        "branch_at_write_time": BRANCH,
        "local_head_at_write_time": "cf75516b3e12cba77ce34fde7e1cd6d1b5a6f811",
        "product_number": checkpoint.get("product_number"),
        "shijiu_product_id": checkpoint.get("shijiu_product_id"),
        "exact_write_scope": [str(row.get("key")) for row in stages],
        "ownership_proof": {
            "create_was_explicitly_authorized_for_product_number": checkpoint.get("product_number"),
            "mapping_completed_by_exact_backend_sku_readback": bool(
                checkpoint.get("mapping_persisted")
            ),
            "update_target_was_the_same_mapped_product_id": checkpoint.get("shijiu_product_id"),
        },
        "cross_source_writes": 0,
        "concurrent_shijiu_writer_observed": "NOT_CAPTURED",
        "production_write_window_started_at": started,
        "production_write_window_ended_at": ended,
        "request_counts": {
            "create": sum(
                row.get("path") == "/shopapi/Goods/newAddGood"
                and "create" in str(row.get("operation") or "").casefold()
                for row in ledger
            ),
            "update": sum(
                row.get("path") == "/shopapi/Goods/newAddGood"
                and "update" in str(row.get("operation") or "").casefold()
                for row in ledger
            ),
            "upload": sum(row.get("path") == "/v1/cos/upload" for row in ledger),
            "readback": sum(row.get("semantic_operation") == "read" for row in ledger),
            "failure": sum(row.get("outcome") == "ERROR" for row in ledger),
            "transport_unknown": sum(
                "UNKNOWN" in str(row.get("outcome") or "").upper() for row in ledger
            ),
        },
        "technical_five_stage_readback_completed": checkpoint.get("status") == "COMPLETED",
        "production_governance_verified": mutex_evidence_satisfied(checkpoint),
        "no_new_shijiu_writes_allowed_without_fresh_mutex_evidence": True,
        "sensitive_values_included": False,
    }
