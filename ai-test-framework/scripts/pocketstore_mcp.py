# -*- coding: utf-8 -*-
"""PocketStore MCP 通用查询入口（BossApp 侧店铺管理）。

所有针对 PocketStore 的临时探测/复测都走这里，不要再写一次性脚本。
（SKILL 铁律：不要为某个具体 MCP 写 _probe_<系统>.py 这类一次性脚本）

用法:
  python scripts/pocketstore_mcp.py tools                    # 列全部工具
  python scripts/pocketstore_mcp.py tools menu -v            # 过滤 + 显示参数
  python scripts/pocketstore_mcp.py schema menu_create       # 看某工具完整入参
  python scripts/pocketstore_mcp.py call menu_list --json '{"pageIndex":1}'
  python scripts/pocketstore_mcp.py call menu_list --store 3 # 用店铺别名
  python scripts/pocketstore_mcp.py call menu_delete --json '{"menuId":"..."}' --go
  python scripts/pocketstore_mcp.py stores                   # 列店铺 + 别名对照

参数外壳自动包 parameters；merchId 默认取 .env 的 POCKETSTORE_MCP_MERCHANT_ID，
可用 --store 别名或 --json 里的 merchId 覆盖。

⚠ 传中文值（店名/分类名/商品名/描述）必须用 --json-file，不能用 --json：
  PowerShell 按 ANSI 码页转码，中文会变乱码（店鋪3 -> 搴楅嫪3），
  而服务端不校验字符集、照样返回 code=0 → 静默写入乱码。
  --json 现在会硬拦非 ASCII 内容并提示改用 --json-file。
  --json-file 已支持带 BOM 的 UTF-8（自动剥除）。

confirm 二段式: 默认 confirm=false（预览，不写数据）；加 --go 才是 confirm=true。
遇到 429 write rate limit 自动退避重试（最多 4 次）。

环境变量（.env）:
  POCKETSTORE_MCP_URL / POCKETSTORE_MCP_TOKEN / POCKETSTORE_MCP_MERCHANT_ID
"""
import argparse
import asyncio
import json
import os
import random
import sys

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# cwd 必须落在项目根: .env / results/ / configs/ 都按相对路径访问
os.chdir(_ROOT)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("POCKETSTORE_MCP_URL")
TOKEN = os.environ.get("POCKETSTORE_MCP_TOKEN")
MID = os.environ.get("POCKETSTORE_MCP_MERCHANT_ID")

WRAP = "parameters"  # 本 MCP 的参数外壳

# 店铺别名：避免每次手敲 uuid。以 2026-10-29 list_my_merchants 实测为准。
STORES = {
    "2": "11fe6ee4-a1fb-4dee-8177-df05506e0f2a",       # cayson店鋪2（token 内置店铺）
    "3": "d87a750e-88a9-4f6e-8a33-2f81cf6d38ed",       # cayson店鋪3
    "4444": "202c2fe7-aa2f-424c-867f-26524a197dfa",    # cayson店鋪4444
    "5": "ec67aaac-e08a-4b37-801c-7114492d8f7a",       # cayson店鋪5
    "6": "a3ba706f-04ff-47d4-b0e5-990df6e3e7e0",       # cayson店鋪6
    "11": "ca1e21de-21ad-4f2e-9f14-540c63d2740c",      # cayson店铺11
    "18": "2adfcbce-ea4c-4126-bd18-79a61bbce18f",      # cayson店铺18
    "20": "64343762-91f8-4089-bc07-46a2b95b7980",      # cayson店铺20
    "chowchow": "6fc0b247-26c7-48c8-a827-749e64c94a2b",
    "rabbit": "ee08ff8a-61fa-4aff-b60d-4c15bd4a43fe",  # RabbitHouse
}

RATE_LIMIT_HINT = "429"
MAX_RETRY = 4


def resolve_store(val):
    """--store 支持别名(3 / chowchow)或直接给 uuid。"""
    if not val:
        return None
    return STORES.get(str(val).lower(), val)


