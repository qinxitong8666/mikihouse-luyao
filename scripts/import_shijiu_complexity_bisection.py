from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_complexity_bisection import (
    BISECTION_MODE,
    BISECTION_WRITE_CONFIRMATION,
    ComplexityBisectionRunner,
    audit_shijiu_legacy_multisku_evidence,
    audit_wawu_multisku_evidence,
    build_bisection_diagnosis,
    build_orphan_asset_register,
    build_payload_scale_comparison,
    load_frozen_bisection_items,
    make_bisection_clients,
    select_bisection_batch,
)
from mikihouse_luyao.shijiu_import import (
    load_category_map,
    load_mapping_state,
    now,
    write_json_atomic,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Fail-closed two-product Shijiu CREATE complexity bisection"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--wawu-evidence", type=Path, required=True)
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--reconcile-only", action="store_true")
    result.add_argument("--finalize-reports-only", action="store_true")
    result.add_argument("--confirm", default="")
    return result


def verify_live_sources(items: list[dict]) -> list[dict]:
    results = []
    for item in items:
        live = fetch_product(item["product_number"], timeout=30, retries=2)
        expected = {row["source_variant_sku"]: row for row in item["source_variants"]}
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


def _write_analysis_documents(
    *,
    master: dict,
    special: set[str],
    category: dict,
    token: str,
    secret: str,
    wawu_evidence: Path,
) -> None:
    comparison = build_payload_scale_comparison(
        master,
        special,
        category,
        json.loads((ROOT / "state/shijiu_canonical_create_checkpoint.json").read_text()),
        json.loads((ROOT / "state/shijiu_complex_live_batch_checkpoint.json").read_text()),
        token=token,
        secret=secret,
    )
    comparison["known_shijiu_multisku_evidence"] = audit_wawu_multisku_evidence(
        wawu_evidence
    )
    comparison["known_shijiu_existing_product_scale"] = (
        audit_shijiu_legacy_multisku_evidence(
            ROOT / "deliverables/shijiu_import/legacy_reference_audit.json"
        )
    )
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/create_payload_scale_comparison.json",
        comparison,
    )
    orphan = build_orphan_asset_register(
        ROOT / "state/shijiu_complex_live_batch_checkpoint.json"
    )
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/orphan_cos_assets_13_9310_490.json",
        orphan,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    master_path = ROOT / "output/storefront-master/master_catalog.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    special_path = ROOT / "special_skus_2026aw.csv"
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    master = json.loads(master_path.read_text(encoding="utf-8"))
    special = set(read_product_numbers(special_path))
    mapping = load_mapping_state(mapping_path)
    selection_path = ROOT / "config/shijiu_complexity_bisection_batch.json"
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        items = load_frozen_bisection_items(master, special, category, selection)
    else:
        items, selection = select_bisection_batch(master, special, mapping, category)
        selection["master_catalog_sha256"] = hashlib.sha256(master_path.read_bytes()).hexdigest()
        write_json_atomic(selection_path, selection)

    client, ui, browser_evidence = make_bisection_clients(
        args.browser_private_dir, ROOT / "config/shijiu_native_create_contract.json"
    )
    _write_analysis_documents(
        master=master,
        special=special,
        category=category,
        token=client.token,
        secret=client.secret,
        wawu_evidence=args.wawu_evidence,
    )
    if args.finalize_reports_only:
        runner = ComplexityBisectionRunner(
            client,
            ui,
            items,
            special,
            category,
            selection,
            checkpoint_path=ROOT / "state/shijiu_complexity_bisection_checkpoint.json",
            mapping_path=mapping_path,
            report_path=ROOT / "deliverables/shijiu_import/complexity_bisection_report.json",
            readbacks_path=ROOT / "deliverables/shijiu_import/complexity_bisection_readbacks.json",
            confirmation="",
        )
        runner._persist()
        checkpoint = json.loads(
            (ROOT / "state/shijiu_complexity_bisection_checkpoint.json").read_text()
        )
        scale_comparison = json.loads(
            (ROOT / "deliverables/shijiu_import/create_payload_scale_comparison.json").read_text()
        )
        diagnosis = build_bisection_diagnosis(
            checkpoint,
            items=items,
            token=client.token,
            secret=client.secret,
            scale_comparison=scale_comparison,
        )
        existing = ROOT / "deliverables/shijiu_import/complexity_bisection_diagnosis.json"
        if existing.exists():
            prior = json.loads(existing.read_text())
            for key in (
                "post_stop_read_only_reconciliation",
                "second_probe_permanently_not_executed_after_initial_readback_failure",
                "target_mutations_in_reconciliation",
            ):
                if key in prior:
                    diagnosis[key] = prior[key]
        write_json_atomic(existing, diagnosis)
        print(json.dumps({
            "status": "REPORTS_FINALIZED_NO_NETWORK_REQUESTS",
            "diagnosis": diagnosis,
        }, ensure_ascii=False, indent=2))
        return 0
    runtime_selection = copy.deepcopy(selection)
    runtime_selection["live_source_verification"] = verify_live_sources(items)
    runtime_selection["canonical_browser_evidence"] = browser_evidence
    runtime_selection["sensitive_values_included"] = False
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/complexity_bisection_candidates.json",
        runtime_selection,
    )
    if args.prepare_only:
        print(json.dumps({
            "status": "PREPARED_NO_SHIJIU_REQUESTS",
            "mode": BISECTION_MODE,
            "products": runtime_selection["products"],
            "live_source_verification": runtime_selection["live_source_verification"],
        }, ensure_ascii=False, indent=2))
        return 0
    runner = ComplexityBisectionRunner(
        client,
        ui,
        items,
        special,
        category,
        runtime_selection,
        checkpoint_path=ROOT / "state/shijiu_complexity_bisection_checkpoint.json",
        mapping_path=mapping_path,
        report_path=ROOT / "deliverables/shijiu_import/complexity_bisection_report.json",
        readbacks_path=ROOT / "deliverables/shijiu_import/complexity_bisection_readbacks.json",
        confirmation=args.confirm,
    )
    if args.reconcile_only:
        result = runner.reconcile_stopped_first_create()
        checkpoint = json.loads(
            (ROOT / "state/shijiu_complexity_bisection_checkpoint.json").read_text()
        )
        scale_comparison = json.loads(
            (ROOT / "deliverables/shijiu_import/create_payload_scale_comparison.json").read_text()
        )
        diagnosis = build_bisection_diagnosis(
            checkpoint,
            items=items,
            token=client.token,
            secret=client.secret,
            scale_comparison=scale_comparison,
        )
        diagnosis["post_stop_read_only_reconciliation"] = result
        diagnosis["second_probe_permanently_not_executed_after_initial_readback_failure"] = True
        diagnosis["target_mutations_in_reconciliation"] = 0
        write_json_atomic(
            ROOT / "deliverables/shijiu_import/complexity_bisection_diagnosis.json",
            diagnosis,
        )
        print(json.dumps({"reconciliation": result, "diagnosis": diagnosis}, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != BISECTION_WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: exact --confirm value missing")
    try:
        report = runner.run()
        exit_code = 0
    except Exception as error:
        report = {
            "status": "STOPPED_ON_FIRST_ERROR",
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        exit_code = 2
    checkpoint = json.loads(
        (ROOT / "state/shijiu_complexity_bisection_checkpoint.json").read_text()
    )
    scale_comparison = json.loads(
        (ROOT / "deliverables/shijiu_import/create_payload_scale_comparison.json").read_text()
    )
    diagnosis = build_bisection_diagnosis(
        checkpoint,
        items=items,
        token=client.token,
        secret=client.secret,
        scale_comparison=scale_comparison,
    )
    diagnosis["maximum_real_create_requests"] = 2
    diagnosis["actual_create_requests"] = sum(
        row.get("path") == "/shopapi/Goods/newAddGood"
        for row in checkpoint.get("request_ledger") or []
    )
    diagnosis["original_complex_batch_checkpoint_sha256"] = hashlib.sha256(
        (ROOT / "state/shijiu_complex_live_batch_checkpoint.json").read_bytes()
    ).hexdigest()
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/complexity_bisection_diagnosis.json",
        diagnosis,
    )
    print(json.dumps({"report": report, "diagnosis": diagnosis}, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
