from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mikihouse_luyao.shijiu_canonical_create import load_verified_browser_credentials


def _capture(token: str, secret: str) -> dict:
    return {
        "playwright_request": {
            "url": f"https://example.invalid/shopapi/Goods/newAddGood?token={token}",
            "post_data": json.dumps({"token": token, "secret": secret}),
        },
        "readback": {
            "goods_index_unique": True,
            "get_format_info_product_verified": True,
            "sku_structure_verified": True,
            "sku_code_verified": True,
        },
    }


def test_credentials_loader_finds_hashed_create_capture_not_latest_file(tmp_path: Path) -> None:
    valid = tmp_path / "shijiu-browser-exact-2026-01-01T00-00-00.private.json"
    valid.write_text(json.dumps(_capture("expected", "secret")), encoding="utf-8")
    newer = tmp_path / "shijiu-browser-exact-2026-01-02T00-00-00.private.json"
    newer.write_text(json.dumps(_capture("newer-edit", "other")), encoding="utf-8")
    newer.touch()
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "browser_exact_private_evidence_sha256": hashlib.sha256(valid.read_bytes()).hexdigest()
    }), encoding="utf-8")

    token, secret, evidence = load_verified_browser_credentials(tmp_path, contract)

    assert (token, secret) == ("expected", "secret")
    assert evidence["private_evidence_sha256"] == hashlib.sha256(valid.read_bytes()).hexdigest()
