#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.quote_assistant_authorization import (
    QuoteAssistantAuthorizationError,
    issue_one_time_authorization,
)


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue one private, short-lived quote assistant production permit")
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = issue_one_time_authorization(
            args.repository_root,
            confirmation=args.confirm,
        )
    except (QuoteAssistantAuthorizationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED_CLOSED", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
