from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.shijiu_capacity_audit import build_capacity_audit_report
from mikihouse_luyao.shijiu_complex_import import UiContextReadClient
from mikihouse_luyao.shijiu_high_sku_probe import build_historical_payload_rows
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Strict read-only Shijiu rich-media empirical capacity audit"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    master = json.loads(
        (ROOT / "output/storefront-master/master_catalog.json").read_text(encoding="utf-8")
    )
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    mapping = load_mapping_state(ROOT / "state/shijiu_mappings.json")
    legacy = json.loads(
        (ROOT / "deliverables/shijiu_import/legacy_reference_audit.json").read_text(
            encoding="utf-8"
        )
    )
    readiness = json.loads(
        (ROOT / "deliverables/shijiu_import/browser_exact_capture_readiness.json").read_text(
            encoding="utf-8"
        )
    )
    ui = UiContextReadClient(
        args.browser_private_dir,
        ROOT / "config/shijiu_native_create_contract.json",
    )
    historical_rows = build_historical_payload_rows(
        ROOT,
        master,
        special,
        category,
        token=ui.query_token,
        secret=ui.base_form["secret"],
    )
    legacy_ids = {
        str(row.get("backend_product_id") or "")
        for row in legacy.get("sample_schemas") or []
        if str(row.get("backend_product_id") or "")
    }
    test_id = str(
        ((readiness.get("current_capture") or {}).get("readback") or {}).get("product_id")
        or ""
    )
    if not test_id:
        raise RuntimeError("browser-exact non-MIKIHOUSE test product ID is unavailable")
    mapped_ids = {
        str(row.get("shijiu_product_id"))
        for row in (mapping.get("products") or {}).values()
        if row.get("shijiu_product_id") not in (None, "")
    }
    report = build_capacity_audit_report(
        ui,
        legacy_product_ids=legacy_ids,
        non_miki_test_product_ids={test_id},
        mapped_mikihouse_product_ids=mapped_ids,
        historical_payload_rows=historical_rows,
    )
    write_json_atomic(
        ROOT / "deliverables/shijiu_import/rich_media_capacity_empirical_audit.json",
        report,
    )
    print(json.dumps({
        "status": report["status"],
        "unique_detail_product_count": report["scope"]["unique_detail_product_count"],
        "request_counts": report["request_counts"],
        "server_hard_limit": report["interpretation"]["server_hard_limit"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
