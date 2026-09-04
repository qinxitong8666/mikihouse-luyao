from __future__ import annotations

import copy
import json
import urllib.error
import warnings

import pytest

from mikihouse_luyao.shijiu_shadow import (
    ShadowControlPlaneClient,
    mutation_context,
)


class Response:
    def __init__(self, payload: dict, status: int = 202):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def emit(client: ShadowControlPlaneClient) -> str:
    return client.emit(
        operation="UPDATE",
        target_type="PRODUCT",
        context=mutation_context("10-1234-567", backend_product_id="42"),
        scope={"payload_sha256": "a" * 64},
    )


@pytest.mark.parametrize(
    "decision",
    [
        "WOULD_ALLOW",
        "WOULD_SKIP_FOREIGN_OWNER",
        "WOULD_FAIL_CLOSED_UNKNOWN_OWNER",
        "WOULD_FAIL_CLOSED_QUARANTINED",
        "WOULD_FAIL_CLOSED_LEASE",
    ],
)
def test_every_shadow_decision_is_observational_only(decision: str) -> None:
    posted = []

    def opener(request, timeout):
        posted.append((json.loads(request.data), timeout))
        return Response({"decision": decision})

    client = ShadowControlPlaneClient(
        endpoint="https://control.example.test", tenant="tenant-secret", opener=opener
    )
    assert emit(client) == decision
    event = posted[0][0]
    assert event["mode"] == "shadow"
    assert event["identity"]["writer"] == "MIKIHOUSE"
    assert event["intent"]["expected_owner"] == "MIKIHOUSE"
    assert "tenant-secret" not in json.dumps(event)
    assert "10-1234-567" not in json.dumps(event)


@pytest.mark.parametrize(
    "opener",
    [
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("DNS failure")
        ),
        lambda *_args, **_kwargs: Response({}, status=500),
        lambda *_args, **_kwargs: Response({"not_decision": "bad"}),
    ],
)
def test_timeout_500_dns_and_invalid_response_fail_open(opener) -> None:
    client = ShadowControlPlaneClient(endpoint="https://control.example.test", opener=opener)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = emit(client)
    assert result == "SHADOW_TELEMETRY_UNAVAILABLE"
    assert len(client.events) == 1


def test_disabled_or_unsafe_endpoint_never_opens_network() -> None:
    calls = []
    client = ShadowControlPlaneClient(endpoint=None, opener=lambda *args, **kwargs: calls.append(args))
    assert emit(client) == "SHADOW_TELEMETRY_UNAVAILABLE"
    client.endpoint = "http://external.example.test"
    assert emit(client) == "SHADOW_TELEMETRY_UNAVAILABLE"
    assert calls == []


def test_emission_does_not_mutate_business_input() -> None:
    context = mutation_context("10-1234-567", variant_ids=["MIKIHOUSE:10-1234-567:v1"])
    scope = {"payload": {"id": 42, "stock": 1}}
    before_context = copy.deepcopy(context)
    before_scope = copy.deepcopy(scope)
    client = ShadowControlPlaneClient()
    client.emit(operation="UPDATE", target_type="PRODUCT", context=context, scope=scope)
    assert context == before_context
    assert scope == before_scope
