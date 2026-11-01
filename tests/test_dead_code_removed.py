"""死代码清理的回归测试。

背景：项目里长期并存两套岗位信息来源 ——
  - app/job_reader.py：直接从 job_intro.md 读文件；
  - app/knowledge_base.py：把同样的内容灌进 rag_knowledge.db。
两套并存、互相不知道对方存在，正是之前「知识库三个岗位、配置表两个岗位」
对不上的根源之一。

另外 main.py 里还有一个 render_hr_page()：手写的整页 HTML（自带 CSS、
岗位下拉、结果区）。Gradio 工作台上线之后它就没有任何调用点了，
只是一直没删。

这里守住三条线：
  1. app/job_reader.py 不许回来；
  2. main.py 里不许再有 render_hr_page；
  3. 全仓源码都不许再出现 job_reader / render_hr_page（防止换个壳复活）。

纯文本检查，不碰数据库，也不发网络请求。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


# 要扫的源码：根目录的 .py、app/ 包、scripts/。
# 排除 tests/（本文件自己就写着这些名字）和 __pycache__。
def source_files() -> list[Path]:
    files: list[Path] = sorted(ROOT.glob("*.py"))
    files += sorted(p for p in (ROOT / "app").rglob("*.py") if "__pycache__" not in p.parts)
    files += sorted(p for p in (ROOT / "scripts").glob("*.py") if "__pycache__" not in p.parts)
    return files


section("1. app/job_reader.py 已经删掉")

reader = ROOT / "app" / "job_reader.py"
check("app/job_reader.py 不存在", not reader.exists(), str(reader))
check("app/ 包里没有 job_reader 模块", not any(p.stem == "job_reader" for p in (ROOT / "app").glob("*.py")))

section("2. main.py 里没有 render_hr_page")

main_source = (ROOT / "main.py").read_text(encoding="utf-8")
check("main.py 里没有 render_hr_page", "render_hr_page" not in main_source)
check("main.py 里没有 html.escape", "html.escape" not in main_source)
check("main.py 里没有裸 import html", "\nimport html\n" not in main_source)

section("3. 全仓源码都不许再引用这两个死名字")

offenders: list[str] = []
for path in source_files():
    text = path.read_text(encoding="utf-8", errors="replace")
    for token in ("job_reader", "render_hr_page"):
        if token in text:
            offenders.append(f"{path.relative_to(ROOT)} -> {token}")

check("没有源码引用 job_reader / render_hr_page", not offenders, "; ".join(offenders))
check("确实扫到了源码文件", len(source_files()) > 10, f"{len(source_files())} 个")

section("4. main.py 还能正常编译")

import py_compile

try:
    py_compile.compile(str(ROOT / "main.py"), doraise=True)
    check("main.py 编译通过", True)
except py_compile.PyCompileError as error:
    check("main.py 编译通过", False, str(error))


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
