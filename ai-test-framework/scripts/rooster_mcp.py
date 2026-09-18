# -*- coding: utf-8 -*-
"""Rooster POS MCP quick query (company-account endpoint).

Usage:
  python rooster_mcp.py tools
  python rooster_mcp.py month 2026-08          # orders + total report for a month
  python rooster_mcp.py orders --start 2026-08-01 --end 2026-09-01
  python rooster_mcp.py call query_merchants --json '{}'

Env (.env): ROOSTER_MCP_URL / ROOSTER_MCP_TOKEN / ROOSTER_MCP_COMPANY_ID / ROOSTER_MERCHANT_ID
Report time zone: America/Los_Angeles (note: total reports use a 05:00 business-day cut).
"""
import argparse
import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# cwd 必须落在项目根: .env / results/ / scripts_params/ 都按相对路径访问,
# 从其他目录调用本脚本时否则会找不到配置. (CJK 路径下更明显)
os.chdir(_ROOT)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("ROOSTER_MCP_URL")
TOKEN = os.environ.get("ROOSTER_MCP_TOKEN")
CID = os.environ.get("ROOSTER_MCP_COMPANY_ID")
MID = os.environ.get("ROOSTER_MERCHANT_ID")

L = "orderCreateAt"


async def call(tool, params, cid=None):
    """Return (is_error, payload). payload is a list of tool names for '__list__'."""
    headers = {"Authorization": f"Bearer {TOKEN}", "CompanyId": cid or CID}
    async with httpx.AsyncClient(headers=headers, timeout=180.0) as http:
        async with streamable_http_client(URL, http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                if tool == "__list__":
                    t = await s.list_tools()
                    return False, [getattr(x, "name", "?") for x in t.tools]
                res = await s.call_tool(tool, {"toolParams": params})
                txt = "".join(getattr(c, "text", "") for c in res.content)
                return getattr(res, "isError", getattr(res, "is_error", None)), txt


# 菜单/分类报表(query_datax_menu_product_category_reports)的字段口径:
#   refId    -> 被统计对象 ID(分类/商品/菜单)
#   name     -> 对象名称
#   type     -> 层级: Category | Product | Menu
#   amount   -> 该对象金额
#   quantity -> 该对象数量
# 注意: 三个层级会互相重叠(Category 含 Product, Menu 含 Category),
#       横向相加必然重复计数, 只允许在单层内比较。
MENU_REPORT_TOOL = "query_datax_menu_product_category_reports"
MENU_LEVELS = ("Category", "Product", "Menu")
_MENU_NOTE = ("\u6ce8: Category/Product/Menu \u4e09\u5c42\u4f1a\u4e92\u76f8\u91cd\u53e0"
              "(\u540c\u4e00\u7b14\u8ba2\u5355\u5728\u4e09\u5c42\u5404\u8ba1\u4e00\u6b21), "
              "\u7981\u6b62\u8de8\u5c42\u76f8\u52a0\u3002Menu \u5c42\u624d\u662f\u5168\u91cf"
              "\u53e3\u5f84(\u7b49\u4e8e totalSales)\u3002")


def flat(d):
    """Merge top-level records + every segment's records."""
    data = d.get("data") if isinstance(d, dict) else None
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    out = list(data.get("records") or [])
    for seg in (data.get("segments") or []):
        out.extend(seg.get("records") or [])
    if not out:
        return out
    # 菜单报表没有 orderId/orderCreateAt, 用 (层级,refId,日期) 去重, 不能按订单字段
    if "refId" in out[0] and "type" in out[0]:
        seen = {}
        for x in out:
            seen[(x.get("type"), x.get("refId"), x.get("date"))] = x
        return list(seen.values())
    if L in out[0]:
        seen = {}
        for x in out:
            seen[x.get("orderId") or x.get(L)] = x
        out = list(seen.values())
    return out


def brief(d):
    if not isinstance(d, dict):
        return d
    data = d.get("data")
    info = {"code": d.get("code"), "msg": d.get("msg")}
    if isinstance(data, dict):
        info["dataKeys"] = sorted(data.keys())[:18]
        for k in ("timeZoneId", "requestedPeriod", "startAt", "endAt", "total", "warnings", "limit"):
            if k in data:
                info[k] = data[k]
    elif isinstance(data, list):
        info["dataLen"] = len(data)
    return info


def show_orders(recs, title):
    print(f"\n=== {title}: {len(recs)} " + "\u7b14" + " ===")
    recs = sorted(recs, key=lambda x: str(x.get(L) or ""))
    total = 0.0
    for i, r in enumerate(recs, 1):
        amt = r.get("totalAmount") or r.get("netSales") or 0
        try:
            total += float(amt)
        except Exception:
            pass
        if i <= 40:
            print(f"{i:>3}|{str(r.get(L))[:16]:<16}|{str(r.get('orderSerial')):>18}|"
                  f"{str(r.get('orderType')):<12}|{str(r.get('orderStatus')):<11}|"
                  f"{str(r.get('orderSourceType')):<14}|"
                  f"net={r.get('netSales')}|amt={r.get('totalAmount')}|id={r.get('orderId')}")
    if len(recs) > 40:
        print(f"  ... {len(recs) - 40} more")
    if recs:
        print(f"  amount sum = {total:.2f}")


def show_report(d, title):
    print(f"\n=== {title} ===")
    print(json.dumps(brief(d), ensure_ascii=False, indent=2)[:2500])
    if isinstance(d, dict) and d.get("code") not in (0, None):
        return
    recs = flat(d)
    if recs:
        print(f"records: {len(recs)}")
        for r in recs[:6]:
            print("  ", json.dumps(r, ensure_ascii=False)[:600])


def show_menu_report(recs, title):
    """Render query_datax_menu_product_category_reports.

    Fields: name / type(Category|Product|Menu) / amount / quantity.
    Layers overlap, so each layer is summed and printed separately.
    """
    from collections import defaultdict
    print(f"\n=== {title}: {len(recs)} " + "\u6761 ===")
    if not recs:
        return
    grouped = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for r in recs:
        lv = str(r.get("type") or "?")
        nm = str(r.get("name") or "?")
        try:
            q = int(r.get("quantity") or 0)
        except Exception:
            q = 0
        grouped[lv][nm][0] += q
        grouped[lv][nm][1] += _num(r.get("amount"))
    for lv in list(MENU_LEVELS) + [k for k in grouped if k not in MENU_LEVELS]:
        items = grouped.get(lv)
        if not items:
            continue
        rows = sorted(items.items(), key=lambda x: -x[1][1])
        sub_q = sum(c[0] for c in items.values())
        sub_a = sum(c[1] for c in items.values())
        print(f"\n[{lv}]  {len(rows)} " + "\u9879  "
              f"quantity={sub_q}  amount=${sub_a:,.2f}")
        for nm, c in rows:
            print(f"  {nm:<52} qty={c[0]:>5}  ${c[1]:>10,.2f}")
    print(f"\n  {_MENU_NOTE}")


def _num(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def agg_stats(recs, title="orders"):
    """Aggregate orders by day / status / type / source / payment."""
    from collections import defaultdict
    buckets = {k: defaultdict(lambda: [0, 0.0]) for k in ("day", "status", "type", "source", "payment")}
    for r in recs:
        amt = _num(r.get("totalAmount") if r.get("totalAmount") is not None else r.get("netSales"))
        keys = {
            "day": str(r.get("orderCreateAtLocal") or "")[:10],
            "status": str(r.get("orderStatus")),
            "type": str(r.get("orderType")),
            "source": str(r.get("orderSourceType")),
            "payment": str(r.get("paymentType") or r.get("sourceName") or "-"),
        }
        for k, v in keys.items():
            buckets[k][v][0] += 1
            buckets[k][v][1] += amt
    print(f"\n--- {title} breakdown ---")
    for k in ("status", "type", "source", "payment"):
        items = sorted(buckets[k].items(), key=lambda x: -x[1][0])
        print(f"[{k}] " + "  ".join(f"{n}:{c[0]}" + f"(${c[1]:.0f})" for n, c in items))
    days = sorted(buckets["day"].items())
    print(f"[day] {len(days)} " + "\u5929" + " with orders")
    for n, c in days:
        print(f"   {n}  n={c[0]:>3}  amt={c[1]:>10.2f}")


# ============================================================
# 字段规范与统计口径: 直接复用 total_report_standard.py 的同一份定义。
# 字段集合 / 顺序 / 合计方式必须与 lemon 侧(POS 标准报表)完全一致;
# 需要增删字段时只改 total_report_standard.py, 不要在这里另写一套。
# ============================================================
try:
    from total_report_standard import (TOTAL_REPORT_FIELDS, DISCOUNT_BREAKDOWN,
                                       CHARGE_BREAKDOWN)
except Exception:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from total_report_standard import (TOTAL_REPORT_FIELDS, DISCOUNT_BREAKDOWN,
                                       CHARGE_BREAKDOWN)


# 费项钻取字段映射: 完全由 total_report_standard 的分类表驱动, 不在这里另写字段名
_D_LABEL = {f: l for l, f in DISCOUNT_BREAKDOWN if f}
_C_LABEL = {f: l for l, f in CHARGE_BREAKDOWN if f}
_D_ORDER = next(l for l, f in DISCOUNT_BREAKDOWN if not f)
_C_ORDER = next(l for l, f in CHARGE_BREAKDOWN if not f)


async def fetch_item_breakdown(start, end, limit=1000):
    """费项钻取: 拉窗口内订单的商品明细, 按 DISCOUNT_BREAKDOWN / CHARGE_BREAKDOWN
    聚合折扣与加收分类, 分类口径与 total_report_standard.py 一致。
    返回 (折扣dict, 加收dict, 明细订单数)。"""
    err, txt = await call("query_datax_order_sales_reports",
                          {"merchantId": MID, "startAt": start, "endAt": end, "limit": limit})
    if err:
        return None, None, 0
    try:
        d = json.loads(txt)
    except Exception:
        return None, None, 0
    ids = [r.get("orderId") for r in flat(d) if r.get("orderId")]
    disc = {l: 0.0 for l in _D_LABEL.values()}
    disc[_D_ORDER] = 0.0
    chg = {l: 0.0 for l in _C_LABEL.values()}
    chg[_C_ORDER] = 0.0
    n = 0
    if not ids:
        return disc, chg, n
    # 同一 MCP 会话内逐单调用, 避免每单重建连接
    headers = {"Authorization": f"Bearer {TOKEN}", "CompanyId": CID}
    async with httpx.AsyncClient(headers=headers, timeout=180.0) as http:
        async with streamable_http_client(URL, http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for oid in ids:
                    try:
                        res = await s.call_tool("query_order_details_by_ids",
                                                {"toolParams": {"merchantId": MID, "orderIds": [oid]}})
                        t2 = "".join(getattr(c, "text", "") for c in res.content)
                        if getattr(res, "isError", getattr(res, "is_error", None)):
                            continue
                        orders = ((json.loads(t2).get("data") or {}).get("orders") or {}).get("list") or []
                    except Exception:
                        continue
                    for o in orders:
                        n += 1
                        disc[_D_ORDER] += _num(o.get("totalDiscountAmount"))
                        chg[_C_ORDER] += _num(o.get("totalChargeAmount"))
                        for it in o.get("items") or []:
                            for f, l in _D_LABEL.items():
                                disc[l] += _num(it.get(f))
                            for f, l in _C_LABEL.items():
                                chg[l] += _num(it.get(f))
    return disc, chg, n


async def print_total_summary(d, daily=False, breakdown=False, start=None, end=None):
    """Print business totals from query_datax_total_reports.

    字段顺序与排版对齐 total_report_standard.py(lemon 侧标准脚本)。
    """
    recs = flat(d)
    agg = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v
    print(f"\n--- total report ({len(recs)} record rows) ---")
    absent = []
    for label, field, kind in TOTAL_REPORT_FIELDS:
        if kind == "HEADER":
            print(f"\n[{label}]")
            continue
        if kind == "N/A" or field is None:
            print(f"  {label:<32}: \u4e0d\u5728DataX(\u7ecf\u8425\u6c47\u603b\u65e0\u6b64\u5b57\u6bb5)")
            continue
        v = float(agg.get(field) or 0)
        if field not in agg:
            absent.append(field)
        val = f"{int(v):,}" if kind == "count" else f"${v:,.2f}"
        print(f"  {label:<32}: {val}")
    if absent:
        print(f"\n  (\u672a\u51fa\u73b0\u5728\u54cd\u5e94\u4e2d\u7684\u5b57\u6bb5: {', '.join(absent)})")
    if daily:
        print(f"\n===== \u9010\u65e5\u660e\u7ec6 =====")
        for r in sorted(recs, key=lambda x: str(x.get("date") or "")):
            if r.get("orderCount"):
                print(f"  {str(r.get('date'))[:10]}: orderCount={r.get('orderCount')} "
                      f"totalSales={r.get('totalSales')} actualRev={r.get('actualRevenue')} "
                      f"due={r.get('dueAmount')}")
    if breakdown:
        print(f"\n===== \u8d39\u9879\u94bb\u53d6\u660e\u7ec6"
              f"(\u6765\u6e90: query_order_details_by_ids) =====")
        disc, chg, n = await fetch_item_breakdown(start, end)
        if disc is None:
            print("  (\u62c9\u53d6\u5931\u8d25)")
        else:
            total_disc = float(agg.get("totalDiscountAmount") or 0)
            total_chg = float(agg.get("totalSurchargeAmount") or 0)
            refund = float(agg.get("refundAmount") or 0)
            print(f"  (\u660e\u7ec6\u8ba2\u5355 {n} \u7b14)")
            print(f"  Total Discount \u603b\u6298\u6263      : ${total_disc:>10,.2f}")
            for label in list(_D_LABEL.values()) + [_D_ORDER]:
                print(f"    \u251c {label:<40}: ${disc.get(label, 0):>10,.2f}")
            print(f"  Total Charge \u603b\u52a0\u6536        : ${total_chg:>10,.2f}")
            for label in list(_C_LABEL.values()) + [_C_ORDER]:
                print(f"    \u251c {label:<40}: ${chg.get(label, 0):>10,.2f}")
            print(f"  Total Refund \u603b\u9000\u6b3e        : ${refund:>10,.2f}")
            print("  \u6ce8: \u6298\u6263/\u52a0\u6536\u5206\u300c\u8ba2\u5355\u7ea7\u300d"
                  "\u4e0e\u300c\u5355\u54c1\u7ea7\u300d\uff0c\u660e\u7ec6\u5b57\u6bb5\u53d6\u540d"
                  "\u4e0e MCP \u8fd4\u56de\u4e00\u81f4\u3002")
    for k in ("startAt", "endAt", "timeZoneId", "warnings", "source"):
        if isinstance(d.get("data"), dict) and k in d["data"]:
            print(f"  {k}: {d['data'][k]}")


async def cmd_tools():
    err, tools = await call("__list__", {})
    print(f"URL: {URL}\nCompanyId: {CID}\nMerchantId: {MID}")
    print(f"\n=== {len(tools)} tools ===")
    for t in tools:
        print(" -", t)


async def cmd_orders(start, end, tool, extra, style="orders", limit=20,
                     daily=False, breakdown=False):
    p = {"merchantId": MID}
    if start:
        p["startAt"] = start
    if end:
        p["endAt"] = end
    p.update(extra or {})
    err, txt = await call(tool, p)
    print(f"[{tool}] params={json.dumps(p, ensure_ascii=False)} isError={err}")
    try:
        d = json.loads(txt)
    except Exception:
        print(txt[:3000])
        return
    show_report(d, tool)
    recs = flat(d)
    if style == "orders" and tool == "query_datax_total_reports":
        style = "total"
    if style == "orders" and tool == MENU_REPORT_TOOL:
        style = "menu"
    if style == "total":
        await print_total_summary(d, daily=daily, breakdown=breakdown, start=start, end=end)
    elif style == "menu":
        show_menu_report(recs, tool)
    elif recs:
        show_orders(recs, tool)
        agg_stats(recs, tool)


async def cmd_month(ym, only, daily=False, breakdown=False, limit=20):
    y, mo = int(ym[:4]), int(ym[5:7])
    start = f"{ym}-01"
    end = f"{y + 1}-01-01" if mo == 12 else f"{y}-{mo + 1:02d}-01"
    print(f"range: {start} ~ {end} (exclusive)  merchant={MID}")
    if only in (None, "orders"):
        await cmd_orders(start, end, "query_datax_order_sales_reports", {"limit": 1000})
    if only in (None, "total"):
        await cmd_orders(start, end, "query_datax_total_reports", {}, style="total",
                         daily=daily, breakdown=breakdown)


async def cmd_call(tool, params):
    err, txt = await call(tool, params)
    print(f"[{tool}] isError={err}")
    try:
        print(json.dumps(json.loads(txt), ensure_ascii=False, indent=2)[:8000])
    except Exception:
        print(txt[:4000])


def load_params(json_str, file_path):
    """Parse tool params from --json literal or --file.

    PowerShell mangles inline JSON quotes (it strips the inner double quotes),
    so --file is the reliable path there. When both are given, --file wins.
    A shell-mangled literal is detected and reported instead of a bare traceback.
    """
    if file_path:
        # utf-8-sig: PowerShell 的 Set-Content -Encoding UTF8 会写入 BOM
        try:
            with open(file_path, encoding="utf-8-sig") as f:
                return json.load(f)
        except FileNotFoundError:
            raise SystemExit(f"--file \u4e0d\u5b58\u5728: {file_path}")
        except json.JSONDecodeError as e:
            raise SystemExit(f"--file \u89e3\u6790\u5931\u8d25: {file_path}\n  {e}")
    if not json_str:
        return {}
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"--json \u89e3\u6790\u5931\u8d25: {e}\n"
            f"  \u539f\u6587: {json_str!r}\n"
            f"  \u63d0\u793a: PowerShell \u4f1a\u541e\u6389\u5185\u5c42\u53cc\u5f15\u53f7, "
            f"\u8bf7\u6539\u7528 --file params.json (\u6216\u5728 bash \u4e0b\u7528 --json)")


def main():
    ap = argparse.ArgumentParser(description="Rooster POS MCP query")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tools")
    sp = sub.add_parser("month")
    sp.add_argument("ym")
    sp.add_argument("--only", choices=["orders", "total"])
    sp.add_argument("--daily", action="store_true", help="\u9644\u52a0\u9010\u65e5\u660e\u7ec6")
    sp.add_argument("--breakdown", action="store_true", help="\u9644\u52a0\u8d39\u9879\u94bb\u53d6")
    sp = sub.add_parser("orders")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.add_argument("--tool", default="query_datax_order_sales_reports")
    sp.add_argument("--json", default="{}")
    sp.add_argument("--file", default="", help="params \u4ece JSON \u6587\u4ef6\u8bfb\u53d6")
    sp.add_argument("--merchants", action="store_true", help="list merchants first")
    sp.add_argument("--daily", action="store_true", help="\u9644\u52a0\u9010\u65e5\u660e\u7ec6")
    sp.add_argument("--breakdown", action="store_true", help="\u9644\u52a0\u8d39\u9879\u94bb\u53d6")
    sp = sub.add_parser("call")
    sp.add_argument("tool")
    sp.add_argument("--json", default="{}")
    sp.add_argument("--file", default="", help="params \u4ece JSON \u6587\u4ef6\u8bfb\u53d6")
    sp = sub.add_parser("order")
    sp.add_argument("order_id")
    a = ap.parse_args()

    if a.cmd == "tools":
        asyncio.run(cmd_tools())
    elif a.cmd == "month":
        asyncio.run(cmd_month(a.ym, a.only, a.daily, a.breakdown))
    elif a.cmd == "orders":
        asyncio.run(cmd_orders(a.start, a.end, a.tool,
                               load_params(a.json, a.file),
                               daily=a.daily, breakdown=a.breakdown))
    elif a.cmd == "call":
        asyncio.run(cmd_call(a.tool, load_params(a.json, a.file)))
    elif a.cmd == "order":
        asyncio.run(cmd_call("query_order_details_by_ids",
                             {"merchantId": MID, "orderIds": [a.order_id]}))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
