#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PREFIXES = ("state/", "deliverables/")
SENSITIVE_ENV_KEYS = (
    "SHIJIU_TOKEN",
    "SHIJIU_SECRET",
    "SHIJIU_COOKIE",
    "SHIJIU_AUTHORIZATION",
    "SHIJIU_WRITE_CONFIRMATION",
)


def run(cmd: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def git_files(*patterns: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *patterns],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [part.decode("utf-8") for part in result.stdout.split(b"\0") if part]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for relative in git_files("state", "deliverables"):
        if not relative.startswith(PROTECTED_PREFIXES):
            continue
        path = ROOT / relative
        if path.is_file():
            snapshot[relative] = sha256_file(path)
    return snapshot


def verify_json_configs() -> None:
    configs = sorted((ROOT / "config").glob("*.json"))
    if not configs:
        raise SystemExit("FAIL: no config/*.json files found")
    for path in configs:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
        print(f"PASS: {path.relative_to(ROOT)}")


def sanitized_test_env(pycache_root: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = pycache_root
    env["MIKIHOUSE_VERIFY_OFFLINE"] = "1"
    for key in SENSITIVE_ENV_KEYS:
        env.pop(key, None)
    return env


def main() -> int:
    os.chdir(ROOT)
    before = protected_snapshot()

    print("[1/6] Git whitespace checks")
    run(["git", "diff", "--check"])
    run(["git", "diff", "--cached", "--check"])

    with tempfile.TemporaryDirectory(prefix="mikihouse-verify-") as temp_dir:
        env = sanitized_test_env(str(Path(temp_dir) / "pycache"))

        print("\n[2/6] Tracked Python syntax check")
        py_files = git_files("*.py")
        if not py_files:
            raise SystemExit("FAIL: no tracked Python files found")
        run([sys.executable, "-m", "py_compile", *py_files], env=env)
        print(f"PASS: tracked Python syntax ({len(py_files)} files)")

        print("\n[3/6] Offline pytest suite")
        run([sys.executable, "-m", "pytest", "-q"], env=env)

    print("\n[4/6] Config JSON parse check")
    verify_json_configs()

    print("\n[5/6] Node helper syntax check")
    node = shutil.which("node")
    helpers = [
        ROOT / "scripts" / "shijiu_browser_exact_capture.mjs",
        ROOT / "scripts" / "shijiu_ui_context_reconcile.mjs",
    ]
    existing_helpers = [helper for helper in helpers if helper.exists()]
    if node:
        for helper in existing_helpers:
            run([node, "--check", str(helper)])
        print(f"PASS: Shijiu browser helper syntax ({len(existing_helpers)} files)")
    elif existing_helpers:
        print("NOT CAPTURED: node is unavailable; browser helper syntax checks skipped")
    else:
        print("NOT APPLICABLE: Shijiu browser helpers not present")

    print("\n[6/6] Protected state / deliverables check")
    after = protected_snapshot()
    if before != after:
        before_keys = set(before)
        after_keys = set(after)
        changed = sorted(
            key
            for key in before_keys & after_keys
            if before[key] != after[key]
        )
        added = sorted(after_keys - before_keys)
        removed = sorted(before_keys - after_keys)
        print(f"FAIL: protected tracked files changed: changed={changed}, added={added}, removed={removed}")
        return 1
    print(f"PASS: protected tracked files unchanged ({len(after)} files)")

    print("\nPASS: offline local verification completed successfully.")
    print("Safety: Shijiu credential variables were removed for pytest; no live-write command was invoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
