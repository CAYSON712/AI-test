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
    return argparse.Namespace(name=name, url=url, token=token, company_id=company_id,
                              merchant_id=merchant_id, company_header=company_header,
                              req_type=req_type, dry_run=False)


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
        json.dump({"name": args.name, "wrapper": wrapper, "tools": tools}, f,
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
        f.write(render_overview(args.name, tools, wrapper, raw_tools))
    print(f"[OK] 概览落盘 -> {os.path.relpath(overview_path, _ROOT)}")

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
            f.write(render_config(args.name, tools, args, wrapper))
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
        args = ap.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
