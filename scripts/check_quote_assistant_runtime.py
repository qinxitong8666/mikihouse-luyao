#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.quote_assistant_runtime import check_quote_assistant_runtime


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check MIKI HOUSE quote assistant local runtime")
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    result = check_quote_assistant_runtime(args.repository_root)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
