# -*- coding: utf-8 -*-
"""一条命令接入新 MCP：拉 schema -> 平铺 -> 写 .env -> 生成 configs/<系统>.yaml 骨架。

产出（results/mcp_schemas/ 下）：
    <系统名>.json            全量工具 schema（含自动探测的 wrapper 字段）
    <系统名>.overview.md     按业务域分组的可读概览（工具数 / 必填参数）

自动识别「参数外壳」（写入 config 的「参数包装」）：
    toolParams  —— POS / 小韩面 / AutoMart / RetailPOS
    parameters  —— PocketStore
    （无外壳则 config 不写，executor 回退 toolParams）

用法（推荐，交互模式，绕开 PowerShell 中文 argv 乱码）:
    python scripts/_01_mcp_setup.py

用法（全参数 CLI，适合脚本化 / 非中文系统名）:
    python scripts/_01_mcp_setup.py --name <系统名> --url <MCP端点> --token <TOKEN> \\
        [--company-id <ID>] [--merchant-id <ID>] [--company-header CompanyId] \\
        [--req-type C] [--dry-run]

完成后还需要人工做的（脚本会打印提示）:
    1. 打开 configs/<系统>.yaml，按真实后端补「探针事实」注释（ID 字符串形态、
       必填隐藏参数、哪些工具恒空/报错等），并补「需要默认上下文参数的工具」等可选块
    2. 编写 ability/能力目录_<系统>.yaml + ability/实体清单_<系统>.yaml
    3. python scripts/_02_generate_dataset.py 生成数据集（参考能力目录的用法注释）
"""
import argparse
import json
import os
import sys

import httpx
import yaml
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # ai-test-framework/
ENV_PATH = os.path.join(_ROOT, ".env")
CONFIG_DIR = os.path.join(_ROOT, "configs")
SCHEMA_DIR = os.path.join(_ROOT, "results", "mcp_schemas")

load_dotenv(ENV_PATH)


# =====================================================================
# MCP 连接
# =====================================================================
async def _list_tools(url, token, company_id, company_header):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if company_id and company_header:
        headers[company_header] = str(company_id)
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                return tools


# =====================================================================
# schema 平铺
# =====================================================================
def _tool_field(t, *keys):
    """兼容 Tool 模型 / dict 的属性读取（inputSchema / input_schema 大小写差异）"""
    if isinstance(t, dict):
        for k in keys:
            if k in t:
                return t[k]
        return None
    for k in keys:
        if hasattr(t, k):
            return getattr(t, k)
    return None


# 已知的参数外壳名（properties 里只有一个壳，壳内才是真参数）
#   toolParams —— POS / AutoMart / RetailPOS 系列
#   parameters —— PocketStore 系列
# 新增系统若用其它外壳名，加进这里即可，无需改别处。
KNOWN_WRAPPERS = ("toolParams", "parameters")


def detect_wrapper(input_schema):
    """识别该工具的「参数外壳」名；常规（properties 直接是参数）返回 None。

    返回 (wrapper_name_or_None, inner_props, inner_required)。
    """
    if not isinstance(input_schema, dict):
        return None, {}, []
    props = input_schema.get("properties") or {}
    for shell in KNOWN_WRAPPERS:
        node = props.get(shell)
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            return shell, node.get("properties") or {}, list(node.get("required") or [])
    return None, props, list(input_schema.get("required") or [])


def _flatten_schema(input_schema):
    """平铺 input_schema -> (params 列表, required 列表)。

    兼容三种形态:
      1. 常规: properties 直接是参数, required 在顶层
      2. AutoMart 型: properties 只有 toolParams 壳, 壳内才是真参数
      3. PocketStore 型: properties 只有 parameters 壳, 壳内才是真参数
    """
    _wrapper, inner, required = detect_wrapper(input_schema)
    return list(inner.keys()), required


def _one_line(text, limit=500):
    if not text:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def build_tools(tools):
    """拉回来的原始工具列表 -> config 的 mcp_tools 平铺条目 [{name,description,params,required}]"""
    out = []
    for t in tools:
        name = _tool_field(t, "name")
        if not name:
            continue
        desc = _tool_field(t, "description", "desc") or ""
        schema = _tool_field(t, "inputSchema", "input_schema") or {}
        params, required = _flatten_schema(schema)
        out.append({
            "name": str(name),
            "description": _one_line(desc),
            "params": params,
            "required": required,
            # 原始 schema（含 type/enum/format）：契约巡检据此按类型造合法参数，
            # 否则会把数组参数传成标量、把实体主键塞成店铺ID，制造大量假缺陷。
            # 落盘时会被剔除（见 run()）。
            "_schema": schema,
        })
    return out


