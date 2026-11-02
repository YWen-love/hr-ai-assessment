"""死代码清理的回归测试。

背景：项目里长期并存两套岗位信息来源 ——
  - app/job_reader.py：直接从 job_intro.md 读文件；
  - app/knowledge_base.py：把同样的内容灌进 rag_knowledge.db。
两套并存、互相不知道对方存在，正是之前「知识库三个岗位、配置表两个岗位」
对不上的根源之一。

另外 main.py 里还有一个 render_hr_page()：手写的整页 HTML（自带 CSS、
岗位下拉、结果区）。Gradio 工作台上线之后它就没有任何调用点了，
只是一直没删。

再后来 /hr/parse-link 拿掉知识库那道闸之后，app/knowledge_base.py 里
那套「反过来问知识库有哪些岗位」的读接口
（list_jobs / get_job_knowledge / jobs_needing_rules / save_screening_rules）
也全成了死代码 —— 它们只会给出第二份岗位名单，正是这次要消灭的东西。

最后整个知识库退役：它把 job_intro.md / screen_rules.md 灌进 rag_knowledge.db，
可从来没人读它 —— 一份只写不读的库，存的却是一份会误导人的第二份岗位名单。
知识库唯一还有用的一小块（job_intro.md 的 ===岗位名=== 段落解析，JD 回填要用）
跟着它唯一的用户搬进了 app/job_config_store.py。

这里守住七条线：
  1. app/job_reader.py 不许回来；
  2. main.py 里不许再有 render_hr_page；
  3. 全仓源码都不许再出现 job_reader / render_hr_page（防止换个壳复活）；
  4. 知识库不许再提供第二份岗位名单读接口；
  5. app/knowledge_base.py 整个不许回来，initialize_knowledge_base 也不许复活；
  6. 该留的还得留：job_intro.md 的段落解析（JD 回填要用）跟着配置表走；
  7. screen_rules.md 彻底退休，运行时和仓库里都不许再有它。

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


section("5. 知识库整个退役了")

# 知识库是把 job_intro.md / screen_rules.md 灌进 rag_knowledge.db 的那套东西。
# 它只写不读，却存着第二份岗位名单 —— 留着就等于把当初那个坑重新挖开。
#
# 这里查的是「有没有定义 / 有没有被引用」，不是「源码里有没有出现这几个字」——
# 注释和 docstring 里提一嘴「以前有这套接口、为什么删的」是正经文档，
# 不该被当成接口复活。判断依据看 AST：定义、import、属性访问。
import ast

kb_dead_api = [
    "list_jobs",
    "get_job_knowledge",
    "jobs_needing_rules",
    "save_screening_rules",
    "generate_missing_screening_rules",
    "JobKnowledge",
]

defined: list[str] = []
imported: list[str] = []
accessed: list[str] = []
kb_imported: list[str] = []
init_defined: list[str] = []

for path in source_files():
    rel = str(path.relative_to(ROOT))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        continue

    for node in ast.walk(tree):
        # 定义：def / class
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in kb_dead_api:
                defined.append(f"{rel} -> def/class {node.name}")
            if node.name == "initialize_knowledge_base":
                init_defined.append(f"{rel} -> def {node.name}")
        # from app.knowledge_base import list_jobs
        elif isinstance(node, ast.ImportFrom) and (node.module or "").endswith("knowledge_base"):
            kb_imported.append(f"{rel} -> from {node.module} import ...")
            for alias in node.names:
                if alias.name in kb_dead_api:
                    imported.append(f"{rel} -> import {alias.name}")
        # app.knowledge_base.list_jobs(...)
        elif isinstance(node, ast.Attribute) and node.attr in kb_dead_api:
            accessed.append(f"{rel} -> .{node.attr}")

check("这些接口没有被定义在任何地方", not defined, "; ".join(defined))
check("这些接口没有被 import", not imported, "; ".join(imported))
check("这些接口没有被当属性访问", not accessed, "; ".join(accessed))

# 模块本身也不许回来
kb_module = ROOT / "app" / "knowledge_base.py"
check("app/knowledge_base.py 不存在", not kb_module.exists(), str(kb_module))
check("app/ 包里没有 knowledge_base 模块", not any(p.stem == "knowledge_base" for p in (ROOT / "app").glob("*.py")))
check("没有源码 import app.knowledge_base", not kb_imported, "; ".join(kb_imported))
check("initialize_knowledge_base 没有被定义", not init_defined, "; ".join(init_defined))

main_text = (ROOT / "main.py").read_text(encoding="utf-8")
check("main.py 里没有 initialize_knowledge_base", "initialize_knowledge_base" not in main_text)

# 该留的还得留：JD 回填靠 job_intro.md 的段落解析，
# 它跟着唯一的用户搬进了 app/job_config_store.py。
store_source = (ROOT / "app" / "job_config_store.py").read_text(encoding="utf-8")
check("_parse_marked_sections 还在（JD 回填要用）", "_parse_marked_sections" in store_source)

try:
    py_compile.compile(str(ROOT / "app" / "job_config_store.py"), doraise=True)
    check("job_config_store.py 编译通过", True)
except py_compile.PyCompileError as error:
    check("job_config_store.py 编译通过", False, str(error))

section("6. screen_rules.md 退休了")

# screen_rules.md 是硬筛/追问话术稿，运行时从来只有知识库读它，
# 知识库退役之后它就没人读了 —— 话术稿留着一个会误导人的副本，
# 硬筛条件该在哪儿就在哪儿（job_configs/*.json 里）。
check("根目录没有 screen_rules.md", not (ROOT / "screen_rules.md").exists())
check("docs/ 里也没有 screen_rules.md", not (ROOT / "docs" / "screen_rules.md").exists())

rule_readers: list[str] = []
for path in source_files():
    text = path.read_text(encoding="utf-8", errors="replace")
    if "screen_rules" in text:
        rule_readers.append(str(path.relative_to(ROOT)))

check("没有源码再引用 screen_rules", not rule_readers, "; ".join(rule_readers))


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
