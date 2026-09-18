# -*- coding: utf-8 -*-
"""POS 经营总报表(FlatTotalReport)规范汇总工具 — 长期可复用。

把用户在总报表中关心的全部字段，按固定顺序汇总输出一个时间段的合计(通常一个月)。
字段与 DataX 经营汇总返回一一对应；DataX 无对应字段的(钱箱现金盘点类)如实标为「不在DataX」。

用法：
    python scripts/total_report_standard.py \
        --merchant 9088143804924933 \
        --start 2026-07-01T00:00:00-07:00 --end 2026-08-01T00:00:00-07:00
可选: --env pos|automart(默认 pos)  --daily(逐日明细)  --raw(原始JSON)
店铺时区: 美西夏令时 -07:00 / 冬令时 -08:00。
"""
import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))

ENVS = {
    "pos": {"name": "POS(proton)", "url": os.environ.get("POS_MCP_URL"),
            "token": os.environ.get("POS_MCP_TOKEN"),
            "company_id": os.environ.get("POS_MCP_COMPANY_ID")},
    "automart": {"name": "AutoMart(somyeon)", "url": os.environ.get("AUTO_MART_MCP_URL"),
                 "token": os.environ.get("AUTO_MART_MCP_TOKEN"),
                 "company_id": os.environ.get("AUTO_MART_COMPANY_ID")},
}

# 已知公司(同 POS URL，companyId 不同)。company_id 可再被 --cid 覆盖
KNOWN_COMPANIES = {
    "9088125566714885": "测试公司",
    "12599656434959365": "cayson自动化公司",
}

# ============================================================
# 字段规范：分组 + 固定顺序。f=DataX字段名; 值为0.0或None表示该字段在DataX不存在
# ============================================================
TOTAL_REPORT_FIELDS = [
    # ---- 基本业绩 ----
    ("基础业绩", None, "HEADER"),
    ("订单数 orderCount", "orderCount", "count"),
    ("客流量 totalGuests", "totalGuests", "count"),
    # ---- 销售与收入 ----
    ("销售与收入", None, "HEADER"),
    ("商品销售总额 totalSales", "totalSales", "money"),
    ("税后营业额 netSales", "netSales", "money"),
    ("税前营业额 preTaxRevenue", "preTaxRevenue", "money"),
    ("实收(已收) actualRevenue", "actualRevenue", "money"),
    ("应收 dueAmount", "dueAmount", "money"),
    ("未收 unpaidAmount", "unpaidAmount", "money"),
    ("退款总额 refundAmount", "refundAmount", "money"),
    ("税 totalTaxAmount", "totalTaxAmount", "money"),
    # ---- 费项 ----
    ("费项", None, "HEADER"),
    ("小费总额 totalTipsAmount", "totalTipsAmount", "money"),
    ("总折扣 totalDiscountAmount", "totalDiscountAmount", "money"),
    ("总加收 totalSurchargeAmount", "totalSurchargeAmount", "money"),
    # ---- 取消 ----
    ("删单/删菜(取消) cancelled*", None, "HEADER"),
    ("删单单数 cancelledOrders", "cancelledOrders", "count"),
    ("删单金额 cancelledOrderAmount", "cancelledOrderAmount", "money"),
    ("删菜金额 cancelledItemTotal", "cancelledItemTotal", "money"),
    ("删菜数量 cancelledItemCount", "cancelledItemCount", "count"),
    # ---- 付款类型细分 ----
    ("付款类型细分", None, "HEADER"),
    ("付款-现金 totalPayCashAmount", "totalPayCashAmount", "money"),
    ("付款-信用卡 totalPayCreditCardAmount", "totalPayCreditCardAmount", "money"),
    ("付款-其他 totalPayOtherAmount", "totalPayOtherAmount", "money"),
    ("付款-会员余额 totalPayMemberBalanceAmount", "totalPayMemberBalanceAmount", "money"),
    ("付款-线上 totalPayOnlineOrderAmount", "totalPayOnlineOrderAmount", "money"),
    ("付款-礼品卡 giftCardPaymentTotalAmount", "giftCardPaymentTotalAmount", "money"),
    ("付款-按类型合计 totalByPaymentTypeAmount", "totalByPaymentTypeAmount", "money"),
    # ---- 订单类型细分 ----
    ("订单类型细分", None, "HEADER"),
    ("订单-堂食 totalDineInAmount", "totalDineInAmount", "money"),
    ("订单-外带 totalToGoAmount", "totalToGoAmount", "money"),
    ("订单-自提 totalPickupAmount", "totalPickupAmount", "money"),
    ("订单-外送 totalDeliveryAmount", "totalDeliveryAmount", "money"),
    ("订单-线上 totalOnlineOrderAmount", "totalOnlineOrderAmount", "money"),
    ("订单-按类型合计 totalByOrderTypeAmount", "totalByOrderTypeAmount", "money"),
    # ---- 充值/其它 ----
    ("充值及其它", None, "HEADER"),
    ("现金充值 totalCashTopUp", "totalCashTopUp", "money"),
    ("信用卡充值 totalCreditCardTopUp", "totalCreditCardTopUp", "money"),
    ("总充值 totalRechargeAmount", "totalRechargeAmount", "money"),
    ("新增会员 newMembers", "newMembers", "count"),
    ("用券数 usedCoupons", "usedCoupons", "count"),
    # ===== 钱箱现金盘点(用户关心, DataX经营汇总无对应字段) =====
    ("钱箱现金盘点(用户要求, 见说明)", None, "HEADER"),
    ("钱箱-现金收入", None, "N/A"),
    ("钱箱-现金入账", None, "N/A"),
    ("钱箱-现金支出", None, "N/A"),
    ("钱箱-钱箱现金", None, "N/A"),
    ("钱箱-Default Drawer", None, "N/A"),
    ("钱箱-钱箱现金(不含小费)", None, "N/A"),
]


