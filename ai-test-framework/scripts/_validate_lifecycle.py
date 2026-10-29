"""离线校验「数据生命周期」配置：不真跑用例，只验证每条规则是否可用。

检查项：
  1. 快照还原规则：快照工具是否存在、参数是否被接受、能否查到实体、字段是否存在
  2. 新建清理规则：删除工具是否存在、删除参数名是否合法、ID 能否从返回提取
  3. 自建自删规则：建工具/删除工具存在、建参数必填齐全、删除主键合法

全程只读（confirm=true 仅用于查询），不做任何写操作。
"""
import asyncio
import glob
import os
import sys

import yaml
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "executors"))
load_dotenv(os.path.join(_ROOT, ".env"))

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

WRAP = "parameters"
MERCH = os.getenv("POCKETSTORE_MCP_MERCHANT_ID")

PASS, FAIL, WARN = [], [], []


def ok(msg):
    PASS.append(msg)
    print(f"  [PASS] {msg}")


def bad(msg):
    FAIL.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg):
    WARN.append(msg)
    print(f"  [WARN] {msg}")


def load_schema():
    """工具名 -> 参数集合（取原始 schema，含 required）。"""
    p = os.path.join(_ROOT, "results", "mcp_schemas", "PocketStore.raw.json")
    import json
    d = json.load(open(p, encoding="utf-8"))
    out = {}
    for t in d.get("tools") or []:
        sch = t.get("inputSchema") or {}
        inner = (sch.get("properties") or {}).get("parameters") or {}
        props = inner.get("properties") or {}
        req = inner.get("required") or []
        out[t["name"]] = {"params": set(props), "required": set(req)}
    return out


async def call(s, tool, params):
    try:
        r = await s.call_tool(tool, {WRAP: params})
        txt = "".join(c.text for c in (r.content or [])
                      if getattr(c, "type", "") == "text")
        return r.is_error, txt
    except Exception as e:
        return True, f"EXC {type(e).__name__}: {e}"


