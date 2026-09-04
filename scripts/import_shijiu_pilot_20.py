from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_import import (
    content_sha256,
    load_category_map,
    load_mapping_state,
    now,
    write_json_atomic,
)
from mikihouse_luyao.shijiu_live_import import LiveImportError
from mikihouse_luyao.shijiu_pilot_20 import (
    PILOT_CONFIRMATION,
    PILOT_MODE,
    PILOT_PROTECTED_FILES,
    build_pilot_completion_report,
    build_pilot_product_selection,
    build_remaining_initialization_plan,
    initial_pilot_checkpoint,
    validate_frozen_pilot_plan,
    waiting_operator_report,
)
from mikihouse_luyao.shijiu_richtext_e2e import (
    RichtextContractE2ERunner,
    make_richtext_e2e_clients,
    verify_live_source_exact,
)
from mikihouse_luyao.shijiu_writer_mutex import (
    global_production_mutex,
    validate_writer_mutex_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Fail-closed executor for the frozen 20-product MIKIHOUSE production pilot"
    )
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--execute", action="store_true")
    result.add_argument("--browser-private-dir", type=Path)
    result.add_argument("--writer-mutex-evidence", type=Path)
    result.add_argument("--confirm", default="")
    return result


def _load_item_from_selection(
    master: dict,
    special: set[str],
    category: dict,
    selection: dict,
) -> dict:
    number = str((selection.get("product") or {}).get("product_number") or "")
    product = next(
        (row for row in master.get("products") or [] if row.get("product_number") == number),
        None,
    )
    if not product:
        raise LiveImportError("pilot product disappeared from the master catalog")
    from mikihouse_luyao.shijiu_import import map_product_to_shijiu
    from mikihouse_luyao.shijiu_staged_media_import import stage_plan

    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if (
        not item.get("publish_ready")
        or item["payload_sha256"] != selection["product"]["source_payload_sha256"]
        or stage_plan(item) != selection.get("stages")
    ):
        raise LiveImportError("persisted pilot selection drifted from the current source payload")
    return item


