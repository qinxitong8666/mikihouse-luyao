from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_complex_import import (
    COMPLEX_WRITE_CONFIRMATION,
    ComplexLiveBatchRunner,
    build_next_20_plan,
    load_frozen_complex_items,
    load_complex_inputs,
    make_live_clients,
)
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state, now, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Fail-closed real validation of five representative complex MIKIHOUSE products"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--confirm", default="")
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--reconcile-only", action="store_true")
    return result


def verify_live_sources(items: list[dict]) -> list[dict]:
    results = []
    for item in items:
        live = fetch_product(item["product_number"], timeout=30, retries=2)
        expected = {
            row["source_variant_sku"]: row for row in item["source_variants"]
        }
        actual = {row.sku: row for row in live.variants}
        if set(actual) != set(expected):
            raise RuntimeError(f"live source SKU set drift: {item['product_number']}")
        for sku, source in expected.items():
            observed = actual[sku]
            if (
                observed.tax_included_price_jpy != source["tax_included_price_jpy"]
                or observed.in_stock != source["available_for_sale"]
                or observed.color != source["color"]
                or observed.size != source["size"]
                or observed.image_url != source["image_url"]
            ):
                raise RuntimeError(f"live source variant drift: {item['product_number']}/{sku}")
        results.append({
            "product_number": item["product_number"],
            "variant_count": len(actual),
            "all_skus_prices_stocks_specs_and_images_match_master": True,
            "verified_at": now(),
        })
    return results


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    master_path = ROOT / "output/storefront-master/master_catalog.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    selection_path = ROOT / "config/shijiu_complex_live_batch.json"
    if selection_path.exists():
        master = json.loads(master_path.read_text(encoding="utf-8"))
        special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        items = load_frozen_complex_items(master, special, category, selection)
    else:
        master, special, items, selection = load_complex_inputs(
            master_path,
            ROOT / "special_skus_2026aw.csv",
            mapping_path,
            category,
        )
        selection["master_catalog_sha256"] = __import__("hashlib").sha256(master_path.read_bytes()).hexdigest()
        write_json_atomic(selection_path, selection)
    runtime_selection = copy.deepcopy(selection)
    runtime_selection["live_source_verification"] = verify_live_sources(items)
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/complex_live_batch_candidates.json", runtime_selection
    )
    if args.prepare_only:
        print(json.dumps({
            "status": "PREPARED_NO_SHIJIU_REQUESTS",
            "products": runtime_selection["products"],
            "live_source_verification": runtime_selection["live_source_verification"],
        }, ensure_ascii=False, indent=2))
        return 0
    client, ui, browser_evidence = make_live_clients(
        args.browser_private_dir, ROOT / "config/shijiu_native_create_contract.json"
    )
    runtime_selection["canonical_browser_evidence"] = browser_evidence
    runner = ComplexLiveBatchRunner(
        client,
        ui,
        items,
        special,
        category,
        runtime_selection,
        checkpoint_path=ROOT / "state/shijiu_complex_live_batch_checkpoint.json",
        mapping_path=mapping_path,
        report_path=ROOT / "deliverables/shijiu_import/complex_live_batch_report.json",
        readbacks_path=ROOT / "deliverables/shijiu_import/complex_live_batch_readbacks.json",
        confirmation=args.confirm,
    )
    if args.reconcile_only:
        result = runner.reconcile_stopped_first_create()
        readiness = {
            "schema_version": 1,
            "generated_at": now(),
            "status": "BLOCKED_AFTER_FIRST_COMPLEX_CREATE_ANOMALY",
            "complex_validation_all_passed": False,
            "verified_product_count": 0,
            "next_batch_plan_generated": False,
            "next_batch_executed": False,
            "batch_frozen": True,
            "reconciliation": result,
            "requires_separate_investigation_and_authorization": True,
            "pdf_special_exclusion_count": len(special),
            "legacy_reference_touched": False,
            "sensitive_values_included": False,
        }
        write_json_atomic(
            ROOT / "deliverables/shijiu_import/complex_live_batch_readiness.json", readiness
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != COMPLEX_WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: exact --confirm value missing")
    try:
        report = runner.run()
    except Exception as error:
        print(json.dumps({"status": "STOPPED_ON_FIRST_ERROR", "error": str(error)}, ensure_ascii=False))
        return 2
    mapping = load_mapping_state(mapping_path)
    next_plan = build_next_20_plan(master, special, mapping, category)
    write_json_atomic(ROOT / "deliverables/shijiu_import/next_20_batch_plan.json", next_plan)
    readiness = {
        "schema_version": 1,
        "generated_at": now(),
        "status": "READY_FOR_SEPARATELY_AUTHORIZED_20_PRODUCT_BATCH",
        "complex_validation_all_passed": report["verified_product_count"] == 5,
        "verified_product_count": report["verified_product_count"],
        "verified_sku_count": report["verified_sku_count"],
        "next_batch_product_count": next_plan["product_count"],
        "next_batch_executed": False,
        "requires_separate_authorization": True,
        "pdf_special_exclusion_count": len(special),
        "legacy_reference_touched": False,
        "sensitive_values_included": False,
    }
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/complex_live_batch_readiness.json", readiness
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
