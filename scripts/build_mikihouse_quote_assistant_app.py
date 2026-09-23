#!/usr/bin/env python3
from __future__ import annotations

import argparse
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "MIKI HOUSE 报价助手.app"
EXECUTABLE_NAME = "mikihouse-quote-assistant"
SWIFT_SOURCE = ROOT / "macos" / "MikihouseQuoteAssistant" / "main.swift"


def info_plist() -> dict[str, object]:
    return {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": "MIKI HOUSE 报价助手",
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": "cn.luyao.mikihouse.quoteassistant",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "MIKI HOUSE 报价助手",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    }


def build_bundle(bundle: Path) -> dict[str, object]:
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    plist_path = contents / "Info.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(info_plist(), handle, sort_keys=True)
    executable = macos / EXECUTABLE_NAME
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise RuntimeError(f"unsupported macOS architecture: {architecture}")
    subprocess.run(
        [
            "/usr/bin/xcrun",
            "swiftc",
            "-module-cache-path",
            "/tmp/mikihouse-quote-assistant-swift-cache",
            str(SWIFT_SOURCE),
            "-target",
            f"{architecture}-apple-macosx13.0",
            "-framework",
            "AppKit",
            "-o",
            str(executable),
        ],
        cwd=ROOT,
        check=True,
    )
    shutil.copy2(SWIFT_SOURCE, resources / "main.swift")
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    (resources / "安装说明.txt").write_text(
        "本应用必须保留在 mikihouse-luyao 仓库根目录，并使用仓库 .venv。\n"
        "正式微信收藏保存继续受 config/wechat_favorite_runtime.json 和精确确认语句保护。\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            "--timestamp=none",
            str(bundle),
        ],
        check=True,
    )
    return {
        "status": "PASS",
        "bundle": str(bundle),
        "executable": str(executable),
        "executable_mode": oct(executable.stat().st_mode & 0o777),
        "architecture": architecture,
        "native_appkit": True,
        "code_signature": "AD_HOC",
        "bundle_identifier": info_plist()["CFBundleIdentifier"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the double-clickable MIKI HOUSE quote assistant app")
    parser.add_argument("--output", type=Path, default=DEFAULT_BUNDLE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_bundle(args.output.resolve())
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
