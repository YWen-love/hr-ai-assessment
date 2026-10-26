"""岗位配置 Schema 定义、校验与打分 DSL 注册表。

设计要点（对应最终定稿四大拍板）：
1. score_rules 全部使用机器可执行 DSL，自然语言条件已被取消 —— 分数必须可复现、不可漂移。
2. 打分只读取「简历静态标签」，主观意向由 conversation_traits 独立承载，二者不混算。
3. AI 生成的配置默认 review_status=pending。它只用于区分「模型生成」与「人工维护」
   这两个来源，测评链路不读这个字段，不会因此拦下任何配置。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 简历静态标签字段表
# ---------------------------------------------------------------------------
# 这是打分层唯一允许读取的数据源。主观层（对话偏好）不参与本表，
# 避免"聊天说得好听就给分"这种不可复现的行为。
# 字段名同时是给 JD 生成 Agent 的封闭词表 —— 模型只能从这里选，
# 校验器会拒绝任何表外字段，从源头堵住幻觉。

FIELD_TYPES: Dict[str, str] = {
    "degree": "string",
    "major": "string",
    "resume_intention": "string",
    "exp_tags": "list[string]",
    "school_type.is_normal_university": "bool",
    "school_type.is_985_211": "bool",
    "school_type.is_first_batch": "bool",
    "school_type.is_public_school": "bool",
}


# ---------------------------------------------------------------------------
# 打分 DSL 操作符
# ---------------------------------------------------------------------------
# 每个操作符签名统一为 (实际值, 规则里的 value) -> bool
# 字符串字段用 contains_any（子串命中），列表字段用 has_any（交集非空），
# 两者刻意分开命名，避免模型混淆语义。

OPERATORS: Dict[str, Any] = {
    # --- string 字段 ---
    "equals": lambda actual, expect: _as_str(actual) == _as_str(expect),
    "not_equals": lambda actual, expect: _as_str(actual) != _as_str(expect),
    "contains_any": lambda actual, expect: any(
        _as_str(e) and _as_str(e) in _as_str(actual) for e in _expect_list(expect)
    ),
    "in": lambda actual, expect: _as_str(actual) in {_as_str(e) for e in _expect_list(expect)},
    # 「不在这一组里」。没有它，模型表达「学历不达标 / 证书不具备」
    # 只能拿 not_equals 配一个数组 —— 引擎把数组当单个字面量比，
    # 条件对所有候选人恒真，等于给每个人扣一次分。
    "not_in": lambda actual, expect: _as_str(actual) not in {
        _as_str(e) for e in _expect_list(expect)
    },
    "is_empty": lambda actual, expect: not _as_str(actual).strip(),
    # --- bool 字段 ---
    "is_true": lambda actual, expect: bool(actual) is True,
    "is_false": lambda actual, expect: bool(actual) is False,
    # --- list 字段 ---
    "has_any": lambda actual, expect: bool(set(_as_list(actual)) & set(_expect_list(expect))),
    "has_all": lambda actual, expect: set(_expect_list(expect)).issubset(set(_as_list(actual))),
    "has_none": lambda actual, expect: not (set(_as_list(actual)) & set(_expect_list(expect))),
}

# 哪些操作符能作用于哪些字段类型 —— 校验器用它拦截类型错配的规则
_OPERATORS_BY_TYPE: Dict[str, set[str]] = {
    "string": {"equals", "not_equals", "contains_any", "in", "not_in", "is_empty"},
    "bool": {"is_true", "is_false"},
    "list[string]": {"has_any", "has_all", "has_none"},
}

# 操作符的语义说明。给模型看 —— 只列名字不解释语义，
# 模型只能靠猜，「not in」这种近义操作就会被猜成 not_equals。
_OPERATOR_HINTS: Dict[str, str] = {
    "equals": "实际值等于 value 里给的那一个（value 只能是单个值）",
    "not_equals": "实际值不等于 value 里给的那一个（value 只能是单个值）",
    "contains_any": "实际值包含 value 里任意一个字面量（子串命中）",
    "in": "实际值是 value 里列出的任意一个",
    "not_in": "实际值不是 value 里列出的任何一个",
    "is_empty": "实际值为空",
    "is_true": "实际值为真",
    "is_false": "实际值为假",
    "has_any": "实际值与 value 至少有一个相同",
    "has_all": "value 里的全都出现在实际值中",
    "has_none": "value 里的一个都没出现在实际值中",
}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_as_str(item) for item in value]
    return [_as_str(value)] if _as_str(value) else []


def _expect_list(value: Any) -> List[str]:
    return _as_list(value)


# ---------------------------------------------------------------------------
# 字段取值
# ---------------------------------------------------------------------------

def resolve_field(tags: Dict[str, Any], dotted: str) -> Any:
    """按点号路径从简历标签里取值，任一层缺失返回 None（不抛异常）。"""
    current: Any = tags
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def field_type(dotted: str) -> str | None:
    return FIELD_TYPES.get(dotted)


def is_valid_field(dotted: str) -> bool:
    return dotted in FIELD_TYPES


# ---------------------------------------------------------------------------
# 单条打分规则求值
# ---------------------------------------------------------------------------

def evaluate_rule(rule: Dict[str, Any], tags: Dict[str, Any]) -> bool:
    """求值单条 DSL 规则。字段缺失一律判 False —— 宁可不加分，不瞎加分。

    两种规则形态：
    - 单条件（原有）：field + op。一次只读一个字段，语义未做任何改动。
    - AND 容器（新增）：all_of 数组，全部子条件为真才算命中。
      「同时是师范院校 且 是师范专业」这类跨字段要求只能靠这种形态表达 ——
      拆成两条独立规则是 OR，单条件 DSL 无论如何组合都跨不到字段之间。
    """
    conds = rule.get("all_of")
    if isinstance(conds, list):
        subs = [c for c in conds if isinstance(c, dict)]
        if not subs:
            return False  # 空条件组不给分，也不给满分
        return all(evaluate_rule(c, tags) for c in subs)

    field = rule.get("field", "")
    op = rule.get("op", "")
    operator = OPERATORS.get(op)
    if operator is None or not is_valid_field(field):
        return False
    return bool(operator(resolve_field(tags, field), rule.get("value")))


def score_rules_for(config: Dict[str, Any], tags: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]]]:
    """按配置计算总分与命中明细。

    返回 (总分, 命中明细列表)。明细用于给 HR 解释"为什么是这个分数"，
    也是 regression test 的比对基线。
    """
    rules = (config.get("score_rules") or {})
    total = 0
    hits: List[Dict[str, Any]] = []

    for rule in _iter_rules(rules.get("add_score")):
        if evaluate_rule(rule, tags):
            total += int(rule.get("score", 0))
            hits.append(rule)

    for rule in _iter_rules(rules.get("minus_score")):
        if evaluate_rule(rule, tags):
            # minus_score 里 score 记正数，方向由所在分组决定，避免 AI 写出 -2 又被减一次
            total -= abs(int(rule.get("score", 0)))
            hits.append(rule)

    return total, hits


def _iter_rules(entries: Any) -> List[Dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    return [
        r
        for r in entries
        if isinstance(r, dict)
        and ((r.get("field") and r.get("op")) or isinstance(r.get("all_of"), list))
    ]


def level_for_score(config: Dict[str, Any], score: int) -> str:
    """按岗位配置自己的阈值划分档位。

    引擎不含任何默认阈值 —— 每个岗位的 high_min / mid_min 完全来自它自己的
    job_config。配置缺阈值时直接抛错，绝不静默套用别的岗位的分界。
    """
    thresholds = config.get("level_threshold") or {}
    high_min = thresholds.get("high_min")
    mid_min = thresholds.get("mid_min")
    if not isinstance(high_min, int) or isinstance(high_min, bool):
        raise ValueError(
            f"岗位配置缺少合法的 level_threshold.high_min（当前值：{high_min!r}）"
        )
    if not isinstance(mid_min, int) or isinstance(mid_min, bool):
        raise ValueError(
            f"岗位配置缺少合法的 level_threshold.mid_min（当前值：{mid_min!r}）"
        )
    if mid_min >= high_min:
        raise ValueError(
            f"岗位配置阈值非法：mid_min({mid_min}) 必须小于 high_min({high_min})"
        )

    if score >= high_min:
        return "高匹配"
    if score >= mid_min:
        return "中匹配"
    return "低匹配"


def _as_int(value: Any, default: int) -> int:
    """宽松取整：给校验器的提示信息用，不参与打分路径。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 终局双维度矩阵