def parse_json_lenient(raw, from_file=False):
    """解析 --json / --json-file 的内容。

    PowerShell 会把命令里的双引号吞掉，导致 '{"pageIndex":1}' 变成 {pageIndex:1}。
    这里做三级降级：
      1) 标准 JSON
      2) 单引号转双引号后重试
      3) 给裸 key 补引号（{a:1,b:"x"} -> {"a:1,"b":"x"}）
    另外支持 key=value 简写: --json pageIndex=1,pageSize=100

    ⚠ 中文陷阱（2026-11-01 实测）：
      PowerShell 5.1 在把参数传给 python.exe 时会按系统 ANSI 码页转码，
      导致 --json "newName=cayson店鋪3" 里的中文变成「cayson搴楅嫪3」。
      更危险的是：MCP 不校验字符集，照样返回 code=0 成功 → 静默写入乱码。
      故此处硬拦含非 ASCII 的 --json，强制走 --json-file（UTF-8，自动剥 BOM）。

    from_file=True 时（走 --json-file）**不做**该拦截：
      文件是按 UTF-8 正确写入的，中文本来就该允许——
      之前无差别拦截，导致「为了传中文才用 --json-file」反而被拒（自相矛盾的 bug）。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    if not from_file and not raw.isascii():
        raise ValueError(
            "--json 含非 ASCII 字符（中文/emoji 等），PowerShell 传参会静默转码成乱码，"
            "且服务端仍返回成功。\n"
            "请改用 --json-file：以 UTF-8 无 BOM 写入 JSON 文件后传路径。\n"
            f"收到内容: {raw[:200]}"
        )
    if "=" in raw and not raw.startswith("{"):
        out = {}
        for part in _split_top_level(raw, ","):
            if not part.strip():
                continue
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            # 数组写法：a=[x] / a=x|y （| 用于规避逗号被当分隔符）
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                out[k] = [_scalar(x) for x in _split_top_level(inner, ",")] if inner else []
            elif "|" in v:
                out[k] = [_scalar(x) for x in v.split("|")]
            else:
                out[k] = _scalar(v)
        return out
    for candidate in (raw, raw.replace("'", '"'), _quote_bare_keys(raw)):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise ValueError(f"无法解析为 JSON: {raw}")


def _quote_bare_keys(raw):
    """给裸 key 补双引号: {a:1, b:'x'} -> {"a":1, "b":'x'}"""
    import re as _re
    fixed = _re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', raw)
    return fixed.replace("'", '"')


def _scalar(v):
    """把字符串字面量转成 bool/int/float/None，其余保持字符串。"""
    if not isinstance(v, str):
        return v
    v = v.strip().strip('"').strip("'")
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if v.replace(".", "", 1).lstrip("-").isdigit():
        return float(v) if "." in v else int(v)
    return v


def _split_top_level(s, sep):
    """按 sep 切分，但忽略 [] {} () 内部与引号内部的 sep。"""
    out, buf, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


async def _call_once(sess, tool, params):
    res = await sess.call_tool(tool, {WRAP: params})
    txt = "".join(getattr(c, "text", "") for c in (res.content or []))
    return getattr(res, "is_error", getattr(res, "isError", None)), txt


def _is_rate_limited(text):
    """判断是否真被限流。

    ⚠ 不能只判「文本里含 429」：业务成功响应也可能带上这个数字
    （如商品名/描述含 429、分页信息含 429），会被误判成限流并反复重试，
    越重试越耗额度，最终把清理/取证流程卡死。实测 2026-11-01 踩过。
    规则：先认业务成功（code=0 / success=true），再要求限流特征同时出现。
    """
    s = str(text or "")
    if "429" not in s:
        return False
    low = "".join(s.lower().split())
    has_limit_word = ("ratelimit" in low or "toomanyrequests" in low
                      or "throttl" in low or "writeoperation" in low)
    if '"code":0' in low and not has_limit_word:
        return False
    return has_limit_word or '"code":1007' in low


async def call(tool, params, retry=True):
    """调用单个工具。返回 (is_error, text)。429 限流自动退避重试。"""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    last = (None, "")
    for attempt in range(MAX_RETRY if retry else 1):
        async with httpx.AsyncClient(headers=headers, timeout=180.0) as http:
            async with streamable_http_client(URL, http_client=http) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    last = await _call_once(s, tool, params)
        if not _is_rate_limited(last[1]):
            return last
        if attempt < MAX_RETRY - 1:
            wait = 15 * (attempt + 1) + random.uniform(0, 5)
            print(f"  [429 限流] 第 {attempt + 1} 次失败，{wait:.0f}s 后重试…",
                  file=sys.stderr)
            await asyncio.sleep(wait)
    return last


async def list_tools(keyword=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(headers=headers, timeout=120.0) as http:
        async with streamable_http_client(URL, http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tl = await s.list_tools()
                out = []
                for t in tl.tools:
                    if keyword and keyword.lower() not in t.name.lower():
                        continue
                    out.append(t)
                return out


def _inner_props(tool):
    props = (tool.input_schema or {}).get("properties", {}) or {}
    wrap = props.get(WRAP, {}) or {}
    return wrap.get("properties", {}) or {}, wrap.get("required", []) or []


def coerce_by_schema(tool, params):
    """按工具 schema 的 type 强制修正参数类型（方案 B，schema 驱动）。

    为什么需要（2026-11-01 实测）：
      --json "newPhone=7014145030" 里 _scalar 把纯数字串转成 int，
      但 schema 声明 newPhone 是 string → 服务端报
      "The JSON value could not be converted to System.String"。
      同类风险：邮编、订单号、只含数字的商品 ID（"007" 会丢前导零）。

    规则：
      schema type=string  且 值是 int/float  → 转回字符串（保前导零用原字面量尽力还原）
      schema type=integer 且 值是 str        → int()
      schema type=number  且 值是 str        → float()
      schema type=boolean 且 值是 str        → 已由 _scalar 处理，无需再做
      其它类型（array/object）不碰，避免破坏结构

    返回 (修正后的 params, 变更记录)。变更记录用于打日志，让用户知道改了什么。
    """
    props, _ = _inner_props(tool)
    if not props:
        return params, []

    out = dict(params)
    changes = []
    for k, v in list(out.items()):
        spec = props.get(k) or {}
        typ = spec.get("type")
        if isinstance(typ, list):  # 兼容 type: ["string","null"]
            typ = next((t for t in typ if t != "null"), None)

        if typ == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
            # bool 是 int 的子类，必须先排除
            new = str(v)
            # int 转 float 会多 .0，这里只在原本是 int 时用 int 分支，无需特殊处理
            out[k] = new
            changes.append(f"{k}: {v!r}({type(v).__name__}) -> {new!r}(str)")
        elif typ == "integer" and isinstance(v, str):
            try:
                out[k] = int(v)
                changes.append(f"{k}: {v!r} -> {out[k]!r}(int)")
            except ValueError:
                changes.append(f"{k}: cannot cast to int, kept {v!r}")
        elif typ == "number" and isinstance(v, str):
            try:
                out[k] = float(v)
                changes.append(f"{k}: {v!r} -> {out[k]!r}(float)")
            except ValueError:
                changes.append(f"{k}: cannot cast to float, kept {v!r}")
    return out, changes


async def fetch_tool_schema(tool_name):
    """按名字取单个工具的 schema 对象；取不到返回 None。"""
    try:
        tools = await list_tools(tool_name)
    except Exception as e:
        print(f"  [schema 拉取失败] {e}", file=sys.stderr)
        return None
    for t in tools:
        if t.name == tool_name:
            return t
    return None


def print_tools(tools, verbose=False):
    print(f"=== {len(tools)} tools ===")
    for t in tools:
        if not verbose:
            print(f" - {t.name}")
            continue
        inner, req = _inner_props(t)
        print(f"\n - {t.name}")
        print(f"   required: {req}")
        for k, v in inner.items():
            ty = v.get("type")
            desc = (v.get("description") or "").replace("\n", " ")[:88]
            print(f"     - {k}: {ty}  {desc}")


def print_result(tool, err, txt):
    """统一格式化：code/msg/data 分开展示，避免整段转义 JSON 刷屏。"""
    print("=" * 66)
    print(f"[{tool}]  is_error={err}")
    try:
        d = json.loads(txt)
    except Exception:
        print(txt[:2000])
        return None

    print(f"  code: {d.get('code')}")
    for line in str(d.get("msg") or "").splitlines():
        print(f"  msg : {line}")
    data = d.get("data")
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            total = data.get("total", data.get("totalCount"))
            extra = f"  total={total}" if total is not None else ""
            print(f"  items: {len(items)} 条{extra}")
            for i, it in enumerate(items):
                print(f"    [{i}] {json.dumps(it, ensure_ascii=False)[:400]}")
        elif items is not None:
            print(f"  items: {json.dumps(items, ensure_ascii=False)[:400]}")
        if "isPreview" in data:
            print(f"  isPreview: {data.get('isPreview')}  "
                  f"isExecuted: {data.get('isExecuted')}  isNoOp: {data.get('isNoOp')}")
        skip = ("items", "previewMessage", "isPreview", "isExecuted", "isNoOp", "message")
        rest = {k: v for k, v in data.items() if k not in skip}
        if rest:
            print(f"  data: {json.dumps(rest, ensure_ascii=False)[:600]}")
    elif data is not None:
        print(f"  data: {json.dumps(data, ensure_ascii=False)[:600]}")
    if d.get("errorDetails"):
        print(f"  errorDetails: {json.dumps(d['errorDetails'], ensure_ascii=False)[:500]}")
    return d


async def cmd_stores():
    err, txt = await call("list_my_merchants", {"confirm": True})
    print_result("list_my_merchants", err, txt)
    try:
        items = (json.loads(txt).get("data") or {}).get("items") or []
    except Exception:
        return
    rev = {v: k for k, v in STORES.items()}
    print("\n=== 别名对照 ===")
    for it in items:
        sid = it.get("id")
        print(f"  {rev.get(sid, '-'):<10} {sid}  {it.get('name')}")


async def main():
    ap = argparse.ArgumentParser(description="PocketStore MCP 通用查询入口")
    sub = ap.add_subparsers(dest="cmd")

    p_t = sub.add_parser("tools", help="列工具")
    p_t.add_argument("keyword", nargs="?", help="工具名关键字过滤")
    p_t.add_argument("-v", "--verbose", action="store_true", help="显示必填与参数")

    p_s = sub.add_parser("schema", help="看单个工具完整 schema")
    p_s.add_argument("tool")

    p_c = sub.add_parser("call", help="调用工具")
    p_c.add_argument("tool")
    p_c.add_argument("--json", default="{}", help="内层参数 JSON（不含外壳）")
    p_c.add_argument("--json-file", help="从文件读取内层参数 JSON")
    p_c.add_argument("--store", help="店铺别名或 uuid（覆盖 merchId）")
    p_c.add_argument("--go", action="store_true", help="confirm=true 真正执行（默认预览）")
    p_c.add_argument("--raw", action="store_true", help="原样打印返回 JSON")
    p_c.add_argument("--no-retry", action="store_true", help="不自动重试 429")
    p_c.add_argument("--no-coerce", action="store_true",
                     help="关闭 schema 类型修正（默认开启：按工具 schema 的 type 修正参数类型）")

    sub.add_parser("stores", help="列账号下店铺 + 别名")

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return 0

    if a.cmd == "tools":
        print_tools(await list_tools(a.keyword), verbose=a.verbose)
        return 0

    if a.cmd == "schema":
        hit = [t for t in await list_tools(a.tool) if t.name == a.tool]
        if not hit:
            print(f"工具不存在: {a.tool}")
            return 1
        t = hit[0]
        print(f"### {t.name}")
        print(f"{(t.description or '').strip()}\n")
        inner, req = _inner_props(t)
        print(f"required: {req}\n")
        print(json.dumps(inner, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "stores":
        await cmd_stores()
        return 0

    if a.cmd == "call":
        if a.json_file:
            # utf-8-sig 会自动剥除 BOM（Windows PowerShell 的 Set-Content -Encoding UTF8
            # 默认写 BOM，会让 json.loads 直接崩）
            raw = open(a.json_file, encoding="utf-8-sig").read()
        else:
            raw = a.json
        try:
            params = parse_json_lenient(raw, from_file=bool(a.json_file))
        except Exception as e:
            print(f"--json 解析失败: {e}")
            return 1

        store = resolve_store(a.store)
        if store and "merchId" not in params:
            params["merchId"] = store
        if "merchId" not in params and MID:
            params["merchId"] = MID
        # confirm 二段式：--go 才执行，否则强制预览
        if "confirm" not in params:
            params["confirm"] = bool(a.go)

        # schema 驱动的类型修正（--no-coerce 可关掉）
        if not a.no_coerce:
            schema_tool = await fetch_tool_schema(a.tool)
            if schema_tool is None:
                print(f"  [warn] schema not found for {a.tool}, skip coercion",
                      file=sys.stderr)
            else:
                params, changes = coerce_by_schema(schema_tool, params)
                for c in changes:
                    print(f"  [coerce] {c}", file=sys.stderr)

        print(f">>> {a.tool}  params={json.dumps(params, ensure_ascii=False)}")
        err, txt = await call(a.tool, params, retry=not a.no_retry)
        if a.raw:
            print("=" * 66)
            print(f"[{a.tool}]  is_error={err}")
            print(txt)
        else:
            print_result(a.tool, err, txt)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
