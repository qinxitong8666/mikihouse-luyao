from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from scripts.finalize_daily_quote_production_evidence import (
    FINAL_STATUS,
    FinalizationError,
    finalize,
    validate_rendered_pages,
)
from mikihouse_luyao.wechat_daily_production import PDF_FILE_PICKER_RECOVERY_MODE


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_finalization_fixture(tmp_path: Path) -> tuple[Path, Path]:
    daily = tmp_path / "2026-09-23"
    render_dir = tmp_path / "renders"
    daily.mkdir()
    render_dir.mkdir()
    pdf = daily / "MIKIHOUSE_2026-09-23_报价全集.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.drawString(72, 760, "10-0001-001")
    canvas.save()
    image = Image.new("RGB", (1654, 2339), "white")
    ImageDraw.Draw(image).rectangle((80, 80, 500, 500), fill="black")
    image.save(render_dir / "page-001.jpg")
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    title = "MIKI HOUSE 9月23日报价｜PDF版"
    write_json(
        daily / "wechat_pdf_favorite_payload.json",
        {"title": title, "attachments": [pdf.name]},
    )
    write_json(
        daily / "wechat_pdf_favorite_manual_file_picker_recovery_validation.json",
        {
            "status": "PASS",
            "recovery_mode": PDF_FILE_PICKER_RECOVERY_MODE,
            "automatic_retry_count": 0,
            "attachment": {
                "filename": pdf.name,
                "byte_count": pdf.stat().st_size,
                "sha256": pdf_sha256,
                "selected_path": str(pdf.resolve()),
                "visible_before_save": True,
                "visible_after_reopen": True,
                "clipboard_placeholder_after_reopen": "[文件]",
            },
            "text_readback": {"status": "EXACT_READBACK_MATCH", "truncated": False},
            "saved_note_search": {
                "search_candidate_count": 1,
                "search_marker_sha256": hashlib.sha256(title.encode()).hexdigest(),
            },
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        },
    )
    write_json(
        daily / "wechat_text_favorite_production_validation.json",
        {
            "status": "PASS",
            "mutation_replayed": False,
            "readback": {
                "truncated": False,
                "normalized_sha256": "a" * 64,
            },
        },
    )
    write_json(
        daily / "daily_quote_stats.json",
        {
            "status": "SUCCESS_VISUAL_QA_PENDING",
            "quote_date": "2026-09-23",
            "pdf": {"path": pdf.name, "sha256": pdf_sha256, "page_count": 1},
            "wechat_real_write_count": 2,
        },
    )
    write_json(
        daily / "PDF压缩检索验收报告.json",
        {"status": "AUTOMATED_PASS_VISUAL_QA_PENDING", "visual_qa": {}},
    )
    write_json(
        daily / "wechat_daily_production_checkpoint.json",
        {
            "status": "PASS",
            "stages": {
                "pdf": {
                    "status": "PASS",
                    "error_type": "OldError",
                    "error": "historical PDF failure",
                    "authorized_recovery": {
                        "error_type": "OldError",
                        "error": "historical authorized failure",
                    },
                },
                "text": {
                    "status": "PASS",
                    "error_type": "OldError",
                    "error": "historical close latency",
                },
            },
            "favorite_create_count": 2,
            "automatic_mutation_retry_count": 0,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        },
    )
    write_json(
        daily / "wechat_daily_production_report.json",
        {
            "status": "PASS",
            "favorite_create_count": 2,
            "automatic_mutation_retry_count": 0,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
            "evidence_files": {},
        },
    )
    (daily / "MIKIHOUSE_2026-09-23_内部变化报告.txt").write_text(
        "MIKI HOUSE 2026-09-23 内部变化报告\n\nShijiu请求/写入：0。微信收藏真实写入：0。\n",
        encoding="utf-8",
    )
    return daily, render_dir


def test_daily_finalization_unifies_pass_state_without_new_wechat_mutation(tmp_path: Path) -> None:
    daily, render_dir = build_finalization_fixture(tmp_path)
    first = finalize(daily, render_dir, dpi=200, manual_review_passed=True)
    second = finalize(daily, render_dir, dpi=200, manual_review_passed=True)
    assert first == second
    assert first["additional_wechat_mutation_count"] == 0
    stats = json.loads((daily / "daily_quote_stats.json").read_text())
    checkpoint = json.loads((daily / "wechat_daily_production_checkpoint.json").read_text())
    report = json.loads((daily / "wechat_daily_production_report.json").read_text())
    qa = json.loads((daily / "PDF压缩检索验收报告.json").read_text())
    assert stats["status"] == FINAL_STATUS
    assert qa["visual_qa"]["status"] == "PASS"
    assert checkpoint["final_state"]["favorite_create_count"] == 2
    assert "error" not in checkpoint["stages"]["pdf"]
    assert len(checkpoint["attempt_history"]) == 4
    assert report["pdf_attachment_recovery_contract"]["status"] == "PASS"
    assert report["additional_wechat_mutation_count_during_finalization"] == 0
    assert report["final_state"]["exactly_two_favorites"] is True
    assert report["final_state"]["additional_wechat_mutation_count_during_finalization"] == 0


def test_render_validation_requires_all_pages_and_200dpi(tmp_path: Path) -> None:
    image = Image.new("RGB", (1654, 2339), "white")
    ImageDraw.Draw(image).rectangle((100, 100, 300, 300), fill="black")
    image.save(tmp_path / "page-001.jpg")
    with pytest.raises(FinalizationError, match="at least 200 dpi"):
        validate_rendered_pages(
            tmp_path,
            page_count=1,
            dpi=199,
            page_width_points=A4[0],
            page_height_points=A4[1],
        )
    with pytest.raises(FinalizationError, match="rendered page mismatch"):
        validate_rendered_pages(
            tmp_path,
            page_count=2,
            dpi=200,
            page_width_points=A4[0],
            page_height_points=A4[1],
        )


def test_finalizer_contains_no_wechat_gui_or_shijiu_mutation_calls() -> None:
    source = Path("scripts/finalize_daily_quote_production_evidence.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("osascript", "System Events", "MacWeChatFavoriteSink", "/shopapi/"):
        assert forbidden not in source
