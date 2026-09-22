#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.daily_quote_text import render_compact_text_quote, text_stats
from mikihouse_luyao.wechat_favorite_runtime import text_fingerprint, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare non-mutating WeChat runtime evidence")
    parser.add_argument("--daily-dir", type=Path, required=True)
    args = parser.parse_args()
    daily = args.daily_dir.resolve()
    manifest = json.loads((daily / "daily_quote_manifest.json").read_text(encoding="utf-8"))
    quote_date = str(manifest["quote_date"])
    original_path = daily / f"MIKIHOUSE_{quote_date}_文字报价.txt"
    original = original_path.read_text(encoding="utf-8")
    compact = render_compact_text_quote(manifest)
    compact_stats = text_stats(compact, len(manifest["products"]))
    original_stats = text_stats(original, len(manifest["products"]))
    report = {
        "schema_version": 1,
        "status": "EXPERIMENT_ONLY_NOT_PRODUCTION_FORMAT",
        "quote_date": quote_date,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "original": {
            **original_stats,
            "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        },
        "lossless_compact": {
            **compact_stats,
            "sha256": hashlib.sha256(compact.encode("utf-8")).hexdigest(),
        },
        "savings": {
            "unicode_characters": len(original) - len(compact),
            "utf8_bytes": len(original.encode("utf-8")) - len(compact.encode("utf-8")),
            "character_percent": round((len(original) - len(compact)) * 100 / len(original), 2),
        },
        "lossless_rules": [
            "product number, CNY price, color, and in-stock size are always retained",
            "footwear cm suffix is declared once in the category header",
            "only exact consecutive 0.5cm sequences are represented as an explicit range",
            "colors are merged only when their complete size sequence and price are identical",
            "production text remains unchanged until runtime capacity evidence requires a decision",
        ],
    }
    write_json(daily / "wechat_compact_text_experiment.json", report)
    capacity = {
        "schema_version": 1,
        "status": "WAITING_RUNTIME",
        "quote_date": quote_date,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "source_text": text_fingerprint(original),
        "planned_body_character_levels": [10000, 30000, 60000, 90000, len(original)],
        "stop_on_first_failure": True,
        "delete_test_note": False,
        "wechat_mutation_scope": "NEW_EXPLICIT_MIKIHOUSE_TEST_FAVORITES_ONLY",
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }
    write_json(daily / "wechat_runtime_capacity_audit.json", capacity)
    print(json.dumps({"compact_report": report, "capacity": capacity}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
