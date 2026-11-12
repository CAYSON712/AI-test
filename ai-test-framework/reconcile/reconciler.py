# -*- coding: utf-8 -*-
"""写入对账核心实现（通用化，零业务硬编码）。

一句话原理
----------
「写操作返回 code=0」不等于「业务真的生效」。本模块通过
  【执行前快照】 → 【执行写操作】 → 【执行后快照】
的前后对比，判断写入是否真的落地。

四种比对方式
------------
state_changed    快照前后必须不同（开关类 / 更新类）；最宽松，通用性最强
contains_id      执行后能在列表里按 ID 找到（创建类）；专治「创建假成功」
not_contains_id  执行后列表里查不到该 ID（删除类）
field_changed    指定字段值必须改变（比 state_changed 更精确）
value_equals     指定字段必须等于写入值（最严格，需读写字段名对齐）

配置来源
--------
configs/<系统>.yaml 的「写入对账」段。本模块不写死任何工具名/字段名。

为什么不做业务字段断言
----------------------
各 MCP 返回结构千差万别，写死断言极易误报。本模块刻意只比对
「状态是否变化」这类结构化事实，避免理解业务语义。
"""
import copy
import json
import re

__all__ = [
    "ReconcileSpec",
    "load_reconcile_specs",
    "build_reconcile_cases",
    "judge_reconcile",
    "extract_list",
    "compare_states",
]

# 比对方式常量
CMP_STATE_CHANGED = "state_changed"
CMP_CONTAINS_ID = "contains_id"
CMP_NOT_CONTAINS_ID = "not_contains_id"
CMP_FIELD_CHANGED = "field_changed"
CMP_VALUE_EQUALS = "value_equals"

_VALID_COMPARES = (
    CMP_STATE_CHANGED,
    CMP_CONTAINS_ID,
    CMP_NOT_CONTAINS_ID,
    CMP_FIELD_CHANGED,
    CMP_VALUE_EQUALS,
)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# =====================================================================
# 配置加载
# =====================================================================
class ReconcileSpec(object):
    """一条「写工具 ↔ 读工具」对账声明。"""

    __slots__ = (
        "write_tool", "read_tool", "compare", "id_field", "id_from",
        "compare_fields", "list_path", "batch_key", "verify_weak",
    )

    def __init__(self, raw):
        self.write_tool = raw.get("write_tool")
        self.read_tool = raw.get("read_tool")
        self.compare = raw.get("compare", CMP_STATE_CHANGED)
        self.id_field = raw.get("id_field")
        self.id_from = raw.get("id_from") or self.id_field
        self.compare_fields = raw.get("compare_fields") or []
        self.list_path = raw.get("list_path")
        self.batch_key = raw.get("batch_key")
        # verify_weak=True 表示读接口与写目标不同源（能力缺口），
        # 只能做弱校验：变更了不一定是这个字段变了，但「完全没变」一定有嫌疑。
        self.verify_weak = bool(raw.get("verify_weak", False))

    def validate(self):
        """返回错误描述列表；空列表表示合法。"""
        errs = []
        if not self.write_tool:
            errs.append("缺少 write_tool")
        if not self.read_tool:
            errs.append(f"{self.write_tool}: 缺少 read_tool")
        if self.compare not in _VALID_COMPARES:
            errs.append(
                f"{self.write_tool}: compare={self.compare!r} 非法，"
                f"应为 {_VALID_COMPARES}"
            )
        if self.compare in (CMP_FIELD_CHANGED, CMP_VALUE_EQUALS):
            if not self.compare_fields:
                errs.append(f"{self.write_tool}: compare={self.compare} 需要 compare_fields")
            if not self.id_field:
                errs.append(f"{self.write_tool}: compare={self.compare} 需要 id_field")
        if self.compare in (CMP_CONTAINS_ID, CMP_NOT_CONTAINS_ID) and not self.id_field:
            errs.append(f"{self.write_tool}: compare={self.compare} 需要 id_field")
        return errs

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self):
        return (
            f"ReconcileSpec({self.write_tool} -> {self.read_tool}, "
            f"{self.compare}, weak={self.verify_weak})"
        )


