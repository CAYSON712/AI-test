# -*- coding: utf-8 -*-
"""POS MCP 通用查询工具（内置全部 17 个工具的知识，无需每次临时写脚本）

用法示例:
  # 1) 快捷命令
  python pos_mcp.py today                      # 今天经营汇总+订单
  python pos_mcp.py yesterday                  # 昨天
  python pos_mcp.py thisweek                   # 本周
  python pos_mcp.py lastweek                   # 上周
  python pos_mcp.py thismonth                  # 本月
  python pos_mcp.py day 2026-09-09             # 指定日汇总+订单
  python pos_mcp.py orders --start 2026-09-09 --end 2026-09-10
  python pos_mcp.py unpaid                     # 未结算订单(Progressing)
  python pos_mcp.py order 13555140533800965    # 单笔订单商品明细
  python pos_mcp.py staff                      # 员工列表+实时状态
  python pos_mcp.py attendance --start 2026-09-01 --end 2026-09-11
  python pos_mcp.py products --name Curry      # 搜商品
  python pos_mcp.py menus                      # 菜单
  python pos_mcp.py companies                  # 公司列表
  python pos_mcp.py merchants                  # 店铺列表
  python pos_mcp.py tools                      # 列出所有工具+schema

  # 2) 通用调用(任意工具)
  python pos_mcp.py call query_datax_total_reports --json '{"merchantId":"...","relativePeriod":"Today"}'

环境变量(.env): POS_MCP_URL / POS_MCP_TOKEN / POS_MCP_COMPANY_ID / POS_MERCHANT_ID
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"))

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx

URL = os.environ.get("POS_MCP_URL")
TOKEN = os.environ.get("POS_MCP_TOKEN")
CID = os.environ.get("POS_MCP_COMPANY_ID")
DEFAULT_M = os.environ.get("POS_MERCHANT_ID")
PDT = timezone(timedelta(hours=-7))   # 洛杉矶夏令时

# =====================================================================
# 全部 17 个工具的知识（参数/必填/枚举）—— 参考文档, 便于查询时对照
# =====================================================================
TOOL_INFO = {
    "query_companies": {"required": [], "params": ["companyName", "companyIds"]},
    "query_merchants": {"required": [], "params": ["merchantName"]},
    "query_datax_total_reports": {"required": ["merchantId"],
        "params": ["merchantName", "merchantId", "startAt", "endAt", "relativePeriod", "limit", "sortField", "sortDirection"]},
    "query_datax_order_sales_reports": {"required": ["merchantId"],
        "params": ["orderId", "orderType", "orderStatus", "paymentStatus", "paymentType", "sourceName",
                   "orderSourceType", "customizedPaymentName", "paymentTerminalType", "paymentTerminalName",
                   "operatorName", "orderSerial", "merchantId", "startAt", "endAt", "relativePeriod",
                   "limit", "sortField", "sortDirection"]},
    "query_datax_menu_product_category_reports": {"required": ["merchantId"],
        "params": ["refId", "name", "type", "merchantId", "startAt", "endAt", "relativePeriod",
                   "limit", "sortField", "sortDirection"]},
    "query_order_details_by_ids": {"required": ["merchantId", "orderIds"], "params": ["merchantId", "orderIds", "page", "size"]},
    "query_merchant_staffs": {"required": ["merchantIds"], "params": ["merchantIds"]},
    "query_staff_attendances": {"required": ["merchantId"],
        "params": ["merchantId", "merchantStaffIds", "startAt", "endAt", "relativePeriod", "page", "size"]},
    "query_products_by_ids": {"required": ["productIds"], "params": ["productIds"]},
    "query_product_detail_by_id": {"required": ["productId"],
        "params": ["productId", "includeCategory", "includeSpecificationGroup", "includeMenu"]},
    "search_products_by_name": {"required": ["productName"], "params": ["merchantIds", "productName", "status"]},
    "query_products_by_filter": {"required": [], "params": ["categoryIds", "type", "status"]},
    "query_categories": {"required": [], "params": ["merchantIds", "categoryName", "menuIds", "categoryIds"]},
    "query_menus": {"required": [], "params": ["merchantIds", "statuses", "saleChannel", "menuName"]},
    "batch_insert_products": {"required": ["merchantId"], "params": ["merchantId", "products"]},
    "update_products_by_ids": {"required": ["productIds"],
        "params": ["merchantIds", "productIds", "stockStatus", "status", "price", "nameLocalization"]},
    "delete_products_by_ids": {"required": ["productIds"], "params": ["productIds"]},
}

# 枚举
ENUM = {
    "relativePeriod": ["Today", "Yesterday", "ThisWeek", "LastWeek", "ThisMonth", "LastMonth"],
    "orderType": ["DineIn", "Pickup", "ToGo", "Delivery", "OnlineDineIn", "OnlinePickup",
                  "OnlineToGo", "OnlineDelivery", "OnlineTakeout", "GiftCard", "Kiosk"],
    "orderStatus": ["Progressing", "Completed", "Cancelled", "BeMerged", "Returned", "Pending"],
    "paymentStatus": ["Unpaid", "PartiallyPaid", "FullyPaid", "Uncaptured", "PartiallyCaptured", "Captured"],
    "orderSourceType": ["EasyPos", "PocketStore", "Deliverect", "AiPhoneOrder", "ScanCodeToOrder", "Kiosk"],
    "sortField": ["Date", "Amount", "Quantity"],
    "sortDirection": ["Ascending", "Descending"],
    "type(menu)": ["Menu", "Product", "Category"],
    "status(product)": ["Off", "Selling"],
    "stockStatus": ["InStock", "OutOfStock"],
}


# ---------------------------------------------------------------------
# 底层调用
# ---------------------------------------------------------------------
async def _call(tool, params, cid=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "CompanyId": cid or CID}
    async with httpx.AsyncClient(headers=headers, timeout=180.0) as http:
        async with streamable_http_client(URL, http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, {"toolParams": params})
                txt = "".join(getattr(c, "text", "") for c in res.content)
                err = getattr(res, "isError", getattr(res, "is_error", None))
                return err, txt


def _flatten(d):
    """合并顶层 records + 所有 segments 的 records。"""
    data = d.get("data") if isinstance(d, dict) else None
    if not isinstance(data, dict):
        return []
    out = list(data.get("records") or [])
    for seg in (data.get("segments") or []):
        out.extend(seg.get("records") or [])
    # 去重(orderId 优先)
    if out and "orderId" in out[0]:
        seen = {}
        for x in out:
            seen[x.get("orderId")] = x
        out = list(seen.values())
    return out


def _pst(s):
    if not s:
        return "-"
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PDT).strftime("%m/%d %H:%M")
    except Exception:
        return str(s)[:16]


def _f(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _print_raw(err, txt):
    print("isError:", err)
    try:
        d = json.loads(txt)
        print(json.dumps(d, ensure_ascii=False, indent=2)[:6000])
    except Exception:
        print(txt[:3000])


def _print_summary(recs, title="汇总"):
    if not recs:
        print(f"[{title}] 无数据")
        return
    agg = {}
    for r in recs:
        for k, v in r.items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v
    print(f"=== {title} ===")
    groups = {
        "基础": [("订单数", "orderCount", "i"), ("客流量", "totalGuests", "i"),
               ("新增会员", "newMembers", "i"), ("用券数", "usedCoupons", "i")],
        "销售额": [("商品销售总额", "totalSales"), ("税后营业额", "netSales"), ("税前营业额", "preTaxRevenue")],
        "收款": [("应收", "dueAmount"), ("实收", "actualRevenue"), ("未收", "unpaidAmount")],
        "费项": [("折扣", "totalDiscountAmount"), ("加收", "totalSurchargeAmount"),
               ("退款", "refundAmount"), ("税", "totalTaxAmount"), ("小费", "totalTipsAmount")],
        "取消": [("删单数", "cancelledOrders", "i"), ("删单金额", "cancelledOrderAmount")],
        "付款": [("现金", "totalPayCashAmount"), ("信用卡", "totalPayCreditCardAmount"),
               ("其他", "totalPayOtherAmount"), ("会员余额", "totalPayMemberBalanceAmount"),
               ("线上", "totalPayOnlineOrderAmount"), ("礼品卡", "giftCardPaymentTotalAmount")],
        "类型": [("堂食", "totalDineInAmount"), ("打包", "totalToGoAmount"),
               ("自提", "totalPickupAmount"), ("外卖", "totalDeliveryAmount"), ("线上", "totalOnlineOrderAmount")],
        "充值": [("现金充值", "totalCashTopUp"), ("信用卡充值", "totalCreditCardTopUp"), ("总充值", "totalRechargeAmount")],
    }
    for g, items in groups.items():
        lines = []
        for it in items:
            label, key = it[0], it[1]
            typ = it[2] if len(it) > 2 else "m"
            v = agg.get(key)
            if v is None:
                continue
            lines.append(f"{label}={int(v) if typ=='i' else '$'+format(v,',.2f')}")
        if lines:
            print(f"  [{g}] " + "  ".join(lines))


def _print_orders(recs, title="订单", full=False):
    """打印订单。full=False 精简行(含折扣/加收); full=True 每笔块状全字段。"""
    print(f"\n=== {title} ({len(recs)} 笔) ===")
    if full:
        for i, r in enumerate(recs, 1):
            print("-" * 72)
            print(f"[{i}] 单号={r.get('orderSerial')} orderId={r.get('orderId')}")
            for k in sorted(r.keys()):
                print(f"    {k} = {r.get(k)}")
        return
    # 精简: 一行一笔, 含折扣/加收
    for i, r in enumerate(recs, 1):
        print(f"{i:>3}|{_pst(r.get('orderCreateAt'))}|{str(r.get('orderSerial','')):>17}|"
              f"{str(r.get('orderType','')):<13}|{str(r.get('orderSourceType','')):<13}|"
              f"{str(r.get('orderStatus','')):<11}|{str(r.get('paymentType') or '-')[:18]:<18}|"
              f"净={_f(r.get('netSales')):>8.2f}|额={_f(r.get('totalAmount')):>8.2f}|"
              f"折={_f(r.get('totalDiscountAmount')):>7.2f}|加={_f(r.get('totalSurchargeAmount')):>6.2f}|"
              f"税={_f(r.get('totalTaxAmount')):>6.2f}|费={_f(r.get('totalTipsAmount')):>6.2f}|"
              f"退={_f(r.get('refundAmount')):>7.2f}|{r.get('orderId')}")


def _merge_params(p):
    p = dict(p)
    if "merchantId" in p and p["merchantId"] is None:
        p["merchantId"] = DEFAULT_M
    return p


# ---------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------
async def cmd_period(period, merchant):
    """relativePeriod 汇总 + 订单。"""
    err, txt = await _call("query_datax_total_reports", _merge_params({"merchantId": merchant, "relativePeriod": period}))
    d = json.loads(txt)
    if d.get("code") != 0:
        print("汇总: code=", d.get("code"), d.get("msg")); return
    data = d.get("data") or {}
    print(f"period={period} timeZone={data.get('timeZoneId')} req={data.get('requestedPeriod')}")
    print("warnings:", data.get("warnings"))
    _print_summary(_flatten(d), f"{period} 经营汇总")

    err2, txt2 = await _call("query_datax_order_sales_reports", _merge_params({"merchantId": merchant, "relativePeriod": period}))
    d2 = json.loads(txt2)
    if d2.get("code") == 0:
        _print_orders(sorted(_flatten(d2), key=lambda x: str(x.get("orderCreateAt") or "")), f"{period} 订单")


async def cmd_day(date, merchant):
    """指定日: 汇总(含当天) + 订单(endAt 不含, 用次日)。"""
    # 汇总
    err, txt = await _call("query_datax_total_reports", _merge_params({"merchantId": merchant, "startAt": date, "endAt": date}))
    d = json.loads(txt)
    if d.get("code") == 0:
        _print_summary(_flatten(d), f"{date} 经营汇总")
    else:
        print("汇总:", d.get("code"), d.get("msg"))
    # 订单(endAt 不含 -> 次日)
    nd = (datetime.date.fromisoformat(date) + timedelta(days=1)).isoformat()
    err2, txt2 = await _call("query_datax_order_sales_reports", _merge_params({"merchantId": merchant, "startAt": date, "endAt": nd}))
    d2 = json.loads(txt2)
    if d2.get("code") == 0:
        _print_orders(sorted(_flatten(d2), key=lambda x: str(x.get("orderCreateAt") or "")), f"{date} 订单")
    else:
        print("订单:", d2.get("code"), d2.get("msg"))


async def cmd_orders(start, end, period, merchant, extra, full=False):
    p = {"merchantId": merchant}
    if period:
        p["relativePeriod"] = period
    else:
        p["startAt"] = start; p["endAt"] = end
    p.update(extra or {})
    err, txt = await _call("query_datax_order_sales_reports", _merge_params(p))
    d = json.loads(txt)
    if d.get("code") != 0:
        print("code=", d.get("code"), d.get("msg")); return
    _print_orders(sorted(_flatten(d), key=lambda x: str(x.get("orderCreateAt") or "")), "订单", full=full)


async def cmd_month(ym, merchant, full=False):
    """按月列订单: ym 形如 2026-09。"""
    y, mo = int(ym[:4]), int(ym[5:7])
    start = f"{ym}-01"
    if mo == 12:
        end = f"{y+1}-01-01"
    else:
        end = f"{y}-{mo+1:02d}-01"
    err, txt = await _call("query_datax_order_sales_reports",
                           _merge_params({"merchantId": merchant, "startAt": start, "endAt": end, "limit": 1000}))
    d = json.loads(txt)
    if d.get("code") != 0:
        print("code=", d.get("code"), d.get("msg")); return
    recs = sorted(_flatten(d), key=lambda x: str(x.get("orderCreateAt") or ""))
    _print_orders(recs, f"{ym} 订单明细", full=full)
    # 合计
    agg = {}
    for r in recs:
        for k, v in r.items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v
    print(f"\n合计: 净额={agg.get('netSales',0):.2f} 总额={agg.get('totalAmount',0):.2f} "
          f"折扣={agg.get('totalDiscountAmount',0):.2f} 加收={agg.get('totalSurchargeAmount',0):.2f} "
          f"税={agg.get('totalTaxAmount',0):.2f} 小费={agg.get('totalTipsAmount',0):.2f} "
          f"退款={agg.get('refundAmount',0):.2f}")
    # 写文件
    cols = ["时间PDT", "单号", "类型", "来源", "状态", "支付", "净额", "总额",
            "折扣", "加收", "税", "小费", "退款", "orderId"]
    lines = ["|".join(cols)]
    for r in recs:
        lines.append("|".join([
            _pst(r.get("orderCreateAt")), str(r.get("orderSerial")), str(r.get("orderType")),
            str(r.get("orderSourceType")), str(r.get("orderStatus")), str(r.get("paymentType") or "-"),
            f"{_f(r.get('netSales')):.2f}", f"{_f(r.get('totalAmount')):.2f}",
            f"{_f(r.get('totalDiscountAmount')):.2f}", f"{_f(r.get('totalSurchargeAmount')):.2f}",
            f"{_f(r.get('totalTaxAmount')):.2f}", f"{_f(r.get('totalTipsAmount')):.2f}",
            f"{_f(r.get('refundAmount')):.2f}", str(r.get("orderId")),
        ]))
    os.makedirs(os.path.join(_ROOT, "report"), exist_ok=True)
    fp = os.path.join(_ROOT, "report", f"订单明细_{ym}.txt")
    open(fp, "w", encoding="utf-8").write("\n".join(lines))
    print(f"已写: report/订单明细_{ym}.txt")


async def cmd_unpaid(merchant, days=7):
    """未结算订单(orderStatus=Progressing)。"""
    start = (datetime.now(PDT) - timedelta(days=days)).strftime("%Y-%m-%d")
    end = (datetime.now(PDT) + timedelta(days=1)).strftime("%Y-%m-%d")
    err, txt = await _call("query_datax_order_sales_reports", _merge_params(
        {"merchantId": merchant, "startAt": start, "endAt": end, "limit": 1000}))
    d = json.loads(txt)
    if d.get("code") != 0:
        print("code=", d.get("code"), d.get("msg")); return
    recs = sorted(_flatten(d), key=lambda x: str(x.get("orderCreateAt") or ""))
    prog = [r for r in recs if str(r.get("orderStatus")) == "Progressing"]
    print(f"=== 未结算订单(orderStatus=Progressing, 近{days}天) {len(prog)} 笔 ===")
    for i, r in enumerate(prog, 1):
        print(f"{i:>3}|{_pst(r.get('orderCreateAt'))}|{str(r.get('orderSerial','')):>17}|"
              f"{str(r.get('orderType','')):<10}|pay={str(r.get('paymentType') or '-')[:18]:<18}|"
              f"payStatus={str(r.get('paymentStatus')):<10}|净={_f(r.get('netSales')):>8.2f}|"
              f"额={_f(r.get('totalAmount')):>8.2f}|未收={_f(r.get('unpaidAmount')):>7.2f}")
    if not prog:
        print("  (无)")


async def cmd_order(oid, merchant):
    """单笔订单商品明细。"""
    err, txt = await _call("query_order_details_by_ids", _merge_params({"merchantId": merchant, "orderIds": [oid]}))
    d = json.loads(txt)
    if d.get("code") != 0:
        print("code=", d.get("code"), d.get("msg")); return
    orders = ((d.get("data") or {}).get("orders") or {}).get("list") or []
    for o in orders:
        print(f"\n单号={o.get('orderSerial')} orderId={o.get('orderId')} "
              f"{o.get('orderType')} {o.get('orderStatus')} pay={o.get('paymentStatus')}")
        print(f"  商品小计(subTotalAmount)={o.get('subTotalAmount')} 税={o.get('totalTaxAmount')} "
              f"折扣={o.get('totalDiscountAmount')} 加收={o.get('totalChargeAmount')} "
              f"小费={o.get('totalTipsAmount')} 总额={o.get('totalAmount')}")
        for it in o.get("items") or []:
            print(f"    - {it.get('name')} x{it.get('quantity')} 单价={it.get('price')} "
                  f"itemAmount={it.get('itemAmount')} finalAmount={it.get('finalAmount')} 税={it.get('itemTax')}")


async def cmd_staff(merchant):
    err, txt = await _call("query_merchant_staffs", {"merchantIds": [merchant]})
    d = json.loads(txt)
    if d.get("code") != 0:
        print("code=", d.get("code"), d.get("msg")); return
    lst = d.get("data") or []
    print(f"=== 员工列表({len(lst)} 名) ===")
    for st in lst:
        print(f"  {st.get('firstName')} {st.get('lastName')} | pin={st.get('pin')} "
              f"staffCode={st.get('staffCode')} owner={st.get('isOwner')} "
              f"needCheckIn={st.get('isNeedCheckIn')} status={st.get('attendanceStatus')} "
              f"updateAt={_pst(st.get('updateAt'))}")


async def cmd_attendance(start, end, period, merchant, staff_ids):
    p = {"merchantId": merchant, "page": 1, "size": 100}
    if period:
        p["relativePeriod"] = period
    else:
        p["startAt"] = start; p["endAt"] = end
    if staff_ids:
        p["merchantStaffIds"] = staff_ids
    err, txt = await _call("query_staff_attendances", p)
    d = json.loads(txt)
    if d.get("code") != 0:
        print("code=", d.get("code"), d.get("msg")); return
    data = d.get("data") or {}
    lst = data.get("list") or []
    print(f"=== 考勤记录({len(lst)} 条) ===")
    for r in lst:
        print(f"  {r.get('firstName')} {r.get('lastName'):<4} "
              f"{_pst(r.get('checkIn'))} ~ {_pst(r.get('checkOut'))} "
              f"时长={_f(r.get('duration')):.2f} 工作={_f(r.get('workTotalTime')):.2f} "
              f"休息={_f(r.get('breakTotalTime')):.2f} 加班={_f(r.get('overWorkTotalTime')):.2f}")


async def cmd_products(name, merchant):
    if name:
        err, txt = await _call("search_products_by_name", {"productName": name, "merchantIds": [merchant]})
    else:
        err, txt = await _call("query_products_by_filter", {})
    _print_raw(err, txt)


async def cmd_simple(tool, params):
    err, txt = await _call(tool, params)
    _print_raw(err, txt)


async def cmd_tools():
    headers = {"Authorization": f"Bearer {TOKEN}", "CompanyId": CID}
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as http:
        async with streamable_http_client(URL, http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                print(f"=== 共 {len(tools.tools)} 个工具 ===")
                for t in tools.tools:
                    info = TOOL_INFO.get(t.name, {})
                    print(f"\n- {t.name}  必填={info.get('required')}")
                    print(f"  参数: {info.get('params')}")


# ---------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="POS MCP 通用查询工具")
    ap.add_argument("--merchant", default=DEFAULT_M, help="店铺ID(默认取.env)")
    sub = ap.add_subparsers(dest="cmd")

    for p in ["today", "yesterday", "thisweek", "lastweek", "thismonth", "lastmonth"]:
        sp = sub.add_parser(p)
    sp = sub.add_parser("day"); sp.add_argument("date")
    sp = sub.add_parser("orders")
    sp.add_argument("--start"); sp.add_argument("--end"); sp.add_argument("--period")
    sp.add_argument("--orderStatus"); sp.add_argument("--orderType")
    sp.add_argument("--orderSourceType"); sp.add_argument("--paymentType")
    sp.add_argument("--operatorName"); sp.add_argument("--limit", type=int)
    sp.add_argument("--full", action="store_true", help="每笔块状输出全部32字段")
    sp = sub.add_parser("month"); sp.add_argument("ym", help="如 2026-09")
    sp.add_argument("--full", action="store_true")
    sp = sub.add_parser("unpaid"); sp.add_argument("--days", type=int, default=7)
    sp = sub.add_parser("order"); sp.add_argument("orderId")
    sub.add_parser("staff")
    sp = sub.add_parser("attendance")
    sp.add_argument("--start"); sp.add_argument("--end"); sp.add_argument("--period")
    sp.add_argument("--staffIds", nargs="*")
    sp = sub.add_parser("products"); sp.add_argument("--name")
    sub.add_parser("menus"); sub.add_parser("companies"); sub.add_parser("merchants")
    sub.add_parser("tools")
    sp = sub.add_parser("call"); sp.add_argument("tool"); sp.add_argument("--json", default="{}")

    a = ap.parse_args()
    m = a.merchant
    pmap = {"today": "Today", "yesterday": "Yesterday", "thisweek": "ThisWeek",
            "lastweek": "LastWeek", "thismonth": "ThisMonth", "lastmonth": "LastMonth"}

    if a.cmd in pmap:
        asyncio.run(cmd_period(pmap[a.cmd], m))
    elif a.cmd == "day":
        asyncio.run(cmd_day(a.date, m))
    elif a.cmd == "orders":
        extra = {}
        for k in ["orderStatus", "orderType", "orderSourceType", "paymentType", "operatorName", "limit"]:
            v = getattr(a, k, None)
            if v is not None:
                extra[k] = v
        asyncio.run(cmd_orders(a.start, a.end, a.period, m, extra, full=getattr(a, "full", False)))
    elif a.cmd == "month":
        asyncio.run(cmd_month(a.ym, m, full=a.full))
    elif a.cmd == "unpaid":
        asyncio.run(cmd_unpaid(m, a.days))
    elif a.cmd == "order":
        asyncio.run(cmd_order(a.orderId, m))
    elif a.cmd == "staff":
        asyncio.run(cmd_staff(m))
    elif a.cmd == "attendance":
        asyncio.run(cmd_attendance(a.start, a.end, a.period, m, a.staffIds))
    elif a.cmd == "products":
        asyncio.run(cmd_products(a.name, m))
    elif a.cmd == "menus":
        asyncio.run(cmd_simple("query_menus", {"merchantIds": [m]}))
    elif a.cmd == "companies":
        asyncio.run(cmd_simple("query_companies", {}))
    elif a.cmd == "merchants":
        asyncio.run(cmd_simple("query_merchants", {}))
    elif a.cmd == "tools":
        asyncio.run(cmd_tools())
    elif a.cmd == "call":
        asyncio.run(cmd_simple(a.tool, json.loads(a.json)))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
