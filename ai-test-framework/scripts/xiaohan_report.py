# -*- coding: utf-8 -*-
"""小韩面(AutoMart / Test Store)经营数据一站式查询工具

时间口径：店铺时区 = Asia/Shanghai = 中国时区，无需换算。
          时间参数一律不带时区偏移；用 --rel 走服务端相对时间最稳。

子命令（可任选，不带则默认 summary）：
    summary     经营汇总(43字段) + 恒等式校验
    orders      订单明细列表
    recon       明细 vs 汇总 对账 + 支付/来源分布
    discount    折扣下钻(订单级/商品行级拆分)
    products    商品销量/销售额排行
    categories  分类销售排行
    members     会员订单统计(用 hasMember 筛选，快)
    payments    支付方式占比
    count       订单计数(resultMode=Count，免大结果集)

通用参数：
    --rel {today,yesterday,thisweek,lastweek,thismonth,lastmonth}
    --date YYYY-MM-DD           单日
    --start / --end             区间(汇总含结束日)
    --merchant ID               默认取 .env
    --daily                     逐日附加
    --member true|false         count: 只算会员/非会员
    --by-status / --by-source   count: 按状态/来源细分
    --legacy                    members: 走旧的逐单下钻
    --raw                       输出原始 JSON
    --json                      以 JSON 输出(便于管道)

示例：
    python xiaohan_report.py summary --rel today
    python xiaohan_report.py members --rel yesterday
    python xiaohan_report.py count --rel thismonth --by-status
    python xiaohan_report.py products --rel thismonth
    python xiaohan_report.py recon orders --rel yesterday
    python xiaohan_report.py categories --date 2026-09-14
"""
import argparse
import asyncio
import collections
import json
import os
import sys

from dotenv import load_dotenv
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))

TZ_NAME = "Asia/Shanghai"
MERCHANT_DEFAULT = os.environ.get("AUTO_MART_MERCHANT_ID", "26377732552517")
MCP_URL = os.environ.get("AUTO_MART_MCP_URL")
MCP_TOKEN = os.environ.get("AUTO_MART_MCP_TOKEN")
COMPANY_ID = os.environ.get("AUTO_MART_COMPANY_ID")

CMDS = ["summary", "orders", "recon", "discount", "products",
        "categories", "members", "payments", "count"]

# ============================================================
# 字段规范：分组 + 顺序，对应 DataX 经营汇总返回
# ============================================================
XIAOHAN_FIELDS = [
    ("基础业绩", None, "HEADER"),
    ("订单数 orderCount", "orderCount", "count"),
    ("客流量 totalGuests", "totalGuests", "count"),
    ("取消单数 cancelledOrders", "cancelledOrders", "count"),
    ("取消单金额 cancelledOrderAmount", "cancelledOrderAmount", "money"),

    ("销售与收入", None, "HEADER"),
    ("商品销售总额 totalSales", "totalSales", "money"),
    ("税前营收 preTaxRevenue", "preTaxRevenue", "money"),
    ("净销售额 netSales", "netSales", "money"),
    ("实收 actualRevenue", "actualRevenue", "money"),
    ("应收 dueAmount", "dueAmount", "money"),
    ("未付 unpaidAmount", "unpaidAmount", "money"),
    ("退款总额 refundAmount", "refundAmount", "money"),

    ("费项(小韩面仅折扣非零)", None, "HEADER"),
    ("总折扣 totalDiscountAmount", "totalDiscountAmount", "money"),
    ("税 totalTaxAmount", "totalTaxAmount", "money"),
    ("加收 totalSurchargeAmount", "totalSurchargeAmount", "money"),
    ("小费 totalTipsAmount", "totalTipsAmount", "money"),

    ("支付方式细分", None, "HEADER"),
    ("付款-现金 totalPayCashAmount", "totalPayCashAmount", "money"),
    ("付款-其他 totalPayOtherAmount", "totalPayOtherAmount", "money"),
    ("付款-信用卡 totalPayCreditCardAmount", "totalPayCreditCardAmount", "money"),
    ("付款-会员余额 totalPayMemberBalanceAmount",
     "totalPayMemberBalanceAmount", "money"),
    ("付款-线上 totalPayOnlineOrderAmount", "totalPayOnlineOrderAmount", "money"),
    ("付款-礼品卡 giftCardPaymentTotalAmount",
     "giftCardPaymentTotalAmount", "money"),
    ("付款-按类型合计 totalByPaymentTypeAmount",
     "totalByPaymentTypeAmount", "money"),

    ("订单类型细分", None, "HEADER"),
    ("订单-堂食 totalDineInAmount", "totalDineInAmount", "money"),
    ("订单-外带 totalToGoAmount", "totalToGoAmount", "money"),
    ("订单-自提 totalPickupAmount", "totalPickupAmount", "money"),
    ("订单-外送 totalDeliveryAmount", "totalDeliveryAmount", "money"),
    ("订单-线上 totalOnlineOrderAmount", "totalOnlineOrderAmount", "money"),
    ("订单-按类型合计 totalByOrderTypeAmount", "totalByOrderTypeAmount", "money"),

    ("会员/充值(小韩面恒为0)", None, "HEADER"),
    ("现金充值 totalCashTopUp", "totalCashTopUp", "money"),
    ("信用卡充值 totalCreditCardTopUp", "totalCreditCardTopUp", "money"),
    ("总充值 totalRechargeAmount", "totalRechargeAmount", "money"),
    ("新增会员 newMembers", "newMembers", "count"),
    ("用券数 usedCoupons", "usedCoupons", "count"),

    ("钱箱现金盘点", None, "HEADER"),
    ("钱箱-现金收入", None, "N/A"),
    ("钱箱-现金入账", None, "N/A"),
    ("钱箱-现金支出", None, "N/A"),
    ("钱箱-钱箱现金", None, "N/A"),
]

