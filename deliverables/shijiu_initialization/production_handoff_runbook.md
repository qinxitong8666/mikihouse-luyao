# MIKIHOUSE production handoff

Current state: `PREPARATION_ONLY / SHIJIU_WRITE_BLOCKED_CONCURRENT_WRITER`.

When WAWU has stopped, tell Codex only: **“WAWU已停止”**. Do not edit JSON, hashes, product lists, checkpoints, or credentials.

Codex must then read current `main` and `AGENTS.md`, start from a clean HEAD, and run:

```bash
PYTHONPATH=src .venv/bin/python scripts/prepare_mikihouse_production_handoff.py
```

The command performs a fresh complete Storefront crawl, rebuilds stable/exclusion and incremental state, rebuilds the 170-batch initialization plan and frozen pilot, validates all policy/identity hashes and disjointness, and fully downloads/decodes every official image required by the current 20-product pilot. It does not import a Shijiu client, call Shijiu, upload COS images, generate writer evidence, or expose a write/bypass flag.

If every check passes, the only permitted response is `READY_TO_REQUEST_EXCLUSIVE_WRITER_WINDOW`. A separate explicit task must then confirm all other writers stopped and authorize the exclusive MIKIHOUSE writer window. `BLOCKED` means no writer window may be requested until the listed machine-readable failures are resolved.

Even after authorization, the frozen pilot protocol remains: exact fresh 20 only; product/stage checkpoints; no mutation retry; exact-name candidate enumeration plus complete MIKI SKU-set `UNIQUE_STRONG_MATCH`; strong readback after every stage; freeze the entire pilot on ambiguity, transport uncertainty, freshness/resource drift, or target mismatch. The 2387-product initialization may not start until all 20 pass. Legacy286 cleanup remains a separate task.
