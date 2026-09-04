from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIRMATION = "我已确认当前SHIJIU正式租户没有其他生产写入任务"
ALLOWED_BASES = (
    "external_coordination",
    "shared_scheduler",
    "operator_confirmed_global_window",
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Capture one operator-confirmed exclusive Shijiu write window outside Git"
    )
    result.add_argument("--private-dir", type=Path, required=True)
    result.add_argument(
        "--confirmation-basis",
        choices=ALLOWED_BASES,
        default="operator_confirmed_global_window",
    )
    result.add_argument("--valid-minutes", type=int, default=120)
    result.add_argument(
        "--plan",
        type=Path,
        default=ROOT / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json",
    )
    return result


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_evidence(plan: dict, *, basis: str, minutes: int) -> dict:
    if not 5 <= minutes <= 240:
        raise ValueError("exclusive window must be between 5 and 240 minutes")
    if plan.get("product_count") != 20 or plan.get("execution_authorized") is not False:
        raise ValueError("expected the frozen, not-yet-executed 20-product plan")
    issued = datetime.now(timezone.utc)
    scopes = [
        {
            "product_number": row["product_number"],
            "allowed_stage_keys": [stage["key"] for stage in row["required_stages"]],
        }
        for row in plan["products"]
    ]
    return {
        "schema_version": 1,
        "shijiu_writer_source": "MIKIHOUSE",
        "repository": "qinxitong8666/mikihouse-luyao",
        "branch": "main",
        "head_sha": _head(),
        "plan_sha256": hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "authorized_scopes": scopes,
        "concurrent_shijiu_writer_observed": False,
        "exclusive_window_confirmed": True,
        "confirmation_basis": basis,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=minutes)).isoformat(),
        "stop_conditions": [
            "first mutation, upload, readback, ownership, or contract anomaly",
            "mutex evidence expiry",
            "local global mutex unavailable",
            "any concurrent or cross-source writer observed",
        ],
        "operator_confirmation_sha256": hashlib.sha256(CONFIRMATION.encode()).hexdigest(),
        "sensitive_values_included": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    private_dir = args.private_dir.expanduser().resolve()
    if ROOT.resolve() == private_dir or ROOT.resolve() in private_dir.parents:
        raise SystemExit("private evidence directory must be outside the Git workspace")
    print("在继续前，请确认所有其他项目/终端/自动任务均未向同一 Shijiu 正式租户写入。")
    print(f"如已通过真实协调确认独占窗口，请完整输入：{CONFIRMATION}")
    entered = input("> ").strip()
    if entered != CONFIRMATION:
        print("未取得人工独占确认；没有生成 evidence，也不会执行生产写入。")
        return 2
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    evidence = build_evidence(plan, basis=args.confirmation_basis, minutes=args.valid_minutes)
    private_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(private_dir, 0o700)
    path = private_dir / f"shijiu-writer-mutex-{evidence['issued_at'].replace(':', '-')}.private.json"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({
        "status": "OPERATOR_CONFIRMED_EXTERNAL_EVIDENCE_CREATED",
        "path": str(path),
        "head_sha": evidence["head_sha"],
        "authorized_product_count": len(evidence["authorized_scopes"]),
        "expires_at": evidence["expires_at"],
        "raw_confirmation_persisted": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