# 小韩面本地化勾稽恒等式
IDENTITIES = [
    ("净销恒等式", "preTaxRevenue == netSales",
     lambda t: (r(t, "preTaxRevenue"), r(t, "netSales")), ""),
    ("营收扣减恒等式", "preTaxRevenue == totalSales + discount + refund",
     lambda t: (round(r(t, "totalSales") + r(t, "totalDiscountAmount")
                     + r(t, "refundAmount"), 2), r(t, "preTaxRevenue")),
     "★ 小韩面专属；POS 那套少 refund 会误报"),
    ("支付合计恒等式", "totalByPaymentTypeAmount == actualRevenue",
     lambda t: (r(t, "totalByPaymentTypeAmount"), r(t, "actualRevenue")), ""),
    ("类型合计恒等式", "totalByOrderTypeAmount == Σ各类型",
     lambda t: (r(t, "totalByOrderTypeAmount"),
                round(sum(r(t, k) for k in
                          ("totalDineInAmount", "totalToGoAmount",
                           "totalPickupAmount", "totalDeliveryAmount",
                           "totalOnlineOrderAmount")), 2)), ""),
    ("实收恒等式(SUSPECT)", "dueAmount - unpaidAmount == actualRevenue",
     lambda t: (round(r(t, "dueAmount") - r(t, "unpaidAmount"), 2),
                r(t, "actualRevenue")),
     "⚠ 实测不成立(与挂账 Account 相关)，勿直接断言"),
    ("销项扣减恒等式", "totalSales + totalDiscountAmount == preTaxRevenue",
     lambda t: (round(r(t, "totalSales") + r(t, "totalDiscountAmount"), 2),
                r(t, "preTaxRevenue")),
     "⚠ POS 那套写法；小韩面差一个退款额"),
]


def r(rec, k):
    try:
        return round(float(rec.get(k) or 0), 2)
    except Exception:
        return 0.0


