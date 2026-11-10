"""测试数据生命周期管理（通用，不含任何业务知识）。

解决的问题
==========
真执行写操作会改变被测系统的真实数据：
  - 改了价格/库存/店名/状态 → 数据集里的实体 ID 与期望值失效，下次跑就错
  - 新建了自提时段/活动/员工 → 越跑越多，污染环境
  - 删除了真实实体 → 不可恢复

三层保护（本模块负责①②的运行时部分）
====================================
  ① 写前快照：调用写工具前，用「快照工具」把目标实体原值记下来落盘。
     即使恢复失败，也「有据可查」——知道该改回什么。
  ② 自动恢复：用例跑完（无论成败）按声明把原值写回。
  ③ 自建自删：由生成器产出「建→测→删」配套用例，不碰真实数据。

通用化原则
==========
本模块不认任何具体系统的工具名/字段名。所有规则来自
configs/<系统>.yaml 的「数据生命周期」段；未声明的系统行为与改动前一致。

配置示例
========
数据生命周期:
  快照目录: results/snapshots        # 可选，默认此值
  可还原:                            # 能查原值 → 能写回
    - 工具: batch_item_price
      快照工具: item_detail          # 用什么只读工具查原值
      主键参数: merchFoodIds         # 写工具里标识目标实体的参数名
      主键字段: itemId               # 快照返回里对应的字段名
      快照字段: [price]              # 需要记录/还原的字段
      恢复工具: item_update          # 可选，默认同「工具」
      恢复参数键: price              # 可选，默认同字段名
  不可恢复:                          # 删了就没，必须自建自删或跳过
    - delete_store_user
    - delete_extra_fee
"""
import json
import os
import time


class LifecycleConfig:
    """从系统配置解析出的数据生命周期规则。未配置时 rules 为空 = 完全旁路。"""

    def __init__(self, raw=None, root_dir=""):
        raw = raw if isinstance(raw, dict) else {}
        self.root_dir = root_dir or os.getcwd()
        self.enabled = bool(raw)
        self.snapshot_dir = raw.get("快照目录") or os.path.join(
            "results", "snapshots")
        # {工具名: 规则dict}
        self.restorable = {}
        for r in (raw.get("可还原") or []):
            if isinstance(r, dict) and r.get("工具"):
                self.restorable[str(r["工具"])] = r
        # 不可恢复工具集合（删了没得救）
        self.irreversible = {str(x) for x in (raw.get("不可恢复") or [])}

        # 新建类：建了要能删掉，否则越跑越多（第 2 批）
        # {建工具名: 规则dict}
        self.creatable = {}
        for r in (raw.get("新建后清理") or []):
            if isinstance(r, dict) and r.get("工具"):
                self.creatable[str(r["工具"])] = r
        # 本次运行新建实体的记录（工具 -> [清理信息]）
        self.created = []

        # 自建自删（第 3 批）：删除类用例不碰真实数据，
        # 先建一个临时实体，再删它，跑完确认已删。
        # {删除工具名: 规则dict}
        self.self_delete = {}
        for r in (raw.get("自建自删") or []):
            if isinstance(r, dict) and r.get("删除工具"):
                self.self_delete[str(r["删除工具"])] = r

        # 受测系统能力缺口：某些写操作「能做但无法验证/无法还原」，
        # 因为被测系统没暴露读取接口（如 set_* 无对应 get_* 查当前值）。
        # 这类用例执行会真改配置且无法自动还原，故默认跳过并标注原因，
        # 作为「受测系统能力缺口」上报，而不是硬跑污染环境。
        # {工具名: 原因说明}
        self.capability_gaps = {}
        for g in (raw.get("能力缺口") or []):
            if isinstance(g, dict) and g.get("工具"):
                self.capability_gaps[str(g["工具"])] = str(g.get("原因") or "")
            elif isinstance(g, str):
                self.capability_gaps[g] = ""

    # ---- 查询 ----
    def rule_for(self, tool_name):
        """返回该写工具的还原规则；无规则返回 None。"""
        if not self.enabled:
            return None
        return self.restorable.get(str(tool_name))

    def rule_for_create(self, tool_name):
        """返回该「新建」工具的清理规则；无规则返回 None。"""
        if not self.enabled:
            return None
        return self.creatable.get(str(tool_name))

    def self_delete_rule(self, delete_tool):
        """返回该删除工具的「自建自删」规则；无规则返回 None。"""
        if not self.enabled:
            return None
        return self.self_delete.get(str(delete_tool))

    def capability_gap(self, tool_name):
        """该工具是否存在「受测系统能力缺口」（能做但无法验证/还原）。

        返回原因字符串；无缺口返回 None。
        """
        if not self.enabled:
            return None
        return self.capability_gaps.get(str(tool_name))

    def is_irreversible(self, tool_name):
        return bool(self.enabled) and str(tool_name) in self.irreversible

    def needs_snapshot(self, tool_name):
        return self.rule_for(tool_name) is not None

    # ---- 路径 ----
    def run_dir(self, run_id=None):
        rid = run_id or time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.root_dir, self.snapshot_dir, rid)