def detect_common_wrapper(raw_tools):
    """统计全部工具，返回出现最多的参数外壳名（无外壳则 None）。

    这批同类工具通常外壳一致；取「命中数最多的外壳」，少数派忽略。
    """
    counter = {}
    for t in raw_tools:
        schema = _tool_field(t, "inputSchema", "input_schema") or {}
        wrapper, _inner, _req = detect_wrapper(schema)
        if wrapper:
            counter[wrapper] = counter.get(wrapper, 0) + 1
    if not counter:
        return None
    return max(counter, key=counter.get)


# 业务域分类规则（工具名关键字 -> 中文域）。通用兜底：未命中归「其他」。
# 新系统若需更准的分类，可在 <系统>.yaml 里覆盖（未来扩展），当前够用。
_DOMAIN_RULES = [
    ("订单/配送", ["order", "delivery", "pickup", "takeout", "preorder",
                "cancel", "payment", "shipping"]),
    ("门店/店铺设置", ["store_", "shop_", "brand_story", "cover_image",
                  "social_account", "bank_account", "third_party", "push_to_pos",
                  "stripe", "qr", "sales_mode", "open_status", "link"]),
    ("商品/规格", ["item_", "spec_", "food_"]),
    ("菜单/分类", ["menu_", "category_"]),
    ("库存", ["stock"]),
    ("税", ["tax_"]),
    ("小费/附加费", ["tip", "extra_fee", "utensil", "charge"]),
    ("营销活动", ["campaign", "discount", "reward", "promotion", "coupon", "flash"]),
    ("员工/用户/角色", ["user", "staff", "role"]),
    ("报表/订阅", ["report", "subscription"]),
    ("监控/摄像头", ["camera", "kitch"]),
]


def _classify_tool(name):
    low = (name or "").lower()
    for tag, keys in _DOMAIN_RULES:
        for k in keys:
            if k in low:
                return tag
    return "其他"


def detect_shape_signals(raw_tools):
    """扫描原始 schema，识别「可能超出当前框架默认能力」的形态信号。

    返回信号列表（供 overview 展示 + Skill 预警规则解读）。纯粹描述性，不阻断。
    这些信号是「接新 MCP 时提前知道会不会踩框架坑」的依据。
    """
    sig = set()
    for t in raw_tools:
        sch = _tool_field(t, "inputSchema", "input_schema") or {}
        props = sch.get("properties") or {}
        inner = None
        for shell in KNOWN_WRAPPERS:
            if isinstance(props.get(shell), dict):
                inner = props[shell].get("properties")
                break
        inner = inner or props
        if not isinstance(inner, dict):
            continue
        for p, spec in inner.items():
            if not isinstance(spec, dict):
                continue
            pl = str(p).lower()
            tp = spec.get("type")
            # 分页：cursor / pageToken 类游标
            if any(k in pl for k in ("cursor", "pagetoken", "nexttoken", "offset")):
                sig.add("分页游标（cursor/pageToken），非 page/size")
            # 嵌套对象/数组：参数是 object 或 array-of-object
            if tp == "object" or (tp == "array" and isinstance(spec.get("items"), dict)
                                  and spec["items"].get("type") == "object"):
                sig.add(f"嵌套参数（{p} 为 object / array-of-object）")
            # 数组参数（批量）
            if tp == "array":
                sig.add("含数组参数（批量语义，需注意元素结构）")
            # 时间戳/日期格式
            if tp == "integer" and any(k in pl for k in ("time", "timestamp", "at")):
                sig.add("整数时间戳参数（时间语义为 epoch，非 ISO 字符串）")
    return sorted(sig)


def render_overview(name, tools, wrapper=None, raw_tools=None):
    """生成 <系统>.overview.md：按业务域分组的可读概览（含必填参数 + 形态信号）。"""
    groups = {}
    for t in tools:
        groups.setdefault(_classify_tool(t["name"]), []).append(t)

    signals = detect_shape_signals(raw_tools or []) if raw_tools else []

    lines = [
        f"# {name} MCP 工具概览",
        "",
        f"- 工具总数：**{len(tools)}**",
        f"- 参数外壳：`{wrapper}`" if wrapper else "- 参数外壳：无（properties 直接是参数）",
        f"- 生成时间：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M}",
    ]
    if signals:
        lines.append("- **形态信号（关注是否需扩展框架）**：")
        for s in signals:
            lines.append(f"    - ⚠ {s}")
    else:
        lines.append("- 形态信号：未发现特殊形态（分页游标/嵌套参数/时间戳等）")
    lines += [
        "",
        "| 业务域 | 工具数 |",
        "|---|---|",
    ]
    for tag in sorted(groups, key=lambda x: -len(groups[x])):
        lines.append(f"| {tag} | {len(groups[tag])} |")
    lines.append("")

    for tag in sorted(groups, key=lambda x: -len(groups[x])):
        lines.append(f"## {tag}（{len(groups[tag])}）")
        lines.append("")
        for t in groups[tag]:
            req = f"  **required={t['required']}**" if t["required"] else ""
            lines.append(f"- `{t['name']}`{req}")
            lines.append(f"    - {t['description'][:220]}")
        lines.append("")
    return "\n".join(lines)