# ---------------------------------------------------------------------------
# 最终是否推进岗位不看单一维度，由「客观档位 × 主观意向」共同决定。
# 这套矩阵直接对应定稿里要修的核心 Bug：简历高分但聊天明确不喜欢的候选人不该被推进。

FINAL_DECISION_MATRIX: Dict[Tuple[str, bool], str] = {
    ("高匹配", True): "推进",
    ("高匹配", False): "不推进",
    ("中匹配", True): "推进",
    ("中匹配", False): "不推进",
    ("低匹配", True): "谨慎推进",
    ("低匹配", False): "不推进",
}


def final_decision(match_level: str, willing: bool) -> str:
    return FINAL_DECISION_MATRIX.get((match_level, willing), "不推进")


# ---------------------------------------------------------------------------
# 备选岗位（alternate_role_mapping）形状
# ---------------------------------------------------------------------------
# 这三个字段名同时是「给 JD 生成 Prompt 的模板」和「校验器」的依据。
# 抄两份就等于埋了一个「Prompt 说 A、引擎只认 B」的静默漂移点 ——
# 实测模型只吐岗位名（["初中数学备课组长"]）时，校验器会报
# 「缺少 backup_job_key / backup_job_name」，整份配置存不进库，
# 岗位管理页的「上传 JD → 自动生成 → 保存」主流程就此断掉。
# 因此形状只在这里定义一次，Prompt 与校验器都从这里取。

