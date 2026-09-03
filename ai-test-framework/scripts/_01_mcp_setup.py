# -*- coding: utf-8 -*-
"""一条命令接入新 MCP：拉 schema -> 平铺 -> 写 .env -> 生成 configs/<系统>.yaml 骨架。

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


def _flatten_schema(input_schema):
    """平铺 input_schema -> (params 列表, required 列表)。

    兼容两种形态:
      1. 常规: properties 直接是参数, required 在顶层
      2. AutoMart 型: properties 只有 toolParams 壳, 壳内才是真参数
    """
    if not isinstance(input_schema, dict):
        return [], []
    props = input_schema.get("properties") or {}
    tp = props.get("toolParams")
    if isinstance(tp, dict) and isinstance(tp.get("properties"), dict):
        inner = tp["properties"]
        return list(inner.keys()), list(tp.get("required") or [])
    return list(props.keys()), list(input_schema.get("required") or [])


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


def render_config(name, tools, args):
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
    print(f"[OK] list_tools 返回 {len(tools)} 个工具：")
    for t in tools:
        req = f"  required={t['required']}" if t["required"] else ""
        print(f"   - {t['name']}{req}")

    # 2. schema 落盘
    os.makedirs(SCHEMA_DIR, exist_ok=True)
    schema_path = os.path.join(SCHEMA_DIR, f"{args.name}.json")
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump({"name": args.name, "tools": tools}, f,
                  ensure_ascii=False, indent=1)
    print(f"[OK] schema 落盘 -> {os.path.relpath(schema_path, _ROOT)}")

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
            f.write(render_config(args.name, tools, args))
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
