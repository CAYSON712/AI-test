# -*- coding: utf-8 -*-
"""POS 实时订单查询工具(长期可复用)。

直连 POS MCP，调用 query_datax_order_sales_reports。
服务端已支持"时间选源"：历史读 DataX、近期(<24h 内)读 EasyPos 实时源，
所以拉取"今天/最近几小时"窗口即可拿到实时 POS 订单。
脚本自动:
  - 用 --cid 指定公司 CompanyId(不同公司不同，如 cayson=12599656434959365)
  - 解析返回的 segments(多段)并合并
  - 按订单创建时间展示
  - 时间统一换算成店铺本地时区(Los_Angeles, PST/PDT)展示
  - 一行一笔输出完整列(对齐 EasyPos 后台订单列表):
    时间 | 单号 | orderId | 类型 | 来源系统/渠道 | 状态 | 支付 |
    税后营业额 | 商品销售总额 | 税 | 小费 | 加收 | 折扣 | 退款金额 | 总额
    (税后营业额=netSales, 口径=preTaxRevenue+totalTaxAmount; 商品销售总额=totalSales)

用法示例(店铺为洛杉矶时区; 新版接口时间不带时区偏移, endAt 含结束日):
    # 查某店最近 6 小时实时订单
    python scripts/query_pos_realtime.py --merchant 12600071176193029 \
        --cid 12599656434959365 --hours 6

    # 查某店某日订单(纯日期, 含当天)
    python scripts/query_pos_realtime.py --merchant 12600071176193029 \
        --cid 12599656434959365 --start 2026-09-09 --end 2026-09-09

    # 相对时间(服务端按店铺时区解析)
    python scripts/query_pos_realtime.py --merchant 12600071176193029 \
        --cid 12599656434959365 --relative today
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

KNOWN_COMPANIES = {
    "9088125566714885": "测试公司",
    "12599656434959365": "cayson自动化公司",
}


def _now_la():
    """当前洛杉矶时间(默认假设店铺是洛杉矶; 有 --tz 可换)。"""
    return datetime.now(ZoneInfo("America/Los_Angeles"))


def _to_tz(s, tzname="America/Los_Angeles"):
    """UTC ISO 字符串转店铺本地时区显示(默认洛杉矶)。"""
    if not s:
        return "-"
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(tzname)).strftime("%m/%d %H:%M")
    except Exception:
        return str(s)[:19]


def _num(v):
    """金额格式化: None/空 -> '-', 否则保留两位(含负号)。"""
    if v is None or v == "":
        return "-"
    try:
        return f"{float(v):.2f}"
    except Exception:
        return str(v)


async def call_tool(env, params):
    headers = {"Authorization": f"Bearer {env['token']}", "CompanyId": env["company_id"]}
    async with httpx.AsyncClient(headers=headers, timeout=120.0) as http:
        async with streamable_http_client(env["url"], http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("query_datax_order_sales_reports",
                                              {"toolParams": params})
                text = "".join(getattr(c, "text", "") for c in res.content)
                return res.is_error, text


# 新版接口要求: 时间不带 Z/时区偏移(店铺本地时间)。支持纯日期或 ISO 本地时间。
# relativePeriod 枚举(首字母大写): Today/Yesterday/ThisWeek/LastWeek/ThisMonth/LastMonth
RELATIVE_PERIODS = {"today", "yesterday", "thisweek", "lastweek", "thismonth", "lastmonth"}


def _norm_relative(v):
    """把用户的 today/thisWeek 等统一成接口枚举(首字母大写驼峰)。
    today->Today, thisweek->ThisWeek, lastmonth->LastMonth"""
    s = v.strip().lower()
    m = {"today": "Today", "yesterday": "Yesterday", "thisweek": "ThisWeek",
         "lastweek": "LastWeek", "thismonth": "ThisMonth", "lastmonth": "LastMonth"}
    return m.get(s, v)


def _fmt_dt(dt):
    """本地时间 -> 新版接口格式(不带偏移)。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_segments(text):
    """解析订单明细返回的 segments, 返回 (records, warnings, truncated)。"""
    d = json.loads(text)
    data = d.get("data")
    if data is None:
        return None, d.get("msg", text), []
    segs = data.get("segments")
    if segs:
        all_recs = []
        for s in segs:
            all_recs.extend(s.get("records") or [])
        return all_recs, data.get("warnings", []), data.get("truncated")
    return list(data.get("records") or []), data.get("warnings", []), data.get("truncated")


