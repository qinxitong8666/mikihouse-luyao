from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


REQUIRED_REPOSITORY_FILES = (
    "pyproject.toml",
    "special_skus_2026aw.csv",
    "config/daily_quote.json",
    "config/wechat_favorite_runtime.json",
    "scripts/generate_daily_quote.py",
    "scripts/run_mikihouse_daily_production.py",
)


def check_quote_assistant_runtime(repository_root: Path) -> dict[str, Any]:
    root = repository_root.resolve()
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def record(name: str, passed: bool, message: str) -> None:
        checks.append({"name": name, "passed": passed, "message": message})
        if not passed:
            errors.append(message)

    record(
        "repository_directory",
        root.is_dir(),
        "仓库目录可访问" if root.is_dir() else f"仓库目录不存在：{root}",
    )
    missing = [name for name in REQUIRED_REPOSITORY_FILES if not (root / name).is_file()]
    record(
        "repository_markers",
        not missing,
        "仓库文件不完整：" + "、".join(missing) if missing else "仓库定位和必要文件完整",
    )
    python = root / ".venv" / "bin" / "python"
    python_ok = python.is_file() and os.access(python, os.X_OK)
    record(
        "python_environment",
        python_ok,
        "项目 Python 环境可用" if python_ok else f"找不到可执行的项目 Python：{python}",
    )
    try:
        import PIL  # noqa: F401
        import pypdf  # noqa: F401
        import reportlab  # noqa: F401

        dependency_ok = True
    except ImportError:
        dependency_ok = False
    record(
        "python_dependencies",
        dependency_ok,
        "Python 依赖不完整，请在仓库中重新安装 .venv。" if not dependency_ok else "PDF 和图片依赖可导入",
    )
    try:
        daily_config = json.loads((root / "config" / "daily_quote.json").read_text(encoding="utf-8"))
        runtime_config = json.loads(
            (root / "config" / "wechat_favorite_runtime.json").read_text(encoding="utf-8")
        )
        config_ok = (
            daily_config.get("shijiu_requests_enabled") is False
            and daily_config.get("wechat_write_enabled") is False
            and runtime_config.get("production_save_enabled") is False
            and runtime_config.get("target_bundle_id") == "com.tencent.xinWeChot2"
        )
    except (OSError, json.JSONDecodeError):
        config_ok = False
    record(
        "safety_configuration",
        config_ok,
        "安全配置无效；仓库默认生产开关必须保持关闭。" if not config_ok else "默认生产开关关闭；App 单次授权模式可用",
    )
    output_root = root / "outputs" / "daily_quote"
    output_parent = output_root if output_root.exists() else output_root.parent
    output_ok = output_parent.is_dir() and os.access(output_parent, os.W_OK)
    record(
        "output_directory",
        output_ok,
        "输出目录可写" if output_ok else f"输出目录不可写：{output_root}",
    )
    return {
        "schema_version": 1,
        "status": "READY" if not errors else "BLOCKED",
        "repository_root": str(root),
        "python_executable": sys.executable,
        "checks": checks,
        "errors": errors,
        "shijiu_request_count": 0,
        "wechat_mutation_count": 0,
    }