BACKUP_KEY_FIELD = "backup_job_key"
BACKUP_NAME_FIELD = "backup_job_name"
BACKUP_CONDITION_FIELD = "match_condition"

# 模型常见的别名。归一化时按别名兜底，形状漂移不必直接判死。
BACKUP_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    BACKUP_KEY_FIELD: (BACKUP_KEY_FIELD, "backup_key", "job_key", "key"),
    BACKUP_NAME_FIELD: (BACKUP_NAME_FIELD, "backup_name", "job_name", "name"),
    BACKUP_CONDITION_FIELD: (
        BACKUP_CONDITION_FIELD,
        "match_conditions",
        "condition",
        "conditions",
    ),
}


def normalize_backups(backups: Any) -> List[Dict[str, Any]]:
    """把备选岗位收敛成规范形状。

    模型给形状漂移时兜底：只给岗位名的、字段名写成 job_key/conditions 的，
    一律收敛成 {backup_job_key, backup_job_name, match_condition}。

    丢弃的只有「连岗位名都没有」的条目 —— 那种条目既显示不出来也匹配不了，
    留着只会污染配置。

    命中条件为空时按岗位名兜一条：条件为空的话匹配环节无从判断，
    等于让模型对着空气瞎猜命中。兜底的措辞取自模型自己的岗位名，
    不是我们另编的业务口径；HR 在预览面板里可以再细化。
    """
    if not isinstance(backups, list):
        return []

    normalized: List[Dict[str, Any]] = []
    used_keys: set[str] = set()

    for index, entry in enumerate(backups):
        if isinstance(entry, str):
            name = entry.strip()
            if not name:
                continue
            entry = {BACKUP_NAME_FIELD: name}

        if not isinstance(entry, dict):
            continue

        picked = {
            canonical: _first_present(entry, aliases)
            for canonical, aliases in BACKUP_FIELD_ALIASES.items()
        }

        name = str(picked[BACKUP_NAME_FIELD] or "").strip()
        key = str(picked[BACKUP_KEY_FIELD] or "").strip()
        if not name and not key:
            continue

        # key 必须是合法 job_key：中文岗位名直接当 key 过不了校验
        if not is_valid_job_key(key):
            key = _derive_backup_key(name or key, index)
        key = _dedupe_key(key, used_keys)

        conditions = _as_condition_list(picked[BACKUP_CONDITION_FIELD])
        if not conditions:
            conditions = [f"岗位方向为{name}"] if name else []

        normalized.append(
            {
                BACKUP_KEY_FIELD: key,
                BACKUP_NAME_FIELD: name or key,
                BACKUP_CONDITION_FIELD: conditions,
            }
        )
        used_keys.add(key)

    return normalized


def count_backups_without_conditions(backups: Any) -> int:
    """统计备选岗位里缺命中条件的条数。生成结果要据此提醒 HR。"""
    if not isinstance(backups, list):
        return 0
    count = 0
    for entry in backups:
        if isinstance(entry, str):
            if entry.strip():
                count += 1
            continue
        if not isinstance(entry, dict):
            continue
        picked = _first_present(entry, BACKUP_FIELD_ALIASES[BACKUP_CONDITION_FIELD])
        if not _as_condition_list(picked):
            count += 1
    return count


def _first_present(entry: Dict[str, Any], aliases: Tuple[str, ...]) -> Any:
    """按别名顺序取第一个有值的字段。"""
    for alias in aliases:
        value = entry.get(alias)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_condition_list(value: Any) -> List[str]:
    """命中条件收敛成非空字符串数组。字符串当单条处理。"""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _derive_backup_key(name: str, index: int) -> str:
    """从岗位名派生合法 job_key。中文转写不了就退回序号 key。"""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    if cleaned and not cleaned[0].isdigit():
        return cleaned
    return f"backup_{index + 1}"