def _val(r, field):
    return float(r.get(field) or 0) if r.get(field) is not None else 0.0


# ============================================================
# 费项钻取: 折扣 / 加收 的明细分类
# 来源: query_order_details_by_ids(订单商品明细) 返回的实际字段, 按实际字段分组展示
# ============================================================
DISCOUNT_BREAKDOWN = [
    ("单品折扣 itemDiscountAmount", "itemDiscountAmount"),
    ("优惠券折扣 couponDiscountAmount", "couponDiscountAmount"),
    ("充值优惠 rechargeDiscountAmount", "rechargeDiscountAmount"),
    ("其他折扣 discountAmount", "discountAmount"),
    ("订单级折扣(明细汇总) totalDiscountAmount", None),  # 用订单级 totalDiscountAmount
]
CHARGE_BREAKDOWN = [
    ("单品加收 itemChargeAmount", "itemChargeAmount"),
    ("其他加收 chargeAmount", "chargeAmount"),
    ("订单级加收(明细汇总) totalChargeAmount", None),
]


async def fetch_item_breakdown(env, merchant, start, end, limit=1000):
    """拉窗口内所有订单的商品明细, 按实际字段聚合折扣/加收分类。
    返回 (折扣dict, 加收dict, 明细订单数)。
    注: 订单查询 limit 上限 1000。"""
    # 1) 先取订单 id
    is_err, text = await call_tool(env, "query_datax_order_sales_reports",
                                   {"merchantId": merchant, "startAt": start, "endAt": end, "limit": 1000})
    if is_err:
        return None, None, 0
    dd = json.loads(text)
    data = dd.get("data") or {}
    recs = []
    for seg in (data.get("segments") or []):
        recs.extend(seg.get("records") or [])
    if not recs:
        recs = list(data.get("records") or [])
    ids = [r.get("orderId") for r in recs if r.get("orderId")]

    disc = {k: 0.0 for k, _ in DISCOUNT_BREAKDOWN if k}
    chg = {k: 0.0 for k, _ in CHARGE_BREAKDOWN if k}
    n = 0
    for oid in ids:
        try:
            is_err2, t2 = await call_tool(env, "query_order_details_by_ids",
                                          {"merchantId": merchant, "orderIds": [oid]})
            if is_err2:
                continue
            d2 = json.loads(t2)
            orders = ((d2.get("data") or {}).get("orders") or {}).get("list") or []
            for o in orders:
                n += 1
                disc["订单级折扣(明细汇总) totalDiscountAmount"] += float(o.get("totalDiscountAmount") or 0)
                chg["订单级加收(明细汇总) totalChargeAmount"] += float(o.get("totalChargeAmount") or 0)
                for it in o.get("items") or []:
                    disc["单品折扣 itemDiscountAmount"] += float(it.get("itemDiscountAmount") or 0)
                    disc["优惠券折扣 couponDiscountAmount"] += float(it.get("couponDiscountAmount") or 0)
                    disc["充值优惠 rechargeDiscountAmount"] += float(it.get("rechargeDiscountAmount") or 0)
                    disc["其他折扣 discountAmount"] += float(it.get("discountAmount") or 0)
                    chg["单品加收 itemChargeAmount"] += float(it.get("itemChargeAmount") or 0)
                    chg["其他加收 chargeAmount"] += float(it.get("chargeAmount") or 0)
        except Exception:
            continue
    return disc, chg, n


async def call_tool(env, tool, params):
    headers = {"Authorization": f"Bearer {env['token']}", "CompanyId": env["company_id"]}
    async with httpx.AsyncClient(headers=headers, timeout=120.0) as http:
        async with streamable_http_client(env["url"], http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool, {"toolParams": params})
                text = "".join(getattr(c, "text", "") for c in res.content)
                return res.is_error, text