async def run(args):
    env = dict(ENVS[args.env])
    if args.cid:
        env["company_id"] = args.cid
    comp = KNOWN_COMPANIES.get(str(env["company_id"]), str(env["company_id"]))

    # 时间窗(新版接口)：
    #   --relative Today/Yesterday/ThisWeek/LastWeek/ThisMonth/LastMonth -> relativePeriod
    #   --start/--end 显式日期(纯日期 YYYY-MM-DD 或本地时间, 不带时区)
    #   否则 --hours 相对当前洛杉矶时间
    params = {"merchantId": args.merchant, "limit": args.limit}
    if args.relative:
        rp = _norm_relative(args.relative)
        params["relativePeriod"] = rp
        win_label = f"relativePeriod={rp}"
    elif args.start and args.end:
        params["startAt"] = args.start
        params["endAt"] = args.end
        win_label = f"{args.start} ~ {args.end}(订单: 不含 endAt)"
    else:
        now = _now_la()
        sa = _fmt_dt(now - timedelta(hours=args.hours))
        ea = _fmt_dt(now)
        params["startAt"] = sa
        params["endAt"] = ea
        win_label = f"最近{args.hours}小时(洛杉矶 {sa[:16]} ~ {ea[:16]})"

    print(f"环境: {env['name']} | CompanyId={env['company_id']}({comp})")
    print(f"店铺: {args.merchant} | 窗口: {win_label}")
    is_err, text = await call_tool(env, params)
    if is_err:
        print("MCP 调用错误:", text[:300])
        return
    recs, warn, truncated = _parse_segments(text)
    if recs is None:
        print("业务错误:", warn)
        return

    # 去重 + 按订单创建时间排序
    seen = {}
    for r in recs:
        seen[r.get("orderId")] = r
    allr = sorted(seen.values(), key=lambda x: x.get("orderCreateAt", ""))

    amt = sum(float(r.get("totalAmount") or 0) for r in allr)
    paid = sum(float(r.get("totalAmount") or 0) for r in allr
               if str(r.get("orderStatus")) != "Cancelled")
    print(f"\n订单数: {len(allr)} 笔 | 总金额: ${amt:.2f} | 有效(非取消)合计: ${paid:.2f}")
    if warn:
        print("warnings:", ", ".join(str(w) for w in warn))
    if truncated:
        print("(truncated: 结果可能不完整)")

    if allr:
        # 方案A: 一行一笔, 完整列(对齐 EasyPos 后台)
        # 时间 | 单号 | orderId | 类型 | 来源系统/渠道 | 状态 | 支付 |
        # 税后营业额 | 商品销售总额 | 税 | 小费 | 加收 | 折扣 | 退款金额 | 总额(含小费)
        hdr = (f"{'时间(PST)':<12}{'单号':>16}  {'orderId':<19}{'类型':<15}"
               f"{'来源':<34}{'状态':<12}{'支付':<15}"
               f"{'税后营业额':>10}{'商品销售总额':>12}{'税':>8}"
               f"{'小费':>8}{'加收':>8}{'折扣':>9}{'退款金额':>10}{'总额':>9}")
        print(hdr)
        print("-" * len(hdr))
        for r in allr:
            t = _to_tz(r.get("orderCreateAt"))
            oid = str(r.get("orderId", ""))
            src = f"{r.get('orderSourceType','')}/{r.get('sourceName','')}"
            print(f"{t:<12}{str(r.get('orderSerial','')):>16}  {oid:<19}"
                  f"{str(r.get('orderType','')):<15}{src:<34}"
                  f"{str(r.get('orderStatus','')):<12}{str(r.get('paymentType') or '-'):<15}"
                  f"{_num(r.get('netSales')):>10}{_num(r.get('totalSales')):>12}"
                  f"{_num(r.get('totalTaxAmount')):>8}{_num(r.get('totalTipsAmount')):>8}"
                  f"{_num(r.get('totalSurchargeAmount')):>8}{_num(r.get('totalDiscountAmount')):>9}"
                  f"{_num(r.get('refundAmount')):>10}{_num(r.get('totalAmount')):>9}")
        print("\n列说明: 税后营业额=netSales(=preTaxRevenue+totalTaxAmount), 商品销售总额=totalSales,")
        print("         税=totalTaxAmount, 小费=totalTipsAmount, 加收=totalSurchargeAmount,")
        print("         折扣=totalDiscountAmount, 退款金额=refundAmount, 总额=totalAmount")
    else:
        print("(该窗口无订单)")


def main():
    ap = argparse.ArgumentParser(description="POS 实时订单查询(DataX+EasyPos 时间选源)")
    ap.add_argument("--merchant", required=True, help="店铺 merchantId")
    ap.add_argument("--cid", default=None, help="CompanyId(覆盖; cayson=12599656434959365)")
    ap.add_argument("--env", default="pos", choices=["pos", "automart"])
    ap.add_argument("--hours", type=int, default=6, help="查最近N小时(默认6, 未给start/end时生效)")
    ap.add_argument("--start", default=None, help="开始日期(纯日期 2026-09-09 或本地时间, 不带时区)")
    ap.add_argument("--end", default=None, help="结束日期(新版: 含结束日期, 如 2026-09-09)")
    ap.add_argument("--relative", default=None,
                    choices=["today", "yesterday", "thisWeek", "lastWeek", "thisMonth", "lastMonth"],
                    help="相对时间(服务端按店铺时区解析, 用它则省略 start/end)")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
