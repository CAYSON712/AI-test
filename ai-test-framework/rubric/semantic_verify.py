# -*- coding: utf-8 -*-
"""确定性语义校验器（通用，不依赖任何具体系统/LLM）。

对 AI 实际输出做可复现的规则化比对，用于"意图/返回处理/参数/上下文"等
能规则化的维度自动评分（via=rule）。

校验原语（都不调 LLM）：
  - contains_text : 实际输出是否包含期望文本/关键字
  - has_fields    : 实际输出（JSON）是否包含期望的字段
  - value_eq      : 实际输出中某字段值是否等于期望值

这些原语是通用的——无论被测系统是餐厅还是客服，只要用例声明了
「期望输出/期望关键字/期望字段」，就能自动校验。
"""

import re
import json


def _to_text(value):
    """把任意值转成用于匹配的规范化文本。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _norm(s):
    """规范化文本：去空白、全角/半角统一、转小写，便于模糊匹配。"""
    s = _to_text(s)
    s = re.sub(r"\s+", "", s)                      # 去所有空白
    s = s.replace("，", ",").replace("。", ".").replace("？", "?") \
         .replace("！", "!").replace("：", ":").replace("“", '"').replace("”", '"')
    return s.lower()


def contains_text(actual, expected):
    """实际输出是否包含期望文本（规范化后子串匹配）。

    返回 (match: bool, detail: str)
    """
    a, e = _norm(actual), _norm(expected)
    if not e:
        return True, "期望文本为空，跳过校验"
    if e in a:
        return True, f"输出包含期望文本: 「{expected}」"
    return False, f"输出未包含期望文本: 「{expected}」"


def has_fields(actual, expected_fields):
    """实际输出（JSON）是否包含期望字段。

    返回 (match: bool, detail: str)
    """
    detail = []
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except Exception:
            return False, f"实际输出不是 JSON，无法校验字段 {expected_fields}"
    if not isinstance(actual, dict):
        return False, f"实际输出不是对象，无法校验字段 {expected_fields}"
    missing = [f for f in expected_fields if f not in actual]
    if not missing:
        return True, f"输出包含全部期望字段: {expected_fields}"
    return False, f"输出缺少字段: {missing}"


def value_eq(actual, field, expected):
    """实际输出中某字段值是否等于期望值（宽松比较：数字/字符串）。

    返回 (match: bool, detail: str)
    """
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except Exception:
            pass
    if not isinstance(actual, dict):
        return False, f"实际输出不是对象，无法校验字段 {field}"
    if field not in actual:
        return False, f"实际输出缺少字段: {field}"
    av, ev = actual[field], expected
    # 数字宽松比较
    try:
        if isinstance(av, (int, float)) or isinstance(ev, (int, float)):
            if float(av) == float(ev):
                return True, f"字段 {field} 值匹配: {av} == {ev}"
    except Exception:
        pass
    # 文本比较
    if _norm(av) == _norm(ev):
        return True, f"字段 {field} 值匹配: {av} == {ev}"
    return False, f"字段 {field} 值不匹配: 期望 {ev}，实际 {av}"


def parse_semantic(expect_text):
    """从能力目录的「成功标准:语义」自然语言期望里，提取可规则化的校验项。

    支持两类可确定性校验（其余留给 LLM）：
      - 字段列表：从「含 A/B/C」提取 → {"fields": [A, B, C]}
      - 输出关键词：从「返回/提示/应 XX」提取 → {"contains": "XX"}

    返回 dict，供 verify_case 直接消费；无可提取时返回 None。
    """
    if not expect_text:
        return None
    checks = {}

    # 1) 提取字段列表：形如"含 X/Y/Z"、"包含 X、Y、Z"、"[A, B]"
    m = re.search(r"[含包含][:：]?\s*([\u4e00-\u9fa5A-Za-z0-9_、/，,\s\[\]]+)", expect_text)
    if m:
        seg = m.group(1)
        # 去掉多余字符，按分隔符拆
        seg = re.sub(r"[\[\]]", "", seg)
        fields = [f.strip() for f in re.split(r"[、/，,]", seg) if f.strip()]
        # 过滤掉明显不是字段的词（动词/连接词）
        fields = [f for f in fields if not any(
            kw in f for kw in ("返回", "的", "列表", "信息", "详情", "条件", "成功"))]
        if fields:
            checks["fields"] = fields

    # 2) 提取输出关键词：形如"应提示 XX"、"明确提示 XX"
    #    仅提取 2~8 字的明确短词（如"不存在""下架成功"）；长句说明是开放
    #    语义判断（留给 LLM），不做不可靠的整句匹配。
    m2 = re.search(r"(?:应|明确|需要)?(?:提示|告知|返回|显示|给出)\s*([\u4e00-\u9fa5A-Za-z0-9_]{2,8})", expect_text)
    if m2:
        kw = m2.group(1)
        # 过滤含修饰词尾缀的过泛词
        if kw and not any(t in kw for t in ("的", "列表", "信息", "详情", "系统")):
            checks.setdefault("contains", []).append(kw)

    # 3) 通用输出期望：整段期望文本做 contains（关键词较宽泛时用）
    #    仅当上面两类都没提取到时，尝试用整段作为输出匹配（可能不精确）
    if not checks:
        return None
    return checks


def verify_case(expectation, actual):
    """根据用例「期望」对实际输出做确定性校验。

    期望支持：
      - expectation.output      : 期望输出文本/关键字 → contains_text
      - expectation.intent      : 期望意图 → contains_text
      - expectation.fields      : 期望返回的字段列表 → has_fields
      - expectation.expect_val  : {"field": ..., "value": ...} → value_eq

    返回 (match: bool, detail: str, used: bool)
      used=False 表示期望里没有可校验的信息，无法自动校验（交给上层）。
    """
    if not isinstance(expectation, dict):
        return True, "无期望定义，跳过自动校验", False

    checks = []
    sem = expectation.get("semantic")

    if isinstance(sem, dict) and (sem.get("fields") or sem.get("contains")):
        # 能力目录「成功标准:语义」声明的真实校验项 → 只用它，忽略生成脚本
        # 造的顶层 output/intent 占位，避免把能力名/话术当输出文本误判。
        for f in sem.get("fields", []):
            checks.append(("fields", [f]))
        for kw in sem.get("contains", []):
            checks.append(("contains", kw))
    else:
        # 无 semantic 时，回退到顶层期望（手工用例可能直接写 output/fields）
        # 1) 期望输出文本 → 输出包含
        exp_out = expectation.get("output")
        if exp_out:
            checks.append(("contains", exp_out))
        # 2) 期望返回字段 → 结构校验
        exp_fields = expectation.get("fields")
        if exp_fields:
            checks.append(("fields", exp_fields))
        # 3) 期望字段值 → 值相等
        exp_val = expectation.get("expect_val")
        if isinstance(exp_val, dict):
            checks.append(("value", exp_val))

    if not checks:
        return True, "期望无可校验信息", False

    # 实际输出：取 output_data 里的有效文本
    actual_text = actual
    if isinstance(actual, dict):
        # 优先取"输出"字段；否则取整段
        actual_text = actual.get("output") or actual.get("reply") or actual

    all_pass = True
    details = []
    for kind, payload in checks:
        if kind == "contains":
            ok, d = contains_text(actual_text, payload)
        elif kind == "fields":
            ok, d = has_fields(actual_text, payload)
        elif kind == "value":
            ok, d = value_eq(actual_text, payload.get("field"), payload.get("value"))
        else:
            continue
        details.append(d)
        if not ok:
            all_pass = False

    return all_pass, "；".join(details), True
