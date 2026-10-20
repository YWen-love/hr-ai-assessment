from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root)
    return project_root()


def resource_path(path: str | Path) -> Path:
    resolved_path = Path(path)
    if resolved_path.is_absolute():
        return resolved_path
    return resource_root() / resolved_path


def _can_use_modelscope_persistent() -> bool:
    """检测当前是否在魔搭容器内（/mnt/workspace 可读写）。

    魔搭创空间的持久化目录是 /mnt/workspace（容器重启数据不丢），
    不是 /data。本地开发无此目录，自动回退到 app/data/。
    """
    if not sys.platform.startswith("linux"):
        return False
    data_dir = Path("/mnt/workspace")
    if not data_dir.is_dir():
        return False
    try:
        test_file = data_dir / ".hr-ai-write-test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return True
    except OSError:
        return False


def data_root() -> Path:
    configured_path = os.getenv("APP_DATA_DIR")
    if configured_path:
        path = Path(configured_path)
    elif _can_use_modelscope_persistent():
        # 魔搭容器：使用 /mnt/workspace 持久化目录（容器重启不丢）
        path = Path("/mnt/workspace/hr-ai-assessment")
    else:
        app_root = project_root()
        if getattr(sys, "frozen", False):
            path = next(
                (
                    candidate_root / "app" / "data"
                    for candidate_root in (app_root, *app_root.parents)
                    if (candidate_root / "app" / "data").is_dir()
                ),
                app_root / "app" / "data",
            )
        else:
            path = app_root / "app" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
