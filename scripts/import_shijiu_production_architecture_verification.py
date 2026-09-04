from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state, now, write_json_atomic
from mikihouse_luyao.shijiu_production_architecture_verification import (
    FINAL_E2E_MODE,
    FINAL_E2E_WRITE_CONFIRMATION,
    FinalE2EStagedRunner,
    analyze_frozen_final_html,
    build_final_e2e_conclusion,
    build_representative_next_20_plan,
    load_final_e2e_candidate,
    make_final_e2e_clients,
    select_final_e2e_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the final one-product Shijiu production architecture proof")
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--preflight-resources", action="store_true")
    result.add_argument("--next-step", action="store_true")
    result.add_argument("--finalize-evidence-only", action="store_true")
    result.add_argument("--confirm", default="")
    return result


def verify_live_source(item: dict) -> dict:
    live = fetch_product(item["product_number"], timeout=30, retries=2)
    expected = {row["source_variant_sku"]: row for row in item["source_variants"]}
    actual = {row.sku: row for row in live.variants}
    if set(actual) != set(expected):
        raise RuntimeError("live source SKU set drift for final E2E candidate")
    for sku, source in expected.items():
        observed = actual[sku]
        if (
            observed.tax_included_price_jpy != source["tax_included_price_jpy"]
            or observed.in_stock != source["available_for_sale"]
            or observed.color != source["color"]
            or observed.size != source["size"]
            or observed.image_url != source["image_url"]
        ):
            raise RuntimeError(f"live source variant drift: {sku}")
    return {
        "verified_at": now(),
        "variant_count": len(actual),
        "all_skus_prices_stocks_colors_sizes_and_images_match_master": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if sum(bool(value) for value in (
        args.prepare_only, args.preflight_resources, args.next_step, args.finalize_evidence_only,
    )) != 1:
        raise SystemExit("select exactly one execution mode")

    master_path = ROOT / "output/storefront-master/master_catalog.json"
    special_path = ROOT / "special_skus_2026aw.csv"
    category_path = ROOT / "config/shijiu_category_map.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    selection_path = ROOT / "config/shijiu_production_architecture_verification_single.json"
    checkpoint_path = ROOT / "state/shijiu_production_architecture_verification_checkpoint.json"
    base = ROOT / "deliverables/shijiu_import"
    report_path = base / "production_architecture_validation_report.json"
    readbacks_path = base / "production_architecture_readbacks.json"
    candidate_path = base / "production_architecture_candidate.json"
    preflight_path = base / "production_architecture_resource_preflight.json"
    conclusion_path = base / "production_architecture_conclusion.json"
    forensics_path = base / "production_architecture_final_html_forensics.json"
    readiness_path = base / "production_architecture_readiness.json"
    next_20_path = base / "production_architecture_next_20_frozen_plan.json"
    contract_path = ROOT / "config/shijiu_native_create_contract.json"

    master = json.loads(master_path.read_text(encoding="utf-8"))
    special = set(read_product_numbers(special_path))
    category = load_category_map(category_path)
    mapping = load_mapping_state(mapping_path)
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        item = load_final_e2e_candidate(master, special, category, selection)
    else:
        item, selection = select_final_e2e_candidate(ROOT, master, special, mapping, category)
        selection["master_catalog_sha256"] = hashlib.sha256(master_path.read_bytes()).hexdigest()
        write_json_atomic(selection_path, selection)

    client, ui, browser_evidence = make_final_e2e_clients(args.browser_private_dir, contract_path)
    runner = FinalE2EStagedRunner(
        client, ui, item, special, category, selection,
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
        forensics = analyze_frozen_final_html(item, checkpoint)
        write_json_atomic(forensics_path, forensics)
        conclusion = build_final_e2e_conclusion(checkpoint, forensics)
        write_json_atomic(conclusion_path, conclusion)
        ready = conclusion["production_import_architecture_verified"]
        readiness = {
            "schema_version": 1,
            "generated_at": now(),
            "status": "VERIFIED_READY_FOR_SEPARATELY_AUTHORIZED_20_PRODUCT_BATCH" if ready else "NOT_READY_SINGLE_PRODUCT_FLOW_INCOMPLETE",
            "production_import_architecture_verified": ready,
            "verified_product_number": item["product_number"] if ready else None,
            "verified_shijiu_product_id": checkpoint.get("shijiu_product_id") if ready else None,
            "next_20_plan_generated": ready,
            "next_20_executed": False,
            "requires_separate_write_authorization": True,
            "pdf_special_exclusion_count": len(special),
            "legacy_reference_touched": False,
            "sensitive_values_included": False,
        }
        write_json_atomic(readiness_path, readiness)
        if ready:
            current_mapping = load_mapping_state(mapping_path)
            prohibited = set(selection["historical_prohibited_product_numbers"])
            prohibited.add(item["product_number"])
            write_json_atomic(next_20_path, build_representative_next_20_plan(
                master, special, current_mapping, category, prohibited
            ))
        return conclusion

    if args.finalize_evidence_only:
        conclusion = write_outcomes()
        print(json.dumps({
            "status": runner.checkpoint["status"],
            "product_number": item["product_number"],
            "evidence_only": True,
            "shijiu_requests_sent": len(client.requests) + len(ui.requests),
            "production_architecture_verified": conclusion["production_import_architecture_verified"],
        }, ensure_ascii=False, indent=2))
        return 0

    candidate = copy.deepcopy(selection)
    candidate["live_source_verification"] = verify_live_source(item)
    candidate["canonical_browser_evidence"] = browser_evidence
    candidate["sensitive_values_included"] = False
    write_json_atomic(candidate_path, candidate)

    if args.prepare_only:
        write_outcomes()
        print(json.dumps({
            "status": runner.checkpoint["status"],
            "mode": FINAL_E2E_MODE,
            "product": candidate["product"],
            "live_source_verification": candidate["live_source_verification"],
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
    if args.confirm != FINAL_E2E_WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: exact --confirm value missing")
    try:
        report = runner.run_next_step()
    except Exception as error:
        write_outcomes()
        print(json.dumps({
            "status": runner.checkpoint["status"],
            "error": {"type": type(error).__name__, "message": str(error)},
        }, ensure_ascii=False, indent=2))
        return 2
    conclusion = write_outcomes()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    cursor = int(checkpoint["stage_cursor"])
    print(json.dumps({
        "status": report["status"],
        "product_number": item["product_number"],
        "shijiu_product_id": report.get("shijiu_product_id"),
        "last_verified_success_state": report.get("last_verified_success_state"),
        "next_stage": checkpoint["stages"][cursor] if cursor < len(checkpoint["stages"]) else None,
        "request_counts": report["request_counts"],
        "ui_read_retry_attempt_count": report["ui_read_retry_attempt_count"],
        "production_architecture_verified": conclusion["production_import_architecture_verified"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