def load_reconcile_specs(config):
    """从系统 config dict 读取「写入对账」段。

    通用化：配置键名同时兼容中英文（写入对账 / reconcile），
    便于不同系统/不同团队命名习惯。
    返回 (specs, errors)。
    """
    if not isinstance(config, dict):
        return [], ["config 不是 dict"]

    section = (
        config.get("写入对账")
        or config.get("reconcile")
        or config.get("reconciliation")
        or {}
    )
    if not section:
        return [], []

    if not section.get("启用", section.get("enabled", True)):
        return [], []

    raw_pairs = section.get("配对") or section.get("pairs") or []
    specs, errors, seen = [], [], set()

    for item in raw_pairs:
        if not isinstance(item, dict):
            errors.append(f"配对项不是 dict: {item!r}")
            continue
        spec = ReconcileSpec(item)
        errs = spec.validate()
        if errs:
            errors.extend(errs)
            continue
        if spec.write_tool in seen:
            errors.append(f"{spec.write_tool}: 重复声明，已跳过后一条")
            continue
        seen.add(spec.write_tool)
        specs.append(spec)

    return specs, errors


# =====================================================================
# 返回体解析（通用，不假设具体结构）
# =====================================================================
def _dig(obj, path):
    """按 a.b.c 取值；path 为空返回原对象。"""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def extract_list(payload, list_path=None):
    """从任意返回体里抽出列表（实体集合）。

    探测顺序（通用，覆盖绝大多数 MCP 返回约定）：
      1. 显式 list_path（配置指定，最优先）
      2. payload 本身就是 list
      3. data.items / data.list / data.rows / items / list
      4. data 本身是 list
      5. data.<首个 list 值>
      6. 上述都失败 → 把单实体包成 [payload]（针对 get_xxx 详情型返回）
    返回 (list, source_desc)。
    """
    if list_path:
        got = _dig(payload, list_path)
        if isinstance(got, list):
            return got, f"list_path={list_path}"
        if got is not None:
            return [got], f"list_path={list_path}(单实体)"

    if isinstance(payload, list):
        return payload, "payload 根为 list"

    if not isinstance(payload, dict):
        return [], "payload 非 dict/list"

    # 常见的容器字段名（不是业务字段，是通用 API 约定）
    containers = ("items", "list", "rows", "records", "data")
    for key in containers:
        val = payload.get(key)
        if isinstance(val, list):
            return val, f"{key} 为 list"
        if isinstance(val, dict):
            for inner in ("items", "list", "rows", "records"):
                sub = val.get(inner)
                if isinstance(sub, list):
                    return sub, f"{key}.{inner} 为 list"
            # data 是单实体（如 get_store_info 的 data.store）
            for sub_key, sub_val in val.items():
                if isinstance(sub_val, list) and sub_val and isinstance(sub_val[0], dict):
                    return sub_val, f"{key}.{sub_key} 为实体 list"

    # 详情型返回：把 payload.data 或 payload 当单实体
    #
    # ⚠ 关键修正（2026-11-10 实测踩坑）：
    #   形如 {"data": {"isPreview": false, "store": {...}}} 的返回，
    #   data 本身只是「信封」，真正的业务实体在 data.store 里。
    #   此前直接返回 [data] → 实体含 {"isPreview":..., "store": <dict>}，
    #   而 _normalize_entity 会把 store 这个 dict 序列化成字符串，
    #   导致 compare_fields=["name"] 用 _dig 取不到值 → 真变更被判「未变」（假阳性）。
    #   故：data 下若只有一个 dict 子键，就把它当实体展开。
    data = payload.get("data")
    if isinstance(data, dict):
        dict_children = {
            k: v for k, v in data.items()
            if isinstance(v, dict)
        }
        if len(dict_children) == 1:
            only_key, only_val = next(iter(dict_children.items()))
            return [only_val], f"data.{only_key} 单实体（信封展开）"
        return [data], "data 单实体"
    return [payload], "payload 单实体"


def _pick_id(entity, id_field):
    """从实体里取 ID，兼容常见大小写差异。"""
    if not isinstance(entity, dict) or not id_field:
        return None
    if id_field in entity:
        return entity[id_field]
    # 大小写不敏感兜底
    low = id_field.lower()
    for k, v in entity.items():
        if k.lower() == low:
            return v
    # 常见别名
    for alias in (id_field + "Id", id_field + "_id", id_field.capitalize()):
        if alias in entity:
            return entity[alias]
    return None