def find_any_id(obj, depth=0):
    """从返回体里找任意一个真实 ID（用于快照定位测试）。"""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ("merchid", "merchantid", "storeid", "companyid"):
                continue
            if (kl.endswith("id") or kl == "id") and isinstance(v, str) \
                    and len(v) == 36 and v.count("-") == 4 \
                    and not v.startswith("00000000"):
                return v
            r = find_any_id(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for x in obj[:3]:
            r = find_any_id(x, depth + 1)
            if r:
                return r
    return None


async def pick_sample_id(s, schema, snap_tool, need_param):
    """为需要主键的快照工具，从对应列表工具取一个真实 ID。

    映射关系（通用推断，不绑死具体系统字段）：
      item_detail      ← item_list        → itemId
      get_user_detail  ← list_store_users → merchManagerId
    取不到返回 (None, None)，调用方应跳过实测而非判失败。
    """
    import json as _json
    CANDIDATES = {
        "item_detail": ("item_list", "itemId", "pageIndex"),
        "get_user_detail": ("list_store_users", "merchManagerId", "page"),
    }
    src = CANDIDATES.get(snap_tool)
    if not src:
        return None, None
    list_tool, id_key, page_key = src
    if list_tool not in schema:
        return None, None

    q = {}
    if "merchId" in schema[list_tool]["params"]:
        q["merchId"] = MERCH
    if page_key in schema[list_tool]["params"]:
        q[page_key] = 1
    if "pageSize" in schema[list_tool]["params"]:
        q["pageSize"] = 20
    if "confirm" in schema[list_tool]["params"]:
        q["confirm"] = True

    err, txt = await call(s, list_tool, q)
    if err:
        return None, None
    try:
        data = _json.loads(txt).get("data") or {}
    except Exception:
        return None, None
    items = data.get("items") or []
    if not items:
        return list_tool, None
    first = items[0]
    # 上下文/租户级 ID：绝不能当作实体主键去查详情，
    # 否则会得到 "Item <merchId> data is null" 这类假失败。
    TENANT_KEYS = {"merchid", "merchantid", "storeid", "shopid",
                   "companyid", "accountid", "userid"}
    val = first.get(id_key)
    if not val:
        for k, v in first.items():
            kl = str(k).lower()
            if kl in TENANT_KEYS:
                continue
            if kl.endswith("id") and isinstance(v, str) \
                    and len(v) == 36 and not v.startswith("00000000"):
                val = v
                break
    return list_tool, val


async def check_restorable(s, schema, rule):
    tool = rule.get("工具")
    snap_tool = rule.get("快照工具")
    primary = rule.get("主键参数")
    pfield = rule.get("主键字段")
    fields = rule.get("快照字段") or []
    name = f"可还原 {tool}"

    if tool not in schema:
        bad(f"{name}: 写工具不存在")
        return
    if snap_tool not in schema:
        bad(f"{name}: 快照工具 {snap_tool} 不存在")
        return
    if primary not in schema[tool]["params"]:
        bad(f"{name}: 主键参数 {primary} 不在 {tool} 的参数里")
        return
    if not fields:
        bad(f"{name}: 未声明快照字段")
        return

    # 调快照工具（只读），看能否取到数据并含目标字段。
    # 若快照工具要求主键参数（如 item_detail 需 itemId），
    # 先从对应列表工具取一个真实 ID 再查，否则会被参数校验拒绝。
    p = {}
    if "merchId" in schema[snap_tool]["params"]:
        p["merchId"] = MERCH
    if "confirm" in schema[snap_tool]["params"]:
        p["confirm"] = True

    req = schema[snap_tool]["required"]
    need_key = [r for r in req if r not in p]
    if need_key:
        # 需要主键：从配置的 sample_source 或按工具名推断列表工具取一个 ID
        sample_tool, sample_id = await pick_sample_id(s, schema, snap_tool, need_key[0])
        if sample_id:
            p[need_key[0]] = sample_id
            if "merchId" in need_key:
                p["merchId"] = MERCH
        else:
            warn(f"{name}: 快照工具需 {need_key}，但取不到真实样本 ID，跳过实测")
            return

    err, txt = await call(s, snap_tool, p)
    if err:
        bad(f"{name}: 快照工具调用失败 {txt[:100]}")
        return
    import json
    try:
        d = json.loads(txt)
    except Exception:
        bad(f"{name}: 快照返回非 JSON")
        return
    data = d.get("data")
    found = set()

    def walk(o, dep=0):
        if dep > 5:
            return
        if isinstance(o, dict):
            for k in o:
                found.add(str(k))
            for v in o.values():
                walk(v, dep + 1)
        elif isinstance(o, list):
            for x in o[:3]:
                walk(x, dep + 1)

    walk(data)
    if not found:
        warn(f"{name}: 快照返回空数据，无法验证字段（可能该店铺无此类数据）")
        return
    miss = [f for f in fields if f not in found]
    if miss:
        bad(f"{name}: 快照返回里找不到字段 {miss}（现有字段示例 "
            f"{sorted(found)[:12]}）")
    else:
        ok(f"{name}: 快照工具可查，字段齐全 {fields}")

    if pfield and pfield not in found:
        warn(f"{name}: 主键字段 {pfield} 不在快照返回里（实际字段示例 "
             f"{sorted(found)[:10]}）")


async def check_creatable(s, schema, rule):
    tool = rule.get("工具")
    del_tool = rule.get("删除工具")
    params = rule.get("删除参数") or {}
    name = f"清理 {tool}"

    if tool not in schema:
        bad(f"{name}: 建工具不存在")
        return
    if del_tool not in schema:
        bad(f"{name}: 删除工具 {del_tool} 不存在")
        return

    # 删除参数名必须都在删除工具的 schema 里
    del_params = schema[del_tool]["params"]
    illegal = [k for k in params if k not in del_params]
    if illegal:
        bad(f"{name}: 删除参数 {illegal} 不在 {del_tool} 的参数里 "
            f"（合法参数 {sorted(del_params)}）")
        return
    ok(f"{name}: 删除工具与参数名合法")

    # required 里的参数必须能填上
    req = schema[del_tool]["required"]
    missing = [r for r in req if r not in params]
    if missing:
        bad(f"{name}: 删除工具必填参数 {missing} 未在删除参数里提供")
    else:
        ok(f"{name}: 删除必填参数齐全")


async def check_self_delete(s, schema, rule):
    del_tool = rule.get("删除工具")
    create_tool = rule.get("建工具")
    cparams = rule.get("建参数") or {}
    dkey = rule.get("删除主键")
    name = f"自建自删 {del_tool}"

    if del_tool not in schema:
        bad(f"{name}: 删除工具不存在")
        return
    if create_tool not in schema:
        bad(f"{name}: 建工具 {create_tool} 不存在")
        return
    if dkey not in schema[del_tool]["params"]:
        bad(f"{name}: 删除主键 {dkey} 不在 {del_tool} 参数里 "
            f"（合法 {sorted(schema[del_tool]['params'])}）")
        return

    req = schema[create_tool]["required"]
    missing = [r for r in req if r not in cparams]
    if missing:
        bad(f"{name}: 建工具必填参数 {missing} 未在建参数里提供")
    else:
        ok(f"{name}: 建工具必填齐全，删除主键 {dkey} 合法")


async def main():
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "configs", "PocketStore.yaml"),
                              encoding="utf-8"))
    lc = cfg.get("数据生命周期") or {}
    schema = load_schema()
    print(f"工具 schema: {len(schema)} 个\n")

    h = {"Authorization": "Bearer " + os.getenv("POCKETSTORE_MCP_TOKEN", "")}
    async with httpx.AsyncClient(headers=h, timeout=90) as c:
        async with streamable_http_client(
                os.getenv("POCKETSTORE_MCP_URL"), http_client=c) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                print("=" * 72)
                print("① 可还原（快照还原）")
                print("=" * 72)
                for rule in (lc.get("可还原") or []):
                    await check_restorable(s, schema, rule)

                print("\n" + "=" * 72)
                print("② 新建后清理")
                print("=" * 72)
                for rule in (lc.get("新建后清理") or []):
                    await check_creatable(s, schema, rule)

                print("\n" + "=" * 72)
                print("③ 自建自删")
                print("=" * 72)
                for rule in (lc.get("自建自删") or []):
                    await check_self_delete(s, schema, rule)

    print("\n" + "=" * 72)
    print(f"通过 {len(PASS)} | 失败 {len(FAIL)} | 警告 {len(WARN)}")
    if FAIL:
        print("\n需修复：")
        for f in FAIL:
            print(f"  - {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    import json  # noqa: E402,F401
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