async def run(args):
    env = dict(ENVS[args.env])
    if args.cid:  # --cid 覆盖 company_id (支持 cayson 等非默认公司)
        env["company_id"] = args.cid
    comp_name = KNOWN_COMPANIES.get(str(env["company_id"]), str(env["company_id"]))
    print(f"环境: {env['name']} | CompanyId={env['company_id']}({comp_name}) | FlatTotalReport 经营总报表")
    print(f"窗口: {args.start} ~ {args.end} | merchantId={args.merchant}")

    params = {"merchantId": args.merchant, "startAt": args.start, "endAt": args.end,
              "limit": args.limit}
    is_err, text = await call_tool(env, "query_datax_total_reports", params)
    if is_err:
        print("MCP调用错误:", text[:300])
        return
    d = json.loads(text)
    data = d.get("data")
    if data is None:
        print("业务返回错误:", text[:300])
        return
    # 经营汇总可能按多段(segments)返回(服务端新结构)，需合并各段 records
    if data.get("segments"):
        allr = []
        for seg in data["segments"]:
            allr.extend(seg.get("records") or [])
        print(f"服务端按 {len(data['segments'])} 段返回; 合并记录 {len(allr)} 条")
    else:
        allr = list(data.get("records") or [])
    # 按窗口月份过滤 date
    allr = [r for r in allr if args.start[:7] <= (r.get("date","")[:7]) <= args.end[:7]]
    print(f"返回记录: {len(allr)} 天\n")

    if args.raw:
        print(json.dumps(allr, ensure_ascii=False, indent=2))
        return

    # 汇总
    group_cur = None
    for label, field, kind in TOTAL_REPORT_FIELDS:
        if kind == "HEADER":
            # 分组标题
            print(f"\n[{label}]")
            group_cur = label
            continue
        if kind == "N/A" or field is None:
            # DataX 无此字段(钱箱盘点类)
            print(f"  {label:<32}: 不在DataX(经营汇总无此字段)")
            continue
        s = sum(_val(r, field) for r in allr)
        if kind == "money":
            print(f"  {label:<32}: ${s:,.2f}")
        else:
            print(f"  {label:<32}: {s:,.0f}")

    if args.breakdown:
        print("\n===== 费项钻取明细(来源: 订单商品明细 query_order_details_by_ids) =====")
        disc, chg, n = await fetch_item_breakdown(env, args.merchant, args.start, args.end)
        if disc is None:
            print("  (拉取失败)")
        else:
            total_disc = sum(_val(r, "totalDiscountAmount") for r in allr)
            total_chg = sum(_val(r, "totalSurchargeAmount") for r in allr)
            print(f"  (明细订单 {n} 笔)")
            print(f"  Total Discount 总折扣      : ${total_disc:>10,.2f}")
            for label, _ in DISCOUNT_BREAKDOWN:
                print(f"    ├ {label:<40}: ${disc.get(label, 0):>10,.2f}")
            print(f"  Total Charge 总加收        : ${total_chg:>10,.2f}")
            for label, _ in CHARGE_BREAKDOWN:
                print(f"    ├ {label:<40}: ${chg.get(label, 0):>10,.2f}")
            refund = sum(_val(r, "refundAmount") for r in allr)
            print(f"  Total Refund 总退款        : ${refund:>10,.2f}")
            print("  注: 折扣/加收分「订单级」与「单品级」，明细字段取名与 MCP 返回一致。")

    if args.daily:
        print("\n===== 逐日明细 =====")
        for r in sorted(allr, key=lambda x: x["date"]):
            if r.get("orderCount"):
                print(f"  {r['date'][:10]}: orderCount={r['orderCount']} totalSales={r.get('totalSales')} "
                      f"actualRev={r.get('actualRevenue')} due={r.get('dueAmount')}")


def main():
    ap = argparse.ArgumentParser(description="POS 经营总报表规范汇总")
    ap.add_argument("--merchant", required=True, help="店铺 merchantId")
    ap.add_argument("--start", required=True, help="开始 ISO8601(店铺时区)")
    ap.add_argument("--end", required=True, help="结束 ISO8601(店铺时区, 不含)")
    ap.add_argument("--env", default="pos", choices=["pos", "automart"])
    ap.add_argument("--cid", default=None, help="CompanyId(覆盖默认; 如 cayson自动化公司 12599656434959365)")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--daily", action="store_true", help="附加逐日明细")
    ap.add_argument("--breakdown", action="store_true",
                    help="附加费项钻取(折扣/加收按商品明细实际字段分类)")
    ap.add_argument("--raw", action="store_true", help="输出原始JSON")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