def _normalize_entity(entity, id_field=None):
    """实体规范化：去掉易变字段，便于快照比对。

    易变字段包含服务端每次返回都可能不同、但与「配置是否生效」无关的内容，
    如时间戳、请求追踪 ID。这里只排除明确的噪音字段名。
    """
    if not isinstance(entity, dict):
        return entity
    noise = {
        "updatedDate", "updatedAt", "modifiedDate", "lastModified",
        "requestId", "traceId", "correlationId", "timestamp",
    }
    out = {}
    for k, v in entity.items():
        if k in noise:
            continue
        if isinstance(v, (dict, list)):
            out[k] = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
        else:
            out[k] = v
    return out


def _snapshot(payload, spec):
    """生成可对比的快照。

    - 列表型返回 → 按 id_field 排序后的 [规范化实体]
    - 单实体返回 → 规范化 dict
    """
    items, _ = extract_list(payload, spec.list_path)
    norm = [_normalize_entity(e, spec.id_field) for e in items if isinstance(e, dict)]

    if spec.id_field:
        def sort_key(e):
            v = _pick_id(e, spec.id_field)
            return str(v) if v is not None else ""
        norm = sorted(norm, key=sort_key)

    if len(norm) == 1 and spec.compare in (CMP_FIELD_CHANGED, CMP_VALUE_EQUALS):
        return norm[0]
    return norm


def compare_states(before_payload, after_payload, spec):
    """比对两次读接口返回。

    返回 (changed: bool, detail: dict)。changed=True 表示状态确实变了。
    """
    before = _snapshot(before_payload, spec)
    after = _snapshot(after_payload, spec)
    detail = {"compare": spec.compare}

    if spec.compare == CMP_STATE_CHANGED:
        changed = json.dumps(before, sort_keys=True, default=str) != json.dumps(
            after, sort_keys=True, default=str
        )
        detail["before_hash"] = _hash(before)
        detail["after_hash"] = _hash(after)
        return changed, detail

    if spec.compare in (CMP_FIELD_CHANGED, CMP_VALUE_EQUALS):
        field_diffs = []
        for f in spec.compare_fields:
            b = _dig(before, f) if isinstance(before, dict) else None
            a = _dig(after, f) if isinstance(after, dict) else None
            if b != a:
                field_diffs.append({"field": f, "before": b, "after": a})
        detail["field_diffs"] = field_diffs
        return bool(field_diffs), detail

    if spec.compare in (CMP_CONTAINS_ID, CMP_NOT_CONTAINS_ID):
        target = detail.get("target_id")
        before_ids = {str(_pick_id(e, spec.id_field)) for e in before} if isinstance(before, list) else set()
        after_ids = {str(_pick_id(e, spec.id_field)) for e in after} if isinstance(after, list) else set()
        detail["before_count"] = len(before_ids)
        detail["after_count"] = len(after_ids)
        if target is None:
            # 没有目标 ID 时退化为「数量变化」判断
            changed = before_ids != after_ids
            detail["note"] = "未提供 target_id，退化为集合比对"
            return changed, detail
        tid = str(target)
        if spec.compare == CMP_CONTAINS_ID:
            exists_before = tid in before_ids
            exists_after = tid in after_ids
            detail.update(target_id=tid, exists_before=exists_before, exists_after=exists_after)
            # 变更 = 从「之前不在」变成「之后在」
            return (exists_after and not exists_before), detail
        exists_before = tid in before_ids
        exists_after = tid in after_ids
        detail.update(target_id=tid, exists_before=exists_before, exists_after=exists_after)
        # 删除类：之前应在、之后应不在
        return (exists_before and not exists_after), detail

    return False, detail


def _hash(obj):
    import hashlib
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