def _pick_demo_tool(tools):
    names = [t["name"] for t in tools]
    for pref in ("query_merchants", "query_companies"):
        if pref in names:
            return pref
    for n in names:
        if n.startswith(("query_", "list_", "get_")):
            return n
    return names[0] if names else ""


# =====================================================================
# .env 更新（去重：命中 key 替换原行，未命中追加）
# =====================================================================
def upsert_env(pairs):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    keys = set(pairs)
    kept, added = [], []
    for ln in lines:
        if not ln or ln.lstrip().startswith("#") or "=" not in ln:
            kept.append(ln)
            continue
        k = ln.split("=", 1)[0].strip()
        if k in keys:
            added.append(f"{k}={pairs[k]}")   # 用新值替换
        else:
            kept.append(ln)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(kept + added) + "\n")
    return added


# =====================================================================
# config 骨架生成
# =====================================================================
def _env_prefix(name):
    import re
    s = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper()
    return s if s else "MCP"


def render_config(name, tools, args, wrapper=None):
    pre = _env_prefix(name)
    demo = _pick_demo_tool(tools)
    conn = {
        "需求类型": args.req_type,
        "base_url_env": f"{pre}_MCP_URL",
        "token_env": f"{pre}_MCP_TOKEN",
    }
    if args.company_id:
        conn["company_id_env"] = f"{pre}_MCP_COMPANY_ID"
    if args.merchant_id:
        conn["merchant_id_env"] = f"{pre}_MCP_MERCHANT_ID"
    if args.company_id:
        conn["请求头"] = {args.company_header or "CompanyId": "{{company_id}}"}
    if args.company_id:
        conn["auth_header"] = "Authorization: Bearer {{token}}"
    if wrapper:
        conn["参数包装"] = wrapper   # executor 按此把入参包进对应外壳

    doc = {
        "连接": conn,
    }
    if demo:
        doc["连接演示工具"] = demo
    doc["mcp_tools"] = tools

    header = [
        "# =====================================================================",
        f"# 系统配置：{name}（mcp_setup 自动生成骨架，{__import__('datetime').datetime.now():%Y-%m-%d %H:%M}）",
        "# =====================================================================",
        "# 用途：给 generic_mcp_executor 提供「连接 + 工具 schema」。",
        "# 注意：「参数包装」已由脚本自动探测填入，勿手改（除非后端改口径）。",
        "# 生成后请人工补：",
        "#   1. 探针事实注释（ID 参数形态/必填隐藏参数/恒空或报错的工具/真实可用时间窗等）",
        "#   2. 可选块（参考 configs/POS_商品管理.yaml / configs/RetailPOS数据查询.yaml）：",
        "#      公司ID请求头 / 默认上下文参数名 / 实体ID参数名 / 按名搜索工具 / 需ID解析的工具",
        "#      需要默认上下文参数的工具 / verify / 需要校验的工具",
        "# =====================================================================",
        "",
    ]
    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return "\n".join(header) + body


# =====================================================================
# 接入期契约巡检（对每个工具真调一次，检查返回体是否自洽）
#
# 定位：schema 只能告诉我们「工具有哪些参数」，看不到「调下去返回对不对」。
#   实测 PocketStore 曾出现的缺陷全部在 schema 层不可见、必须真调才暴露：
#     ① list_all_campaigns 返回的 merchId 恒为全零 UUID（响应字段未回填）
#     ② list_report_subscriptions 无视店铺、恒返回另一个租户的数据
#     ③ 只读工具不带 confirm=true 时返回 isPreview 空壳（items=[]/total=0）
#     ④ list_discount_campaigns(filterType="") 抛 1006 反序列化异常
#   这些都不是「调用成功/字段存在」能发现的，故在接入阶段一次性真调巡检。
#
# 安全边界：写操作一律 confirm=false（只预览不执行），绝不真实改/删数据。
# =====================================================================

# 判定「写操作」的关键字（与 _02 的 _op_of 语义对齐，此处只用于圈定安全边界，
# 宁可多判为写操作——误判成写操作只是降级为预览，误判成只读则会真改数据）。
_WRITE_HINTS = (
    "add_", "create_", "update_", "delete_", "remove_", "set_", "bind_",
    "unbind_", "publish_", "batch_", "generate_", "menu_create", "item_create",
)


def _is_write_tool(name):
    n = str(name or "").lower()
    return any(n.startswith(h) or h in n for h in _WRITE_HINTS)