def _dedupe_key(key: str, used: set[str]) -> str:
    """同一条配置里 key 不能重复，否则报告侧按 key 归并时会串。"""
    if key not in used:
        return key
    suffix = 2
    while f"{key}_{suffix}" in used:
        suffix += 1
    return f"{key}_{suffix}"


# ---------------------------------------------------------------------------
# Schema 校验
# ---------------------------------------------------------------------------

_TOP_LEVEL_REQUIRED = (
    "job_key",
    "job_name",
    "job_profile",
    "dialog_check_items",
    "exp_keywords",
    "score_rules",
    "level_threshold",
    "alternate_role_mapping",
)

_JOB_PROFILE_REQUIRED = ("responsibilities", "work_time", "salary")

# job_profile 的规范形状：除 industry 是字符串外，其余都是字符串数组。
# 形状错（类型不对）是结构问题，挡入库；内容空是完整度问题，只提醒。
# 分清这两件事是必须的：JD 生成 Prompt 明确要求「JD 没写的字段留空数组，
# 不要编造薪资和工时」，保存闸门若把「空」当结构错误，
# 就会把生成器自己造不出来的配置挡在门外 —— 主流程直接断掉。
_JOB_PROFILE_LIST_FIELDS = (
    "responsibilities",
    "nature",
    "work_time",
    "rest_schedule",
    "salary",
    "hard_gates",
    "soft_traits",
)

# 字段的中文含义。给模型看的 —— 只用中文描述字段，模型会直接把中文
# 当键名写出来（"职责": [...]），运行时按 responsibilities 取值，取不到就是空。
_JOB_PROFILE_LABELS: Dict[str, str] = {
    "responsibilities": "职责，JD 里的岗位职责要点",
    "nature": "性质，全职/兼职/实习/外聘等",
    "work_time": "工作时间，上班时段或工时",
    "rest_schedule": "休息，休息日或休假安排",
    "salary": "薪资，薪资范围或构成",
    "hard_gates": "硬性门槛，学历、证书、经验年限等一票否决项",
    "soft_traits": "软性素质，沟通、耐心、责任心等",
}

# 模型把中文字段名直接当键名写出来时的对照表。
# 不收敛的后果是：内容其实在，但运行时按英文键名取，取不到就是空 ——
# 报告里显示「（未配置）」，HR 看不出是模型写错了键名。
_JOB_PROFILE_ALIASES: Dict[str, str] = {
    "职责": "responsibilities",
    "岗位职责": "responsibilities",
    "工作职责": "responsibilities",
    "主要职责": "responsibilities",
    "性质": "nature",
    "岗位性质": "nature",
    "工作时间": "work_time",
    "工作时长": "work_time",
    "工时": "work_time",
    "上班时间": "work_time",
    "休息": "rest_schedule",
    "休息日": "rest_schedule",
    "休息安排": "rest_schedule",
    "薪资": "salary",
    "薪酬": "salary",
    "薪资待遇": "salary",
    "薪酬待遇": "salary",
    "硬性门槛": "hard_gates",
    "硬性要求": "hard_gates",
    "硬性条件": "hard_gates",
    "一票否决": "hard_gates",
    "软性素质": "soft_traits",
    "软性要求": "soft_traits",
    "软性条件": "soft_traits",
    "加分项": "soft_traits",
}

# 校验结果里带这个前缀的条目是「建议确认」，不是「必须修」。
# 保存闸门只按后者拦截 —— 参见 split_issues。
WARN_PREFIX = "[警告]"


def split_issues(issues: List[str]) -> Tuple[List[str], List[str]]:
    """把校验结果分成「必须修」和「建议确认」两段。

    分段靠 WARN_PREFIX 前缀，不靠调用方各自判断 strict 标志 ——
    生成预览和保存入库走的是同一个校验器，两边对「这条拦不拦」
    的看法必须一致，否则就会出现「预览说没问题、保存却被拦」。
    """
    errors = [issue for issue in issues if not issue.startswith(WARN_PREFIX)]
    warnings = [issue for issue in issues if issue.startswith(WARN_PREFIX)]
    return errors, warnings


