from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.shijiu_session_audit import (
    build_session_audit,
    write_session_audit,
)


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit Shijiu browser-session/native-save evidence without network or writes"
    )
    parser.add_argument("--target-env-file", type=Path, required=True)
    parser.add_argument("--native-template", type=Path, required=True)
    parser.add_argument("--native-capture-script", type=Path, required=True)
    parser.add_argument("--native-result", type=Path, required=True)
    parser.add_argument("--direct-loop-result", type=Path, required=True)
    parser.add_argument(
        "--wawu-reference-repo",
        type=Path,
        default=ROOT / "tmp/reference-wawu-product-sync",
    )
    parser.add_argument(
        "--mikihouse-probe-report",
        type=Path,
        default=ROOT / "deliverables/shijiu_import/minimal_create_probe_report.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "deliverables/shijiu_import/session_auth_audit.json",
    )
    parser.add_argument("--iab-shijiu-tab-count", type=int, default=0)
    parser.add_argument("--chrome-running", action="store_true")
    parser.add_argument("--chrome-extension-connected", action="store_true")
    parser.add_argument("--authenticated-shijiu-admin-visible", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    browser_evidence = {
        "inspection_mode": "visible_session_only_no_cookie_or_storage_access",
        "iab_open_shijiu_tab_count": args.iab_shijiu_tab_count,
        "chrome_running": args.chrome_running,
        "chrome_extension_connected": args.chrome_extension_connected,
        "authenticated_shijiu_admin_visible": args.authenticated_shijiu_admin_visible,
    }
    report = build_session_audit(
        env_path=args.target_env_file,
        native_template_path=args.native_template,
        capture_script_path=args.native_capture_script,
        native_result_path=args.native_result,
        direct_loop_result_path=args.direct_loop_result,
        wawu_repo=args.wawu_reference_repo,
        mikihouse_probe_report=args.mikihouse_probe_report,
        browser_evidence=browser_evidence,
    )
    write_session_audit(args.output, report)
    print(
        json.dumps(
            {
                "status": report["decision"]["state"],
                "output": str(args.output),
                "shijiu_requests": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
