"""LLM 响应解析测试（纯本地，不发起网络请求）。

覆盖实测踩到的三类响应形状差异 + MiniMax-M3 的思维链污染。
JSON 解析是整条链路最容易崩的一环，这里必须锁死。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm_client import (
    _extract_content,
    extract_json_object,
    strip_reasoning,
)

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL}  {label}: 实际={actual!r} 期望={expected!r}")
    if not ok:
        failures.append(label)


print("=" * 68)
print("1. 思维链剥离（MiniMax-M3 等推理模型）")
print("=" * 68)
check("闭合 think 块",
      strip_reasoning('<think>分析中 {括号干扰}</think>最终答案'), "最终答案")
check("未闭合 think 块（被 max_tokens 截断）",
      strip_reasoning('<think>还没想完就被截断了'), "")
check("无 think 块原样透传", strip_reasoning("普通回复"), "普通回复")
check("空串", strip_reasoning(""), "")
check("多个 think 块", strip_reasoning('<think>a</think>答案<think>b</think>'), "答案")

print()
print("=" * 68)
print("2. 响应正文提取：跨模型形状降级")
print("=" * 68)
check("常规 content 字段",
      _extract_content({"choices": [{"message": {"content": "你好"}}]}), "你好")
check("content 缺失，回落 reasoning_content",
      _extract_content({"choices": [{"message": {"content": "", "reasoning_content": "推理结果"}}]}),
      "推理结果")
check("content 为片段数组",
      _extract_content({"choices": [{"message": {"content": [{"text": "片段"}, {"text": "拼接"}]}}]}),
      "片段拼接")
check("顶层 choices[].text",
      _extract_content({"choices": [{"text": "网关模式", "message": {}}]}), "网关模式")
check("content 正常时不会被 reasoning 干扰",
      _extract_content({"choices": [{"message": {
          "content": "正式答案", "reasoning_content": "长推理"}}]}),
      "正式答案")

for label, payload in [
    ("空 choices", {"choices": []}),
    ("message 完全缺失", {"choices": [{}]}),
    ("content 全是空白", {"choices": [{"message": {"content": "   "}}]}),
]:
    try:
        _extract_content(payload)
        print(f"{FAIL}  {label}: 应当抛错却返回了内容")
        failures.append(label)
    except Exception as exc:
        print(f"{PASS}  {label}: 正确抛错 -> {str(exc)[:70]}")

print()
print("=" * 68)
print("3. 思维链 + JSON 混合：必须能正确取出 JSON")
print("=" * 68)
# 这是最危险的场景：推理里带花括号，扫描时容易截到错的括号位置
tricky = (
    '<think>候选人简历里有 {"noise": true} 这种干扰，'
    '我需要判断 major 是不是 汉语言文学</think>\n'
    '{"degree": "本科", "major": "汉语言文学"}'
)
check("思维链含花括号时仍能正确解析",
      extract_json_object(tricky), {"degree": "本科", "major": "汉语言文学"})

check("未经剥离的原始内容也能解析（兜底）",
      extract_json_object(tricky.replace("<think>", "").replace("</think>", "")),
      {"degree": "本科", "major": "汉语言文学"})

print()
print("=" * 68)
print("4. extract_json_object 容错矩阵")
print("=" * 68)
FENCE = chr(96) * 3
cases = [
    ("裸 JSON", '{"a":1}', {"a": 1}),
    ("代码围栏", FENCE + "json\n{\"a\":1}\n" + FENCE, {"a": 1}),
    ("前后有废话", '好的：{"a":1} 希望有帮助', {"a": 1}),
    ("尾逗号", '{"a":1,}', {"a": 1}),
    ("嵌套对象", '{"a":{"b":[1,2,{"c":3}]},"d":"}"}', {"a": {"b": [1, 2, {"c": 3}]}, "d": "}"}),
    ("值里含转义引号", '{"a":"他说\\"你好\\""}', {"a": '他说"你好"'}),
    ("中文键值", '{"档位":"高匹配","分值":7}', {"档位": "高匹配", "分值": 7}),
]
for label, raw, expected in cases:
    try:
        check(label, extract_json_object(raw), expected)
    except Exception as exc:
        print(f"{FAIL}  {label}: 抛错 {type(exc).__name__}: {exc}")
        failures.append(label)

for label, raw in [("纯文本无JSON", "我不知道"), ("空字符串", ""), ("只有左括号", '{"a":')]:
    try:
        extract_json_object(raw)
        print(f"{FAIL}  {label}: 应当抛错")
        failures.append(label)
    except Exception:
        print(f"{PASS}  {label}: 正确抛错")

print()
print("=" * 68)
print(f"结论：{'全部通过' if not failures else f'{len(failures)} 项失败 -> {failures}'}")
print("=" * 68)
sys.exit(1 if failures else 0)