def validate_job_config(config: Any, *, strict: bool = True) -> List[str]:
    """校验岗位配置。返回错误列表，空列表表示通过。

    strict=True（保存入库时）：任何问题都拒绝。
    strict=False（JD 生成预览时）：只挡会跑崩流程的结构性问题，
    其余以 warning 形式放行，交给人工在预览面板里改。
    """
    errors: List[str] = []

    if not isinstance(config, dict):
        return ["配置根节点必须是 JSON 对象"]

    for key in _TOP_LEVEL_REQUIRED:
        if key not in config:
            errors.append(f"缺少必填字段：{key}")

    job_key = config.get("job_key")
    if job_key and not is_valid_job_key(job_key):
        errors.append("job_key 只能包含小写字母、数字和下划线")

    errors.extend(_validate_job_profile(config.get("job_profile"), strict))
    errors.extend(_validate_check_items(config.get("dialog_check_items"), strict))
    errors.extend(_validate_keywords(config.get("exp_keywords")))
    errors.extend(_validate_score_rules(config.get("score_rules")))
    errors.extend(_validate_thresholds(config.get("level_threshold")))
    errors.extend(_validate_backups(config.get("alternate_role_mapping"), strict))

    return errors


def is_valid_job_key(value: Any) -> bool:
    """job_key / backup_job_key 的合法性。只允许小写字母、数字、下划线。

    对外公开：保存配置的 API 在复制岗位时要先校验 HR 自填的 key，
    不能等到校验器报错才返回。
    """
    return isinstance(value, str) and bool(value) and all(
        c.islower() or c.isdigit() or c == "_" for c in value
    )


def _validate_job_profile(profile: Any, strict: bool) -> List[str]:
    if not isinstance(profile, dict):
        return ["job_profile 必须是对象"]

    errors: List[str] = []

    # 形状检查：类型不对才是结构错误，会让下游渲染炸掉。
    # 数组和单串两种写法运行时都认（见 assessment_agent._profile_lines），
    # 这里不能比运行时更严，否则会拒掉本来能跑的配置。
    for field_name in _JOB_PROFILE_LIST_FIELDS:
        value = profile.get(field_name)
        if value is None or isinstance(value, str):
            continue
        if not isinstance(value, list):
            errors.append(f"job_profile.{field_name} 必须是数组或字符串")
        elif not all(isinstance(item, str) for item in value):
            errors.append(f"job_profile.{field_name} 必须全是非空字符串数组")

    # 内容空：是完整度问题，不是结构问题。运行时本来就会兜底成「（未配置）」，
    # 所以只提醒 HR 去补，不拦入库 —— 否则 JD 没写薪资的岗位根本存不进去。
    missing = [k for k in _JOB_PROFILE_REQUIRED if not profile.get(k)]
    if missing:
        errors.append(
            f"{WARN_PREFIX} job_profile 缺少 {', '.join(missing)}，建议人工补充后再启用"
        )

    return errors


def _validate_check_items(items: Any, strict: bool) -> List[str]:
    if not isinstance(items, list):
        return ["dialog_check_items 必须是数组"]
    errors = []
    if not items:
        # 同上：没有校验项时对话照样能跑（渲染成「（无）」），
        # 拦在入库这一步只会让 HR 只能靠手改 JSON 才能把岗位建起来。
        errors.append(
            f"{WARN_PREFIX} dialog_check_items 为空：没有校验项，对话无从确认起，建议补充"
        )
    for index, item in enumerate(items):
        if isinstance(item, str):
            continue  # 兼容纯字符串写法
        if not isinstance(item, dict):
            errors.append(f"dialog_check_items[{index}] 必须是字符串或对象")
            continue
        if not item.get("text"):
            errors.append(f"dialog_check_items[{index}] 缺少 text")
    return errors


def _validate_keywords(keywords: Any) -> List[str]:
    if not isinstance(keywords, list):
        return ["exp_keywords 必须是数组"]
    return [f"exp_keywords[{i}] 不是字符串" for i, k in enumerate(keywords) if not isinstance(k, str)]


def _validate_score_rules(rules: Any) -> List[str]:
    if not isinstance(rules, dict):
        return ["score_rules 必须是对象"]
    errors: List[str] = []
    for group in ("add_score", "minus_score"):
        entries = rules.get(group) or []
        if not isinstance(entries, list):
            errors.append(f"score_rules.{group} 必须是数组")
            continue
        seen_ids: set[str] = set()
        for index, rule in enumerate(entries):
            errors.extend(f"score_rules.{group}[{index}]：{msg}" for msg in _validate_one_rule(rule))
            rule_id = rule.get("id") if isinstance(rule, dict) else None
            if rule_id and rule_id in seen_ids:
                errors.append(f"score_rules.{group}[{index}]：id 重复（{rule_id}）")
            if rule_id:
                seen_ids.add(rule_id)
    return errors


