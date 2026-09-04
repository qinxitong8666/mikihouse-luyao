from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_canonical_create import (
    CANONICAL_CREATE_CONFIRMATION,
    CanonicalCreateRunner,
    load_single_candidate,
    load_verified_browser_credentials,
)
from mikihouse_luyao.shijiu_import import write_json_atomic
from mikihouse_luyao.shijiu_live_import import ShijiuLiveClient


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="One-product MIKIHOUSE validation against the verified Shijiu browser CREATE contract"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--confirm", default="")
    result.add_argument("--prepare-only", action="store_true")
    return result


def verify_source(item: dict) -> dict:
    live = fetch_product(item["product_number"], timeout=30, retries=2)
    expected = item["source_variants"][0]
    passed = (
        live.product_number == item["product_number"]
        and len(live.variants) == 1
        and live.variants[0].sku == expected["source_variant_sku"]
        and live.variants[0].tax_included_price_jpy == expected["tax_included_price_jpy"]
        and live.variants[0].in_stock
    )
    if not passed:
        raise RuntimeError("live MIKI HOUSE source verification differs from the frozen master")
    return {
        "passed": True,
        "product_number": live.product_number,
        "source_url": live.source_url,
        "variant_sku": live.variants[0].sku,
        "tax_included_price_jpy": live.variants[0].tax_included_price_jpy,
        "mini_program_price_jpy": expected["mini_program_price_jpy"],
        "available_for_sale": live.variants[0].in_stock,
        "currency": "JPY",
        "currency_conversion_applied": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    item, special, selection = load_single_candidate(
        ROOT / "output/storefront-master/master_catalog.json",
        ROOT / "special_skus_2026aw.csv",
        ROOT / "state/shijiu_mappings.json",
    )
    source_verification = verify_source(item)
    candidate_report = {
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "selection": selection,
        "product_number": item["product_number"],
        "source_product_id": item["source_product_id"],
        "backend_sku_codes": [row["backend_sku_code"] for row in item["source_variants"]],
        "variant_count": len(item["source_variants"]),
        "image_count": len(item["image_upload_plan"]),
        "fixed_target_category_id": 294884,
        "pdf_special_excluded": item["product_number"] in special,
        "previously_tested": False,
        "source_verification": source_verification,
        "write_executed": False,
    }
    candidate_path = ROOT / "deliverables/shijiu_import/canonical_create_candidate.json"
    write_json_atomic(candidate_path, candidate_report)
    if args.prepare_only:
        print(json.dumps({"status": "PREPARED_NO_TARGET_REQUESTS", **candidate_report}, ensure_ascii=False))
        return 0
    if args.confirm != CANONICAL_CREATE_CONFIRMATION:
        raise SystemExit("real create blocked: exact confirmation phrase missing")
    token, secret, browser_evidence = load_verified_browser_credentials(
        args.browser_private_dir,
        ROOT / "config/shijiu_native_create_contract.json",
    )
    client = ShijiuLiveClient(
        token,
        secret,
        write_confirmation=CANONICAL_CREATE_CONFIRMATION,
    )
    runner = CanonicalCreateRunner(
        client,
        item,
        special,
        selection,
        browser_evidence,
        ROOT / "state/shijiu_canonical_create_checkpoint.json",
        ROOT / "state/shijiu_mappings.json",
        ROOT / "deliverables/shijiu_import/canonical_create_validation_report.json",
        confirmation=args.confirm,
    )
    try:
        report = runner.run()
    except Exception as error:
        # Safe for terminal checkpoints: refresh sanitized evidence only. The
        # runner rejects before any request and its request list is empty.
        runner._persist()
        checkpoint = json.loads(
            (ROOT / "state/shijiu_canonical_create_checkpoint.json").read_text(encoding="utf-8")
        )
        candidate_report["write_executed"] = checkpoint.get("create_attempts", 0) > 0
        candidate_report["result"] = checkpoint.get("status")
        candidate_report["shijiu_product_id"] = checkpoint.get("shijiu_product_id")
        write_json_atomic(candidate_path, candidate_report)
        print(json.dumps({"status": "STOPPED", "error": str(error)}, ensure_ascii=False))
        return 2
    candidate_report["write_executed"] = True
    candidate_report["shijiu_product_id"] = report["shijiu_product_id"]
    write_json_atomic(candidate_path, candidate_report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
