from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_high_sku_probe import (
    HIGH_SKU_MODE,
    HIGH_SKU_WRITE_CONFIRMATION,
    HighSkuProbeRunner,
    build_high_sku_diagnosis,
    build_high_sku_selection,
    build_staged_rich_media_plan,
    load_frozen_high_sku_item,
    make_high_sku_clients,
)
from mikihouse_luyao.shijiu_import import load_category_map, now, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Independent one-product Shijiu 14-SKU real CREATE validation"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--confirm", default="")
    return result


def verify_live_source(item: dict) -> dict:
    live = fetch_product(item["product_number"], timeout=30, retries=2)
    expected = {row["source_variant_sku"]: row for row in item["source_variants"]}
    actual = {row.sku: row for row in live.variants}
    if set(actual) != set(expected):
        raise RuntimeError("live source SKU set drift for fixed high-SKU probe")
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
        "all_14_skus_prices_stocks_colors_sizes_and_images_match_master": len(actual) == 14,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    master_path = ROOT / "output/storefront-master/master_catalog.json"
    audit_path = ROOT / "deliverables/shijiu_import/rich_media_capacity_empirical_audit.json"
    selection_path = ROOT / "config/shijiu_high_sku_14_probe.json"
    checkpoint_path = ROOT / "state/shijiu_high_sku_14_probe_checkpoint.json"
    report_path = ROOT / "deliverables/shijiu_import/high_sku_14_probe_report.json"
    readbacks_path = ROOT / "deliverables/shijiu_import/high_sku_14_probe_readbacks.json"
    diagnosis_path = ROOT / "deliverables/shijiu_import/high_sku_14_probe_diagnosis.json"
    staged_plan_path = ROOT / "deliverables/shijiu_import/staged_rich_media_update_plan.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    master = json.loads(master_path.read_text(encoding="utf-8"))
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        items = load_frozen_high_sku_item(
            ROOT, master, special, category, selection, audit_path
        )
    else:
        items, selection = build_high_sku_selection(
            ROOT, master, special, category, audit_path
        )
        selection["master_catalog_sha256"] = hashlib.sha256(master_path.read_bytes()).hexdigest()
        write_json_atomic(selection_path, selection)
    client, ui, browser_evidence = make_high_sku_clients(
        args.browser_private_dir,
        ROOT / "config/shijiu_native_create_contract.json",
    )
    runtime_selection = copy.deepcopy(selection)
    runtime_selection["live_source_verification"] = verify_live_source(items[0])
    runtime_selection["canonical_browser_evidence"] = browser_evidence
    runtime_selection["sensitive_values_included"] = False
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/high_sku_14_probe_candidate.json",
        runtime_selection,
    )
    if args.prepare_only:
        print(json.dumps({
            "status": "PREPARED_NO_SHIJIU_REQUESTS",
            "mode": HIGH_SKU_MODE,
            "product": runtime_selection["products"][0],
            "live_source_verification": runtime_selection["live_source_verification"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != HIGH_SKU_WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: exact --confirm value missing")
    runner = HighSkuProbeRunner(
        client,
        ui,
        items,
        special,
        category,
        runtime_selection,
        checkpoint_path=checkpoint_path,
        mapping_path=mapping_path,
        report_path=report_path,
        readbacks_path=readbacks_path,
        confirmation=args.confirm,
        root=ROOT,
        capacity_audit_path=audit_path,
    )
    try:
        report = runner.run()
        exit_code = 0
    except Exception as error:
        report = {
            "status": "STOPPED_ON_FIRST_ERROR",
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        exit_code = 2
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    audit_sha256 = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    diagnosis = build_high_sku_diagnosis(checkpoint, audit_sha256)
    diagnosis["actual_product_create_requests"] = sum(
        row.get("path") == "/shopapi/Goods/newAddGood"
        for row in checkpoint.get("request_ledger") or []
    )
    diagnosis["maximum_product_create_requests"] = 1
    write_json_atomic(diagnosis_path, diagnosis)
    if diagnosis["fourteen_sku_scale_passed"]:
        write_json_atomic(staged_plan_path, build_staged_rich_media_plan())
    print(json.dumps({"report": report, "diagnosis": diagnosis}, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