def _validate_one_rule(rule: Any) -> List[str]:
    if not isinstance(rule, dict):
        return ["必须是对象"]
    errors: List[str] = []

    if isinstance(rule.get("all_of"), list):
        # AND 容器：field/op 写在子条件里，顶层只留分值与说明
        if rule.get("field") or rule.get("op"):
            errors.append("all_of 容器不能再带 field/op（子条件写在 all_of 数组里）")
        conds = [c for c in rule["all_of"] if isinstance(c, dict)]
        if not conds:
            errors.append("all_of 必须是非空的对象数组")
        for index, cond in enumerate(conds):
            errors.extend(f"all_of[{index}]：{msg}" for msg in _validate_one_condition(cond))
    else:
        errors.extend(_validate_one_condition(rule))

    if not isinstance(rule.get("score"), int) or rule.get("score") is None:
        errors.append("score 必须是整数（扣分项记正数，方向由所属分组决定）")

    if not rule.get("desc"):
        errors.append("缺少 desc（HR 要靠它理解这条规则为什么命中）")

    return errors


def _validate_one_condition(cond: Any) -> List[str]:
    """校验单个条件（field + op）。AND 容器的子条件与普通规则共用这一套。"""
    if not isinstance(cond, dict):
        return ["必须是对象"]
    errors: List[str] = []
    field = cond.get("field")
    op = cond.get("op")

    if not field:
        errors.append("缺少 field")
    elif not is_valid_field(field):
        errors.append(f"field={field} 不在简历标签字段表中，允许的字段：{', '.join(FIELD_TYPES)}")

    if not op:
        errors.append("缺少 op")
    elif op not in OPERATORS:
        errors.append(f"op={op} 未注册，允许：{', '.join(sorted(OPERATORS))}")
    elif field and is_valid_field(field):
        allowed = _OPERATORS_BY_TYPE.get(FIELD_TYPES[field], set())
        if op not in allowed:
            errors.append(f"op={op} 不适用于 {field}（{FIELD_TYPES[field]}），可用：{', '.join(sorted(allowed))}")

    return errors


def _validate_thresholds(thresholds: Any) -> List[str]:
    if not isinstance(thresholds, dict):
        return ["level_threshold 必须是对象"]
    errors = []
    high = thresholds.get("high_min")
    mid = thresholds.get("mid_min")
    for name, value in (("high_min", high), ("mid_min", mid)):
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"level_threshold.{name} 必须是整数")
    if isinstance(high, int) and isinstance(mid, int) and not isinstance(high, bool) and not isinstance(mid, bool):
        if mid >= high:
            errors.append(f"level_threshold.mid_min({mid}) 必须小于 high_min({high})")
    return errors


def _validate_backups(backups: Any, strict: bool) -> List[str]:
    if not isinstance(backups, list):
        return ["alternate_role_mapping 必须是数组"]
    errors: List[str] = []
    for index, backup in enumerate(backups):
        if not isinstance(backup, dict):
            errors.append(f"alternate_role_mapping[{index}] 必须是对象")
            continue
        if not backup.get(BACKUP_KEY_FIELD):
            errors.append(f"alternate_role_mapping[{index}] 缺少 {BACKUP_KEY_FIELD}")
        elif not is_valid_job_key(backup[BACKUP_KEY_FIELD]):
            errors.append(f"alternate_role_mapping[{index}].{BACKUP_KEY_FIELD} 只能包含小写字母、数字和下划线")
        if not backup.get(BACKUP_NAME_FIELD):
            errors.append(f"alternate_role_mapping[{index}] 缺少 {BACKUP_NAME_FIELD}")
        conditions = backup.get(BACKUP_CONDITION_FIELD)
        if not isinstance(conditions, list) or not conditions:
            # 备选判定本身就是语义判断，保留自然语言条件。
            # 没有条件只是「这条备选永远匹配不上」，属于质量问题不是结构问题，
            # 归一化阶段已经会按岗位名兜一条，这里只兜底提醒。
            errors.append(
                f"{WARN_PREFIX} alternate_role_mapping[{index}] 没有 "
                f"{BACKUP_CONDITION_FIELD}，这条备选岗位不会命中，建议补充命中条件"
            )
        elif not all(isinstance(c, str) and c.strip() for c in conditions):
            errors.append(
                f"alternate_role_mapping[{index}].{BACKUP_CONDITION_FIELD} 必须全是非空字符串"
            )
    return errors


# ---------------------------------------------------------------------------
# 规范化：让 AI 生成 / 手写 / 文件导入三种来源收敛成同一形状
# ---------------------------------------------------------------------------

