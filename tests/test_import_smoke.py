"""导入链冒烟测试：所有模块都能被 import，且没有指向已删除模块的 import 语句。

这一类事故的代价很高，而且症状具有误导性：

    2026-10-26 17:35:36  ModuleNotFoundError: No module named 'app.<被删掉的模块>'
    2026-10-26 17:35:36  Function instance exited unexpectedly(code 1)

部署日志里只有一段 import 栈，看不出"文件被人删了"。
实际原因是被删的文件在镜像构建前就不在了（平台合规扫描自动回滚了那个文件），
而 import 语句还留在 assessment_service.py 里。

所以这里做两件事：

1. **静态扫描**（不执行代码）：把仓库里所有 .py 的 import 语句解析成 AST，
   凡是指向本仓库模块的，逐个验证目标文件真实存在。
   —— 改文件名/移动文件后忘了改 import，这一关就红。
2. **动态导入**（真执行）：把 app/ 下每个模块和 main 依次 import 一遍。
   —— 语法错误、循环 import、模块级初始化炸掉，这一关就红。
   main 是必测项：容器启动链是 app.py → main.py → app/*，它断在哪一环都会
   表现成"部署失败"。

纯本地，不发起网络请求。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL}  {label}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}  {detail}".strip())


# ---------------------------------------------------------------------------
# 1. 静态扫描：import 语句指向的本地模块必须存在
# ---------------------------------------------------------------------------

def _repo_modules() -> dict[str, Path]:
    """本仓库里可被 import 的模块名 → 文件路径。

    根目录的 app.py 与 app/ 包同名，靠包优先于模块的规则，
    `import app` 拿到的是包。这里按「包」登记，与运行时一致。
    """
    found: dict[str, Path] = {}
    for path in sorted(ROOT.glob("*.py")):
        if path.name == "app.py":
            continue  # 与 app/ 包同名，实际被包遮蔽，不作为模块入口
        found[path.stem] = path
    for path in sorted((ROOT / "app").glob("*.py")):
        if path.name == "__init__.py":
            continue
        found[f"app.{path.stem}"] = path
    return found


MODULES = _repo_modules()
ROOT_NAMES = {name.split(".")[0] for name in MODULES}
# 仓库根目录下可作为模块导入的顶层名（含 app 包本身）
ROOT_NAMES.add("app")


def _dotted_exists(dotted: str) -> bool:
    """dotted 名（app.rag_service）是否对应真实文件。"""
    parts = dotted.split(".")
    base = ROOT
    for index, part in enumerate(parts):
        if index == len(parts) - 1:
            return (base / f"{part}.py").exists() or (base / part / "__init__.py").exists()
        if not (base / part / "__init__.py").exists():
            return False
        base = base / part
    return False


def _relative_target(node: ast.ImportFrom, source: Path) -> str | None:
    """相对 import（from .x import y）解析成仓库内的点分名。"""
    if node.level <= 0:
        return None
    package_parts = source.parent.relative_to(ROOT).parts
    # level=1 指当前包，level=2 指上一级
    if node.level - 1 > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - (node.level - 1)]
    if node.module:
        base_parts = base_parts + tuple(node.module.split("."))
    return ".".join(base_parts) if base_parts else None


def _check_imports() -> None:
    sources = sorted(ROOT.glob("*.py")) + sorted((ROOT / "app").glob("*.py"))
    broken: list[str] = []

    for source in sources:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except SyntaxError as exc:
            broken.append(f"{source.relative_to(ROOT)} 语法错误：{exc}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    target = _relative_target(node, source)
                    if target and not _dotted_exists(target):
                        broken.append(
                            f"{source.relative_to(ROOT)}:{node.lineno} 相对 import 指向不存在的模块 {target}"
                        )
                    continue
                if not node.module:
                    continue
                root = node.module.split(".")[0]
                if root in ROOT_NAMES and not _dotted_exists(node.module):
                    broken.append(
                        f"{source.relative_to(ROOT)}:{node.lineno} import 的本地模块不存在："
                        f"from {node.module} import ..."
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in ROOT_NAMES and not _dotted_exists(alias.name):
                        broken.append(
                            f"{source.relative_to(ROOT)}:{node.lineno} import 的本地模块不存在："
                            f"import {alias.name}"
                        )

    check(
        f"静态 import 扫描（{len(sources)} 个文件）：无指向缺失模块的 import",
        not broken,
        "\n      " + "\n      ".join(broken),
    )


# ---------------------------------------------------------------------------
# 2. 动态导入：app/* 与 main 真跑一遍
# ---------------------------------------------------------------------------

def _check_dynamic_imports() -> None:
    targets = ["main"] + sorted(
        f"app.{p.stem}" for p in (ROOT / "app").glob("*.py") if p.stem != "__init__"
    )
    broken: list[str] = []
    for name in targets:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 冒烟测试要看到任何异常
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    check(
        f"动态 import（{len(targets)} 个模块，含 main）",
        not broken,
        "\n      " + "\n      ".join(broken),
    )


# ---------------------------------------------------------------------------
# 3. 入口文件：模拟容器里 `python app.py` 的 import 链
# ---------------------------------------------------------------------------

def _check_entrypoint() -> None:
    spec = importlib.util.spec_from_file_location("entry_smoke", ROOT / "app.py")
    if spec is None or spec.loader is None:
        check("入口 app.py 可加载", False, "spec_from_file_location 返回空")
        return
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        check("入口 app.py 的 import 链（app.py → main → app/*）", False, f"{type(exc).__name__}: {exc}")
        return
    check("入口 app.py 的 import 链（app.py → main → app/*）", hasattr(module, "app"))


if __name__ == "__main__":
    print("=" * 68)
    print("导入链冒烟测试")
    print("=" * 68)
    _check_imports()
    _check_dynamic_imports()
    _check_entrypoint()
    print("-" * 68)
    if failures:
        print(f"失败 {len(failures)} 项")
        sys.exit(1)
    print("全部通过")