# =====================================================================
# 用例生成
# =====================================================================
def build_reconcile_cases(specs, base_cases=None, max_cases=None):
    """为每条对账声明生成对账用例（L2 层）。

    base_cases: 可选，从已有用例里挑一条同工具的用例作为「参数模板」，
                这样生成的用例能复用合法参数，提高可执行率。

    用例结构（框架内部约定，executor/评分器共同消费）：
    {
      "id": ...,
      "tool_name": <write_tool>,
      "reconcile": {
          "read_tool": ...,
          "compare": ...,
          "id_field": ...,
          "compare_fields": [...],
          "list_path": ...,
          "verify_weak": bool,
          "phase": "before" | "after",   # executor 用
      },
      "期望": {...}
    }
    """
    cases = []
    base_index = {}
    for c in (base_cases or []):
        t = c.get("tool_name")
        if t and t not in base_index:
            base_index[t] = c

    for spec in specs:
        base = base_index.get(spec.write_tool)
        params = copy.deepcopy((base or {}).get("入参")) if base else None

        case = {
            "id": f"RC_{spec.write_tool}",
            "level": "L2",
            "layer": "写入对账",
            "tool_name": spec.write_tool,
            "描述": (
                f"写后回读对账：{spec.write_tool} 执行后，"
                f"通过 {spec.read_tool} 校验状态确实改变（{spec.compare}）"
                + ("；弱校验（读接口与写目标不同源）" if spec.verify_weak else "")
            ),
            "reconcile": {
                "read_tool": spec.read_tool,
                "compare": spec.compare,
                "id_field": spec.id_field,
                "id_from": spec.id_from,
                "compare_fields": spec.compare_fields,
                "list_path": spec.list_path,
                "batch_key": spec.batch_key,
                "verify_weak": spec.verify_weak,
            },
            "期望": {
                "reconcile_result": "changed",
                "说明": "执行前后快照必须不同；若完全一致判定为「假成功」",
            },
        }
        if params is not None:
            case["入参"] = params
        if base:
            for k in ("前置", "前置条件", "block"):
                if k in base:
                    case[k] = base[k]

        cases.append(case)
        if max_cases and len(cases) >= max_cases:
            break

    return cases


# =====================================================================
# 评分
# =====================================================================
def judge_reconcile(result, reconcile_meta, before_payload, after_payload):
    """对账评分。

    result: 写操作本身的执行结果（dict，含 code/success 等）
    reconcile_meta: 用例里的 reconcile 段
    before_payload / after_payload: 前后两次读接口原始返回

    返回 (score: int 1-5, reason: str, detail: dict)
    评分口径：
      5  写成功且状态确实改变
      3  弱校验下状态改变（但读接口与写目标不同源，不能完全确认）
      1  写报失败
      1  写报成功但状态未变（假成功，本次改造的核心目标）
    """
    detail = {}

    if not isinstance(result, dict):
        return 1, "写操作无有效返回", detail

    code = result.get("code")
    success = result.get("success")
    write_ok = (code == 0) and (success is not False)
    detail["write_code"] = code
    detail["write_success"] = success

    if not write_ok:
        return 1, f"写操作失败（code={code}）", detail

    # 叠加 isNoOp 信号：写操作自称「无需变更」时，后续对账必须证明状态真的变了
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    is_noop = data.get("isNoOp")
    detail["write_isNoOp"] = is_noop

    spec_like = _MetaSpec(reconcile_meta)
    changed, cmp_detail = compare_states(before_payload, after_payload, spec_like)
    detail.update(cmp_detail)

    if changed:
        if spec_like.verify_weak:
            return 3, "状态已改变（弱校验：读接口与写目标不同源）", detail
        return 5, "写后状态已按预期改变", detail

    # 状态没变 → 假成功
    reason = "写操作返回成功，但回读状态完全未变（疑似假成功）"
    if is_noop is True:
        reason += "；且返回 isNoOp=true"
    elif spec_like.verify_weak:
        reason += "；注意本对账为弱校验"
    return 1, reason, detail


class _MetaSpec(object):
    """把用例里的 reconcile dict 适配成 compare_states 需要的对象。"""

    __slots__ = (
        "compare", "id_field", "id_from", "compare_fields",
        "list_path", "verify_weak", "write_tool", "read_tool", "batch_key",
    )

    def __init__(self, meta):
        meta = meta or {}
        self.compare = meta.get("compare", CMP_STATE_CHANGED)
        self.id_field = meta.get("id_field")
        self.id_from = meta.get("id_from") or self.id_field
        self.compare_fields = meta.get("compare_fields") or []
        self.list_path = meta.get("list_path")
        self.verify_weak = bool(meta.get("verify_weak", False))
        self.write_tool = meta.get("write_tool")
        self.read_tool = meta.get("read_tool")
        self.batch_key = meta.get("batch_key")