def normalize_job_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """补齐缺省字段、统一 dialog_check_items 形态。校验前的预处理。"""
    normalized = dict(config)
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized.setdefault("alternate_role_mapping", [])
    normalized.setdefault("review_status", "pending")

    items = normalized.get("dialog_check_items")
    if isinstance(items, list):
        normalized["dialog_check_items"] = [
            {"id": f"check_{i + 1}", "text": it} if isinstance(it, str)
            else {"id": f"check_{i + 1}", "text": (it or {}).get("text", "")}
            for i, it in enumerate(items)
        ]

    switch = normalized.get("backup_show_switch")
    switch = dict(switch) if isinstance(switch, dict) else {}
    # 旧字段 when_accept_main_job 的语义是「候选人愿意主岗时才展示」，
    # 与现行业务（明确拒绝主岗时才推备选）正好相反。这里直接丢弃而不是取反：
    # 取反会让历史上显式关掉推荐的岗位又自动打开，和要修的 bug 是同一类问题。
    switch.pop("when_accept_main_job", None)
    switch.setdefault("when_reject_main_job", True)
    normalized["backup_show_switch"] = switch

    # 备选岗位同样要收敛形状：模型给的形状漂移不该让整份配置存不进库
    normalized["alternate_role_mapping"] = normalize_backups(
        normalized.get("alternate_role_mapping")
    )

    # 打分规则同理：op 与 value 的形状不匹配，引擎算出来的结果恒真
    normalized["score_rules"] = normalize_score_rules(normalized.get("score_rules"))

    # job_profile 的键名同理：模型写中文键，内容在但运行时取不到
    normalized["job_profile"] = normalize_job_profile(normalized.get("job_profile"))

    return normalized


def normalize_job_profile(profile: Any) -> Dict[str, Any]:
    """把 job_profile 的键名收敛成规范键。

    只改键名，不动内容，也不丢模型写的表外键 ——
    那些键留着，HR 在 JSON 编辑器里还能看到模型到底写了什么。
    """
    if not isinstance(profile, dict):
        return profile if isinstance(profile, dict) else {}

    normalized: Dict[str, Any] = {}
    for key, value in profile.items():
        canonical = _JOB_PROFILE_ALIASES.get(key, key)
        # 规范键已经存在时不覆盖：先到的优先，避免别名把正式值顶掉
        if canonical in normalized and normalized[canonical] not in (None, [], ""):
            continue
        normalized[canonical] = value
    return normalized


# op 期望的 value 形状与模型写法的对应关系。
# 引擎里的 equals / not_equals 把 value 当**单个**字面量比，
# 传进来一个数组就等于拿「数组的 repr」去比 —— 条件对所有候选人恒真。
# 模型想表达「在这一组里 / 不在这一组里」时会写成一个数组。
_SET_OPS = {"equals": "in", "not_equals": "not_in"}


def normalize_score_rules(rules: Any) -> Any:
    """把打分规则收敛成引擎算得动的形状。

    形状漂移的后果是「静默算错」而不是「报错」：not_equals 配数组时
    条件恒真，等于给每个候选人扣一次分。所以这里统一改成集合语义。

    扁平数组也在这里收：模型（以及手写 JSON）有时把两组规则拍成一个数组，
    当成「全是加分项」处理。
    """
    if isinstance(rules, list):
        rules = {"add_score": rules}
    if not isinstance(rules, dict):
        return rules

    normalized: Dict[str, Any] = dict(rules)
    for group in ("add_score", "minus_score"):
        entries = rules.get(group)
        if not isinstance(entries, list):
            continue
        normalized[group] = [
            _normalize_rule(rule) if isinstance(rule, dict) else rule
            for rule in entries
        ]
    return normalized


def _normalize_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    op = rule.get("op")
    value = rule.get("value")
    replacement = _SET_OPS.get(op)
    if replacement and isinstance(value, list):
        fixed = dict(rule)
        fixed["op"] = replacement
        return fixed
    return rule


def iter_check_item_texts(config: Dict[str, Any]) -> List[str]:
    """对话 Prompt 用：取出待确认项的纯文本列表。"""
    return [
        item.get("text", "")
        for item in (config.get("dialog_check_items") or [])
        if isinstance(item, dict) and item.get("text")
    ]


# ---------------------------------------------------------------------------
# Prompt 词表渲染
# ---------------------------------------------------------------------------
# JD 生成 Prompt 里的字段表与操作符表，全部从上面两份常量渲染，
# 不在 prompts.py 里另抄一份。抄第二份就等于埋了一个「Prompt 说 A、
# 引擎只认 B」的静默漂移点，且这种漂移只会在线上暴露。

