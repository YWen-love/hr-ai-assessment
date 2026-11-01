"""工作台岗位下拉的自动刷新。

背景：岗位管理（/hr/jobs/）和测评工作台（/hr）是两个各自挂载的 Gradio 应用
（见 app/gradio_ui.py 开头的说明），页面之间没有推送机制。把两页合并成一个
Tab 的做法在本项目里被试过并否决了 —— 新建岗位的表单塞在工作台左栏，谁都看不清。

于是有一个真实的坑：
  HR 在标签页 A 打开测评工作台，在标签页 B 的岗位管理里新增 / 改名 / 删掉岗位，
  回到 A 时岗位下拉还是挂载那一刻的快照 —— 最坑的是「岗位已经被删了，
  这边还能拿它生成测评链接」。

解决方式是工作台自己探：每隔几秒读一次岗位列表，算一个「指纹」，
指纹没变就什么都不推（gr.skip()），变了才把新选项推下去。

这里守住六条线：
  1. 指纹稳：同样的列表算出同样的指纹；列表一变指纹就变；
  2. 没变就跳：返回 gr.skip()，不带 choices —— 正在点下拉的人不会被重绘踩空；
  3. 变了才推：推的时候保住还存在的选中项；
  4. 选中的被删了：退回第一项；
  5. 一个岗位都没有：value 是 None，不能是空串以外的怪东西；
  6. 兜底提示：拿着已删除的岗位去生成链接，要说清楚「下拉会自动刷新」。

另外确认定时刷新真的挂上了：工作台里恰好一个 gr.Timer，
它的 tick 恰好接的是 refresh_job_picker。

不碰真实数据库：list_job_configs / list_sessions 猴补丁成假数据。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gradio as gr

import app.gradio_ui as ui

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  <- {detail}" if not condition and detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


@contextmanager
def patched(**attrs):
    """临时替换 app.gradio_ui 模块里的名字（list_job_configs 等）。"""
    old = {key: getattr(ui, key) for key in attrs}
    for key, value in attrs.items():
        setattr(ui, key, value)
    try:
        yield
    finally:
        for key, value in old.items():
            setattr(ui, key, value)


def _job(job_key: str, job_name: str) -> dict:
    return {
        "job_key": job_key,
        "job_name": job_name,
        "source_type": "manual",
        "review_status": "reviewed",
        "raw_jd": "",
    }


def _fake_configs(*jobs: dict):
    def _list_job_configs(*args, **kwargs):
        return list(jobs)

    return _list_job_configs


def _fake_sessions(*sessions: dict):
    def _list_sessions(*args, **kwargs):
        return list(sessions)

    return _list_sessions


A = _job("key_a", "岗位 A")
B = _job("key_b", "岗位 B")
C = _job("key_c", "岗位 C")

CHOICES_AB = [("岗位 A", "key_a"), ("岗位 B", "key_b")]
CHOICES_BC = [("岗位 B", "key_b"), ("岗位 C", "key_c")]
CHOICES_C = [("岗位 C", "key_c")]


# ===========================================================================
section("1. 指纹：同样的列表算出同样的指纹，列表变了指纹就变")
# ===========================================================================

sig_ab = ui._job_choices_signature(CHOICES_AB)
sig_ab2 = ui._job_choices_signature([("岗位 A", "key_a"), ("岗位 B", "key_b")])
sig_bc = ui._job_choices_signature(CHOICES_BC)
sig_c = ui._job_choices_signature(CHOICES_C)
sig_empty = ui._job_choices_signature([])

check("同样的列表指纹相同", sig_ab == sig_ab2, f"{sig_ab} vs {sig_ab2}")
check("不同列表指纹不同", sig_ab != sig_bc)
check("多一个岗位指纹就变", sig_ab != sig_c)
check("空列表也有指纹", isinstance(sig_empty, str) and sig_empty != "")

# 只改岗位名也算变 —— HR 看到的是岗位名，改名就是变了
renamed = ui._job_choices_signature([("岗位 A 改名了", "key_a"), ("岗位 B", "key_b")])
check("只改岗位名指纹也变", renamed != sig_ab)

# 只改 job_key 也算变
rekeyed = ui._job_choices_signature([("岗位 A", "key_a2"), ("岗位 B", "key_b")])
check("只改 job_key 指纹也变", rekeyed != sig_ab)

# 顺序变了也要认出来 —— 列表顺序变了，HR 看到的下拉顺序就变了
reordered = ui._job_choices_signature(list(reversed(CHOICES_AB)))
check("顺序变了指纹也变", reordered != sig_ab)


# ===========================================================================
section("2. 没变就跳过：不推任何选项下去")
# ===========================================================================

with patched(list_job_configs=_fake_configs(A, B)):
    known = ui._job_choices_signature(ui._job_choices())
    check("准备好的指纹就是当前列表的指纹", known == sig_ab, known)

    update, new_sig = ui.refresh_job_picker("key_a", known)

    check("返回的是 gr.skip()", update == {"__type__": "update"}, str(update))
    check("skip 里不带 choices（不会重绘下拉）", "choices" not in update, str(update))
    check("指纹原样传回", new_sig == known, f"{new_sig} vs {known}")

    # 再问一次，结果一样（不是一次性生效）
    update2, new_sig2 = ui.refresh_job_picker("key_a", new_sig)
    check("重复探测结果一致", update2 == update and new_sig2 == new_sig)


section("2b. 探一次失败不该把整个工作台带崩")


def _boom_list(*args, **kwargs):
    raise RuntimeError("数据库炸了")


with patched(list_job_configs=_boom_list):
    update, new_sig = ui.refresh_job_picker("key_a", known)
    check("失败时返回 gr.skip()", update == {"__type__": "update"}, str(update))
    check("失败时保留旧指纹（下次再探）", new_sig == known, str(new_sig))


# ===========================================================================
section("3. 变了才推：新的选项下去，选中项还活着就保住")
# ===========================================================================

with patched(list_job_configs=_fake_configs(B, C)):
    # 已知指纹是旧的（A、B），现在库里变成（B、C）了
    update, new_sig = ui.refresh_job_picker("key_b", known)

    check("返回的是更新而不是 skip", update.get("__type__") == "update" and "choices" in update, str(update))
    check("推下去的是新选项", update.get("choices") == CHOICES_BC, str(update.get("choices")))
    check("还活着的选中项保住了", update.get("value") == "key_b", str(update.get("value")))
    check("指纹更新成新的", new_sig == sig_bc, f"{new_sig} vs {sig_bc}")


section("4. 选中的岗位被删了：退回第一项")

with patched(list_job_configs=_fake_configs(C)):
    # 旧列表 A、B，现在只剩 C —— HR 正选着的 key_b 没了
    update, new_sig = ui.refresh_job_picker("key_b", known)

    check("推下去的是剩下的选项", update.get("choices") == CHOICES_C, str(update.get("choices")))
    check("选中项退回第一项", update.get("value") == "key_c", str(update.get("value")))
    check("指纹更新成新的", new_sig == sig_c, f"{new_sig} vs {sig_c}")

with patched(list_job_configs=_fake_configs()):
    update, new_sig = ui.refresh_job_picker("key_b", known)

    check("一个岗位都没有时选项为空", update.get("choices") == [], str(update.get("choices")))
    check("一个岗位都没有时选中值是 None", update.get("value") is None, str(update.get("value")))
    check("指纹更新成空列表的指纹", new_sig == sig_empty, f"{new_sig} vs {sig_empty}")


# ===========================================================================
section("5. refresh_workbench 的第 6 个返回值就是岗位指纹")
# ===========================================================================

with patched(list_job_configs=_fake_configs(A, B), list_sessions=_fake_sessions()):
    result = ui.refresh_workbench(None, "key_a", None)

    check("一共返回 6 个值", len(result) == 6, str(len(result)))
    check("第 6 个是岗位指纹", result[5] == sig_ab, str(result[5]))
    check("指纹和当前列表一致", result[5] == ui._job_choices_signature(ui._job_choices()))

    picker_update = result[2]
    check("第 3 个是岗位下拉的更新", picker_update.get("choices") == CHOICES_AB, str(picker_update))
    check("下拉更新里带着当前选中项", picker_update.get("value") == "key_a", str(picker_update.get("value")))

    # 选中的岗位已经不在列表里 —— 下拉要退回第一项，不能留着一个空选项
    result2 = ui.refresh_workbench(None, "key_没了", None)
    check("选中项失效时退回第一项", result2[2].get("value") == "key_a", str(result2[2].get("value")))


# ===========================================================================
section("6. 工作台里确实挂上了定时刷新")
# ===========================================================================

with patched(list_job_configs=_fake_configs(A, B), list_sessions=_fake_sessions()):
    demo = ui.build_workbench_ui()

# Gradio 6 里 Blocks.blocks / Blocks.fns 都是 dict（id → 对象），
# 直接迭代拿到的是 key（int），要用 .values()。
all_blocks = list(demo.blocks.values())
all_fns = list(demo.fns.values())

timers = [b for b in all_blocks if isinstance(b, gr.Timer)]
check("工作台里恰好一个定时器", len(timers) == 1, str(len(timers)))
if timers:
    timer = timers[0]
    check("定时器间隔是 5 秒", timer.value == 5, str(timer.value))
    check("定时器是激活状态", timer.active is True, str(timer.active))

picker_fns = [f for f in all_fns if getattr(f, "fn", None) is ui.refresh_job_picker]
check("恰好一处接的是 refresh_job_picker", len(picker_fns) == 1, str(len(picker_fns)))

if picker_fns:
    fn = picker_fns[0]
    in_types = [type(c).__name__ for c in fn.inputs]
    out_types = [type(c).__name__ for c in fn.outputs]
    check("入参是 [岗位下拉, 指纹]", in_types == ["Dropdown", "State"], str(in_types))
    check("出参是 [岗位下拉, 指纹]", out_types == ["Dropdown", "State"], str(out_types))

    # 它必须挂在定时器的 tick 上，而不是某个点击事件上
    target_blocks = [
        b for b in all_blocks
        if b._id in {t[0] for t in fn.targets}
    ]
    target_events = [t[1] for t in fn.targets]
    check("接的是定时器的 tick 事件", "tick" in target_events, str(target_events))
    check("触发的正是那个定时器", any(isinstance(b, gr.Timer) for b in target_blocks), str(target_blocks))

workbench_fns = [f for f in all_fns if getattr(f, "fn", None) is ui.refresh_workbench]
check("refresh_workbench 挂了两处（刷新按钮 + 页面加载）", len(workbench_fns) == 2, str(len(workbench_fns)))
for fn in workbench_fns:
    out_types = [type(c).__name__ for c in fn.outputs]
    check(
        "refresh_workbench 的 6 个输出里最后一个是指纹 State",
        len(out_types) == 6 and out_types[-1] == "State",
        str(out_types),
    )


# ===========================================================================
section("7. 拿着已删除的岗位去生成链接：要说清楚下拉会自动刷新")
# ===========================================================================

with tempfile.TemporaryDirectory() as tmp:
    resume = Path(tmp) / "resume.txt"
    resume.write_text("张三的简历", encoding="utf-8")

    with patched(get_job_config_object=lambda job_key: None):
        html_out, _, _ = asyncio.run(
            ui.create_candidate("张三", "key_已被删除", str(resume), None)
        )

    check("提示岗位已不存在", "所选岗位已不存在" in html_out, html_out)
    check("提示下拉会自动刷新", "下拉框会自动刷新" in html_out, html_out)
    check("不再说「请选择有效岗位」", "请选择有效岗位" not in html_out, html_out)

    # 什么都没选还是要走原来那条提示
    with patched(get_job_config_object=lambda job_key: None):
        html_out2, _, _ = asyncio.run(ui.create_candidate("张三", "", str(resume), None))

    check("没选岗位时提示先选岗位", "请先选择岗位" in html_out2, html_out2)


print()
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
