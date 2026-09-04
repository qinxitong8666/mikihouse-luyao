from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state, now, write_json_atomic
from mikihouse_luyao.shijiu_richtext_e2e import (
    RICHTEXT_E2E_MODE,
    RICHTEXT_E2E_WRITE_CONFIRMATION,
    RichtextContractE2ERunner,
    build_representative_next_20_plan,
    build_richtext_e2e_conclusion,
    build_richtext_e2e_selection,
    load_richtext_e2e_candidate,
    make_richtext_e2e_clients,
    verify_live_source_exact,
)
from mikihouse_luyao.shijiu_writer_mutex import (
    build_retrospective_mutex_audit,
    production_write_window,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Execute the frozen 10-9332-796 rich-text-contract final E2E proof"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--preflight-resources", action="store_true")
    result.add_argument("--next-step", action="store_true")
    result.add_argument("--finalize-evidence-only", action="store_true")
    result.add_argument("--confirm", default="")
    result.add_argument("--writer-mutex-evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    modes = (
        args.prepare_only,
        args.preflight_resources,
        args.next_step,
        args.finalize_evidence_only,
    )
    if sum(bool(value) for value in modes) != 1:
        raise SystemExit("select exactly one execution mode")

    master_path = ROOT / "output/storefront-master/master_catalog.json"
    special_path = ROOT / "special_skus_2026aw.csv"
    category_path = ROOT / "config/shijiu_category_map.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    frozen_readiness_path = ROOT / "deliverables/shijiu_import/richtext_contract_readiness.json"
    selection_path = ROOT / "config/shijiu_richtext_e2e_single.json"
    checkpoint_path = ROOT / "state/shijiu_richtext_e2e_checkpoint.json"
    base = ROOT / "deliverables/shijiu_import"
    report_path = base / "richtext_e2e_validation_report.json"
    readbacks_path = base / "richtext_e2e_readbacks.json"
    candidate_path = base / "richtext_e2e_candidate.json"
    live_source_path = base / "richtext_e2e_live_source_verification.json"
    preflight_path = base / "richtext_e2e_resource_preflight.json"
    conclusion_path = base / "richtext_e2e_conclusion.json"
    readiness_path = base / "richtext_e2e_production_readiness.json"
    next_20_path = base / "richtext_e2e_next_20_frozen_plan.json"
    mutex_audit_path = base / "richtext_e2e_writer_mutex_audit.json"
    contract_path = ROOT / "config/shijiu_native_create_contract.json"

    master = json.loads(master_path.read_text(encoding="utf-8"))
    special = set(read_product_numbers(special_path))
    category = load_category_map(category_path)
    mapping = load_mapping_state(mapping_path)
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        item = load_richtext_e2e_candidate(master, special, category, selection)
    else:
        frozen_readiness = json.loads(frozen_readiness_path.read_text(encoding="utf-8"))
        item, selection = build_richtext_e2e_selection(
            ROOT, master, special, mapping, category, frozen_readiness
        )
        selection["master_catalog_sha256"] = hashlib.sha256(master_path.read_bytes()).hexdigest()
        write_json_atomic(selection_path, selection)

    client, ui, browser_evidence = make_richtext_e2e_clients(
        args.browser_private_dir, contract_path
    )
    runner = RichtextContractE2ERunner(
        client,
        ui,
        item,
        special,
        category,
        selection,
        root=ROOT,
        checkpoint_path=checkpoint_path,
        mapping_path=mapping_path,
        report_path=report_path,
        readbacks_path=readbacks_path,
        confirmation=args.confirm,
    )

    def write_outcomes() -> dict:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        preflight = copy.deepcopy(checkpoint.get("resource_preflight") or {})
        preflight.update({
            "schema_version": 1,
            "generated_at": now(),
            "product_number": item["product_number"],
            "sensitive_values_included": False,
        })
        write_json_atomic(preflight_path, preflight)
        conclusion = build_richtext_e2e_conclusion(checkpoint)
        write_json_atomic(conclusion_path, conclusion)
        mutex_audit = build_retrospective_mutex_audit(checkpoint)
        write_json_atomic(mutex_audit_path, mutex_audit)
        ready = conclusion["production_import_architecture_verified"]
        readiness = {
            "schema_version": 1,
            "generated_at": now(),
            "status": (
                "VERIFIED_READY_FOR_SEPARATELY_AUTHORIZED_20_PRODUCT_BATCH"
                if ready
                else "NOT_READY_MUTEX_EVIDENCE_NOT_CAPTURED"
                if conclusion.get("technical_five_stage_readback_completed")
                else "NOT_READY_SINGLE_PRODUCT_FLOW_INCOMPLETE"
            ),
            "production_import_architecture_verified": ready,
            "production_architecture": conclusion["production_architecture"],
            "verified_product_number": item["product_number"] if ready else None,
            "verified_shijiu_product_id": checkpoint.get("shijiu_product_id") if ready else None,
            "next_20_plan_generated": ready,
            "next_20_executed": False,
            "requires_separate_write_authorization": True,
            "pdf_special_exclusion_count": len(special),
            "legacy_reference_touched": False,
            "non_mikihouse_richtext_test_cleanup_included": False,
            "image_type_good_details_generated_or_attempted": False,
            "production_write_mutex_evidence_verified": conclusion[
                "production_write_mutex_evidence_verified"
            ],
            "fail_closed_no_further_write": not conclusion[
                "production_write_mutex_evidence_verified"
            ],
            "sensitive_values_included": False,
        }
        write_json_atomic(readiness_path, readiness)
        if ready:
            current_mapping = load_mapping_state(mapping_path)
            prohibited = set(selection["historical_prohibited_product_numbers"])
            prohibited.add(item["product_number"])
            write_json_atomic(
                next_20_path,
                build_representative_next_20_plan(
                    master, special, current_mapping, category, prohibited
                ),
            )
        elif next_20_path.exists():
            frozen_plan = json.loads(next_20_path.read_text(encoding="utf-8"))
            frozen_plan.update({
                "status": "FROZEN_BLOCKED_MUTEX_EVIDENCE_NOT_CAPTURED",
                "execution_authorized": False,
                "real_write_requests": 0,
                "production_write_mutex_evidence_verified": False,
                "fail_closed_no_write": True,
            })
            write_json_atomic(next_20_path, frozen_plan)
        return conclusion

    if args.finalize_evidence_only:
        conclusion = write_outcomes()
        print(json.dumps({
            "status": conclusion["status"],
            "technical_checkpoint_status": runner.checkpoint["status"],
            "product_number": item["product_number"],
            "evidence_only": True,
            "shijiu_requests_sent": len(client.requests) + len(ui.requests),
            "production_architecture_verified": conclusion[
                "production_import_architecture_verified"
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    live = fetch_product(item["product_number"], timeout=30, retries=2)
    live_source = verify_live_source_exact(item, live)
    write_json_atomic(live_source_path, live_source)
    candidate = copy.deepcopy(selection)
    candidate["live_source_verification"] = live_source
    candidate["canonical_browser_evidence"] = browser_evidence
    candidate["sensitive_values_included"] = False
    write_json_atomic(candidate_path, candidate)

    if args.prepare_only:
        write_outcomes()
        print(json.dumps({
            "status": runner.checkpoint["status"],
            "mode": RICHTEXT_E2E_MODE,
            "product": candidate["product"],
            "live_source_verification": live_source,
            "shijiu_requests_sent": len(client.requests) + len(ui.requests),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.preflight_resources:
        try:
            result = runner.run_resource_preflight()
        except Exception as error:
            write_outcomes()
            print(json.dumps({
                "status": "BLOCKED_RESOURCE_PREFLIGHT_ZERO_SHIJIU_WRITES",
                "error": {"type": type(error).__name__, "message": str(error)},
                "shijiu_requests_sent": len(client.requests) + len(ui.requests),
            }, ensure_ascii=False, indent=2))
            return 2
        write_outcomes()
        print(json.dumps({
            "status": result["status"],
            "verified_reference_count": result["verified_reference_count"],
            "approved_source_host_suffixes": result["approved_source_host_suffixes"],
            "shijiu_requests_sent": result["shijiu_requests_sent"],
            "shijiu_write_requests_sent": result["shijiu_write_requests_sent"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != RICHTEXT_E2E_WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: exact --confirm value missing")
    if args.writer_mutex_evidence is None:
        raise SystemExit(
            "FAIL_CLOSED_NO_WRITE: --writer-mutex-evidence is required for every production stage"
        )
    cursor_before = int(runner.checkpoint["stage_cursor"])
    stage_key = str(runner.checkpoint["stages"][cursor_before]["key"])
    window = None
    try:
        with production_write_window(
            args.writer_mutex_evidence,
            root=ROOT,
            product_number=item["product_number"],
            stage_key=stage_key,
        ) as window:
            report = runner.run_next_step()
    except Exception as error:
        if window is not None:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint.setdefault("production_write_windows", []).append(window)
            write_json_atomic(checkpoint_path, checkpoint)
        write_outcomes()
        print(json.dumps({
            "status": runner.checkpoint["status"],
            "error": {"type": type(error).__name__, "message": str(error)},
        }, ensure_ascii=False, indent=2))
        return 2
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint.setdefault("production_write_windows", []).append(window)
    write_json_atomic(checkpoint_path, checkpoint)
    runner.checkpoint = checkpoint
    runner._persist()
    conclusion = write_outcomes()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    cursor = int(checkpoint["stage_cursor"])
    print(json.dumps({
        "status": report["status"],
        "product_number": item["product_number"],
        "shijiu_product_id": report.get("shijiu_product_id"),
        "last_verified_success_state": report.get("last_verified_success_state"),
        "next_stage": (
            checkpoint["stages"][cursor] if cursor < len(checkpoint["stages"]) else None
        ),
        "request_counts": report["request_counts"],
        "ui_read_retry_attempt_count": report["ui_read_retry_attempt_count"],
        "production_architecture_verified": conclusion[
            "production_import_architecture_verified"
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