def _scan_collect_ids(obj, out):
    """递归收集返回体里所有形如 *id / *Ids 的字段值及其字段名。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl == "merchid" or kl == "merchantid" or kl == "storeid":
                out.setdefault("_merch_fields", []).append((k, str(v)))
            elif kl == "ispreview":
                out.setdefault("_is_preview", []).append(v)
            if kl.endswith("id") or kl.endswith("ids"):
                if isinstance(v, str):
                    out.setdefault("_ids", []).append((k, v))
            _scan_collect_ids(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _scan_collect_ids(x, out)
    return out


_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# 跨店对比时要剔除的「请求回显 / 分页」字段：它们随请求变化，
# 保留会让「数据本体完全一致」的两次调用看起来不同，漏掉硬编码缺陷。
_ECHO_KEYS = {"merchid", "merchantid", "storeid", "shopid", "pageindex",
              "pagesize", "page", "keyword", "filtertype", "msg", "message"}

# 账号级工具：本就不该按店铺隔离，返回一致属正常，不参与跨店对比。
# 判定依据仅限「语义上明确是账号/身份级」的工具名，不做泛化猜测。
#   ⚠ 不要把「有 merchId 参数、但返回值归属与请求不一致」的工具放进来：
#     那是真缺陷（见 get_bank_account_info / list_report_subscriptions）。
_ACCOUNT_LEVEL_TOOLS = {
    "list_my_merchants",     # 列出账号下所有店铺，本来就与单个店铺无关
}


def _biz_signature(text):
    """提取返回体的「业务数据签名」：剥离分页/请求回显后做稳定序列化。

    返回 (signature, sample_count)：
      signature    —— 稳定序列化串；无法解析/业务失败时 None
      sample_count —— 参与对比的业务条目数。0 表示「空结果」，
                      空结果不能作为「无视店铺过滤」的证据
                      （两个店铺都真的没数据，返回一致是正常现象）。
    """
    try:
        data = json.loads(text)
    except Exception:
        return None, 0
    if not isinstance(data, dict):
        return None, 0
    code = data.get("code")
    succ = data.get("success")
    if (isinstance(code, (int, float)) and code not in (0, 200)) \
            or (isinstance(succ, bool) and not succ):
        return None, 0
    payload = data.get("data")
    if payload is None:
        return None, 0

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items()
                    if str(k).lower() not in _ECHO_KEYS}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o

    def count(o):
        """业务条目数：优先取 items 列表长度，其次 total/totalCount。"""
        if isinstance(o, dict):
            for k in ("items", "roles", "campaigns"):
                if isinstance(o.get(k), list):
                    return len(o[k])
            for k in ("total", "totalCount"):
                if isinstance(o.get(k), int):
                    return o[k]
            return 1 if o else 0
        if isinstance(o, list):
            return len(o)
        return 0

    return json.dumps(strip(payload), ensure_ascii=False, sort_keys=True), count(payload)


def _check_response(tool, params, text, expect_merch=None, expect_preview=False):
    """对单次调用返回做通用契约检查。返回 issues 列表（空=无问题）。

    不含任何业务知识，全部规则对任意 MCP 通用。
    """
    issues = []
    if not text:
        return [("EMPTY", "返回内容为空")]

    try:
        data = json.loads(text)
    except Exception:
        # 非 JSON 返回不判缺陷，只记录（部分系统返回纯文本属正常契约）
        return []

    # 统一成功标志（各系统字段名不同，做了兼容）
    code = data.get("code") if isinstance(data, dict) else None
    succ = data.get("success") if isinstance(data, dict) else None
    is_err = (isinstance(code, (int, float)) and code not in (0, 200)) \
        or (isinstance(succ, bool) and not succ)

    acc = _scan_collect_ids(data, {})

    # ① 客户端已传 confirm=true 却仍返回预览空壳 → confirm 未生效
    if not expect_preview:
        previews = acc.get("_is_preview") or []
        if any(p is True for p in previews):
            issues.append(("PREVIEW_IGNORED",
                           "已传 confirm=true 但仍返回 isPreview=true（confirm 未生效）"))

    # ② 响应里的归属店铺字段与请求不一致 → 字段未回填 / 跨租户
    if expect_merch and not is_err:
        for field, val in (acc.get("_merch_fields") or []):
            v = (val or "").strip()
            if not v:
                continue
            if v == _ZERO_UUID:
                issues.append(("MERCH_ZERO",
                               f"字段 {field} 为全零 UUID（未回填真实归属）"))
            elif v != str(expect_merch):
                issues.append(("MERCH_MISMATCH",
                               f"字段 {field}={v} 与请求店铺 {expect_merch} 不一致"
                               "（疑似跨租户/未按店铺过滤）"))

    # ③ 业务成功却返回全零 UUID 主键 → 占位/脏数据
    if not is_err:
        zeros = [(k, v) for k, v in (acc.get("_ids") or []) if v == _ZERO_UUID]
        if zeros:
            names = ", ".join(sorted({k for k, _ in zeros}))
            issues.append(("ZERO_ID",
                           f"返回体含全零 UUID 的 ID 字段：{names}"))

    # ④ 业务失败时的错误类别（区分「友好校验」与「服务端异常」）
    if is_err:
        msg = str(data.get("msg") or data.get("message") or "")
        low = msg.lower()
        if any(k in low for k in ("internal error", "could not be converted",
                                  "stack", "traceback", "unhandled")):
            issues.append(("SERVER_CRASH",
                           f"服务端内部错误（非友好校验）：{msg[:120]}"))
    return issues


async def _probe_contracts(url, token, company_id, company_header, tools,
                           merchant_ids, wrapper, only=None, timeout=60.0):
    """接入期契约巡检：逐工具真调，返回 [(工具, 检查项, 结果, 说明)]。

    merchant_ids: 两个及以上店铺 ID，用于「跨店对比」判断是否按店铺过滤。
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if company_id and company_header:
        headers[company_header] = str(company_id)

    wrap = wrapper
    results = []

    # ---- 参数生成：严格按 schema 的 type/enum/format 造值 ----
    def _props_of(t):
        sch = t.get("_schema") or {}
        _w, inner, _req = detect_wrapper(sch)
        return inner or {}

    def build_args(t, mid):
        """按 schema 类型生成最小合法参数集。

        关键约束（不加会制造大量假缺陷）：
          - array 类型必须传数组，传标量会触发服务端反序列化异常，
            看起来像「服务端崩溃」实为探针参数错。
          - 实体主键（itemId/menuId/categoryId/taxId/specId/merchManagerId 等）
            不能塞 merchId：拿店铺ID当商品ID必然查不到，会误报缺陷。
            这类参数在接入期拿不到真实值，直接不传（多数为可选）。
        """
        props = _props_of(t)
        a = {}
        for p in (t.get("params") or []):
            pl = str(p).lower()
            spec = props.get(p) or {}
            tp = spec.get("type")
            tps = tp if isinstance(tp, list) else [tp]
            enum = spec.get("enum")

            if pl == "confirm":
                a[p] = not is_write
                continue
            if pl in ("page", "pageindex"):
                a[p] = 1
                continue
            if pl in ("pagesize",):
                a[p] = 20
                continue
            # 租户级 ID：可安全用请求店铺填充
            if pl in ("merchid", "merchantid", "storeid", "shopid"):
                if mid:
                    a[p] = mid
                continue
            # 其它 *Id：属具体实体主键，接入期无真实值 → 跳过（多为可选）
            if "id" in pl:
                continue
            if "array" in tps:
                a[p] = []
                continue
            if enum:
                a[p] = enum[0]
                continue
            if "boolean" in tps:
                a[p] = False
                continue
            if "integer" in tps or "number" in tps:
                a[p] = 1
                continue
            if "string" in tps:
                fmt = spec.get("format")
                if fmt == "uuid":
                    continue
                if fmt == "email":
                    a[p] = "probe@example.com"
                    continue
                # 日期/时间：必须给合法 ISO 串。传 "probe" 会触发服务端
                # DateTimeOffset 反序列化异常，看起来是「服务端崩溃」，
                # 实为探针造错参数（create_discount_campaign 曾因此误报）。
                if fmt in ("date-time", "datetime", "date") \
                        or pl.endswith("date") or "time" in pl:
                    a[p] = "2026-12-31T00:00:00Z" if (
                        fmt in ("date-time", "datetime") or pl.endswith("time")
                    ) else "2026-12-31"
                    continue
                desc = str(spec.get("description") or "").lower()
                if "date" in desc or "time" in desc:
                    a[p] = "2026-12-31T00:00:00Z"
                    continue
                a[p] = "probe"
        return a

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                for t in tools:
                    name = t.get("name")
                    if not name:
                        continue
                    if only and only not in str(name):
                        continue
                    is_write = _is_write_tool(name)
                    # 只对「自身声明了 merchId 参数」的工具做店铺一致性校验：
                    # 没有该参数却返回带商铺归属的数据，属租户上下文硬编码，
                    # 归入 SAME_ACROSS_MERCH 那类（另判），不在 MERCH_* 里重复报。
                    props = _props_of(t)
                    has_merch_param = any(
                        str(p).lower() in ("merchid", "merchantid", "storeid")
                        for p in props)

                    mids = merchant_ids[:1] if is_write else (merchant_ids or [None])
                    seen = []
                    for mid in mids:
                        args = build_args(t, mid)
                        payload = {wrap: args} if wrap else args
                        try:
                            r = await session.call_tool(name, payload)
                            text = "".join(
                                c.text for c in (r.content or [])
                                if getattr(c, "type", None) == "text")
                        except Exception as e:
                            results.append((name, "CALL_FAIL", "FAIL",
                                            f"{type(e).__name__}: {e}"))
                            seen = []
                            break

                        issues = _check_response(
                            name, args, text,
                            expect_merch=mid if (has_merch_param and not is_write) else None,
                            expect_preview=is_write)
                        for code, desc in issues:
                            results.append((name, code, "FAIL", desc))
                        if not issues:
                            results.append((name, "BASIC", "PASS",
                                            "写操作仅预览" if is_write else "无异常"))
                        seen.append(text)

                    # 跨店对比：比对「业务数据本体」（剔除请求回显字段），
                    # 不能直接比原始文本 —— 中转的 merchId/分页参数不同也会导致
                    # 文本不等，从而把「确实没按店铺过滤」的缺陷漏掉。
                    if not is_write and len(seen) >= 2 \
                            and name not in _ACCOUNT_LEVEL_TOOLS:
                        pairs = [_biz_signature(s) for s in seen]
                        sigs = [p[0] for p in pairs]
                        counts = [p[1] for p in pairs]
                        # 空结果 / 解析失败不参与对比：
                        # 两店都真的没有数据时返回一致是正常现象，
                        # 不能据此判「无视店铺过滤」。
                        nonempty = all(c > 0 for c in counts)
                        if nonempty and all(x is not None for x in sigs) \
                                and len(set(sigs)) == 1:
                            if has_merch_param:
                                results.append((name, "SAME_ACROSS_MERCH", "FAIL",
                                                f"{len(seen)} 个不同店铺返回 {counts[0]} 条"
                                                "完全相同的业务数据"
                                                "（已传 merchId 但仍无视过滤）"))
                            else:
                                results.append((name, "TENANT_FIXED", "FAIL",
                                                f"工具无店铺参数，各店铺均返回同 {counts[0]} 条"
                                                "数据（租户上下文硬编码，"
                                                "无法查询其它店铺）"))
    return results


