from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state, now, write_json_atomic
from mikihouse_luyao.shijiu_staged_media_import import (
    MODE,
    WRITE_CONFIRMATION,
    StagedMediaRunner,
    build_capacity_conclusion,
    build_plan_validation,
    load_frozen_candidate,
    make_clients,
    select_staged_media_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Advance one fail-closed Shijiu staged rich-media mutation"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--next-step", action="store_true")
    result.add_argument("--readonly-confirm-frozen", action="store_true")
    result.add_argument("--confirm", default="")
    return result


def verify_live_source(item: dict) -> dict:
    live = fetch_product(item["product_number"], timeout=30, retries=2)
    expected = {row["source_variant_sku"]: row for row in item["source_variants"]}
    actual = {row.sku: row for row in live.variants}
    if set(actual) != set(expected):
        raise RuntimeError("live source SKU set drift for staged rich-media candidate")
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
    modes = [args.prepare_only, args.next_step, args.readonly_confirm_frozen]
    if sum(bool(value) for value in modes) != 1:
        raise SystemExit(
            "select exactly one of --prepare-only, --next-step or --readonly-confirm-frozen"
        )
    master_path = ROOT / "output/storefront-master/master_catalog.json"
    special_path = ROOT / "special_skus_2026aw.csv"
    category_path = ROOT / "config/shijiu_category_map.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    selection_path = ROOT / "config/shijiu_staged_rich_media_single.json"
    checkpoint_path = ROOT / "state/shijiu_staged_rich_media_single_checkpoint.json"
    report_path = ROOT / "deliverables/shijiu_import/staged_rich_media_validation_report.json"
    readbacks_path = ROOT / "deliverables/shijiu_import/staged_rich_media_validation_readbacks.json"
    candidate_path = ROOT / "deliverables/shijiu_import/staged_rich_media_candidate.json"
    contract_path = ROOT / "config/shijiu_native_create_contract.json"
    plan_path = ROOT / "deliverables/shijiu_import/staged_rich_media_update_plan.json"
    conclusion_path = ROOT / "deliverables/shijiu_import/staged_rich_media_capacity_conclusion.json"

    master = json.loads(master_path.read_text(encoding="utf-8"))
    special = set(read_product_numbers(special_path))
    category = load_category_map(category_path)
    mapping = load_mapping_state(mapping_path)
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        item = load_frozen_candidate(ROOT, master, special, mapping, category, selection)
    else:
        item, selection = select_staged_media_candidate(ROOT, master, special, mapping, category)
        selection["master_catalog_sha256"] = hashlib.sha256(master_path.read_bytes()).hexdigest()
        write_json_atomic(selection_path, selection)

    client, ui, browser_evidence = make_clients(args.browser_private_dir, contract_path)
    candidate = copy.deepcopy(selection)
    candidate["live_source_verification"] = verify_live_source(item)
    candidate["canonical_browser_evidence"] = browser_evidence
    candidate["sensitive_values_included"] = False
    write_json_atomic(candidate_path, candidate)

    runner = StagedMediaRunner(
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

    def write_outcomes() -> None:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        original_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        write_json_atomic(conclusion_path, build_capacity_conclusion(checkpoint))
        write_json_atomic(plan_path, build_plan_validation(original_plan, selection, checkpoint))

    if args.prepare_only:
        write_outcomes()
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        cursor = int(checkpoint["stage_cursor"])
        print(json.dumps({
            "status": (
                "PREPARED_NO_SHIJIU_REQUESTS"
                if checkpoint["status"] == "READY_FOR_CREATE"
                else checkpoint["status"]
            ),
            "mode": MODE,
            "product": candidate["product"],
            "stage_count": len(candidate["stages"]),
            "next_stage": (
                checkpoint["stages"][cursor]
                if checkpoint["status"] not in {"FROZEN_ON_FIRST_ANOMALY", "COMPLETED"}
                and cursor < len(checkpoint["stages"])
                else None
            ),
            "live_source_verification": candidate["live_source_verification"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.readonly_confirm_frozen:
        result = runner.confirm_frozen_state_read_only()
        write_outcomes()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: exact --confirm value missing")
    try:
        report = runner.run_next_step()
    except Exception as error:
        write_outcomes()
        print(json.dumps({
            "status": "FROZEN_ON_FIRST_ANOMALY",
            "error": {"type": type(error).__name__, "message": str(error)},
        }, ensure_ascii=False, indent=2))
        return 2
    write_outcomes()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    cursor = int(checkpoint["stage_cursor"])
    print(json.dumps({
        "status": report["status"],
        "product_number": item["product_number"],
        "shijiu_product_id": report.get("shijiu_product_id"),
        "last_verified_success_state": report.get("last_verified_success_state"),
        "next_stage": checkpoint["stages"][cursor] if cursor < len(checkpoint["stages"]) else None,
        "request_counts": report["request_counts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