def _append_window(checkpoint_path: Path, runner: RichtextContractE2ERunner, window: dict) -> None:
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint.setdefault("production_write_windows", []).append(copy.deepcopy(window))
    write_json_atomic(checkpoint_path, checkpoint)
    runner.checkpoint = checkpoint
    runner._persist()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if bool(args.prepare_only) == bool(args.execute):
        raise SystemExit("select exactly one of --prepare-only or --execute")

    plan_path = ROOT / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json"
    batch_checkpoint_path = ROOT / "state/shijiu_pilot_20_batch_checkpoint.json"
    status_path = ROOT / "deliverables/shijiu_import/pilot_20_mutex_readiness.json"
    report_path = ROOT / "deliverables/shijiu_import/pilot_20_completion_report.json"
    remaining_path = ROOT / "deliverables/shijiu_import/remaining_mikihouse_initialization_plan.json"
    product_state_root = ROOT / "state/shijiu_pilot_20"
    product_report_root = ROOT / "deliverables/shijiu_import/pilot_20_products"
    master = json.loads((ROOT / "output/storefront-master/master_catalog.json").read_text())
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    mapping = load_mapping_state(ROOT / "state/shijiu_mappings.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    checkpoint = (
        json.loads(batch_checkpoint_path.read_text(encoding="utf-8"))
        if batch_checkpoint_path.exists()
        else initial_pilot_checkpoint(plan)
    )
    allow_mapped = {
        number
        for number, row in checkpoint["products"].items()
        if row.get("status") in {"IN_PROGRESS", "COMPLETED"}
    }
    rows = validate_frozen_pilot_plan(plan, special, mapping, allow_mapped=allow_mapped)
    if checkpoint.get("plan_sha256") != content_sha256(plan):
        raise SystemExit("pilot batch checkpoint plan hash mismatch")

    if args.prepare_only:
        checkpoint["status"] = "WAITING_OPERATOR_MUTEX_CONFIRMATION"
        checkpoint["stop_reason"] = "VALID_EXTERNAL_PRODUCTION_WRITER_MUTEX_EVIDENCE_REQUIRED"
        checkpoint["updated_at"] = now()
        write_json_atomic(batch_checkpoint_path, checkpoint)
        report = waiting_operator_report(plan, checkpoint)
        write_json_atomic(status_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if not args.browser_private_dir or not args.writer_mutex_evidence:
        raise SystemExit(
            "FAIL_CLOSED_NO_WRITE: --browser-private-dir and --writer-mutex-evidence are required"
        )
    if args.confirm != PILOT_CONFIRMATION:
        raise SystemExit("FAIL_CLOSED_NO_WRITE: exact pilot confirmation is missing")
    raw_evidence = json.loads(args.writer_mutex_evidence.expanduser().read_text(encoding="utf-8"))
    if raw_evidence.get("plan_sha256") != content_sha256(plan):
        raise SystemExit("FAIL_CLOSED_NO_WRITE: mutex evidence plan hash mismatch")
    first_pending = next(
        (
            row
            for row in rows
            if checkpoint["products"][row["product_number"]]["status"] != "COMPLETED"
        ),
        None,
    )
    if first_pending is None:
        raise SystemExit("pilot batch is already terminal; no write is permitted")
    first_prefix = f"{int(first_pending['sequence']):02d}_{first_pending['product_number'].replace('-', '_')}"
    first_product_checkpoint = product_state_root / f"{first_prefix}_checkpoint.json"
    first_cursor = 0
    if first_product_checkpoint.exists():
        first_cursor = int(json.loads(first_product_checkpoint.read_text())["stage_cursor"])
    first_stage = first_pending["required_stages"][first_cursor]["key"]
    validate_writer_mutex_evidence(
        args.writer_mutex_evidence,
        root=ROOT,
        product_number=first_pending["product_number"],
        stage_key=first_stage,
    )

    checkpoint["status"] = "IN_PROGRESS_EXCLUSIVE_WRITER_WINDOW"
    checkpoint["stop_reason"] = None
    checkpoint["production_write_window_started_at"] = now()
    write_json_atomic(batch_checkpoint_path, checkpoint)
    try:
        with global_production_mutex():
            for plan_row in rows:
                number = plan_row["product_number"]
                record = checkpoint["products"][number]
                if record["status"] == "COMPLETED":
                    continue
                if record["status"] not in {"PLANNED", "IN_PROGRESS"}:
                    raise LiveImportError("pilot resume encountered a terminal product checkpoint")
                sequence = int(plan_row["sequence"])
                prefix = f"{sequence:02d}_{number.replace('-', '_')}"
                selection_path = product_state_root / f"{prefix}_selection.json"
                product_checkpoint_path = product_state_root / f"{prefix}_checkpoint.json"
                product_report_path = product_report_root / f"{prefix}_report.json"
                product_readbacks_path = product_report_root / f"{prefix}_readbacks.json"
                live_source_path = product_report_root / f"{prefix}_live_source.json"
                preflight_path = product_report_root / f"{prefix}_resource_preflight.json"
                if selection_path.exists():
                    selection = json.loads(selection_path.read_text(encoding="utf-8"))
                    item = _load_item_from_selection(master, special, category, selection)
                else:
                    current_mapping = load_mapping_state(ROOT / "state/shijiu_mappings.json")
                    item, selection = build_pilot_product_selection(
                        ROOT, master, special, current_mapping, category, plan_row
                    )
                    write_json_atomic(selection_path, selection)

                live = fetch_product(number, timeout=30, retries=2)
                live_evidence = verify_live_source_exact(item, live)
                write_json_atomic(live_source_path, live_evidence)
                client, ui, _ = make_richtext_e2e_clients(
                    args.browser_private_dir,
                    ROOT / "config/shijiu_native_create_contract.json",
                    write_confirmation=PILOT_CONFIRMATION,
                )
                runner = RichtextContractE2ERunner(
                    client,
                    ui,
                    item,
                    special,
                    category,
                    selection,
                    root=ROOT,
                    checkpoint_path=product_checkpoint_path,
                    mapping_path=ROOT / "state/shijiu_mappings.json",
                    report_path=product_report_path,
                    readbacks_path=product_readbacks_path,
                    confirmation=args.confirm,
                    mode=PILOT_MODE,
                    expected_confirmation=PILOT_CONFIRMATION,
                    protected_frozen_files=PILOT_PROTECTED_FILES,
                )
                record["status"] = "IN_PROGRESS"
                write_json_atomic(batch_checkpoint_path, checkpoint)
                if runner.checkpoint["status"] == "READY_FOR_RESOURCE_PREFLIGHT":
                    preflight = runner.run_resource_preflight()
                    write_json_atomic(preflight_path, {
                        **copy.deepcopy(preflight),
                        "product_number": number,
                        "sensitive_values_included": False,
                    })
                while runner.checkpoint["status"] != "COMPLETED":
                    cursor = int(runner.checkpoint["stage_cursor"])
                    stage_key = str(runner.checkpoint["stages"][cursor]["key"])
                    mutex = validate_writer_mutex_evidence(
                        args.writer_mutex_evidence,
                        root=ROOT,
                        product_number=number,
                        stage_key=stage_key,
                    )
                    window = {
                        **mutex,
                        "production_write_window_started_at": now(),
                    }
                    try:
                        runner.run_next_step()
                    finally:
                        window["production_write_window_ended_at"] = now()
                        _append_window(product_checkpoint_path, runner, window)
                        stage_snapshot = product_state_root / f"{prefix}_{cursor + 1:02d}_{stage_key}.json"
                        write_json_atomic(stage_snapshot, runner.checkpoint)
                    record["completed_stage_count"] = cursor + 1
                    record["shijiu_product_id"] = runner.checkpoint.get("shijiu_product_id")
                    write_json_atomic(batch_checkpoint_path, checkpoint)
                record["status"] = "COMPLETED"
                checkpoint["next_sequence"] = sequence + 1
                write_json_atomic(batch_checkpoint_path, checkpoint)
    except Exception as error:
        checkpoint["status"] = "FROZEN_ON_FIRST_ANOMALY"
        checkpoint["stop_reason"] = {"type": type(error).__name__, "message": str(error)}
        checkpoint["production_write_window_ended_at"] = now()
        checkpoint["updated_at"] = now()
        write_json_atomic(batch_checkpoint_path, checkpoint)
        write_json_atomic(status_path, {
            "schema_version": 1,
            "generated_at": now(),
            "status": "FROZEN_ON_FIRST_ANOMALY",
            "error": checkpoint["stop_reason"],
            "automatic_retry": False,
            "cross_source_writes": checkpoint["cross_source_writes"],
            "sensitive_values_included": False,
        })
        print(json.dumps({"status": checkpoint["status"], "error": checkpoint["stop_reason"]}, ensure_ascii=False, indent=2))
        return 2

    checkpoint["status"] = "COMPLETED"
    checkpoint["production_write_window_ended_at"] = now()
    checkpoint["updated_at"] = now()
    write_json_atomic(batch_checkpoint_path, checkpoint)
    product_checkpoints = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(product_state_root.glob("*_checkpoint.json"))
    ]
    completion = build_pilot_completion_report(
        plan,
        checkpoint,
        product_checkpoints,
        load_mapping_state(ROOT / "state/shijiu_mappings.json"),
    )
    write_json_atomic(report_path, completion)
    if completion["status"] == "COMPLETED":
        write_json_atomic(
            remaining_path,
            build_remaining_initialization_plan(
                master,
                special,
                load_mapping_state(ROOT / "state/shijiu_mappings.json"),
                category,
            ),
        )
    print(json.dumps(completion, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
