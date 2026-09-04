from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.scraper import fetch_product
from mikihouse_luyao.shijiu_live_import import ShijiuLiveClient, client_from_env
from mikihouse_luyao.shijiu_minimal_probe import (
    PROBE_CONFIRMATION,
    MinimalCreateProbeRunner,
    load_probe_inputs,
)


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-candidate Shijiu minimal-create diagnostic"
    )
    parser.add_argument("--target-env-file", type=Path)
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="run source verification and complete Shijiu read-only preflight, then stop",
    )
    parser.add_argument(
        "--render-evidence-only",
        action="store_true",
        help="refresh reports from the existing checkpoint without credentials or network calls",
    )
    return parser


def online_source_verification(inputs: dict) -> dict:
    selected = inputs["selected_product"]
    live = fetch_product(selected["product_number"], timeout=30, retries=2)
    expected_variant = selected["variants"][0]
    live_skus = [variant.sku for variant in live.variants]
    live_prices = [variant.tax_included_price_jpy for variant in live.variants]
    return {
        "checked_product_url": live.source_url,
        "product_number": live.product_number,
        "name": live.name,
        "variant_skus": live_skus,
        "tax_included_prices_jpy": live_prices,
        "available_for_sale": [variant.in_stock for variant in live.variants],
        "main_image_url": live.main_image_url,
        "main_image_matches": live.main_image_url == selected["main_image"]["url"],
        "expected_mini_program_price_jpy": expected_variant[
            "mini_program_price_jpy"
        ],
        "currency": "JPY",
        "currency_conversion_applied": False,
        "passed": (
            live.product_number == selected["product_number"]
            and live.name == selected["name"]
            and live_skus == [expected_variant["sku"]]
            and live_prices == [expected_variant["tax_included_price_jpy"]]
            and all(variant.in_stock for variant in live.variants)
            and live.main_image_url == selected["main_image"]["url"]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.render_evidence_only and args.prepare_only:
        raise SystemExit("choose only one diagnostic mode")
    if not args.render_evidence_only and not args.target_env_file:
        raise SystemExit("--target-env-file is required outside evidence-only mode")
    if not args.prepare_only and not args.render_evidence_only and args.confirm != PROBE_CONFIRMATION:
        raise SystemExit("minimal create blocked: exact confirmation phrase missing")
    inputs = load_probe_inputs(
        ROOT / "output/storefront-master/master_catalog.json",
        ROOT / "special_skus_2026aw.csv",
        ROOT / "state/shijiu_mappings.json",
        ROOT / "config/shijiu_native_create_shape_fixture.json",
        ROOT / "state/shijiu_first_product_recovery_checkpoint.json",
        ROOT / "state/shijiu_first_live_batch_checkpoint.json",
    )
    client = (
        ShijiuLiveClient(
            "evidence-only-token",
            "evidence-only-secret",
            write_confirmation=PROBE_CONFIRMATION,
        )
        if args.render_evidence_only
        else client_from_env(
            args.target_env_file, write_confirmation=PROBE_CONFIRMATION
        )
    )
    runner = MinimalCreateProbeRunner(
        client,
        inputs,
        ROOT / "state/shijiu_minimal_create_probe_checkpoint.json",
        ROOT / "state/shijiu_mappings.json",
        ROOT / "deliverables/shijiu_import/minimal_create_probe_report.json",
        ROOT / "deliverables/shijiu_import/minimal_create_probe_candidate.json",
        ROOT / "deliverables/shijiu_import/minimal_create_payload_diff.json",
        ROOT / "deliverables/shijiu_import/minimal_create_probe_readback.json",
        confirmation=PROBE_CONFIRMATION,
    )
    if args.render_evidence_only:
        print(
            json.dumps(
                {
                    "status": "EVIDENCE_REFRESHED",
                    "checkpoint_state": runner.checkpoint["state"],
                    "network_requests": len(client.requests),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if runner.checkpoint.get("online_source_verification") is None:
        runner.record_online_source_verification(online_source_verification(inputs))
    try:
        report = runner.run_preflight_only() if args.prepare_only else runner.run()
    except Exception as error:
        print(
            json.dumps(
                {"status": "STOPPED", "error": str(error)}, ensure_ascii=False
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
