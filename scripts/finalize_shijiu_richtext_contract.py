from __future__ import annotations

import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state, write_json_atomic
from mikihouse_luyao.shijiu_production_architecture_verification import select_final_e2e_candidate
from mikihouse_luyao.shijiu_richtext_contract import build_richtext_contract_comparison
from mikihouse_luyao.shijiu_richtext_contract import current_contract_static_evidence


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deliverables/shijiu_import"


def read(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def main() -> int:
    read_only_audit = read("deliverables/shijiu_import/richtext_contract_readonly_audit.json")
    read_only_audit["static_contract_evidence"] = current_contract_static_evidence(
        ROOT,
        Path("/private/tmp/wawu-product-sync-richtext-audit"),
    )
    write_json_atomic(BASE / "richtext_contract_readonly_audit.json", read_only_audit)
    comparison = build_richtext_contract_comparison(
        create_capture=read("deliverables/shijiu_import/richtext_native_test_create_capture.json"),
        text_edit_capture=read("deliverables/shijiu_import/richtext_native_text_edit_capture.json"),
        image_edit_capture=read("deliverables/shijiu_import/richtext_native_image_edit_capture.json"),
        failed_miki_checkpoint=read("state/shijiu_production_architecture_verification_checkpoint.json"),
        failed_miki_forensics=read("deliverables/shijiu_import/production_architecture_final_html_forensics.json"),
        canonical_create_contract=read("config/shijiu_native_create_contract.json"),
        read_only_audit=read_only_audit,
    )
    write_json_atomic(BASE / "richtext_contract_comparison.json", comparison)

    master = read("output/storefront-master/master_catalog.json")
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    mapping = load_mapping_state(ROOT / "state/shijiu_mappings.json")
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    _, selection = select_final_e2e_candidate(ROOT, master, special, mapping, category)
    readiness = {
        "schema_version": 1,
        "status": "READY_FOR_LAST_ONE_PRODUCT_MIKIHOUSE_E2E_VALIDATION",
        "production_contract": {
            "good_details": "TEXT_OR_LIGHT_HTML_NO_IMAGE_OR_URL_MAX_1024",
            "detail_images": "GOOD_DETAIL_PICS_ORDERED_SHIJIU_COS_URLS",
            "final_image_html_update_stage": "REMOVED",
            "save_path": "NATIVE_FULL_PAYLOAD_NEW_ADD_GOOD",
            "readback": "UI_CONTEXT_EXACT_GOOD_NAME_THEN_GET_FORMAT_INFO",
        },
        "frozen_next_product": selection["product"],
        "planned_stages": selection["stages"],
        "execution_authorized": False,
        "maximum_future_product_create_requests_after_new_authorization": 1,
        "pdf_special_exclusion_count": len(special),
        "pdf_special_selected": selection["product"]["product_number"] in special,
        "legacy_286_mode": "READ_ONLY_UNCHANGED",
        "historical_frozen_products_retried": False,
        "shijiu_sku_id_policy": "nullable",
        "bulk_20_generated_or_executed": False,
        "current_round": {
            "mikihouse_write_requests": 0,
            "non_mikihouse_disposable_test_product_create_requests": 1,
            "non_mikihouse_disposable_test_product_edit_requests": 2,
        },
        "sensitive_values_included": False,
    }
    write_json_atomic(BASE / "richtext_contract_readiness.json", readiness)
    print(json.dumps({
        "status": comparison["status"],
        "next_status": readiness["status"],
        "next_product": selection["product"]["product_number"],
        "execution_authorized": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
