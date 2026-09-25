# MIKIHOUSE Phase I Shadow control-plane integration

Issue: `qinxitong8666/mikihouse-luyao#5`
Upstream contract: `qinxitong8666/shijiu-10kaifa#63`, commit `4d08b93140e800691ac0b977a06ad762b7696beb`

## Safety boundary

This integration is telemetry-only. It supports `ENFORCEMENT_MODE=shadow` and has no code path that can block, skip, retry, alter, or authorize a SHIJIU mutation. A missing endpoint, timeout, DNS error, HTTP error, malformed response, or unrecognized decision becomes `SHADOW_TELEMETRY_UNAVAILABLE`; the existing mutation proceeds with its original payload and exception semantics. It does not replace the existing write-confirmation gate or host-local mutex.

No live command was run for this change. No product, inventory, price, listing, freight, mapping, checkpoint, or production configuration was changed.

## Contract

When `SHIJIU_SHADOW_CONTROL_PLANE_URL` is configured, the client sends `POST /v1/shadow/events` with:

- `mode=shadow`;
- writer identity: `MIKIHOUSE`, repository, branch, and commit SHA;
- intent: operation, target type, target hash, expected owner `MIKIHOUSE`, and scope;
- SHA-256 tenant, source-product, backend-product, variant/mapping, source-image, and payload evidence where applicable.

Only HTTPS endpoints are allowed remotely. Loopback HTTP is accepted for a protected local tunnel. Raw tenant IDs, product numbers, backend IDs, variant IDs, URLs, payloads, credentials, cookies, and authorization values are not emitted.

Recognized decisions are `WOULD_ALLOW`, `WOULD_SKIP_FOREIGN_OWNER`, `WOULD_FAIL_CLOSED_UNKNOWN_OWNER`, `WOULD_FAIL_CLOSED_QUARANTINED`, and `WOULD_FAIL_CLOSED_LEASE`. All remain observations in Phase I.

## Mutation-site inventory

All real writer families converge on `ShijiuLiveClient`, so the transport boundary covers:

| Writer family | Mutation path | Shadow operation |
|---|---|---|
| first batch / canonical create / complex batch / recovery | `create_product` | `CREATE` |
| minimal / high-SKU / staged detail/media / richtext / production architecture | `create_product_native` | `CREATE` |
| staged detail/media / richtext / production architecture | `update_product_native` | `UPDATE` |
| all image-bearing import and recovery flows | `upload_image` | `UPLOAD` |
| normal post-create, controlled recovery, minimal-probe mapping adoption, and canonical unknown-create reconciliation | verified mapping persistence functions | `OWNERSHIP_SEED_PROPOSAL` |

Seed proposals are emitted only after the existing strong readback has established a stable MIKIHOUSE product number and backend identity. Historical unknown ownership is never guessed. The proposal result is not consulted by mapping persistence.

## Compatibility evidence

The tests prove:

- every allowed `WOULD_*` result remains observational;
- timeout, DNS failure, HTTP 500, malformed response, missing endpoint, and unsafe endpoint all reduce to `SHADOW_TELEMETRY_UNAVAILABLE`;
- emission does not mutate caller-owned business context or scope;
- secrets and raw stable identities are absent from the emitted event;
- the complete existing import, recovery, staged-media, reconciliation, and writer suite still passes.

The implementation deliberately does not include enforcement branching. Entering enforcement requires a separate Issue and is not authorized here.

## Configuration

- `ENFORCEMENT_MODE=shadow` (the only supported active mode)
- `SHIJIU_SHADOW_CONTROL_PLANE_URL`
- `SHIJIU_TENANT_ID`
- `SHIJIU_WRITER_BRANCH`
- `SHIJIU_WRITER_SHA`

The central PostgreSQL/mTLS service is not yet live, so this repository does not depend on a reachable service and no real shadow event was transmitted during validation.
