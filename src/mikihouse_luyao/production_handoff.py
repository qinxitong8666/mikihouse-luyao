from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .csv_input import read_product_numbers
from .initialization_plan import (
    EXPECTED_SPECIAL_COUNT,
    TARGET_CATEGORY_ID,
    WRITE_BLOCKED_STATUS,
    historical_frozen_product_numbers,
    main as build_initialization_plan,
    validate_plan_freshness,
)
from .shijiu_import import content_sha256, load_category_map
from .stable_catalog import (
    NON_SELLABLE_SERVICE_OR_ADDON,
    PDF_SPECIAL,
    REVIEW_REQUIRED,
    STABLE,
    assess_product_stability,
    run_stable_catalog_sync,
)
from .stable_sync import run_cycle, validate_complete_snapshot


MODE = "PREPARATION_ONLY"
READY = "READY_TO_REQUEST_EXCLUSIVE_WRITER_WINDOW"
BLOCKED = "BLOCKED"
SOURCE = "MIKIHOUSE"
TARGET = "SHIJIU"
ALLOWED_OFFICIAL_HOST_SUFFIXES = (
    "shopify.com",
    "mikihouse.co.jp",
    "img.mksk.me",
)
MAX_IMAGE_BYTES = 25 * 1024 * 1024
REPORT_RELATIVE_PATH = Path(
    "deliverables/shijiu_initialization/production_handoff_readiness.json"
)
PREFLIGHT_RELATIVE_PATH = Path(
    "deliverables/shijiu_initialization/production_handoff_resource_preflight.json"
)


class ProductionHandoffError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ProductionHandoffError(f"JSON root must be an object: {path}")
    return value


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _host_allowed(host: str | None) -> bool:
    normalized = str(host or "").lower().rstrip(".")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in ALLOWED_OFFICIAL_HOST_SUFFIXES
    )


def _verified_exact_https_urls(manifest: dict[str, Any]) -> set[str]:
    return {
        str(row.get("canonical_https_url"))
        for row in manifest.get("entries") or []
        if row.get("content_hash_equal") is True
        and row.get("mime_equal") is True
        and row.get("decoded_dimensions_equal") is True
        and str(row.get("canonical_https_url") or "").startswith("https://")
    }


def _url_allowed(url: str, verified_exact_urls: set[str]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme.lower() == "https"
        and (_host_allowed(parsed.hostname) or url in verified_exact_urls)
    )


def preflight_source_image(
    url: str,
    *,
    verified_exact_urls: set[str],
    timeout: float = 30,
) -> dict[str, Any]:
    """Download and decode one source image exactly once; never contacts Shijiu."""
    if not _url_allowed(url, verified_exact_urls):
        raise ProductionHandoffError("source image is not an allowlisted HTTPS resource")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "mikihouse-luyao-production-handoff-preflight/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        if not _url_allowed(final_url, verified_exact_urls):
            raise ProductionHandoffError("source image redirected outside official HTTPS allowlist")
        content_type = str(response.headers.get_content_type() or "").lower()
        if not content_type.startswith("image/"):
            raise ProductionHandoffError(f"source resource MIME is not image/*: {content_type}")
        advertised = response.headers.get("Content-Length")
        if advertised and int(advertised) > MAX_IMAGE_BYTES:
            raise ProductionHandoffError("source image exceeds 25 MiB preflight limit")
        body = response.read(MAX_IMAGE_BYTES + 1)
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise ProductionHandoffError("source image is empty or exceeds 25 MiB preflight limit")
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            width, height = image.size
            decoded_format = str(image.format or "UNKNOWN")
            frame_count = int(getattr(image, "n_frames", 1))
    except Exception as exc:  # Pillow exposes format-specific exception subclasses.
        raise ProductionHandoffError("source image could not be fully decoded") from exc
    if width <= 0 or height <= 0:
        raise ProductionHandoffError("source image decoded with invalid dimensions")
    return {
        "status": "PASSED",
        "source_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "source_host": urllib.parse.urlsplit(url).hostname,
        "final_url_sha256": hashlib.sha256(final_url.encode("utf-8")).hexdigest(),
        "final_host": urllib.parse.urlsplit(final_url).hostname,
        "mime_type": content_type,
        "byte_count": len(body),
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "decoded_format": decoded_format,
        "decoded_width": width,
        "decoded_height": height,
        "decoded_frame_count": frame_count,
        "http_attempt_count": 1,
        "shijiu_requests": 0,
        "shijiu_cos_upload_requests": 0,
    }