class SnapshotStore:
    """快照落盘/读取。一条快照 = 一个实体的原值集合。"""

    def __init__(self, base_dir):
        self.base_dir = base_dir

    def _path(self, tool, key):
        safe = str(key).replace("/", "_").replace("\\", "_")[:120]
        return os.path.join(self.base_dir, f"{tool}__{safe}.json")

    def save(self, tool, key, payload):
        os.makedirs(self.base_dir, exist_ok=True)
        p = self._path(tool, key)
        try:
            # 已存在则不覆盖：保留「最初的原值」，
            # 否则被改第二次时会把已污染的值当成原值存下来。
            if os.path.exists(p):
                return p
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
        except Exception:
            return None
        return p

    def load(self, tool, key):
        p = self._path(tool, key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_all(self):
        if not os.path.isdir(self.base_dir):
            return []
        out = []
        for name in sorted(os.listdir(self.base_dir)):
            if name.endswith(".json"):
                out.append(os.path.join(self.base_dir, name))
        return out


# =====================================================================
# 运行时：快照 + 恢复
# =====================================================================
async def take_snapshot(executor, session, tool_name, tool_params, cfg,
                        store, log=None):
    """写操作前记录目标实体原值。返回快照列表（供恢复用）。

    只对配置里声明过「可还原」的工具生效；抛异常不影响主流程。
    """
    rule = cfg.rule_for(tool_name) if cfg else None
    if not rule:
        return []
    snap_tool = rule.get("快照工具")
    primary = rule.get("主键参数")
    primary_field = rule.get("主键字段")
    fields = rule.get("快照字段") or []
    if not (snap_tool and primary and primary_field and fields):
        return []

    # 取出本次操作的目标 ID（支持标量与数组）
    target = (tool_params or {}).get(primary)
    ids = target if isinstance(target, list) else ([target] if target else [])
    ids = [str(x) for x in ids if x]

    # 快照查询要带的上下文参数：沿用写工具里的租户类参数。
    # 注意：不少写工具本身不带 merchId（如 item_update 只要 itemId），
    # 此时不能因此放弃快照 —— 回退用系统默认店铺补齐（快照工具通常需要）。
    ctx = {k: v for k, v in (tool_params or {}).items()
           if str(k).lower() in ("merchid", "merchantid", "storeid", "companyid")}
    if not ctx:
        syscfg = getattr(executor, "sys", None)
        default_merch = getattr(syscfg, "merchant_id", None) if syscfg else None
        merch_param = getattr(syscfg, "merchant_param", None) if syscfg else None
        if default_merch and merch_param:
            ctx[merch_param] = default_merch

    # 两个字段各司其职，不能混用：
    #   主键参数 primary      —— 写工具里目标实体的参数名（item_update 用 itemId）
    #   快照主键参数         —— 快照工具里查该实体用的参数名（item_detail 也用 itemId）
    #   主键字段 primary_field —— 快照返回里定位该实体的字段名（item_detail 返回 id）
    # 默认快照参数名与写工具一致；不一致时可用「快照主键参数」覆盖。
    query_param = rule.get("快照主键参数") or primary
    lookup_field = primary_field or "id"

    # 快照工具是否接受「按主键查询」参数：
    # 有些工具（如 get_payment_methods）只返回全量列表、不接受过滤参数，
    # 此时不能塞查询参数（会被 strict schema 拒绝或忽略导致定位失败），
    # 改为「查全量 + 在返回里按主键字段过滤」。
    accepts_query = _tool_accepts(executor, snap_tool, query_param)

    snaps = []
    for eid in ids:
        q = dict(ctx)
        if accepts_query:
            q[query_param] = eid
        if _tool_accepts(executor, snap_tool, "confirm"):
            q["confirm"] = True
        try:
            ok, text, _ = await executor._call_tool(session, snap_tool, q)
        except Exception as e:
            if log:
                log(f"快照失败 {snap_tool}({query_param}={eid}): {e}")
            continue
        if ok:
            if log:
                log(f"快照工具报错 {snap_tool}({query_param}={eid}): {str(text)[:160]}")
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        # 定位用 lookup_field（快照返回里的字段名），不是写工具的 primary_field
        found = _find_entity(data, lookup_field, eid)
        if found is None:
            if log:
                log(f"快照未定位到实体 {snap_tool}({lookup_field}={eid})")
            continue
        origin = {f: found.get(f) for f in fields if f in found}
        if not origin:
            continue
        payload = {"工具": tool_name, "快照工具": snap_tool,
                   "主键参数": primary, "主键字段": primary_field,
                   "主键值": eid, "原值": origin, "ts": time.time()}
        p = store.save(tool_name, eid, payload)
        snaps.append(payload)
        if log:
            log(f"已快照 {tool_name}({eid}) 原值={origin} -> {p}")
    return snaps


async def restore_snapshots(executor, session, snaps, cfg, log=None):
    """按快照把原值写回。返回 (成功数, 失败清单)。

    恢复失败不抛异常（避免掩盖原始测试结果），只记日志。
    """
    if not snaps or not cfg:
        return 0, []
    ok_n, fails = 0, []
    for s in snaps:
        tool = s.get("工具")
        rule = cfg.rule_for(tool)
        if not rule:
            continue
        restore_tool = rule.get("恢复工具") or tool
        primary = s.get("主键参数")
        primary_field = s.get("主键字段")
        eid = s.get("主键值")
        origin = s.get("原值") or {}
        params = {primary: eid}
        # 字段名到恢复参数的映射（可选，默认同名）
        keymap = rule.get("恢复参数键") or {}
        if isinstance(keymap, str):
            keymap = {}
        for f, v in origin.items():
            params[keymap.get(f, f) if isinstance(keymap, dict) else f] = v
        # confirm 必须显式置 true，否则服务端只预览不执行——
        # 会出现「日志说已恢复、实际数据没变」的假恢复（实测踩过）。
        # 仅当恢复工具确实声明了 confirm 参数时才传，避免被 strict schema 拒绝。
        if _tool_accepts(executor, restore_tool, "confirm"):
            params["confirm"] = True
        # 租户上下文：快照里没存时回退系统默认店铺
        ctx_added = False
        for k in ("merchId", "merchantId", "storeId", "companyId"):
            if k in origin:
                params[k] = origin[k]
                ctx_added = True
        if not ctx_added and _tool_accepts(executor, restore_tool, "merchId"):
            syscfg = getattr(executor, "sys", None)
            if syscfg is not None and getattr(syscfg, "merchant_id", None):
                params["merchId"] = syscfg.merchant_id
        try:
            ok, text, _ = await executor._call_tool(session, restore_tool, params)
        except Exception as e:
            fails.append((eid, f"{type(e).__name__}: {e}"))
            continue
        if ok or _is_biz_error(text):
            fails.append((eid, str(text)[:160]))
            if log:
                log(f"恢复失败 {restore_tool}({eid}): {str(text)[:160]}")
            continue
        ok_n += 1
        if log:
            log(f"已恢复 {restore_tool}({eid}) -> {origin}")
    return ok_n, fails


def _tool_accepts(executor, tool_name, param):
    """该工具的 schema 是否声明了某个参数。

    用于「按需补参」：confirm / merchId 等只在工具支持时才加，
    避免给不支持的工具塞多余参数被 strict schema 拒绝。
    拿不到 schema 时保守返回 True（多数 MCP 对多余参数宽容）。
    """
    try:
        syscfg = getattr(executor, "sys", None)
        tools = getattr(syscfg, "tools", None) or []
        for t in tools:
            if isinstance(t, dict) and t.get("name") == tool_name:
                return param in (t.get("params") or [])
    except Exception:
        return True
    return True


def _find_entity(obj, field, value):
    """在返回体里递归找「该字段等于指定值」的那个对象。"""
    target = str(value)
    if isinstance(obj, dict):
        if str(obj.get(field)) == target:
            return obj
        for v in obj.values():
            r = _find_entity(v, field, target)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _find_entity(x, field, target)
            if r is not None:
                return r
    return None


def _is_biz_error(text):
    """判断返回是否为业务失败（code 非 0/200 或 success=false）。"""
    try:
        d = json.loads(text)
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    code = d.get("code")
    succ = d.get("success")
    if isinstance(code, (int, float)) and code not in (0, 200):
        return True
    if isinstance(succ, bool) and not succ:
        return True
    return False


# =====================================================================
# 第 2 批：新建后清理（防「越跑越多」）
# =====================================================================
def register_created(executor, cfg, tool_name, tool_params, text):
    """新建成功后登记「待清理」记录。返回记录 dict 或 None。

    难点：各系统「怎么删」差异极大，不能硬编码：
      - delete_extra_fee         要 feeId
      - delete_pickup_time_slot  要 day + slotIndex（根本不是 ID）
      - remove_notification_email 要 id
    故由配置声明「删除工具 + 删除参数怎么填」；这里只负责：
      ① 从新建返回体里捞出可用的 ID（createdXxxId / id 等多种命名）
      ② 记录新建时用过的参数（清理时可能需要，如 day / slotIndex）
    """
    rule = cfg.rule_for_create(tool_name) if cfg else None
    if not rule:
        return None
    del_tool = rule.get("删除工具")
    if not del_tool:
        return None

    # 新建时若用了「客户端预生成 ID」，它就在建参数里，直接取（最可靠）
    new_id = None
    id_param = rule.get("预生成ID参数")
    if id_param and isinstance(tool_params, dict):
        new_id = tool_params.get(id_param)
    id_field = rule.get("返回ID字段")
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if new_id is None and isinstance(data, dict):
        if id_field:
            new_id = _deep_get(data, id_field)
        if new_id is None:
            new_id = _first_created_id(data)

    rec = {
        "工具": tool_name,
        "删除工具": del_tool,
        "新实体ID": str(new_id) if new_id else None,
        "建时参数": dict(tool_params or {}),
        "按标识回查": rule.get("按标识回查"),
        "ts": time.time(),
    }
    cfg.created.append(rec)
    return rec


async def resolve_id_by_marker(executor, session, cfg, rec, log=None):
    """新建返回里没有 ID 时，按业务标识回查列表把它找出来。

    典型场景：add_report_subscription 建完只返回成功，
    但列表里有 email，可据此定位刚建那条的 id。
    """
    if not rec:
        return None
    spec = rec.get("按标识回查")
    if not isinstance(spec, dict):
        return None
    q_tool = spec.get("查询工具")
    marker_param = spec.get("标识参数")
    marker_field = spec.get("标识字段")
    id_field = spec.get("ID字段") or "id"
    if not (q_tool and marker_param and marker_field):
        return None

    marker_val = (rec.get("建时参数") or {}).get(marker_param)
    if marker_val is None:
        return None

    syscfg = getattr(executor, "sys", None)
    mer = getattr(syscfg, "merchant_id", None) if syscfg else None
    q = {}
    if mer and _tool_accepts(executor, q_tool, "merchId"):
        q["merchId"] = mer
    if _tool_accepts(executor, q_tool, "pageSize"):
        q["pageSize"] = 100
    if _tool_accepts(executor, q_tool, "confirm"):
        q["confirm"] = True

    try:
        ok, text, _ = await executor._call_tool(session, q_tool, q)
    except Exception:
        return None
    if ok:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None

    payload = data.get("data") if isinstance(data, dict) else None
    for item in _iter_dicts(payload):
        if str(item.get(marker_field)) == str(marker_val):
            got = item.get(id_field)
            if got:
                rec["新实体ID"] = str(got)
                if log:
                    log(f"按 {marker_field}={marker_val} 回查到 id={got}")
                return got
    return None


def _iter_dicts(obj):
    """遍历嵌套结构里的所有 dict。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _iter_dicts(x)


async def fill_tail_index(executor, session, cfg, rec, rule, log=None):
    """为「无 ID、靠索引删除」的资源补齐末尾索引。

    某些资源（如自提时段）删除时不用 ID，而用 (day, index)。
    新建后重新查一次列表，取该 day 下的最后一个 slot 索引，
    即刚新建的那条（列表按 index 递增追加）。
    """
    if not rec or not rule:
        return None
    snap_tool = rule.get("索引查询工具")
    if not snap_tool:
        return None
    days_field = rule.get("索引查询天字段") or "days"
    slots_field = rule.get("索引查询时段字段") or "slots"
    idx_field = rule.get("索引查询索引字段") or "index"

    # 建时参数里的 days 可能是数组，取第一个作为目标 day
    days = (rec.get("建时参数") or {}).get("days")
    if isinstance(days, list):
        day = days[0] if days else None
    else:
        day = days
    if day is None:
        return None

    syscfg = getattr(executor, "sys", None)
    mer = getattr(syscfg, "merchant_id", None) if syscfg else None
    q = {}
    if mer:
        q["merchId"] = mer
    if _tool_accepts(executor, snap_tool, "confirm"):
        q["confirm"] = True

    try:
        ok, text, _ = await executor._call_tool(session, snap_tool, q)
    except Exception:
        return None
    if ok:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None

    payload = data.get("data") if isinstance(data, dict) else None
    day_list = (payload or {}).get(days_field) or []
    for d in day_list:
        if str(d.get("day")) == str(day):
            slots = d.get(slots_field) or []
            if not slots:
                return None
            last = slots[-1].get(idx_field)
            rec["末尾索引"] = last
            if log:
                log(f"已确定 {day} 末尾索引={last}")
            return last
    return None


def _deep_get(obj, field):
    """在嵌套结构里按字段名取值（不区分大小写）。"""
    want = str(field).lower()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() == want:
                return v
        for v in obj.values():
            r = _deep_get(v, field)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _deep_get(x, field)
            if r is not None:
                return r
    return None


# 新建返回里「新建实体 ID」的常见命名，按优先级匹配
_CREATED_ID_HINTS = ("createdfeeid", "createditemid", "createdmenuid",
                     "createdcategoryid", "createdid", "newid", "id")


def _first_created_id(obj):
    """从新建返回里找新建实体的 ID。

    只认 UUID 形态、且排除 merchId 等上下文 ID，
    避免把「店铺 ID」误当成「刚新建的实体 ID」而删错东西。
    """
    found = {}
    _scan_ids(obj, found)
    for hint in _CREATED_ID_HINTS:
        if hint in found:
            return found[hint]
    return None


def _scan_ids(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ("merchid", "merchantid", "storeid", "companyid",
                      "userid", "accountid"):
                continue                      # 上下文 ID，不是新建实体
            if isinstance(v, str) and _looks_uuid(v):
                out.setdefault(kl, v)
            _scan_ids(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _scan_ids(x, out)


def _looks_uuid(s):
    s = str(s)
    return len(s) == 36 and s.count("-") == 4 and not s.startswith("00000000")


async def cleanup_created(executor, session, cfg, log=None):
    """清理本次运行新建的实体。返回 (成功数, 失败清单)。

    删除参数按配置声明的「删除参数」构造，支持占位符：
      {新实体ID}        —— 新建返回里捞到的 ID
      {merchId}         —— 系统默认店铺
      {建时参数.xxx}    —— 新建时用过的参数（如 day / days）
    """
    if not cfg or not cfg.created:
        return 0, []
    ok_n, fails = 0, []
    for rec in cfg.created:
        del_tool = rec.get("删除工具")
        if not del_tool:
            continue
        params = _build_delete_params(executor, rec, cfg)
        if params is None:
            fails.append((rec.get("新实体ID"), "无法构造删除参数"))
            if log:
                log(f"清理跳过 {del_tool}：无法构造删除参数（缺 ID 或建时参数）")
            continue
        if _tool_accepts(executor, del_tool, "confirm"):
            params["confirm"] = True
        try:
            ok, text, _ = await executor._call_tool(session, del_tool, params)
        except Exception as e:
            fails.append((rec.get("新实体ID"), f"{type(e).__name__}: {e}"))
            continue
        if ok or _is_biz_error(text):
            fails.append((rec.get("新实体ID"), str(text)[:160]))
            if log:
                log(f"清理失败 {del_tool}({params}): {str(text)[:160]}")
            continue
        ok_n += 1
        if log:
            log(f"已清理 {del_tool} 新建实体 {rec.get('新实体ID')}")
    cfg.created = []
    return ok_n, fails


def _build_delete_params(executor, rec, cfg):
    """按配置构造删除参数。返回 dict 或 None（无法构造）。"""
    rule = cfg.rule_for_create(rec["工具"]) or {}
    spec = rule.get("删除参数")
    new_id = rec.get("新实体ID")

    if isinstance(spec, dict) and spec:
        out = {}
        for k, v in spec.items():
            s = str(v)
            if s == "{新实体ID}":
                if not new_id:
                    return None
                out[k] = new_id
            elif s in ("{merchId}", "{merchIds}"):
                syscfg = getattr(executor, "sys", None)
                mid = getattr(syscfg, "merchant_id", None) if syscfg else None
                if not mid:
                    return None
                out[k] = mid
            elif s.startswith("{建时参数.") and s.endswith("}"):
                # 支持数组索引：{建时参数.days.0}
                key = s[len("{建时参数."):-1]
                val = rec.get("建时参数") or {}
                for part in key.split("."):
                    if isinstance(val, list):
                        try:
                            val = val[int(part)]
                        except (ValueError, IndexError):
                            return None
                    elif isinstance(val, dict):
                        val = val.get(part)
                    else:
                        return None
                if val is None:
                    return None
                out[k] = val
            elif s == "{新建后末尾索引}":
                # 列表型资源（如自提时段）新建后的末尾索引：
                # 由登记时记录的「新建前数量」推出（原数量即新元素的 index）
                idx = rec.get("末尾索引")
                if idx is None:
                    return None
                out[k] = idx
            else:
                out[k] = v
        return out

    # 未声明删除参数：退化为「按新建 ID 删」
    if not new_id:
        return None
    return {rule.get("删除主键") or "id": new_id}


# =====================================================================
# 第 3 批：自建自删（删除类用例不碰真实数据）
# =====================================================================
async def prepare_self_delete(executor, session, delete_tool, tool_params, cfg,
                              log=None):
    """删除类用例执行前：先建一个临时实体，把它的 ID 填进删除参数。

    返回 (ok, new_params, reason)：
      ok=True  → new_params 已把目标 ID 换成刚建的临时实体
      ok=False → reason 说明原因，调用方应跳过该用例（不能拿真实数据去删）

    为什么必须这样：delete_* 传真实 ID 会真删业务数据且无法还原。
    建一个临时实体再删它，测试语义完全等价（都验证了删除逻辑），
    但不造成任何真实损失。
    """
    rule = cfg.self_delete_rule(delete_tool) if cfg else None
    if not rule:
        return False, tool_params, "未配置「自建自删」规则"

    create_tool = rule.get("建工具")
    if not create_tool:
        return False, tool_params, "自建自删规则缺「建工具」"
    create_params = dict(rule.get("建参数") or {})

    # 建参数里的占位符替换
    syscfg = getattr(executor, "sys", None)
    default_merch = getattr(syscfg, "merchant_id", None) if syscfg else None
    for k, v in list(create_params.items()):
        s = str(v)
        if s in ("{merchId}", "{merchIds}"):
            if not default_merch:
                return False, tool_params, "缺默认店铺，无法构造建参数"
            create_params[k] = default_merch

    # ① 优先「客户端预生成 ID」：不少 MCP 的 create 接口接受客户端指定主键
    #   （schema 原文：Client should generate a new GUID. If empty, a new GUID
    #    will be generated）。预生成后无需从响应里解析 ID，最可靠。
    #   实测踩坑：menu_create / category_create 的返回体里没有 createdMenuId /
    #   createdCategoryId，靠响应解析必然失败 → 整条用例被跳过。
    preset_id = None
    id_param = rule.get("预生成ID参数")
    if id_param:
        import uuid as _uuid
        preset_id = str(_uuid.uuid4())
        create_params[id_param] = preset_id

    if _tool_accepts(executor, create_tool, "confirm"):
        create_params["confirm"] = True

    try:
        ok, text, _ = await executor._call_tool(session, create_tool, create_params)
    except Exception as e:
        return False, tool_params, f"建临时实体异常：{e}"
    if ok or _is_biz_error(text):
        return False, tool_params, f"建临时实体失败：{str(text)[:160]}"

    # 从新建返回里捞 ID：预生成的优先（客户端自带，服务端不一定回显）
    new_id = preset_id
    id_field = rule.get("返回ID字段")
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if new_id is None and isinstance(data, dict):
        if id_field:
            new_id = _deep_get(data, id_field)
        if new_id is None:
            new_id = _first_created_id(data)
    if not new_id:
        return False, tool_params, "建了临时实体但拿不到它的 ID（不敢删真实数据）"

    # 把删除参数里的占位符替换成临时 ID
    target_key = rule.get("删除主键")
    new_params = dict(tool_params or {})
    if not target_key:
        # 未声明主键：替换参数里第一个含 id 的键
        cands = [k for k in new_params if "id" in str(k).lower()]
        target_key = cands[0] if cands else None
    if not target_key:
        return False, tool_params, "无法确定删除主键参数名"

    new_params[target_key] = new_id
    # 列表型主键要包成数组
    if isinstance(tool_params.get(target_key), list):
        new_params[target_key] = [new_id]

    if log:
        log(f"自建自删：已创建临时实体 {new_id}（{create_tool}），"
            f"将用它替代真实目标执行 {delete_tool}")
    return True, new_params, ""


def check_irreversible(tool_name, cfg):
    """高危保护：不可恢复的写操作（删除类）在执行前给出明确提示。

    返回 (allow, reason)。allow=False 时调用方应跳过该用例。
    仅当配置声明了「不可恢复」清单时生效，否则一律放行（不影响老系统）。
    """
    if not cfg or not cfg.is_irreversible(tool_name):
        return True, ""
    reason = (f"{tool_name} 属不可恢复操作（配置声明于「数据生命周期.不可恢复」），"
              "删除后无法还原。请改用「自建自删」（先建临时实体再删它），"
              "或设置 ALLOW_DESTRUCTIVE=1 明确承担风险。")
    return False, reason
