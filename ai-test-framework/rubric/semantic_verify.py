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

【断言卫生 2026-09-04】
历史数据集里出现把"中文描述句"（如 `数值来自报表不得编造`）当作 JSON
字段名、把整句截段（如 `已完成订单销售记`）当作输出文本来断言的假阳性。
本文件新增两道护栏：
  a. has_fields 只接受「JSON 字段名」形态（^[A-Za-z_][A-Za-z0-9_.]*$）；
     非字段名形态的期望项视为无效描述 → 忽略并记录，不再导致"缺字段"误判。
  b. verify_case 对"业务拒绝/无业务数据"的响应（code!=0 / success:false /
     errorDetails / 空响应）跳过结构断言——工具没返回数据时不该被判"缺字段"。
"""

import re
import json

# JSON 字段名形态：真实工具返回的 key 一定是拉丁标识符。
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# 泛化描述黑名单（contains 提取用）：命中即视为不可靠的宽泛话术。
_CONTAINS_BLACKLIST = ("指定", "相应", "对应", "该笔", "命中", "相关",
                       "所有", "任意", "合适的", "符合条件")
_CONTAINS_TAIL = ("记录", "明细", "列表", "信息", "详情", "数据", "内容")
_CONTAINS_LINK = ("与", "和", "及", "以及", "或")


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


def _decode_json_text(value):
    """把实际输出还原成可读文本再做匹配。

    MCP/HTTP 工具返回的 JSON 常以 ensure_ascii（\\uXXXX 转义）编码，
    直接拿中文关键词匹配会必败。这里做一层解码：
      - 纯 JSON → loads 后 ensure_ascii=False 重新序列化（中文/嵌套内容还原）；
      - 非纯 JSON 但含 \\u 转义片段 → 尝试 unicode_escape 局部解码；
      - 其余原样返回。
    """
    s = _to_text(value)
    try:
        obj = json.loads(s)
        if isinstance(obj, (dict, list)):
            return json.dumps(obj, ensure_ascii=False)
    except Exception:
        pass
    if "\\u" in s:
        try:
            return s.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
    return s


def contains_text(actual, expected):
    """实际输出是否包含期望文本（规范化后子串匹配）。

    返回 (match: bool, detail: str)
    """
    a, e = _norm(_decode_json_text(actual)), _norm(_decode_json_text(expected))
    if not e:
        return True, "期望文本为空，跳过校验"
    if e in a:
        return True, f"输出包含期望文本: 「{expected}」"
    return False, f"输出未包含期望文本: 「{expected}」"


def _has_key_deep(obj, key):
    """递归查找字段：JSON 任意嵌套层（dict/list）出现即命中。

    真实工具返回常见包壳结构 {code, msg, data:[...]}，业务字段都在
    data 内层——只查顶层会误判缺字段，故做深度查找。
    """
    if isinstance(obj, dict):
        # 大小写不敏感：期望写 "ID"（能力目录中文描述）也能命中 JSON 的 "id"；
        # camelCase 的 categoryName vs 期望 categoryname 同样兼容。
        k = str(key).lower()
        if any(str(x).lower() == k for x in obj):
            return True
        return any(_has_key_deep(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_key_deep(v, key) for v in obj)
    return False


def _is_field_name(f):
    return isinstance(f, str) and bool(_FIELD_NAME_RE.match(f.strip()))


def has_fields(actual, expected_fields):
    """实际输出（JSON）是否包含期望字段（含嵌套层，data:[...] 内层可命中）。

    护栏 a：期望项若不是 JSON 字段名形态（含中文/空格/标点的描述句），
    视为"无效描述"忽略，不参与匹配——避免 `含菜单 ID` 这类伪字段必败。

    返回 (match: bool, detail: str)
    """
    expected_fields = [str(f) for f in (expected_fields or [])]
    valid = [f for f in expected_fields if _is_field_name(f)]
    dropped = [f for f in expected_fields if not _is_field_name(f)]
    note = f"（忽略非字段名描述 {dropped}）" if dropped else ""

    if not valid:
        # 全部是无效描述 → 无有效断言，视作跳过（不能判系统缺字段）
        return True, f"{note}期望字段均为非字段名描述，已忽略，无有效断言"
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except Exception:
            return False, f"{note}实际输出不是 JSON，无法校验字段 {valid}"
    if not isinstance(actual, (dict, list)):
        return False, f"{note}实际输出不是对象，无法校验字段 {valid}"

    # 护栏 b：结果集为空（零记录）时无法校验记录级字段，视作跳过。
    # 动机（2026-11-04，POS Local 路由 A-L1-010）：Local 店当日订单明细返回
    # {code:0, total:0, records:[]} —— 服务端成功、契约正确，只是当天没单。
    # 原实现直接对空 records 查 orderId/orderStatus/paymentType，必然 missing，
    # 判成 semantic_miss 并归因 ai_system，把「数据时效」误报成「系统缺字段」。
    # 空集合不构成「字段缺失」的任何证据，不能据此判分。
    _empty = None
    if isinstance(actual, list) and not actual:
        _empty = "输出为空列表"
    elif isinstance(actual, dict):
        _recs = actual.get("records")
        _total = actual.get("total")
        if isinstance(_recs, list) and not _recs:
            try:
                _zero = (_total is None) or int(_total) == 0
            except (TypeError, ValueError):
                _zero = False
            if _zero:
                _empty = "输出 records 为空且 total=0/缺省（结果集为空）"
    if _empty:
        return True, f"{note}跳过字段校验：{_empty}，无记录可供校验 {valid}"

    missing = [f for f in valid if not _has_key_deep(actual, f)]
    if not missing:
        return True, f"{note}输出包含全部期望字段: {valid}"
    return False, f"{note}输出缺少字段: {missing}"


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


def _clean_item(item):
    """清洗待判定项：去掉括号内说明、首尾修饰。"""
    item = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", item)  # 去括号内容
    item = re.sub(r"^[\s、,，/]+|[\s、,，/]+$", "", item)
    return item


def _extract_contains(expect_text):
    """从期望文本提取可断言的「输出关键词」。

    只认三种可靠来源，避免把整句截段当关键词：
      1) 引号（「」“”''""『』）包裹的明确短语/实体；
      2) 动词提示后的拉丁标识符/数字（提示语后接枚举）；
      3) 动词提示后的 2~4 字中文明确词（需过黑名单，滤掉
         「指定…/命中结果/…记录/…明细」这类宽泛/截断话术）。
    """
    out = []
    # 1) 引号实体
    for m in re.finditer(r"[「“\"'『]([^」”\"'』]{1,30})[」”\"'』]", expect_text):
        t = m.group(1).strip()
        if t and not any(k in t for k in ("返回", "列表", "信息", "详情", "系统")):
            if t not in out:
                out.append(t)
    # 2) 动词后的拉丁/数字（如"返回 code 为 0"）
    m2 = re.search(r"(?:应|明确|需要)?(?:提示|告知|返回|显示|给出|输出)\s*"
                   r"[是为:：]?\s*([A-Za-z_][A-Za-z0-9_.]{1,30}|\d{4,})", expect_text)
    if m2:
        kw = m2.group(1)
        if kw not in out:
            out.append(kw)
    # 3) 动词后的 2~4 字明确中文词
    m3 = re.search(r"(?:应|明确|需要)?(?:提示|告知|返回|显示|给出|输出)\s*"
                   r"[是为:：]?\s*([\u4e00-\u9fa5]{2,4})", expect_text)
    if m3:
        kw = m3.group(1)
        if any(b in kw for b in _CONTAINS_BLACKLIST):
            kw = None
        elif kw and any(t for t in _CONTAINS_TAIL if kw.endswith(t)):
            kw = None  # "…记录/…明细"这类截断
        elif kw and any(t for t in _CONTAINS_LINK if t in kw):
            kw = None  # "订单号与金额"这类连接短语
        if kw and kw not in out:
            out.append(kw)
    return out


def parse_semantic(expect_text):
    """从能力目录的「成功标准:语义」自然语言期望里，提取可规则化的校验项。

    支持两类可确定性校验（其余留给 LLM）：
      - 字段列表：从「含 A/B/C」提取 → {"fields": [A, B, C]}
        · 动词（包含/含有/含）整体消费，避免"含"字残留在字段名里；
        · 每项必须是 JSON 字段名形态（拉丁标识符）；中文描述句自动丢弃。
      - 输出关键词：仅提取引号实体/明确枚举/短明确词 → {"contains": [...]}

    返回 dict，供 verify_case 直接消费；无可提取时返回 None。
    """
    if not expect_text:
        return None
    checks = {}

    # 1) 提取字段列表：动词整体消费，逐项标识符化过滤
    seg = None
    m = re.search(r"(?:输出|返回|给出|拥有)?\s*(?:包含|包括|含有|含)"
                  r"\s*(?:字段|以下)?\s*[:：]?\s*([^。；;\n]+)", expect_text)
    if m:
        seg = m.group(1)
    if seg:
        items = []
        for raw in re.split(r"[、/，,]", seg):
            item = _clean_item(raw)
            if _is_field_name(item):
                items.append(item)
        # 单大段含连接词（"名称与 ID"）时按连接词再拆
        if not items and any(t in seg for t in ("与", "和", "及")):
            for conn in ("与", "和", "及"):
                for raw in seg.split(conn):
                    item = _clean_item(raw)
                    if _is_field_name(item):
                        items.append(item)
        items = list(dict.fromkeys(items))
        if items:
            checks["fields"] = items

    # 2) 输出关键词（仅可靠来源）
    contains = _extract_contains(expect_text)
    if contains:
        checks["contains"] = contains

    if not checks:
        return None
    return checks


def _biz_payload(actual):
    """从执行器输出里提取『业务输出』用于结构校验。

    Direct/Generic 执行器把工具原始返回（JSON 文本）塞在 result 的
    output 字段里；结构校验若直接对执行器结构 dict 做字段深查，业务
    字段（orderCount/amount/...）在 output 字符串内部必然判"缺少"，
    反而 executor 自己的键（steps[].name / steps[].type）会误命中——
    造成 A/D 语义校验系统性错位（如报表返回里明明有 orderCount 却报缺）。
    这里把 output 文本解析成 JSON 返回给校验器；非 JSON 输出则回退原样。
    """
    if isinstance(actual, dict):
        _raw = actual.get("output")
        if isinstance(_raw, str) and _raw.strip():
            try:
                _po = json.loads(_raw)
                if isinstance(_po, (dict, list)):
                    return _po
            except Exception:
                pass
    return actual


def _is_empty_result(payload):
    """判断成功响应是否是「合法空结果」（查不到数据，而非缺陷）。

    背景事故（2026-09-17）：语义期望声明的字段（id/name/amount）在"零命中"
    响应里必然不存在——如 query_categories 传了不存在的 menu+category 组合
    返回 {"code":0,"data":[]}，此时判"缺字段"是假阳性。空结果本身就是
    正确的业务回答（应如实告知"查不到"），不该执行结构字段断言。

    识别成功但为空的形态：
      - data 为空列表 / 空 dict / None（且无 records）
      - records 为空列表（报表 Detail 模式 records:[]）
      - 分页体 list 为空 + totalElements == 0（考勤/订单明细分页）
    """
    if not isinstance(payload, dict):
        return False
    d = payload.get("data")
    if isinstance(d, list) and not d:
        return True
    if isinstance(d, dict):
        if not d:
            return True
        # 分页体：list 空且 totalElements 为 0
        if "list" in d and isinstance(d["list"], list) and not d["list"]:
            te = d.get("totalElements")
            if te is None or (isinstance(te, (int, float)) and te == 0):
                return True
        # 嵌套分页体（如 orders.list）：内层 list 空 + totalElements == 0
        for v in d.values():
            if isinstance(v, dict) and isinstance(v.get("list"), list) \
                    and not v["list"]:
                te = v.get("totalElements")
                if te is None or (isinstance(te, (int, float)) and te == 0):
                    return True
        # 报表 Detail 模式：records 空（且 total 为 0）
        rec = d.get("records")
        if isinstance(rec, list) and not rec:
            tot = d.get("total")
            if tot is None or (isinstance(tot, (int, float)) and tot == 0):
                return True
    if d is None:
        rec = payload.get("records")
        if isinstance(rec, list) and not rec:
            return True
    return False


def _has_biz_data(payload):
    """判断结构断言的对象里是否有『真实业务数据』。

    业务拒绝/失败响应（code!=0 / success:false / 含 errorDetails）或空响应，
    不应执行字段断言（工具没返回数据 → 判"缺字段"是假阳性）。返回 False
    时上层跳过结构断言；文本类（contains）断言不受影响。
    """
    if payload is None:
        return False
    if isinstance(payload, dict):
        code = payload.get("code")
        if code is not None:
            try:
                if int(code) != 0:
                    return False
            except Exception:
                pass
        if payload.get("success") is False:
            return False
        if isinstance(payload.get("errorDetails"), dict):
            return False
        if payload.get("data") is None and payload.get("records") is None:
            # 无 data/records 的结构化错误对象（如 {type, field, message}）
            if any(k in payload for k in ("type", "field", "message", "error",
                                          "errorDetails", "validationErrors")):
                return False
        # 成功但空结果（0 命中）→ 无业务数据可断言
        if _is_empty_result(payload):
            return False
        return True
    if isinstance(payload, list):
        return bool(payload)
    return payload not in (None, "")


def _find_key_deep(obj, key, depth=0):
    """在嵌套结构里找某个键的所有取值（用于 field_values 断言）。"""
    if depth > 6:
        return []
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) == str(key):
                hits.append(v)
            hits.extend(_find_key_deep(v, key, depth + 1))
    elif isinstance(obj, list):
        for it in obj:
            hits.extend(_find_key_deep(it, key, depth + 1))
    return hits


def _check_field_contains_value(obj, field, value):
    """断言 obj 中 field 字段的取值集合包含 value。

    用途（2026-11-09，DST 告警）：验证服务端告警的**精确作用范围**，
    如 data.aggregateWindowUnverifiedDates = ["2026-10-31"] 必须包含 2026-10-31。

    与 value_eq 的区别：value_eq 比「字段值等于」，这里是「集合里包含」——
    告警日期是数组，只关心目标日在不在里面，不关心数组还有没有别的元素。

    容错：字段值可能是列表、标量、或（JSON 字符串形式的）列表，都要能判。
    """
    if obj in (None, "", [], {}):
        return False, f"输出为空，无法校验字段 {field}"
    vals = _find_key_deep(obj, field)
    if not vals:
        return False, f"输出中找不到字段 {field}"

    target = str(value).strip()
    for v in vals:
        cands = v if isinstance(v, (list, tuple, set)) else [v]
        # 字段值本身可能是 JSON 字符串（服务端偶尔把数组序列化成字符串）
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    cands = json.loads(s)
                except Exception:
                    cands = [v]
        for c in cands:
            if str(c).strip() == target:
                return True, f"字段 {field} 包含期望值 {value}"
    return False, (f"字段 {field} 不含期望值 {value}"
                   f"（实际: {json.dumps(vals, ensure_ascii=False, default=str)[:120]}）")


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

    if isinstance(sem, dict) and (sem.get("fields") or sem.get("contains")
                                  or sem.get("not_contains")
                                  or sem.get("field_values")):
        # 能力目录「成功标准:语义」声明的真实校验项 → 只用它，忽略生成脚本
        # 造的顶层 output/intent 占位，避免把能力名/话术当输出文本误判。
        for f in sem.get("fields", []):
            checks.append(("fields", [f]))
        for kw in sem.get("contains", []):
            checks.append(("contains", kw))
        # not_contains：反向断言（2026-11-09 新增）
        # 用途：验证「不该出现的告警确实没出现」。例如 DST 用例里，
        #   区间不含 2026-10-31 时**不得**返回 AGGREGATE_DAY_WINDOW_UNVERIFIED，
        #   否则说明告警滥发、失去信噪比（天天告警 = 用户直接忽略）。
        for kw in sem.get("not_contains", []):
            checks.append(("not_contains", kw))
        # field_values：结构化字段取值断言（2026-11-09 新增）
        # 用途：比对服务端告警的**精确作用范围**。例：
        #   data.aggregateWindowUnverifiedDates 必须包含 "2026-10-31"。
        # 比 warnings 枚举更细 —— 枚举只说「有这类问题」，这个才说「是哪一天」。
        fv = sem.get("field_values")
        if isinstance(fv, dict):
            for f, v in fv.items():
                checks.append(("field_values", {"field": f, "value": v}))
        # B 类纯对话标记：contains 用「任一命中」（对话回复只需体现核心信息之一）
        any_of = sem.get("any_of", False)
    else:
        any_of = False
        # 无 semantic 时，回退到顶层期望（手工用例可能直接写 fields/expect_val）。
        # ⚠ 顶层 output 是「测试意图描述」（如"返回匹配的商品完整信息"），不是
        #    "输出应包含的文本"。拿整句做 contains 对 JSON 业务输出（A 类）必
        #    假失败、对 LLM 长回复（B/C 类）也几乎不可能逐字复现 → 一律不启用。
        #    需要文本断言时必须显式写 semantic.contains。
        exp_fields = expectation.get("fields")
        if exp_fields:
            checks.append(("fields", exp_fields))
        # 期望字段值 → 值相等
        exp_val = expectation.get("expect_val")
        if isinstance(exp_val, dict):
            checks.append(("value", exp_val))

    if not checks:
        return True, "期望无可校验信息", False

    # 实际输出拆两路：
    #   - contains 匹配文本：拼接 dict 的文本键（output/reply/error/biz_error/msg...），
    #     空则退回整段。目的：业务错误响应 {code,msg: 店铺ID不能为空} 与内部异常
    #     error（MCP 调用失败: ...）的文本也要参与关键词匹配——否则"系统已拒绝但
    #     文案不同"的用例会被误判成无响应。
    #   - 结构校验（fields/value）：仍用原始 dict（has_fields 已支持嵌套深查）。
    actual_obj = actual
    actual_text = actual
    if isinstance(actual, dict):
        text_parts = []
        for _k in ("output", "reply", "error", "biz_error", "msg", "message"):
            _v = actual.get(_k)
            if _v not in (None, ""):
                text_parts.append(str(_v))
        if text_parts:
            actual_text = "\n".join(text_parts)

    # 拆分 contains / not_contains / 其它检查：
    #   contains      → any_of 时任一命中、否则全部命中
    #   not_contains  → 必须**全部不出现**（反向断言，不受 any_of 影响）
    #   其它(fields/value/field_values) → 全部命中，且需有业务数据
    contains_checks = [p for k, p in checks if k == "contains"]
    not_contains_checks = [p for k, p in checks if k == "not_contains"]
    other_checks = [(k, p) for k, p in checks
                    if k not in ("contains", "not_contains")]

    details = []
    all_pass = True

    # any_of（B 类纯对话）：contains 里任一命中即可（对话回复只需体现核心信息之一）
    if any_of and contains_checks:
        hits = []
        for kw in contains_checks:
            ok, d = contains_text(actual_text, kw)
            hits.append(ok)
            details.append(d)
        if any(hits):
            # 任一命中 → contains 部分通过
            pass
        else:
            all_pass = False
    else:
        for kw in contains_checks:
            ok, d = contains_text(actual_text, kw)
            details.append(d)
            if not ok:
                all_pass = False

    # not_contains：必须全部不出现（反向断言）。
    # 文本来源与 contains 相同（actual_text，含 msg/output 等文本键）。
    # 不做 any_of：哪怕只有一条被命中，就说明「不该出现的告警出现了」，判失败。
    for kw in not_contains_checks:
        hit, d = contains_text(actual_text, kw)
        if hit:
            details.append(f"不应出现但出现了: {kw}")
            all_pass = False
        else:
            details.append(f"确认未出现: {kw}")

    # 非 contains 检查（fields/value/field_values）：全部命中。
    # 基于「业务输出」（_biz_payload：解析 output 字符串里的工具 JSON），
    # 支持嵌套深查。修复前直接对执行器结构 dict 查字段 → 业务字段必误判缺失。
    check_obj = _biz_payload(actual_obj)
    has_biz = _has_biz_data(check_obj)
    for kind, payload in other_checks:
        if kind == "fields":
            if not has_biz:
                details.append("输出为业务失败/无数据响应，跳过字段断言")
                continue
            ok, d = has_fields(check_obj, payload)
        elif kind == "value":
            if not has_biz:
                details.append("输出为业务失败/无数据响应，跳过字段值断言")
                continue
            ok, d = value_eq(check_obj, payload.get("field"), payload.get("value"))
        elif kind == "field_values":
            ok, d = _check_field_contains_value(check_obj, payload.get("field"),
                                                payload.get("value"))
        else:
            continue
        details.append(d)
        if not ok:
            all_pass = False

    return all_pass, "；".join(details), True