def num(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def money(v):
    try:
        return f"{float(v or 0):,.2f}"
    except Exception:
        return "0.00"


def flat(data):
    """兼容 segments / records 两种结构"""
    recs = list(data.get("records") or [])
    for seg in (data.get("segments") or []):
        recs.extend(seg.get("records") or [])
    return recs


def next_day(d):
    from datetime import datetime, timedelta
    try:
        return (datetime.strptime(d[:10], "%Y-%m-%d")
                + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return d


# ============================================================
# MCP 访问层
# ============================================================
class MCP:
    def __init__(self, merchant, retries=4):
        self.merchant = merchant
        self.retries = retries
        self._http: httpx.AsyncClient | None = None
        self._ctx = None
        self._ses: ClientSession | None = None

    async def __aenter__(self):
        hdr = {"Authorization": f"Bearer {MCP_TOKEN}",
               "CompanyId": str(COMPANY_ID)}
        self._http = httpx.AsyncClient(headers=hdr, timeout=300.0,
                                       trust_env=False)
        self._ctx = streamable_http_client(str(MCP_URL),
                                           http_client=self._http)
        read, write = await self._ctx.__aenter__()
        self._ses = ClientSession(read, write)
        await self._ses.__aenter__()
        await self._ses.initialize()
        return self

    async def __aexit__(self, *a):
        if self._ses is not None:
            await self._ses.__aexit__(*a)
        if self._ctx is not None:
            await self._ctx.__aexit__(*a)
        if self._http is not None:
            await self._http.aclose()

    async def call(self, tool, params=None):
        p = {"merchantId": self.merchant}
        p.update(params or {})
        ses = self._ses
        if ses is None:
            return {"code": "GIVEUP", "msg": "会话未初始化"}
        for _ in range(self.retries):
            try:
                res = await ses.call_tool(tool, {"toolParams": p})
                txt = "\n".join(getattr(c, "text", "") for c in res.content)
                try:
                    d = json.loads(txt)
                except Exception:
                    d = {"_raw": txt}
                if d.get("code") == 40001:      # 限流
                    await asyncio.sleep(20)
                    continue
                return d
            except Exception:
                await asyncio.sleep(8)
        return {"code": "GIVEUP", "msg": "重试耗尽"}

    # ---- 语义化封装 ----
    async def total(self, tp, op):
        return flat((await self.call("query_datax_total_reports", tp)).get("data") or [])

    async def orders(self, op):
        return flat((await self.call("query_datax_order_sales_reports",
                                     op)).get("data") or [])

    async def details(self, order_ids, batch=50):
        out = []
        for i in range(0, len(order_ids), batch):
            d = await self.call("query_order_details_by_ids",
                                {"orderIds": order_ids[i:i + batch],
                                 "size": batch})
            out.extend(((d.get("data") or {}).get("orders") or {}).get("list") or [])
            await asyncio.sleep(1.5)
        return out

    async def menu_report(self, op):
        return flat((await self.call(
            "query_datax_menu_product_category_reports", op)).get("data") or [])

    async def products(self):
        d = await self.call("query_products_by_filter", {})
        data = d.get("data") or {}
        out = []
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    out.extend(v)
        elif isinstance(data, list):
            out = data
        return out

    async def product_detail(self, pid):
        d = await self.call("query_product_detail_by_id", {"productId": pid})
        return d.get("data") or {}


# ============================================================
# 时间窗口
# ============================================================
def build_windows(a):
    """返回 (汇总参数, 订单参数, 描述)"""
    if a.rel:
        p = {"relativePeriod": a.rel.capitalize()}
        return dict(p), dict(p), f"relativePeriod={a.rel}"
    if a.date:
        return ({"startAt": a.date, "endAt": a.date},
                {"startAt": a.date, "endAt": next_day(a.date)},
                f"单日 {a.date}")
    return ({"startAt": a.start, "endAt": a.end},
            {"startAt": a.start, "endAt": a.end},
            f"{a.start} ~ {a.end}")


# ============================================================
# 各子命令实现
# ============================================================
async def cmd_summary(m, tp, op, a, d):
    tot = await m.total(tp, {})
    if not tot:
        print("汇总查询失败"); return None
    keys = [k for _, k, kind in XIAOHAN_FIELDS if k]
    agg = {k: round(sum(num(x.get(k)) for x in tot), 2) for k in keys}

    if a.raw:
        print(json.dumps(tot, ensure_ascii=False, indent=2)); return agg
    if a.json:
        print(json.dumps(agg, ensure_ascii=False, indent=2)); return agg

    print(f"返回 {len(tot)} 天记录")
    for label, field, kind in XIAOHAN_FIELDS:
        if kind == "HEADER":
            print(f"\n[{label}]"); continue
        if kind == "N/A" or field is None:
            print(f"  {label:<40}: 不在DataX(经营汇总无此字段)"); continue
        v = agg.get(field, 0.0)
        print(f"  {label:<40}: {money(v) if kind == 'money' else int(v)}")

    print("\n" + "=" * 76)
    print("勾稽恒等式校验(小韩面本地化)")
    print("=" * 76)
    for name, expr, fn, note in IDENTITIES:
        lhs, rhs = fn(agg)
        diff = round(lhs - rhs, 2)
        ok = "✅" if abs(diff) < 0.011 else "❌"
        print(f"  {ok} {name:<22} {expr}")
        print(f"       左={lhs}  右={rhs}  差={diff}")
        if note:
            print(f"       注: {note}")
    return agg


async def cmd_daily(m, tp, op, a, d):
    tot = await m.total(tp, {})
    if not tot:
        return
    print("\n" + "=" * 76)
    print("逐日明细")
    print("=" * 76)
    print(f"{'日期':<26}{'单数':>6}{'流水':>12}{'净销':>12}{'实收':>12}")
    seen = {}
    total_n = 0
    for x in sorted(tot, key=lambda y: str(y.get("date"))):
        day = str(x.get("date"))[:10]
        dup = "  ⚠重复日期" if day in seen else ""
        seen[day] = 1
        n = int(num(x.get("orderCount")))
        total_n += n
        print(f"{str(x.get('date'))[:26]:<26}{n:>6}"
              f"{money(x.get('totalSales')):>12}{money(x.get('netSales')):>12}"
              f"{money(x.get('actualRevenue')):>12}{dup}")
    print(f"{'累加(含重复)':<26}{total_n:>6}")


async def cmd_orders(m, tp, op, a, d):
    recs = await m.orders(op)
    if not recs:
        print("订单查询失败或为空"); return recs
    recs.sort(key=lambda y: str(y.get("orderCreateAtLocal") or ""))
    if a.json:
        print(json.dumps(recs, ensure_ascii=False, indent=2)); return recs
    print("\n" + "=" * 104)
    print(f"订单明细(共 {len(recs)} 笔，按本地时间升序)")
    print("=" * 104)
    print(f"{'本地时间':<21}{'订单号':<18}{'状态':<13}{'来源':<10}{'来源名':<8}"
          f"{'操作员':<7}{'支付状态':<17}{'支付方式':<22}{'净销':>9}{'实收':>9}")
    for x in recs:
        print(f"{str(x.get('orderCreateAtLocal'))[:19]:<21}"
              f"{str(x.get('orderId')):<18}"
              f"{str(x.get('orderStatus')):<13}"
              f"{str(x.get('orderSourceType') or '-'):<10}"
              f"{str(x.get('sourceName') or '-'):<8}"
              f"{str(x.get('operatorName') or '-'):<7}"
              f"{str(x.get('paymentStatus')):<17}"
              f"{str(x.get('paymentType') or '-'):<22}"
              f"{money(x.get('netSales')):>9}{money(x.get('actualRevenue')):>9}")
    return recs


async def cmd_recon(m, tp, op, a, d):
    tot = await m.total(tp, {})
    recs = await m.orders(op)
    if not tot or not recs:
        print("对账失败: 数据缺失"); return
    agg = {k: round(sum(num(x.get(k)) for x in tot), 2)
           for _lb, k, _kd in XIAOHAN_FIELDS if k}
    noncancel = [x for x in recs if str(x.get("orderStatus")) != "Cancelled"]

    print("\n" + "=" * 76)
    print("对账: 明细 vs 汇总")
    print("=" * 76)
    print(f"{'项目':<28}{'明细':>14}{'汇总':>14}{'差':>14}")
    rows = [
        ("笔数(全)", len(recs),
         num(agg.get("orderCount")) + num(agg.get("cancelledOrders"))),
        ("笔数(非取消)", len(noncancel), num(agg.get("orderCount"))),
        ("totalSales", round(sum(num(x.get("totalSales")) for x in noncancel), 2),
         agg.get("totalSales", 0)),
        ("netSales", round(sum(num(x.get("netSales")) for x in noncancel), 2),
         agg.get("netSales", 0)),
        ("actualRevenue", round(sum(num(x.get("actualRevenue")) for x in noncancel), 2),
         agg.get("actualRevenue", 0)),
        ("cancelledOrderAmount",
         round(sum(num(x.get("cancelledOrderAmount")) for x in recs), 2),
         agg.get("cancelledOrderAmount", 0)),
    ]
    for lab, x, y in rows:
        print(f"{lab:<28}{round(float(x), 2):>14}{round(float(y), 2):>14}"
              f"{round(float(x) - float(y), 2):>14}")
    print("\n  归因提示:")
    print(f"    · netSales 差应 ≈ refundAmount({money(agg.get('refundAmount'))})")
    print("    · actualRevenue 差 ≈ 未到账部分(挂账 Account)")
    print(f"    · 笔数(全) 差应 = cancelledOrders({int(num(agg.get('cancelledOrders')))})")
    await _dist(m, recs, noncancel)


async def _dist(m, recs, noncancel):
    print("\n  支付方式分布(明细非取消):")
    pay, amt = collections.Counter(), collections.defaultdict(float)
    for x in noncancel:
        k = str(x.get("paymentType") or "(空)")
        pay[k] += 1
        amt[k] += num(x.get("actualRevenue"))
    for k in sorted(pay, key=lambda y: -pay[y]):
        print(f"    {k:<34}{pay[k]:>4} 单   实收 {money(amt[k]):>10}")
    print("\n  来源分布(明细非取消):")
    src, samt = collections.Counter(), collections.defaultdict(float)
    for x in noncancel:
        k = f"{x.get('orderSourceType') or '-'} / {x.get('sourceName') or '-'}"
        src[k] += 1
        samt[k] += num(x.get("netSales"))
    for k in sorted(src, key=lambda y: -src[y]):
        print(f"    {k:<34}{src[k]:>4} 单   净销 {money(samt[k]):>10}")


async def cmd_payments(m, tp, op, a, d):
    recs = await m.orders(op)
    if not recs:
        print("无订单"); return
    N = len(recs)
    combo, camb = collections.Counter(), collections.defaultdict(float)
    chan, chanab = collections.Counter(), collections.defaultdict(float)
    paid = 0
    for x in recs:
        raw = str(x.get("paymentType") or "").strip()
        act = num(x.get("actualRevenue"))
        key = raw if raw and raw != "-" else "(未支付)"
        combo[key] += 1
        camb[key] += act
        if key == "(未支付)":
            continue
        paid += 1
        for c in [p.strip() for p in raw.split(",") if p.strip()]:
            chan[c] += 1
            chanab[c] += act

    print("\n" + "=" * 76)
    print(f"支付方式占比  (订单 {N} / 已支付 {paid})")
    print("=" * 76)
    print(f"{'支付组合':<34}{'单数':>6}{'占比':>9}{'实收':>11}")
    for k in sorted(combo, key=lambda y: -combo[y]):
        print(f"{k:<34}{combo[k]:>6}{combo[k] / N * 100:>8.1f}%"
              f"{camb[k]:>11.2f}")
    T = sum(chan.values())
    if T:
        print(f"\n{'单通道(多通道拆计入各通道)':<34}{'次数':>6}{'占比':>9}{'实收':>11}")
        for k in sorted(chan, key=lambda y: -chan[y]):
            print(f"{k:<34}{chan[k]:>6}{chan[k] / T * 100:>8.1f}%"
                  f"{chanab[k]:>11.2f}")


async def cmd_discount(m, tp, op, a, d):
    recs = await m.orders(op)
    if not recs:
        print("无订单"); return
    ids = [str(x.get("orderId")) for x in recs]
    det = {str(o.get("orderId")): o
           for o in await m.details(ids)}
    rep = sum(num(x.get("totalDiscountAmount")) for x in recs)
    detv = sum(num(o.get("totalDiscountAmount")) for o in det.values())

    print("\n" + "=" * 76)
    print("折扣汇总")
    print("=" * 76)
    print(f"  订单报表折扣合计 : {money(rep)}")
    print(f"  明细单头折扣合计 : {money(detv)}")
    print(f"  差额             : {money(rep - detv)}")

    bucket = collections.defaultdict(float)
    cnt = collections.Counter()
    for o in det.values():
        bucket["订单级 totalDiscountAmount"] += num(o.get("totalDiscountAmount"))
        for it in (o.get("items") or []):
            for k, lab in (("discountAmount", "行级 discountAmount"),
                           ("itemDiscountAmount", "单品折扣 itemDiscountAmount"),
                           ("couponDiscountAmount", "优惠券 couponDiscountAmount"),
                           ("rechargeDiscountAmount", "充值优惠 rechargeDiscountAmount")):
                v = num(it.get(k))
                if abs(v) > 1e-9:
                    bucket[lab] += v
                    cnt[lab] += 1
    print("\n商品行折扣拆分:")
    for k, v in bucket.items():
        print(f"  {k:<38}{money(v):>12}  (行数 {cnt.get(k, 0)})")

    bad = []
    print("\n逐单比对(报表 vs 明细):")
    print(f"  {'订单号':<18}{'状态':<12}{'报表':>9}{'明细':>9}{'判定':>6}")
    for x in recs:
        oid = str(x.get("orderId"))
        rd = num(x.get("totalDiscountAmount"))
        dd = num((det.get(oid) or {}).get("totalDiscountAmount"))
        if abs(rd - dd) > 0.001:
            bad.append((oid, str(x.get("orderStatus")), rd, dd))
            print(f"  {oid:<18}{str(x.get('orderStatus')):<12}{rd:>9.2f}"
                  f"{dd:>9.2f}{'❌':>6}")
    if not bad:
        print("    (无不一致订单 ✅)")
    else:
        print(f"\n  ⚠ 不一致 {len(bad)} 单，疑似'退货单折扣翻倍'缺陷")


async def cmd_products(m, tp, op, a, d):
    recs = await m.orders(op)
    if not recs:
        print("无订单"); return
    det = await m.details([str(x.get("orderId")) for x in recs])
    agg = collections.defaultdict(lambda: {
        "name": "", "qty": 0.0, "amt": 0.0, "orig": 0.0, "disc": 0.0,
        "qty_ok": 0.0, "amt_ok": 0.0, "n": 0})
    for o in det:
        ok = str(o.get("orderStatus")) != "Returned"
        for it in (o.get("items") or []):
            q = num(it.get("quantity"))
            if q == 0:
                continue
            p = agg[str(it.get("productId"))]
            p["name"] = it.get("name")
            p["qty"] += q
            p["amt"] += num(it.get("finalAmount"))
            p["orig"] += num(it.get("originalPrice")) * q
            p["disc"] += (num(it.get("discountAmount"))
                          + num(it.get("itemDiscountAmount"))
                          + num(it.get("couponDiscountAmount")))
            p["n"] += 1
            if ok:
                p["qty_ok"] += q
                p["amt_ok"] += num(it.get("finalAmount"))

    print("\n" + "=" * 96)
    print("商品销量排行(含退货)")
    print("=" * 96)
    print(f"{'#':<4}{'商品':<34}{'数量':>8}{'成交额':>11}{'原价额':>11}{'折扣':>10}")
    for i, p in enumerate(sorted(agg.values(), key=lambda x: -x["qty"])[:30], 1):
        print(f"{i:<4}{str(p['name'])[:33]:<34}{p['qty']:>8.0f}{p['amt']:>11.2f}"
              f"{p['orig']:>11.2f}{p['disc']:>10.2f}")

    print("\n" + "=" * 96)
    print("商品成交额排行")
    print("=" * 96)
    for i, p in enumerate(sorted(agg.values(), key=lambda x: -x["amt"])[:15], 1):
        print(f"{i:<4}{str(p['name'])[:33]:<34}{p['qty']:>8.0f}{p['amt']:>11.2f}")

    print("\n" + "=" * 96)
    print("剔除退货后排行(仅有效销售)")
    print("=" * 96)
    for i, p in enumerate(sorted(agg.values(), key=lambda x: -x["qty_ok"])[:15], 1):
        print(f"{i:<4}{str(p['name'])[:33]:<34}{p['qty_ok']:>8.0f}{p['amt_ok']:>11.2f}")

    print(f"\n合计: 数量 {sum(p['qty'] for p in agg.values()):.0f} "
          f"| 成交额 {sum(p['amt'] for p in agg.values()):.2f} "
          f"| 商品种类 {len(agg)}")


async def cmd_categories(m, tp, op, a, d):
    prods = await m.products()
    pmap = {}
    for p in prods:
        pid = str(p.get("id"))
        dd = await m.product_detail(pid)
        cats = dd.get("categories") or []
        pmap[pid] = [c.get("name") for c in cats]
        await asyncio.sleep(0.4)
    recs = await m.orders(op)
    if not recs:
        print("无订单"); return
    det = await m.details([str(x.get("orderId")) for x in recs])
    agg = collections.defaultdict(lambda: {
        "qty": 0.0, "amt": 0.0, "skus": set(), "qty_ok": 0.0, "amt_ok": 0.0})
    for o in det:
        ok = str(o.get("orderStatus")) != "Returned"
        for it in (o.get("items") or []):
            q = num(it.get("quantity"))
            if q == 0:
                continue
            pid = str(it.get("productId"))
            cats = pmap.get(pid) or ["(未分类)"]
            c = agg[cats[0]]
            c["qty"] += q
            c["amt"] += num(it.get("finalAmount"))
            c["skus"].add(pid)
            if ok:
                c["qty_ok"] += q
                c["amt_ok"] += num(it.get("finalAmount"))

    print("\n" + "=" * 84)
    print("分类销售排行(含退货，按数量)")
    print("=" * 84)
    print(f"{'分类':<30}{'数量':>8}{'成交额':>12}{'SKU':>6}")
    for k, c in sorted(agg.items(), key=lambda kv: -kv[1]["qty"]):
        print(f"{str(k)[:29]:<30}{c['qty']:>8.0f}{c['amt']:>12.2f}"
              f"{len(c['skus']):>6}")
    print("\n剔退货后:")
    print(f"{'分类':<30}{'数量':>8}{'成交额':>12}")
    for k, c in sorted(agg.items(), key=lambda kv: -kv[1]["qty_ok"]):
        print(f"{str(k)[:29]:<30}{c['qty_ok']:>8.0f}{c['amt_ok']:>12.2f}")


async def cmd_members(m, tp, op, a, d):
    """会员统计：优先用 hasMember 筛选(快)；失败则回退逐单下钻"""
    use_filter = not a.legacy

    if use_filter:
        # 路径A: hasMember 直接筛（1~2 次请求搞定）
        mem = await m.orders(dict(op, hasMember=True))
        non = await m.orders(dict(op, hasMember=False))
        if mem is not None and non is not None:
            print("\n" + "=" * 84)
            print(f"会员订单统计 (hasMember 筛选)  "
                  f"会员 {len(mem)} 单 / 非会员 {len(non)} 单")
            print("=" * 84)
            _mem_amounts(mem, non)
            _mem_rank(mem)
            print("\n  数据来源: query_datax_order_sales_reports + hasMember ✅")
            return

    # 路径B: 逐单下钻（回退，慢但拿得到会员名/手机）
    print("  (hasMember 不可用，回退逐单下钻)")
    recs = await m.orders(op)
    if not recs:
        print("无订单"); return
    base = {str(x.get("orderId")): x for x in recs}
    det = await m.details(list(base.keys()))
    mem, non = [], []
    for o in det:
        oid = str(o.get("orderId"))
        b = base.get(oid, {})
        row = {
            "oid": oid, "status": str(o.get("orderStatus")),
            "memberId": o.get("memberId") or "",
            "member": o.get("orderMember") or {},
            "sales": num(b.get("totalSales")),
            "disc": num(b.get("totalDiscountAmount")),
            "net": num(b.get("netSales")),
            "actual": num(b.get("actualRevenue")),
        }
        (mem if row["memberId"] else non).append(row)
    print("\n" + "=" * 84)
    print(f"会员消费统计(下钻)  会员 {len(mem)} 单 / 非会员 {len(non)} 单")
    print("=" * 84)
    _mem_amounts(mem, non)
    _mem_rank(mem)


def _mem_amounts(mem, non):
    def agg(rows, only_ok=False):
        rr = [x for x in rows
              if str(x.get("orderStatus")) == "Completed"] if only_ok else rows
        return (len(rr), sum(num(x.get("totalSales")) for x in rr),
                sum(num(x.get("totalDiscountAmount")) for x in rr),
                sum(num(x.get("netSales")) for x in rr),
                sum(num(x.get("actualRevenue")) for x in rr))
    print(f"{'口径':<26}{'单数':>6}{'流水':>11}{'折扣':>11}"
          f"{'净销':>11}{'实收':>11}")
    for lab, rows, ok in (("会员-全部订单", mem, False),
                          ("会员-仅已完成", mem, True),
                          ("非会员-全部", non, False),
                          ("非会员-仅已完成", non, True)):
        n, sv, dd, nt, ac = agg(rows, ok)
        print(f"{lab:<26}{n:>6}{sv:>11.2f}{dd:>11.2f}{nt:>11.2f}{ac:>11.2f}")


def _mem_rank(mem):
    agg = collections.defaultdict(lambda: {
        "name": "", "phone": "", "n": 0, "sales": 0.0, "actual": 0.0})
    for x in mem:
        mm = x.get("member") or {}
        mid = x.get("memberId") or "?"
        aa = agg[mid]
        aa["name"] = mm.get("name") or aa["name"] or "(未知)"
        aa["phone"] = mm.get("phone") or aa["phone"]
        aa["n"] += 1
        aa["sales"] += num(x.get("totalSales"))
        aa["actual"] += num(x.get("actualRevenue"))
    if not agg:
        print("\n  无会员订单")
        return
    print("\n会员排行:")
    print(f"{'会员ID':<20}{'会员':<22}{'手机':<14}{'单数':>5}"
          f"{'流水':>10}{'实收':>10}")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["actual"]):
        print(f"{k:<20}{str(v['name'])[:21]:<22}{str(v['phone'])[:13]:<14}"
              f"{v['n']:>5}{v['sales']:>10.2f}{v['actual']:>10.2f}")
    print(f"\n不同会员数: {len(agg)}")


async def cmd_count(m, tp, op, a, d):
    """订单计数：用 resultMode=Count，只返回去重订单数(不受大结果集限制)"""
    cases = [("全部", {})]
    if a.member is not None:
        cases.append((f"hasMember={a.member}", {"hasMember": a.member}))
    else:
        cases += [("会员", {"hasMember": True}),
                  ("非会员", {"hasMember": False})]
    if a.by_status:
        cases += [(f"状态={s}", {"orderStatus": s})
                  for s in ["Completed", "Returned", "Cancelled", "Progressing"]]
    if a.by_source:
        cases += [(f"来源={s}", {"orderSourceType": s})
                  for s in ["AutoMart", "Kiosk"]]

    print("\n" + "=" * 74)
    print("订单计数 (resultMode=Count)")
    print("=" * 74)
    print(f"{'筛选':<22}{'订单数':>9}")
    total = None
    for lab, extra in cases:
        p = dict(op)
        p.update(extra)
        p["resultMode"] = "Count"
        dd = await m.call("query_datax_order_sales_reports", p)
        data = dd.get("data") or {}
        n = (data.get("summary") or {}).get("orderCount")
        if dd.get("code") != 0 or n is None:
            print(f"{lab:<22}{'失败':>9}  {str(dd.get('msg'))[:44]}")
            await asyncio.sleep(2)
            continue
        if total is None:
            total = n
        print(f"{lab:<22}{n:>9}")
        await asyncio.sleep(2)
    return total


HANDLERS = {
    "summary": cmd_summary, "orders": cmd_orders, "recon": cmd_recon,
    "discount": cmd_discount, "products": cmd_products,
    "categories": cmd_categories, "members": cmd_members,
    "payments": cmd_payments, "count": cmd_count,
}


def main():
    ap = argparse.ArgumentParser(
        description="小韩面经营数据一站式查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("cmds", nargs="*", default=[],
                    choices=CMDS + [[]], help="子命令(默认 summary)")
    ap.add_argument("--merchant", default=MERCHANT_DEFAULT)
    ap.add_argument("--rel", default=None,
                    choices=["today", "yesterday", "thisweek", "lastweek",
                             "thismonth", "lastmonth"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--daily", action="store_true", help="附加逐日表")
    ap.add_argument("--raw", action="store_true", help="输出原始JSON")
    ap.add_argument("--json", action="store_true", help="JSON输出")
    ap.add_argument("--member", dest="member", default=None,
                    type=lambda v: str(v).lower() in ("1", "true", "yes"),
                    help="count 子命令：只统计会员(true)/非会员(false)")
    ap.add_argument("--by-status", action="store_true",
                    help="count 子命令：按状态细分")
    ap.add_argument("--by-source", action="store_true",
                    help="count 子命令：按来源细分")
    ap.add_argument("--legacy", action="store_true",
                    help="members 子命令：强制走逐单下钻(旧方式)")
    args = ap.parse_args()

    cmds = args.cmds or ["summary"]
    if not (args.rel or args.date or (args.start and args.end)):
        ap.error("需指定 --rel / --date / (--start 且 --end)")

    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(_run(args, cmds))


async def _run(args, cmds):
    tp, op, desc = build_windows(args)
    print(f"店铺 : {args.merchant} (时区 {TZ_NAME} = 中国时区)")
    print(f"窗口 : {desc}")
    print("=" * 76)
    async with MCP(args.merchant) as m:
        ctx = None
        for c in cmds:
            fn = HANDLERS.get(c)
            if not fn:
                continue
            got = await fn(m, tp, op, args, None)
            if c == "summary":
                ctx = got
        if args.daily and "summary" in cmds:
            await cmd_daily(m, tp, op, args, ctx)


if __name__ == "__main__":
    main()
