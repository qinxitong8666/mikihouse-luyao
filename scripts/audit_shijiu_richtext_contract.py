from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.shijiu_complex_import import UiContextReadClient
from mikihouse_luyao.shijiu_import import write_json_atomic
from mikihouse_luyao.shijiu_richtext_contract import (
    build_read_only_richtext_sample_audit,
    current_contract_static_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WAWU_ROOT = Path("/private/tmp/wawu-product-sync-richtext-audit")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Strict read-only Shijiu rich-text contract audit")
    result.add_argument("--browser-private-dir", type=Path, required=True)
    result.add_argument("--wawu-root", type=Path, default=DEFAULT_WAWU_ROOT)
    result.add_argument("--sample-pages", type=int, default=32)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    legacy = json.loads(
        (ROOT / "deliverables/shijiu_import/legacy_reference_audit.json").read_text(
            encoding="utf-8"
        )
    )
    legacy_ids = {
        str(row.get("backend_product_id") or "")
        for row in legacy.get("sample_schemas") or []
        if str(row.get("backend_product_id") or "")
    }
    ui = UiContextReadClient(
        args.browser_private_dir,
        ROOT / "config/shijiu_native_create_contract.json",
    )
    report = build_read_only_richtext_sample_audit(
        ui,
        legacy_product_ids=legacy_ids,
        minimum_nonempty_samples=3,
        sampled_page_count=args.sample_pages,
    )
    report["static_contract_evidence"] = current_contract_static_evidence(
        ROOT,
        args.wawu_root,
    )
    output = ROOT / "deliverables/shijiu_import/richtext_contract_readonly_audit.json"
    write_json_atomic(output, report)
    print(json.dumps({
        "status": report["status"],
        "samples": report["scope"]["nonempty_good_details_samples_collected"],
        "request_counts": report["request_counts"],
        "output": str(output.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
