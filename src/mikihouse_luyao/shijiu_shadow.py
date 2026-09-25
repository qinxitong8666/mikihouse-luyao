"""Best-effort Phase I ownership/lease shadow telemetry.

This module is deliberately incapable of enforcement.  Its return value is
diagnostic only and callers must never branch their mutation behaviour on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable


DECISIONS = {
    "WOULD_ALLOW",
    "WOULD_SKIP_FOREIGN_OWNER",
    "WOULD_FAIL_CLOSED_UNKNOWN_OWNER",
    "WOULD_FAIL_CLOSED_QUARANTINED",
    "WOULD_FAIL_CLOSED_LEASE",
}


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mutation_context(
    product_number: str,
    *,
    backend_product_id: str | int | None = None,
    variant_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a secret-free stable alias/proof for one MIKIHOUSE mutation."""
    aliases = [f"MIKIHOUSE:{product_number}"]
    aliases.extend(variant_ids or [])
    return {
        "source_product_hash": stable_hash(aliases[0]),
        "mapping_proof_hash": stable_hash("\n".join(sorted(aliases))),
        "backend_product_hash": (
            stable_hash(f"SHIJIU:{backend_product_id}")
            if backend_product_id not in (None, "")
            else None
        ),
    }


@dataclass
class ShadowControlPlaneClient:
    endpoint: str | None = None
    tenant: str | None = None
    repo: str = "qinxitong8666/mikihouse-luyao"
    branch: str = ""
    sha: str = ""
    timeout: float = 0.75
    opener: Callable[..., Any] = urllib.request.urlopen
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "ShadowControlPlaneClient":
        mode = os.getenv("ENFORCEMENT_MODE", "shadow").strip().lower()
        if mode != "shadow":
            warnings.warn("MIKIHOUSE control plane supports shadow mode only; telemetry disabled")
            return cls()
        return cls(
            endpoint=os.getenv("SHIJIU_SHADOW_CONTROL_PLANE_URL") or None,
            tenant=os.getenv("SHIJIU_TENANT_ID") or None,
            branch=os.getenv("SHIJIU_WRITER_BRANCH", ""),
            sha=os.getenv("SHIJIU_WRITER_SHA", ""),
        )

    def _safe_endpoint(self) -> bool:
        if not self.endpoint:
            return False
        parsed = urllib.parse.urlparse(self.endpoint)
        return parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        )

    def emit(
        self,
        *,
        operation: str,
        target_type: str,
        context: dict[str, Any] | None,
        scope: dict[str, Any],
    ) -> str:
        target_material = context or {"unresolved": True}
        event = {
            "mode": "shadow",
            "identity": {
                "writer": "MIKIHOUSE",
                "repo": self.repo,
                "branch": self.branch,
                "sha": self.sha,
            },
            "intent": {
                "operation": operation,
                "target_type": target_type,
                "target_hash": stable_hash(json.dumps(target_material, sort_keys=True)),
                "expected_owner": "MIKIHOUSE",
                "scope": {
                    "tenant_hash": stable_hash(self.tenant) if self.tenant else None,
                    **scope,
                    **target_material,
                },
            },
        }
        self.events.append(event)
        if not self._safe_endpoint():
            event["shadow_result"] = "SHADOW_TELEMETRY_UNAVAILABLE"
            return "SHADOW_TELEMETRY_UNAVAILABLE"
        try:
            request = urllib.request.Request(
                self.endpoint.rstrip("/") + "/v1/shadow/events",
                data=json.dumps(event, separators=(",", ":")).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.opener(request, timeout=self.timeout) as response:
                if getattr(response, "status", 200) not in {200, 202}:
                    event["shadow_result"] = "SHADOW_TELEMETRY_UNAVAILABLE"
                    return "SHADOW_TELEMETRY_UNAVAILABLE"
                decision = json.loads(response.read()).get("decision")
            result = decision if decision in DECISIONS else "SHADOW_TELEMETRY_UNAVAILABLE"
            event["shadow_result"] = result
            return result
        except Exception as error:  # telemetry is strictly fail-open in Phase I
            warnings.warn(f"MIKIHOUSE shadow telemetry unavailable: {type(error).__name__}")
            event["shadow_result"] = "SHADOW_TELEMETRY_UNAVAILABLE"
            return "SHADOW_TELEMETRY_UNAVAILABLE"
