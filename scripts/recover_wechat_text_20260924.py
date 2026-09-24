#!/usr/bin/env python3
"""Audit by default; the fixed task confirmation permits one append-only run."""
import argparse
import json
from pathlib import Path
from mikihouse_luyao.wechat_text_recovery import recover_text_once

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    config = json.loads((ROOT / "config/wechat_favorite_runtime.json").read_text())
    result = recover_text_once(ROOT / "outputs/daily_quote/2026-09-24", config,
                              repository_root=ROOT, confirmation=args.confirm, audit_only=not args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