def render_contract_scan(name, results, tool_count):
    """巡检结果 -> Markdown 体检表（按「工具+检查项」聚合，避免逐条重复刷屏）。"""
    from collections import OrderedDict

    bad = [r for r in results if r[2] == "FAIL"]
    agg = OrderedDict()
    for t, c, _s, d in bad:
        key = (t, c)
        if key not in agg:
            agg[key] = [d, 1]
        else:
            agg[key][1] += 1

    lines = [
        f"# 契约巡检报告：{name}",
        "",
        f"- 工具总数：{tool_count}",
        f"- 检查记录：{len(results)} 条",
        f"- 异常工具：{len({t for t, _c in agg})} 个",
        f"- 异常项：{len(agg)} 项"
        + (f"（去重前 {len(bad)} 条）" if len(bad) != len(agg) else ""),
        "",
    ]
    if not agg:
        lines.append("**全部通过：未发现契约异常。**")
    else:
        lines.append("## 异常明细")
        lines.append("")
        lines.append("| 工具 | 检查项 | 命中 | 说明 |")
        lines.append("|---|---|---|---|")
        for (t, c), (d, n) in agg.items():
            d = str(d).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{t}` | {c} | {n} | {d} |")
        lines.append("")
        lines.append("## 检查项说明")
        lines.append("")
        lines.append("| 代码 | 含义 |")
        lines.append("|---|---|")
        lines.append("| MERCH_ZERO | 响应归属店铺字段为全零 UUID（未回填真实归属） |")
        lines.append("| MERCH_MISMATCH | 响应归属店铺与请求不一致（疑似跨租户） |")
        lines.append("| SAME_ACROSS_MERCH | 已传 merchId 但换店铺返回完全一致（无视过滤） |")
        lines.append("| TENANT_FIXED | 工具无店铺参数，恒返回 token 绑定店铺的数据 |")
        lines.append("| PREVIEW_IGNORED | 传 confirm=true 仍返回预览空壳 |")
        lines.append("| ZERO_ID | 返回全零 UUID 主键（占位/脏数据） |")
        lines.append("| SERVER_CRASH | 服务端内部错误（非友好校验） |")
        lines.append("| CALL_FAIL | 调用直接抛异常 |")
        lines.append("")
        lines.append("> 说明：巡检按 schema 类型（type/enum/format）造合法参数。")
        lines.append("> 实体主键（itemId/menuId 等）在接入期拿不到真实值时**不传**，")
        lines.append("> 避免「拿店铺ID当商品ID」产生假缺陷。")
        lines.append("> 写操作一律 confirm=false，仅预览不执行。")
    return "\n".join(lines) + "\n"


# =====================================================================
# 主流程
# =====================================================================
def _ask(label, default="", secret=False):
    if default:
        label = f"{label}（默认 {default}）"
    v = input(f"{label}: ").strip()
    return v if v else default


def _interactive():
    print("== 新 MCP 接入（交互式；Ctrl+C 退出）==")
    name = _ask("系统名（用于 config 文件名与 .env 前缀，如 小韩面MCP）")
    if not name:
        print("系统名必填"); sys.exit(2)
    url = _ask("MCP 端点 URL")
    if not url:
        print("URL 必填"); sys.exit(2)
    token = _ask("Bearer token（可留空后续补 .env）")
    company_id = _ask("请求头公司 ID（如 26377677164869，可留空）")
    merchant_id = _ask("默认店铺 ID merchantId（可留空）")
    company_header = _ask("公司 ID 请求头名", "CompanyId")
    req_type = _ask("需求类型", "C")
    # 巡检用店铺：默认用上面的默认店铺；多给一个即可做跨店对比
    probe_m = _ask("契约巡检用店铺 ID（逗号分隔，给 2 个可做跨店对比；留空跳过）",
                   merchant_id)
    probe_merchants = [x.strip() for x in str(probe_m or "").split(",") if x.strip()]
    return argparse.Namespace(name=name, url=url, token=token, company_id=company_id,
                              merchant_id=merchant_id, company_header=company_header,
                              req_type=req_type, dry_run=False,
                              probe=bool(probe_merchants),
                              probe_merchants=probe_merchants, probe_only="")


def run(args):
    # 1. 拉 schema
    import asyncio
    try:
        raw_tools = asyncio.run(_list_tools(
            args.url, args.token, args.company_id, args.company_header))
    except Exception as e:
        print(f"[FAIL] 连接 MCP 失败：{e}")
        print("  检查：URL 是否正确 / token 是否有效 / 公司 ID 请求头名是否正确")
        return 2
    tools = build_tools(raw_tools)
    if not tools:
        print("[FAIL] list_tools 返回空（可能鉴权隐藏全部工具）")
        return 2
    # 落盘用的干净副本（剔除仅巡检需要的 _schema，保持 config 格式不变）
    tools_for_schema = [{k: v for k, v in t.items() if k != "_schema"}
                        for t in tools]
    wrapper = detect_common_wrapper(raw_tools)
    print(f"[OK] list_tools 返回 {len(tools)} 个工具"
          f"（参数外壳: {wrapper or '无（properties 直接是参数）'}）：")
    for t in tools:
        req = f"  required={t['required']}" if t["required"] else ""
        print(f"   - {t['name']}{req}")

    # 2. schema 落盘
    os.makedirs(SCHEMA_DIR, exist_ok=True)
    schema_path = os.path.join(SCHEMA_DIR, f"{args.name}.json")
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump({"name": args.name, "wrapper": wrapper, "tools": tools_for_schema}, f,
                  ensure_ascii=False, indent=1)
    print(f"[OK] schema 落盘 -> {os.path.relpath(schema_path, _ROOT)}")

    # 2.1 完整原始 schema（含参数 description）另存一份 <系统>.raw.json。
    #     生成器从描述里提取「候选枚举值」（很多 MCP 把合法值写在描述而非 enum），
    #     用于给必填参数生成真实可用的默认值。平铺版丢失了 description，故必须单独存。
    raw_path = os.path.join(SCHEMA_DIR, f"{args.name}.raw.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump({"name": args.name, "wrapper": wrapper,
                   "tool_count": len(raw_tools),
                   "tools": raw_tools}, f, ensure_ascii=False, indent=1)
    print(f"[OK] 原始 schema 落盘 -> {os.path.relpath(raw_path, _ROOT)}")

    # 2.5 业务域概览（人眼速览，免去每次写分类脚本）
    overview_path = os.path.join(SCHEMA_DIR, f"{args.name}.overview.md")
    with open(overview_path, "w", encoding="utf-8") as f:
        f.write(render_overview(args.name, tools_for_schema, wrapper, raw_tools))
    print(f"[OK] 概览落盘 -> {os.path.relpath(overview_path, _ROOT)}")

    # 2.6 契约巡检（真调每个工具，检查返回体是否自洽）
    #     位置说明：schema 只能看到「有哪些参数」，看不到「调下去对不对」。
    #     历史上多个缺陷（响应 merchId 全零 / 跨租户返回 / confirm 未生效 /
    #     枚举报 1006）全部在 schema 层不可见，必须真调才暴露，故放在这里。
    #     安全边界：写操作一律 confirm=false（只预览），不真改数据。
    probed_mids = [m for m in args.probe_merchants if m]
    if args.probe and probed_mids:
        print(f"\n== 契约巡检（{len(tools)} 个工具，店铺 {len(probed_mids)} 个）==")
        print("   安全边界：写操作一律 confirm=false，仅预览不执行")
        try:
            scans = asyncio.run(_probe_contracts(
                args.url, args.token, args.company_id, args.company_header,
                tools, probed_mids, wrapper, only=args.probe_only))
        except Exception as e:
            print(f"[WARN] 契约巡检失败（不影响 config 生成）：{e}")
            scans = []
        if scans:
            scan_path = os.path.join(SCHEMA_DIR, f"{args.name}.contract.md")
            with open(scan_path, "w", encoding="utf-8") as f:
                f.write(render_contract_scan(args.name, scans, len(tools)))
            bad = [r for r in scans if r[2] == "FAIL"]
            print(f"[OK] 巡检完成：{len(scans)} 条记录，异常 {len(bad)} 条")
            if bad:
                seen = set()
                for t, c, _s, _d in bad:
                    if t in seen:
                        continue
                    seen.add(t)
                    codes = sorted({x[1] for x in bad if x[0] == t})
                    print(f"   ✗ {t}: {', '.join(codes)}")
            print(f"[OK] 体检表 -> {os.path.relpath(scan_path, _ROOT)}")
    elif args.probe and not probed_mids:
        print("[跳过巡检] 未提供 --probe-merchants（需要至少 1 个店铺 ID，"
              "给 2 个以上才能做跨店对比）")

    if args.dry_run:
        print("[dry-run] 未写 .env / config")
        return 0

    # 3. .env
    pre = _env_prefix(args.name)
    pairs = {f"{pre}_MCP_URL": args.url}
    if args.token:
        pairs[f"{pre}_MCP_TOKEN"] = args.token
    if args.company_id:
        pairs[f"{pre}_MCP_COMPANY_ID"] = str(args.company_id)
    if args.merchant_id:
        pairs[f"{pre}_MCP_MERCHANT_ID"] = str(args.merchant_id)
    changed = upsert_env(pairs)
    if changed:
        print(f"[OK] .env 更新 {len(changed)} 项：{', '.join(changed)}")

    # 4. config 骨架
    os.makedirs(CONFIG_DIR, exist_ok=True)
    cfg_path = os.path.join(CONFIG_DIR, f"{args.name}.yaml")
    if os.path.exists(cfg_path):
        print(f"[SKIP] config 已存在，未覆盖 -> {os.path.relpath(cfg_path, _ROOT)}")
        print("       如需重建请先删除该文件，或手动改 .env 变量")
    else:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(render_config(args.name, tools_for_schema, args, wrapper))
        print(f"[OK] config 骨架 -> {os.path.relpath(cfg_path, _ROOT)}")

    print("\n下一步（人工）:")
    print(f"  1. 按需补 configs/{args.name}.yaml 的探针事实与可选块（参考 POS_商品管理.yaml）")
    print("  2. 写 ability/能力目录_<系统>.yaml + 实体清单_<系统>.yaml")
    print("  3. python scripts/_02_generate_dataset.py 生成数据集")
    print("  4. python scripts/_06_run_test.py --executor mock 验证可消费")
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) == 1:
        args = _interactive()
    else:
        ap = argparse.ArgumentParser(description="一条命令接入新 MCP")
        ap.add_argument("--name", required=True)
        ap.add_argument("--url", required=True)
        ap.add_argument("--token", default="")
        ap.add_argument("--company-id", default="")
        ap.add_argument("--merchant-id", default="")
        ap.add_argument("--company-header", default="CompanyId")
        ap.add_argument("--req-type", default="C")
        ap.add_argument("--dry-run", action="store_true")
        # 契约巡检（真调每个工具，检查返回体自洽性）
        ap.add_argument("--probe", dest="probe", action="store_true", default=True,
                        help="启用契约巡检（默认启用）")
        ap.add_argument("--no-probe", dest="probe", action="store_false",
                        help="跳过契约巡检（只拉 schema，行为与旧版一致）")
        ap.add_argument("--probe-merchants", default="",
                        help="巡检用店铺 ID，逗号分隔；给 2 个以上可做跨店对比。"
                             "留空则回退 --merchant-id")
        ap.add_argument("--probe-only", default="",
                        help="只巡检工具名包含该子串的工具（调试用）")
        args = ap.parse_args()
        # 未显式给 --probe-merchants 时，回退到 --merchant-id
        if not args.probe_merchants and args.merchant_id:
            args.probe_merchants = str(args.merchant_id)
        args.probe_merchants = [x.strip() for x in str(args.probe_merchants).split(",")
                                if x.strip()]
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