def preflight_pilot_resources(
    pilot: dict[str, Any],
    verified_https_manifest: dict[str, Any],
    *,
    timeout: float = 30,
    workers: int = 8,
    fetcher: Callable[..., dict[str, Any]] = preflight_source_image,
) -> dict[str, Any]:
    verified_urls = _verified_exact_https_urls(verified_https_manifest)
    products: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    references: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, Any]] = []
    for product in pilot.get("products") or []:
        product_number = str(product.get("product_number") or "")
        manifests = product.get("resource_manifest") or []
        product_refs = []
        for row in manifests:
            url = str(row.get("source_url") or "")
            reference = {
                "product_number": product_number,
                "upload_reference": row.get("upload_reference"),
                "order": row.get("order"),
                "role": row.get("role"),
                "source_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            }
            product_refs.append(reference)
            references.setdefault(url, []).append(reference)
        products.append({
            "product_number": product_number,
            "resource_count": len(manifests),
            "resource_references": product_refs,
        })
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(
                fetcher, url, verified_exact_urls=verified_urls, timeout=timeout
            ): url
            for url in references
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                by_url[url] = future.result()
            except Exception as exc:
                errors.append({
                    "source_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    "references": references[url],
                    "error_type": type(exc).__name__,
                    "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
                })
    observations = sorted(
        by_url.values(), key=lambda row: str(row["source_url_sha256"])
    )
    result = {
        "schema_version": 1,
        "mode": MODE,
        "write_status": WRITE_BLOCKED_STATUS,
        "status": "PASSED" if not errors else "FAILED",
        "pilot_product_count": len(products),
        "resource_reference_count": sum(row["resource_count"] for row in products),
        "unique_source_url_count": len(references),
        "passed_unique_source_url_count": len(observations),
        "failed_unique_source_url_count": len(errors),
        "products": products,
        "resource_observations": observations,
        "failures": errors,
        "safety": {
            "source_http_get_count": len(observations) + len(errors),
            "source_http_retry_count": 0,
            "shijiu_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "writer_mutex_evidence_generated": False,
        },
    }
    result["evidence_logical_sha256"] = content_sha256(result)
    return result


def _append_check(
    checks: list[dict[str, Any]], check_id: str, passed: bool, evidence: Any
) -> None:
    checks.append({
        "id": check_id,
        "status": "PASSED" if passed else "FAILED",
        "evidence_logical_sha256": content_sha256(evidence),
        "evidence": evidence,
    })


def evaluate_handoff(
    *,
    root: Path,
    head_sha: str,
    branch: str,
    source_snapshot: dict[str, Any],
    stable_catalog: dict[str, Any],
    stable_audit: dict[str, Any],
    sync_cycle_report: dict[str, Any],
    pilot: dict[str, Any],
    batch_plan: dict[str, Any],
    mapping: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    richtext_contract: dict[str, Any],
    duplicate_identity_contract: dict[str, Any],
    price_policy: dict[str, Any],
    protocol: dict[str, Any],
    preflight: dict[str, Any],
    historical_frozen: set[str] | None = None,
    legacy_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        validate_complete_snapshot(source_snapshot)
        complete = True
        complete_evidence: Any = {
            "complete_pagination_validated": source_snapshot.get(
                "complete_pagination_validated"
            ),
            "product_count": len(source_snapshot.get("products") or []),
        }
    except Exception as exc:
        complete = False
        complete_evidence = {
            "error_type": type(exc).__name__,
            "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest(),
        }
    _append_check(checks, "COMPLETE_STOREFRONT_CRAWL", complete, complete_evidence)

    stable_numbers_for_audit = {
        str(row.get("product_number")) for row in stable_catalog.get("products") or []
    }
    exclusion_for_audit = stable_catalog.get("stability_exclusion") or {}
    special_for_audit = stable_catalog.get("special_exclusion") or {}
    excluded_for_audit = (
        set(special)
        | set(exclusion_for_audit.get("web_exclusive_product_numbers") or [])
        | set(exclusion_for_audit.get("limited_time_price_product_numbers") or [])
        | set(exclusion_for_audit.get("non_sellable_service_or_addon_product_numbers") or [])
        | set(exclusion_for_audit.get("review_required_product_numbers") or [])
    )
    stable_counts = stable_audit.get("counts") or {}
    stable_rebuild_ok = (
        stable_audit.get("status") == "COMPLETED_READ_ONLY_STOREFRONT"
        and stable_counts.get("storefront_total_product_count")
        == len(source_snapshot.get("products") or [])
        and stable_counts.get("stable_catalog_product_count") == len(stable_numbers_for_audit)
        and stable_counts.get("pdf_special_list_manifest_count") == EXPECTED_SPECIAL_COUNT
        and special_for_audit.get("total_count") == EXPECTED_SPECIAL_COUNT
        and special_for_audit.get("online_excluded_count", 0)
        + special_for_audit.get("offline_remembered_count", 0)
        == EXPECTED_SPECIAL_COUNT
        and stable_counts.get("review_required_stability_count") == 0
        and not (stable_numbers_for_audit & excluded_for_audit)
    )
    _append_check(
        checks,
        "STABLE_AND_EXCLUSION_POOLS_REBUILT",
        stable_rebuild_ok,
        {
            "counts": stable_counts,
            "stable_forbidden_intersection": sorted(
                stable_numbers_for_audit & excluded_for_audit
            ),
            "audit_logical_sha256": content_sha256(stable_audit),
        },
    )
    sync_safety = sync_cycle_report.get("safety") or {}
    sync_ok = (
        sync_cycle_report.get("status") == "SYNC_CYCLE_PLANNED_NO_WRITE"
        and sync_cycle_report.get("mode") == "PLANNING_ONLY"
        and sync_cycle_report.get("complete_crawl_required_and_validated") is True
        and sync_cycle_report.get("actions_are_non_executable") is True
        and sync_cycle_report.get("identical_snapshot_replay") is True
        and sync_cycle_report.get("idempotent_replay_produced_no_new_events") is True
        and sync_cycle_report.get("captured_at") == source_snapshot.get("captured_at")
        and sync_safety.get("shijiu_requests") == 0
        and sync_safety.get("shijiu_create_requests") == 0
        and sync_safety.get("shijiu_update_requests") == 0
        and sync_safety.get("shijiu_cos_upload_requests") == 0
        and sync_safety.get("writer_mutex_evidence_generated") is False
        and sync_safety.get("planning_only_hard_stop") is True
    )
    _append_check(
        checks,
        "SOURCE_INCREMENTAL_PLANNING_ONLY",
        sync_ok,
        {
            "status": sync_cycle_report.get("status"),
            "mode": sync_cycle_report.get("mode"),
            "captured_at": sync_cycle_report.get("captured_at"),
            "complete_crawl_required_and_validated": sync_cycle_report.get(
                "complete_crawl_required_and_validated"
            ),
            "actions_are_non_executable": sync_cycle_report.get(
                "actions_are_non_executable"
            ),
            "identical_snapshot_replay": sync_cycle_report.get(
                "identical_snapshot_replay"
            ),
            "idempotent_replay_produced_no_new_events": sync_cycle_report.get(
                "idempotent_replay_produced_no_new_events"
            ),
            "counts": sync_cycle_report.get("counts"),
            "safety": sync_safety,
        },
    )

    _append_check(
        checks,
        "REPOSITORY_IDENTITY",
        branch == "main" and len(head_sha) == 40,
        {"repo": "qinxitong8666/mikihouse-luyao", "branch": branch, "head_sha": head_sha},
    )
    agents_text = (root / "AGENTS.md").read_text(encoding="utf-8")
    agents_ok = all(
        marker in agents_text
        for marker in (
            "## 14. SHIJIU 来源所有权与生产写入互斥",
            "### 14.1 来源所有权",
            "### 14.2 全局 SHIJIU production write mutex",
            "concurrent_shijiu_writer_observed=false",
            "cross_source_writes=0",
        )
    )
    _append_check(
        checks,
        "AGENTS_SECTION_14_GOVERNANCE",
        agents_ok,
        {
            "agents_file_sha256": hashlib.sha256(agents_text.encode("utf-8")).hexdigest(),
            "source_ownership_rule_present": agents_ok,
            "production_writer_mutex_rule_present": agents_ok,
        },
    )
    protocol_ok = (
        protocol.get("mode") == MODE
        and protocol.get("write_status") == WRITE_BLOCKED_STATUS
        and protocol.get("hard_stop_before_writer_mutex") is True
        and protocol.get("writer_mutex_evidence_generation_allowed") is False
        and protocol.get("shijiu_requests_allowed") is False
        and protocol.get("pilot_product_count") == 20
        and protocol.get("full_initialization_batch_count") == 170
    )
    _append_check(checks, "PREPARATION_ONLY_PROTOCOL", protocol_ok, protocol)

    category_value = category.get("target_category") or category
    category_id = int(category_value.get("id") or 0)
    contract_hashes = {
        "richtext_contract_logical_sha256": content_sha256(richtext_contract),
        "duplicate_name_identity_contract_logical_sha256": content_sha256(
            duplicate_identity_contract
        ),
        "price_policy_logical_sha256": content_sha256(price_policy),
    }
    freshness = pilot.get("freshness_guard") or {}
    contract_ok = (
        category_id == TARGET_CATEGORY_ID
        and freshness.get("richtext_contract_logical_sha256")
        == contract_hashes["richtext_contract_logical_sha256"]
        and freshness.get("duplicate_name_identity_contract_logical_sha256")
        == contract_hashes["duplicate_name_identity_contract_logical_sha256"]
        and freshness.get("price_policy_logical_sha256")
        == contract_hashes["price_policy_logical_sha256"]
    )
    _append_check(
        checks,
        "CATEGORY_AND_CONTRACT_HASHES",
        contract_ok,
        {"target_category_id": category_id, **contract_hashes, "pilot_freshness": freshness},
    )

    try:
        plan_freshness = validate_plan_freshness(
            pilot, stable_catalog, source_snapshot, special, mapping
        )
    except Exception as exc:
        plan_freshness = {
            "valid": False,
            "status": "VALIDATION_EXCEPTION",
            "error_type": type(exc).__name__,
            "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest(),
        }
    _append_check(
        checks, "PILOT_SOURCE_FRESHNESS", bool(plan_freshness.get("valid")), plan_freshness
    )

    pilot_numbers = [str(row.get("product_number") or "") for row in pilot.get("products") or []]
    pilot_shape_ok = (
        len(pilot_numbers) == len(set(pilot_numbers)) == 20
        and pilot.get("product_count") == 20
        and pilot.get("status") == "FROZEN_PLANNING_ONLY"
        and pilot.get("execution_authorized") is False
    )
    _append_check(
        checks,
        "CURRENT_FROZEN_PILOT_20",
        pilot_shape_ok,
        {"product_count": len(pilot_numbers), "product_numbers": pilot_numbers},
    )

    stable_by_number = {
        str(row.get("product_number")): row for row in stable_catalog.get("products") or []
    }
    exclusion = stable_catalog.get("stability_exclusion") or {}
    forbidden_by_reason = {
        PDF_SPECIAL: set(special),
        "WEB_EXCLUSIVE": set(exclusion.get("web_exclusive_product_numbers") or []),
        "LIMITED_TIME_PRICE": set(
            exclusion.get("limited_time_price_product_numbers") or []
        ),
        NON_SELLABLE_SERVICE_OR_ADDON: set(
            exclusion.get("non_sellable_service_or_addon_product_numbers") or []
        ),
        REVIEW_REQUIRED: set(exclusion.get("review_required_product_numbers") or []),
    }
    planned_numbers = {
        str(row.get("product_number") or "") for row in batch_plan.get("products") or []
    }
    mapped_numbers = {
        str(number)
        for number, row in (mapping.get("products") or {}).items()
        if row.get("shijiu_product_id") not in (None, "")
    }
    if historical_frozen is None:
        historical_frozen, _ = historical_frozen_product_numbers(root)
    leaks = {
        reason: sorted(planned_numbers & numbers)
        for reason, numbers in forbidden_by_reason.items()
    }
    leaks.update({
        "ALREADY_MAPPED": sorted(planned_numbers & mapped_numbers),
        "HISTORICAL_FROZEN": sorted(planned_numbers & historical_frozen),
        "NOT_STABLE": sorted(
            number
            for number in planned_numbers
            if number not in stable_by_number
            or assess_product_stability(stable_by_number[number], special).get("status") != STABLE
        ),
    })
    all_leaks = sorted({number for rows in leaks.values() for number in rows})
    counts = batch_plan.get("counts") or {}
    # Initialization disposition deliberately gives a verified mapping precedence over
    # historical-attempt evidence: an already mapped product is handed to incremental
    # sync and is never counted as a new-create frozen row.
    stable_historical = (historical_frozen & set(stable_by_number)) - mapped_numbers
    stable_mapped = mapped_numbers & set(stable_by_number)
    batch_shape_ok = (
        counts.get("stable_catalog_product_count") == len(stable_by_number)
        and counts.get("planned_initial_create_product_count") == len(planned_numbers)
        and counts.get("already_mapped_handoff_count") == len(stable_mapped)
        and counts.get("historical_frozen_count") == len(stable_historical)
        and counts.get("initialization_review_required_count") == 0
        and counts.get("batch_count") == len(batch_plan.get("batches") or []) == 170
        and not all_leaks
    )
    _append_check(
        checks,
        "INITIALIZATION_PLAN_DISJOINTNESS",
        batch_shape_ok,
        {"counts": counts, "leaks": leaks, "all_leak_product_numbers": all_leaks},
    )

    legacy = legacy_audit or {}
    legacy_ok = (
        (
            legacy.get("legacy_reference_only") is True
            or legacy.get("classification") == "legacy_reference_only"
            or legacy.get("status") == "PASSED"
        )
        and (legacy.get("legacy_product_count") == 286 or legacy.get("product_count") == 286)
    )
    _append_check(
        checks,
        "LEGACY_286_REFERENCE_ONLY",
        legacy_ok,
        {
            "legacy_product_count": legacy.get("legacy_product_count", legacy.get("product_count")),
            "legacy_reference_only": legacy.get("legacy_reference_only"),
            "status": legacy.get("status"),
            "planned_target_ids": [],
        },
    )

    preflight_ok = (
        preflight.get("status") == "PASSED"
        and preflight.get("pilot_product_count") == 20
        and preflight.get("failed_unique_source_url_count") == 0
        and (preflight.get("safety") or {}).get("shijiu_requests") == 0
        and (preflight.get("safety") or {}).get("shijiu_cos_upload_requests") == 0
    )
    _append_check(checks, "PILOT_RESOURCE_PREFLIGHT", preflight_ok, {
        key: preflight.get(key) for key in (
            "status", "pilot_product_count", "resource_reference_count",
            "unique_source_url_count", "passed_unique_source_url_count",
            "failed_unique_source_url_count", "evidence_logical_sha256",
        )
    })

    safety = {
        "shijiu_requests": 0,
        "shijiu_create_requests": 0,
        "shijiu_update_requests": 0,
        "shijiu_cos_upload_requests": 0,
        "shijiu_shelf_price_inventory_writes": 0,
        "writer_mutex_evidence_generated": False,
        "writer_mutex_evidence_generation_allowed": False,
        "legacy_286_touched": False,
        "pilot_execution_count": 0,
        "full_initialization_execution_count": 0,
    }
    _append_check(checks, "ZERO_SHIJIU_MUTATION_BOUNDARY", True, safety)
    failed = [row["id"] for row in checks if row["status"] != "PASSED"]
    decision = READY if not failed else BLOCKED
    result = {
        "schema_version": 1,
        "status": MODE,
        "write_status": WRITE_BLOCKED_STATUS,
        "handoff_decision": decision,
        "source": SOURCE,
        "target": TARGET,
        "repo": "qinxitong8666/mikihouse-luyao",
        "branch": branch,
        "head_sha": head_sha,
        "blocked_reason_codes": failed,
        "checks": checks,
        "counts": counts,
        "pilot": {
            "product_count": len(pilot_numbers),
            "product_numbers": pilot_numbers,
            "resource_reference_count": preflight.get("resource_reference_count"),
            "unique_source_url_count": preflight.get("unique_source_url_count"),
        },
        "contracts": contract_hashes,
        "future_execution_protocol": copy.deepcopy(
            protocol.get("future_execution_protocol") or {}
        ),
        "terminal_boundary": (
            "Preparation checks passed. A separate explicitly authorized future task must "
            "confirm all other writers stopped and request the exclusive writer window."
            if decision == READY
            else "Preparation is blocked. No writer window may be requested until all failed checks pass."
        ),
        "safety": safety,
    }
    deterministic = copy.deepcopy(result)
    result["decision_evidence_logical_sha256"] = content_sha256(deterministic)
    result["machine_auditable_evidence_complete"] = all(
        bool(row.get("evidence_logical_sha256")) for row in checks
    )
    return result


def backup_protected_inputs(root: Path, head_sha: str) -> dict[str, Any]:
    backup_root = Path("/private/tmp/mikihouse-production-handoff-backups")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_root / f"{timestamp}-{head_sha[:12]}"
    protected = [
        root / "deliverables/storefront_stable_catalog",
        root / "deliverables/shijiu_initialization",
        root / "state/mikihouse_source_sync_state.json.gz",
        root / "state/mikihouse_initialization_checkpoint.json.gz",
    ]
    files = [
        child
        for path in protected
        for child in ([path] if path.is_file() else sorted(path.rglob("*")) if path.exists() else [])
        if child.is_file()
    ]
    for source in files:
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = {
        "backup_location": str(destination),
        "git_external": not str(destination).startswith(str(root)),
        "head_sha": head_sha,
        "file_count": len(files),
        "files": [
            {"path": str(path.relative_to(root)), "sha256": file_sha256(path)} for path in files
        ],
    }
    _write_json_atomic(destination / "backup_manifest.json", manifest)
    return manifest


def write_protected_state_audit(
    root: Path,
    *,
    head_sha: str,
    captured_at: str,
    backup: dict[str, Any],
    stable_audit: dict[str, Any],
    pilot_preflight_source_get_count: int,
) -> dict[str, Any]:
    before = {row["path"]: row["sha256"] for row in backup.get("files") or []}
    state_paths = [
        "state/mikihouse_initialization_checkpoint.json.gz",
        "state/mikihouse_source_sync_state.json.gz",
    ]
    deliverable_paths = [
        "deliverables/storefront_stable_catalog/stable_catalog.json.gz",
        "deliverables/shijiu_initialization/stable_initialization_batch_plan.json.gz",
    ]
    affected_state = []
    for relative in state_paths:
        path = root / relative
        affected_state.append({
            "path": relative,
            "reason": "atomically refresh complete-source planning state for production handoff preparation",
            "before_sha256": before.get(relative),
            "after_sha256": file_sha256(path),
            "status": _read_json(path).get("status"),
            "shijiu_mutation_count": _read_json(path).get("shijiu_mutation_count", 0),
            "writer_mutex_evidence_generated": False,
        })
    affected_deliverables = []
    for relative in deliverable_paths:
        path = root / relative
        affected_deliverables.append({
            "path": relative,
            "reason": "rebuild from the newly completed official Storefront crawl",
            "before_sha256": before.get(relative),
            "after_sha256": file_sha256(path),
        })
    unchanged = []
    for relative in (
        "state/shijiu_mappings.json",
        "deliverables/mikihouse_2026AW_price_catalog.pdf",
    ):
        path = root / relative
        unchanged.append({
            "path": relative,
            "sha256": file_sha256(path),
            "matches_preparation_backup": before.get(relative) == file_sha256(path),
            "mapping_rows_modified": 0 if relative.endswith("shijiu_mappings.json") else None,
        })
    audit = {
        "schema_version": 1,
        "generated_at": captured_at,
        "status": "PROTECTED_STATE_INTENTIONALLY_UPDATED_OFFLINE",
        "mode": MODE,
        "write_status": WRITE_BLOCKED_STATUS,
        "parent_commit": head_sha,
        "affected_state": affected_state,
        "affected_protected_deliverables": affected_deliverables,
        "unchanged_protected_artifacts": unchanged,
        "backup_evidence": {
            "changed_state_and_deliverables_copied_before_refresh": True,
            "git_external_backup_directory": backup.get("backup_location"),
            "backup_manifest_sha256": file_sha256(
                Path(str(backup["backup_location"])) / "backup_manifest.json"
            ),
            "sensitive_values_included": False,
        },
        "historical_stale_plan": {
            "path": "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json",
            "sha256": file_sha256(
                root / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json"
            ),
            "changed": False,
        },
        "safety": {
            "official_storefront_api_read_requests": (
                stable_audit.get("crawl") or {}
            ).get("api_request_count"),
            "official_pilot_resource_read_requests": pilot_preflight_source_get_count,
            "shijiu_read_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "shijiu_shelf_price_inventory_writes": 0,
            "writer_mutex_evidence_generated": False,
            "legacy_products_modified": 0,
        },
    }
    _write_json_atomic(
        root / "deliverables/shijiu_initialization/protected_state_change_audit.json",
        audit,
    )
    return audit


def orchestrate(root: Path) -> dict[str, Any]:
    head_sha = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current")
    dirty = _git(root, "status", "--porcelain")
    if dirty:
        raise ProductionHandoffError(
            "working tree must be clean before the auditable full preparation run"
        )
    backup = backup_protected_inputs(root, head_sha)

    special_path = root / "special_skus_2026aw.csv"
    source_dir = root / "output/storefront-stable"
    stable_reports = root / "deliverables/storefront_stable_catalog"
    run_stable_catalog_sync(
        special_path,
        source_dir,
        stable_reports,
        old_master_path=root / "output/storefront-master/master_catalog.json",
        page_size=100,
        delay=0.1,
        timeout=30,
        retries=2,
        max_pages=1000,
        verified_https_equivalents_path=(
            root / "config/mikihouse_verified_https_image_equivalents.json"
        ),
    )
    run_cycle(
        source_path=source_dir / "source_catalog.json",
        stable_path=source_dir / "stable_catalog.json",
        special_path=special_path,
        mapping_path=root / "state/shijiu_mappings.json",
        price_guard_path=root / "config/shijiu_price_guard.json",
        state_path=root / "state/mikihouse_source_sync_state.json.gz",
        output_dir=root / "output/storefront-sync-cycle",
        report_dir=stable_reports,
        initialize_baseline=False,
        trigger="production_handoff_preparation",
    )
    first_incremental_pass = _read_json(
        root / "output/storefront-sync-cycle/sync_cycle_report.json"
    )
    # A second pass over the exact same completed snapshot is deliberately part of
    # preparation. It proves the event ledger/checkpoint is idempotent and must not
    # perform another Storefront crawl or any target request.
    run_cycle(
        source_path=source_dir / "source_catalog.json",
        stable_path=source_dir / "stable_catalog.json",
        special_path=special_path,
        mapping_path=root / "state/shijiu_mappings.json",
        price_guard_path=root / "config/shijiu_price_guard.json",
        state_path=root / "state/mikihouse_source_sync_state.json.gz",
        output_dir=root / "output/storefront-sync-cycle",
        report_dir=stable_reports,
        initialize_baseline=False,
        trigger="production_handoff_idempotency_replay",
    )
    build_initialization_plan(["--replace-planning-only-checkpoint"])

    source = _read_json(source_dir / "source_catalog.json")
    stable = _read_json(stable_reports / "stable_catalog.json.gz")
    stable_audit = _read_json(stable_reports / "stable_pool_audit.json")
    sync_cycle_report = _read_json(
        root / "output/storefront-sync-cycle/sync_cycle_report.json"
    )
    pilot = _read_json(
        root / "deliverables/shijiu_initialization/stable_pilot_20_frozen_plan.json"
    )
    batch_plan = _read_json(
        root / "deliverables/shijiu_initialization/stable_initialization_batch_plan.json.gz"
    )
    mapping = _read_json(root / "state/shijiu_mappings.json")
    special = set(read_product_numbers(special_path))
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise ProductionHandoffError("PDF special manifest is not exactly 351 product numbers")
    category = load_category_map(root / "config/shijiu_category_map.json")
    richtext = _read_json(root / "config/shijiu_richtext_contract.json")
    duplicate = _read_json(
        root / "config/shijiu_duplicate_good_name_identity_contract.json"
    )
    price = _read_json(root / "config/shijiu_price_guard.json")
    protocol = _read_json(root / "config/mikihouse_production_handoff_protocol.json")
    verified = _read_json(
        root / "config/mikihouse_verified_https_image_equivalents.json"
    )
    preflight = preflight_pilot_resources(pilot, verified)
    preflight.update({
        "generated_at": source.get("captured_at"),
        "head_sha": head_sha,
        "source_snapshot_logical_sha256": content_sha256(source),
        "stable_catalog_logical_sha256": content_sha256(stable),
    })
    preflight["evidence_logical_sha256"] = content_sha256(
        {key: value for key, value in preflight.items() if key != "evidence_logical_sha256"}
    )
    write_protected_state_audit(
        root,
        head_sha=head_sha,
        captured_at=str(source.get("captured_at")),
        backup=backup,
        stable_audit=stable_audit,
        pilot_preflight_source_get_count=int(
            (preflight.get("safety") or {}).get("source_http_get_count") or 0
        ),
    )
    legacy = _read_json(
        root / "deliverables/shijiu_import/legacy_reference_audit.json"
    )
    historical, historical_sources = historical_frozen_product_numbers(root)
    readiness = evaluate_handoff(
        root=root,
        head_sha=head_sha,
        branch=branch,
        source_snapshot=source,
        stable_catalog=stable,
        stable_audit=stable_audit,
        sync_cycle_report=sync_cycle_report,
        pilot=pilot,
        batch_plan=batch_plan,
        mapping=mapping,
        special=special,
        category=category,
        richtext_contract=richtext,
        duplicate_identity_contract=duplicate,
        price_policy=price,
        protocol=protocol,
        preflight=preflight,
        historical_frozen=historical,
        legacy_audit=legacy,
    )
    readiness.update({
        "generated_at": source.get("captured_at"),
        "protected_state_backup": {
            "location": backup["backup_location"],
            "git_external": backup["git_external"],
            "file_count": backup["file_count"],
            "manifest_sha256": file_sha256(
                Path(backup["backup_location"]) / "backup_manifest.json"
            ),
        },
        "historical_frozen_sources": historical_sources,
        "source_incremental_first_pass": {
            "status": first_incremental_pass.get("status"),
            "captured_at": first_incremental_pass.get("captured_at"),
            "counts": first_incremental_pass.get("counts"),
            "actions_are_non_executable": first_incremental_pass.get(
                "actions_are_non_executable"
            ),
            "safety": first_incremental_pass.get("safety"),
            "evidence_logical_sha256": content_sha256(first_incremental_pass),
        },
        "outputs": {
            "readiness": str(REPORT_RELATIVE_PATH),
            "resource_preflight": str(PREFLIGHT_RELATIVE_PATH),
            "runbook": "deliverables/shijiu_initialization/production_handoff_runbook.md",
        },
    })
    _write_json_atomic(root / PREFLIGHT_RELATIVE_PATH, preflight)
    _write_json_atomic(root / REPORT_RELATIVE_PATH, readiness)
    return readiness


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Refresh and audit the MIKIHOUSE production handoff in PREPARATION_ONLY mode. "
            "This command has no Shijiu client, writer-mutex, live-write, or bypass option."
        )
    )


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    root = Path.cwd()
    try:
        report = orchestrate(root)
    except Exception as exc:
        blocked = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": MODE,
            "write_status": WRITE_BLOCKED_STATUS,
            "handoff_decision": BLOCKED,
            "blocked_reason_codes": ["ORCHESTRATION_EXCEPTION"],
            "error_type": type(exc).__name__,
            "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            "safety": {
                "shijiu_requests": 0,
                "shijiu_create_requests": 0,
                "shijiu_update_requests": 0,
                "shijiu_cos_upload_requests": 0,
                "writer_mutex_evidence_generated": False,
            },
        }
        _write_json_atomic(root / REPORT_RELATIVE_PATH, blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["handoff_decision"] == READY else 2