def field_table_for_prompt() -> str:
    """给模型看的简历标签字段表（含类型与说明）。"""
    hints = {
        "degree": "最高学历，如 本科 / 硕士研究生",
        "major": "专业名称",
        "resume_intention": "简历上的求职意向原文",
        "exp_tags": "经验标签数组，取值来自 exp_keywords 封闭词表",
        "school_type.is_normal_university": "师范类院校",
        "school_type.is_985_211": "985 / 211 院校",
        "school_type.is_first_batch": "原一本 / 双一流院校",
        "school_type.is_public_school": "公办院校",
    }
    return "\n".join(
        f"- {name}（{field_type}）：{hints.get(name, '')}" for name, field_type in FIELD_TYPES.items()
    )


def operator_table_for_prompt() -> str:
    """按字段类型分组的可用操作符。模型必须按此表选 op。

    每个操作符都要带一句语义说明：只列名字不解释语义，模型只能靠猜，
    「not in」这种近义操作会被猜成 not_equals —— 而 not_equals 不接受
    数组值，猜错的后果是条件恒真、给所有候选人扣分。
    """
    lines = []
    for field_type, ops in _OPERATORS_BY_TYPE.items():
        examples = [f for f, t in FIELD_TYPES.items() if t == field_type][:3]
        lines.append(
            f"- {field_type}：{'、'.join(sorted(ops))}"
            f"（适用于 {'、'.join(examples)} 等）"
        )
        for op in sorted(ops):
            hint = _OPERATOR_HINTS.get(op)
            if hint:
                lines.append(f"  - {op}：{hint}")
    return "\n".join(lines)


def job_profile_table_for_prompt() -> str:
    """给模型看的 job_profile 字段表。键名与校验器同源，不会漂移。

    只用中文描述字段、又不给键名，模型就会把中文直接当键名写出来
    （"职责": [...]），运行时按 responsibilities 取值，取不到就是空 ——
    内容明明在，报告里却显示「（未配置）」。
    """
    return "\n".join(
        f"- {name}（{_JOB_PROFILE_LABELS.get(name, '')}）"
        for name in _JOB_PROFILE_LIST_FIELDS
    )


def score_rule_table_for_prompt() -> str:
    """给模型看的打分规则结构说明。形状与校验器同源，不会漂移。

    模型把 score_rules 吐成扁平数组时，合并逻辑会退回占位模板 ——
    模板里每条规则的 value 都是空的，等于「一条都不会命中」。
    不把分组和字段说全，模型写的规则会被整份丢掉。
    """
    return "\n".join(
        [
            'score_rules = {"add_score": [加分项...], "minus_score": [扣分项...]}',
            "- add_score：加分项。",
            "- minus_score：扣分项。扣分项的 score 同样写正数，方向由分组决定，不要写负数。",
            "每条规则的字段：",
            "- id：规则的英文标识，给 HR 核对用，可以省略。",
            "- field：简历标签字段名，必须取自上面的【允许使用的简历标签字段】。",
            "- op：操作符，必须取自上面的【允许使用的打分操作符】，且与 field 的类型匹配。",
            "- value：字符串数组。contains_any / in 放要匹配的字面量，"
            "has_any / has_all / has_none 放经验关键词 —— 这些关键词必须逐字取自"
            "你在 exp_keywords 里写下的那一份，不要另造词："
            "简历解析只按 exp_keywords 捞标签，词表里没有的词永远匹配不上，"
            "规则等于不存在。",
            "is_true / is_false / is_empty 不需要 value。",
            "- score：整数，加分项建议 1-10 分。",
            "- desc：一句话说明这条规则为什么加分，HR 要靠它理解。",
            "只有当一条要求必须同时满足多个字段时才用 AND 容器：",
            '{"id": "normal_school_and_major", "all_of": [{"field": "school_type.is_normal_university", "op": "is_true", "value": true}], "score": 3, "desc": "..."}',
            "示例（一条加分项）：",
            '{"id": "exp_teaching", "field": "exp_tags", "op": "has_any", "value": ["授课", "代课"], "score": 5, "desc": "有授课或代课经历"}',
        ]
    )


def backup_table_for_prompt() -> str:
    """给模型看的备选岗位字段表。形状与校验器同源，不会漂移。

    模型少写一个字段，校验器就会报「缺少 backup_job_key」，
    整份配置存不进库 —— 所以这里必须把形状说全。
    """
    return "\n".join(
        [
            f"- {BACKUP_KEY_FIELD}（string）：备选岗位唯一标识，"
            "小写英文+数字+下划线，如 education_erjiang",
            f"- {BACKUP_NAME_FIELD}（string）：备选岗位名称，如 初中二讲老师",
            f"- {BACKUP_CONDITION_FIELD}（list[string]）：命中条件，"
            "2-4 条自然语言条件，逐条独立判断，如 有公立校初中教学经历",
        ]
    )