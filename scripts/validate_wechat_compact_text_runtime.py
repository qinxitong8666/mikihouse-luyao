#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from mikihouse_luyao.daily_quote_text import render_compact_text_quote, text_stats
from mikihouse_luyao.wechat_favorite_runtime import (
    MacWeChatFavoriteSink,
    WeChatRuntimeError,
    get_windows,
    read_note_text_until_match,
    select_target_process,
    text_fingerprint,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate one new disposable compact-text WeChat Favorite"
    )
    parser.add_argument("--daily-dir", type=Path, required=True)
    parser.add_argument("--chunk-chars", type=int, default=4000)
    parser.add_argument(
        "--reuse-existing-blank-test-note",
        action="store_true",
        help="Reuse one manually verified empty 笔记 window after a create-menu transport error.",
    )
    parser.add_argument(
        "--resume-proven-chunks",
        type=int,
        default=0,
        help="Resume only after this many leading chunks have been strongly read back.",
    )
    parser.add_argument(
        "--finalize-open-reopened-note",
        action="store_true",
        help="Read-only finalization after an exact Favorites search reopened the saved test note.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "wechat_favorite_runtime.json",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    daily_dir = args.daily_dir.resolve()
    manifest = json.loads((daily_dir / "daily_quote_manifest.json").read_text(encoding="utf-8"))
    compact = render_compact_text_quote(manifest)
    quote_date = str(manifest["quote_date"])
    title = f"MIKIHOUSE_TEST_{quote_date}_COMPACT_{len(compact)}"
    payload = {
        "schema_version": 1,
        "mode": "RUNTIME_TEST",
        "real_wechat_write_enabled": True,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "quote_date": quote_date,
        "favorite_kind": "TEXT_COMPACT_CAPACITY_TEST",
        "title": title,
        "body": compact,
        "attachments": [],
    }
    report_path = args.report or (
        daily_dir / "wechat_compact_text_runtime_validation.json"
    )
    runtime_config = json.loads(args.config.read_text(encoding="utf-8"))
    sink = MacWeChatFavoriteSink(runtime_config)
    try:
        if args.finalize_open_reopened_note:
            if args.resume_proven_chunks or args.reuse_existing_blank_test_note:
                raise WeChatRuntimeError("finalize mode cannot be combined with a write/resume mode")
            process = select_target_process()
            candidates = [
                row for row in get_windows(int(process["pid"]))
                if row.title and title.startswith(row.title) and row.title != "笔记"
            ]
            if len(candidates) != 1:
                raise WeChatRuntimeError(
                    f"expected one manually reopened compact test note, got {len(candidates)}"
                )
            expected_text = f"{title}\n{compact}".rstrip() + "\n"
            actual, readback, comparison = read_note_text_until_match(
                int(process["pid"]),
                str(process["bundle_id"]),
                candidates[0],
                expected_text,
            )
            if not comparison["normalized_hash_match"] or comparison["truncated"]:
                raise WeChatRuntimeError("reopened compact note final hash verification failed")
            evidence = {
                "status": "PASS",
                "target_process": process,
                "manual_workflow": {
                    "saved_closed": True,
                    "exact_title_search_candidate_count": 1,
                    "unique_search_reopened": True,
                },
                "readback": readback,
                "comparison": comparison,
                "actual": text_fingerprint(actual),
            }
        elif args.resume_proven_chunks:
            if args.reuse_existing_blank_test_note:
                raise WeChatRuntimeError("resume and blank-note reuse are mutually exclusive")
            evidence = sink.resume_test_note(
                payload,
                proven_chunk_count=args.resume_proven_chunks,
                chunk_chars=args.chunk_chars,
            )
        else:
            evidence = sink.save(
                payload,
                production=False,
                chunk_chars=args.chunk_chars,
                reuse_existing_blank_test_note=args.reuse_existing_blank_test_note,
            )
    except WeChatRuntimeError as exc:
        write_json(report_path, {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "FAILED_STOPPED",
            "quote_date": quote_date,
            "source_manifest_sha256": manifest["manifest_sha256"],
            "test_note_title": title,
            "body": text_fingerprint(compact),
            "stats": text_stats(compact, len(manifest["products"])),
            "chunk_character_limit": args.chunk_chars,
            "error": str(exc),
            "automatic_retry_performed": False,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        })
        print(f"BLOCKED: {exc}")
        return 2

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "quote_date": quote_date,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "test_note_title": title,
        "body": text_fingerprint(compact),
        "stats": text_stats(compact, len(manifest["products"])),
        "chunk_character_limit": args.chunk_chars,
        "saved_closed": True,
        "unique_search_reopened": True,
        "full_body_hash_match": evidence["comparison"]["normalized_hash_match"],
        "truncation_observed": evidence["comparison"]["truncated"],
        "runtime": evidence,
        "test_note_retained": True,
        "automatic_delete_performed": False,
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
