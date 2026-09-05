from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from .csv_input import read_product_numbers
from .shijiu_complex_import import UiContextReadClient
from .shijiu_duplicate_name_identity import (
    AMBIGUOUS,
    NOT_FOUND,
    UNIQUE_STRONG_MATCH,
    analyze_duplicate_names,
    resolve_duplicate_good_name_candidates,
)
from .shijiu_import import (
    content_sha256,
    load_category_map,
    load_mapping_state,
    map_product_to_shijiu,
    now,
    write_json_atomic,
)


VALIDATION_GROUP_NAMES = (
    "ウォーターベビーサンダル",
    "フーディー",
    "セーター",
    "スタイ",
    "セカンドベビーシューズ",
    "カバーオール",
    "トレーナー",
    "ワンピース",
    "パンツ",
    "半袖Ｔシャツ",
)


class DuplicateNameReadonlyAuditError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise DuplicateNameReadonlyAuditError(f"JSON root must be an object: {path}")
    return value


def _normalize_ui_detail(detail: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(detail)
    if str(normalized.get("code")) == "200" and str(normalized.get("msg") or "").casefold() == "success":
        normalized["code"] = 1
    return normalized


def _safe_resolution(
    resolution: dict[str, Any],
    *,
    expected_mapped_product_id: str | None,
) -> dict[str, Any]:
    safe = copy.deepcopy(resolution)
    for observation in safe.get("candidate_observations") or []:
        product_id = str(observation.pop("product_id", ""))
        observation["product_id_sha256"] = hashlib.sha256(product_id.encode()).hexdigest()
        observation["is_expected_mapped_product_id"] = (
            bool(expected_mapped_product_id) and product_id == expected_mapped_product_id
        )
    strong_ids = [str(value) for value in safe.pop("strong_match_product_ids", [])]
    safe["strong_match_product_id_sha256s"] = [
        hashlib.sha256(value.encode()).hexdigest() for value in strong_ids
    ]
    product_id = safe.pop("shijiu_product_id", None)
    safe["shijiu_product_id"] = (
        str(product_id)
        if expected_mapped_product_id and str(product_id) == expected_mapped_product_id
        else None
    )
    safe["unmatched_candidate_product_ids_included"] = False
    return safe


def run_readonly_validation(
    *,
    ui: UiContextReadClient,
    stable_catalog: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    group_names: tuple[str, ...] = VALIDATION_GROUP_NAMES,
) -> dict[str, Any]:
    if len(special) != 351:
        raise DuplicateNameReadonlyAuditError("PDF special manifest must contain exactly 351 products")
    products = [row for row in stable_catalog.get("products") or [] if row.get("active", True)]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for name in group_names:
        rows = [row for row in products if str(row.get("name") or "").strip() == name]
        if len(rows) < 2:
            raise DuplicateNameReadonlyAuditError(f"selected validation name is not duplicated: {name}")
        by_name[name] = rows

    groups = []
    mapped_unique_count = 0
    unmapped_not_found_count = 0
    unexpected_outcomes = []
    for name in group_names:
        candidates, query_evidence = ui.exact_name_candidates(name)
        detail_by_id = {
            str(row.get("id") or row.get("good_id") or row.get("goods_id")): _normalize_ui_detail(
                ui.product_detail(str(row.get("id") or row.get("good_id") or row.get("goods_id")))
            )
            for row in candidates
            if str(row.get("id") or row.get("good_id") or row.get("goods_id"))
        }
        product_results = []
        for product in sorted(by_name[name], key=lambda row: str(row["product_number"])):
            number = str(product["product_number"])
            mapped = (mapping.get("products") or {}).get(number) or {}
            mapped_id = (
                str(mapped["shijiu_product_id"])
                if mapped.get("source") == "MIKIHOUSE"
                and mapped.get("shijiu_product_id") not in (None, "")
                else None
            )
            item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
            if not item.get("publish_ready"):
                raise DuplicateNameReadonlyAuditError(
                    f"selected read-only validation product is not publish-ready: {number}"
                )
            resolution = resolve_duplicate_good_name_candidates(
                good_name=name,
                sku_info=item["shijiu_payload_preview"]["sku_info"],
                candidate_rows=candidates,
                detail_by_product_id=detail_by_id,
                category_id=294884,
            )
            if mapped_id:
                passed = (
                    resolution["status"] == UNIQUE_STRONG_MATCH
                    and str(resolution["shijiu_product_id"]) == mapped_id
                )
                expected_outcome = UNIQUE_STRONG_MATCH
                mapped_unique_count += int(passed)
            else:
                passed = resolution["status"] == NOT_FOUND
                expected_outcome = NOT_FOUND
                unmapped_not_found_count += int(passed)
            if not passed:
                unexpected_outcomes.append({
                    "good_name": name,
                    "product_number": number,
                    "mapped": bool(mapped_id),
                    "expected_outcome": expected_outcome,
                    "actual_outcome": resolution["status"],
                })
            product_results.append({
                "product_number": number,
                "mapped_before_audit": bool(mapped_id),
                "expected_outcome": expected_outcome,
                "passed": passed,
                "resolution": _safe_resolution(
                    resolution,
                    expected_mapped_product_id=mapped_id,
                ),
            })
        groups.append({
            "good_name": name,
            "source_product_count": len(by_name[name]),
            "source_variant_count_range": [
                min(len(row.get("variants") or []) for row in by_name[name]),
                max(len(row.get("variants") or []) for row in by_name[name]),
            ],
            "target_exact_name_candidate_count": len(candidates),
            "target_candidate_detail_read_count": len(detail_by_id),
            "ui_query_evidence": query_evidence,
            "product_results": product_results,
        })
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": (
            "READ_ONLY_DUPLICATE_GOOD_NAME_VALIDATION_PASSED"
            if not unexpected_outcomes
            else "READ_ONLY_DUPLICATE_GOOD_NAME_VALIDATION_FAILED_CLOSED"
        ),
        "mode": "SHIJIU_READ_ONLY",
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "selected_group_count": len(groups),
        "selected_groups": groups,
        "mapped_unique_strong_match_count": mapped_unique_count,
        "unmapped_not_found_count": unmapped_not_found_count,
        "unexpected_outcome_count": len(unexpected_outcomes),
        "unexpected_outcomes": unexpected_outcomes,
        "resolver_contract": {
            "good_name_role": "CANDIDATE_SCOPE_ONLY_NOT_BINDING_PROOF",
            "primary_identity": "EXACT_COMPLETE_BACKEND_SKU_CODE_SET",
            "accepted_binding_outcome": UNIQUE_STRONG_MATCH,
            "zero_match_outcome": NOT_FOUND,
            "multiple_match_outcome": AMBIGUOUS,
            "shijiu_sku_id": None,
        },
        "request_ledger": ui.requests,
        "request_ledger_sha256": content_sha256(ui.requests),
        "safety": {
            "shijiu_read_requests": len(ui.requests),
            "allowed_paths": ["/shopapi/Goods/index", "/shopapi/goods/getFormatInfo"],
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "shijiu_shelf_price_inventory_writes": 0,
            "writer_mutex_evidence_generated": False,
            "mapping_modified": False,
            "legacy_products_modified": 0,
            "sensitive_values_included": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate duplicate good_name identity using Shijiu read-only UI context"
    )
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument(
        "--stable",
        type=Path,
        default=Path("deliverables/storefront_stable_catalog/stable_catalog.json.gz"),
    )
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--mapping", type=Path, default=Path("state/shijiu_mappings.json"))
    parser.add_argument("--category", type=Path, default=Path("config/shijiu_category_map.json"))
    parser.add_argument(
        "--canonical-contract",
        type=Path,
        default=Path("config/shijiu_native_create_contract.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deliverables/shijiu_initialization/duplicate_good_name_shijiu_readonly_validation.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stable = _read_json(args.stable)
    special = set(read_product_numbers(args.special))
    mapping = load_mapping_state(args.mapping)
    category = load_category_map(args.category)
    ui = UiContextReadClient(args.private_dir, args.canonical_contract)
    report = run_readonly_validation(
        ui=ui,
        stable_catalog=stable,
        special=special,
        mapping=mapping,
        category=category,
    )
    write_json_atomic(args.output, report)
    print(json.dumps({
        "status": report["status"],
        "selected_group_count": report["selected_group_count"],
        "shijiu_read_requests": report["safety"]["shijiu_read_requests"],
        "shijiu_mutation_requests": 0,
    }, ensure_ascii=False, indent=2))
    return 0 if report["unexpected_outcome_count"] == 0 else 2
