# -*- coding: utf-8 -*-
"""Rooster POS MCP quick query (company-account endpoint).

Usage:
  python rooster_mcp.py tools
  python rooster_mcp.py month 2026-08          # orders + total report for a month
  python rooster_mcp.py orders --start 2026-08-01 --end 2026-09-01
  python rooster_mcp.py call query_merchants --json '{}'

  # 登录并列出某账号名下全部店铺（最快路径，不需要手工取 token）
  python rooster_mcp.py login --email a@b.com --password xxx
  python rooster_mcp.py login --email a@b.com --password xxx --save-env  # 写回 .env

  # 跨公司 / Local 店铺（CompanyId 必须随之切换，否则 40100 越权）
  python rooster_mcp.py stores                                # 全部公司+店铺+serverMode
  python rooster_mcp.py stores --mode Local                   # 只看 Local
  python rooster_mcp.py --company-id 11897209476088837 --merchant-id 11897214392730629 \
      call query_datax_total_reports --json '{"relativePeriod":"Today"}'

Env (.env): ROOSTER_MCP_URL / ROOSTER_MCP_TOKEN / ROOSTER_MCP_COMPANY_ID / ROOSTER_MERCHANT_ID
Report time zone: America/Los_Angeles (note: total reports use a 05:00 business-day cut).

【跨公司说明】同一 TOKEN 可见多间公司；CompanyId 请求头决定能操作哪间公司。
Cloud 店与 Local 店分属不同公司时，CompanyId 必须跟着店铺切，否则服务端报
40100 "The requested merchant is not authorized for the current company."
全局开关 --company-id / --merchant-id 可放在任意子命令之前，也可用环境变量
ROOSTER_MCP_COMPANY_ID / ROOSTER_MERCHANT_ID 覆盖默认值。
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

# 公鸡 POS 登录网关（≠ MCP 端点，≠ 前端域名）。
# MCP 端点只有 /mcp，没有登录接口；前端 roosterpos-test.proton-system.com 的
# #/mcp/auth-login 页面调的是这个 API 网关的 /company/account/login。
# 该地址写在页面 /config.js 的 apiBaseUrl 里，不是硬编码常量：
#   apiBaseUrl: 'https://roosterpos-test-api.proton-system.com/api'
LOGIN_URL = os.environ.get(
    "ROOSTER_LOGIN_URL",
    "https://roosterpos-test-api.proton-system.com/api/company/account/login")

L = "orderCreateAt"

# ---- 日期 / 金额口径（2026-11-06 实测校准，勿随意改动）----
# 记录里有三个时间字段，含义完全不同，混用会导致「每天差一天」：
#   orderCreateAt      : UTC 时间戳          例 2026-11-06T03:14:27+00:00
#   orderCreateAtLocal : 店铺本地时间        例 2026-11-05T19:14:27-08:00
#   date               : 营业日归属（当天 08:00 UTC，= 美西 00:00）
#                        例 2026-11-05T08:00:00+00:00
# 报表口径由响应体的 requestedPeriod.basis 声明，两个工具还不一样：
#   query_datax_order_sales_reports -> basis="order-created-at"（按下单时间）
#   query_datax_total_reports       -> basis="report-label"   （按报表标签日）
# 故凡「按天统计」一律用 date 字段（营业日），不要用 orderCreateAt（UTC），
# 否则美西晚上下的单会被算进 UTC 的第二天。
DATE_FIELD = "date"

# POS 界面「销售汇总」表头 → 字段映射（2026-11-06 逐日交叉验证通过）
#
# ⚠ 易踩坑：没有 totalAmount 这个字段（取到 None）。真实映射是
#     应收金额 = dueAmount       （欠收额，含未付部分）
#     已收金额 = actualRevenue
#     未收金额 = unpaidAmount
#     税后营业额 = netSales
#   而 totalSales / preTaxRevenue 是另一套口径（税前净额），与界面「税后」差一个
#   税+小费+加收，实测 11-05：netSales=251.83（界面）vs totalSales=228.93。
#   验证方法：取 11-01~11-09，逐日比对 POS 界面五列，四个金额列全部零误差。
POS_SUMMARY_COLS = (
    ("订单总数", "orderCount"),
    ("应收金额", "dueAmount"),
    ("已收金额", "actualRevenue"),
    ("未收金额", "unpaidAmount"),
    ("税后营业额", "netSales"),
)


def set_context(cid=None, mid=None, token=None):
    """覆盖默认 CompanyId / MerchantId / Token（跨公司、多账号、Local 店铺探测用）。

    同一 TOKEN 可见多间公司，但 CompanyId 头决定能操作哪间公司；Local 店
    与 Cloud 店常分属不同公司，故探测前必须成套切换 cid + mid，不能只换 mid
    （只换 mid 会稳定报 40100 The requested merchant is not authorized...）。
    """
    global CID, MID, TOKEN
    if cid:
        CID = str(cid)
    if mid:
        MID = str(mid)
    if token:
        TOKEN = str(token)


def login(email, password):
    """用账号密码登录公鸡 POS，返回 (token, account, companies)。

    为什么需要这个入口（2026-11-06 排查记录）：
      · MCP 端点 roosterpos-test-mcp.proton-system.com 只有 /mcp，无登录路由；
      · 前端 roosterpos-test.proton-system.com 是 SPA（/config.js + /assets/*.js）；
      · 真实登录接口在 config.js 的 apiBaseUrl 所指网关：
          POST {apiBaseUrl}/company/account/login  {"email":..,"password":..}
      · 返回体除 token 外，**直接带 companies[].merchants[] 全量店铺**，
        不必再调 MCP 查询 —— 这是最快确认「某账号有哪些店铺」的路径。

    ⚠ 该接口按 email 登录时，若同一邮箱对应多条账号记录会返回
      500 "Sequence contains more than one element."（LINQ .Single() 崩溃），
      且错误文案被包装成笼统的「账号或密码错误」，易误导排查方向。
      实测 944600294qq@gmail.com 邮箱登录必崩；该账号实际用上面的
      password 可正常登录（返回 11 家公司 18 家店铺），故此处不做特殊处理，
      只把原始错误原样抛出，便于识别这类数据问题。
    """
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(LOGIN_URL, json={"email": email, "password": password},
                   headers={"Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        raise RuntimeError(f"登录失败 HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("code") not in (0, None):
        raise RuntimeError(f"登录失败: {body.get('msg') or r.text[:300]}")
    data = body.get("data") or {}
    tok = data.get("token")
    if not tok:
        raise RuntimeError(f"登录成功但未返回 token: {r.text[:300]}")
    return tok, data.get("account") or {}, data.get("companies") or []


def stores_of(companies):
    """把登录返回的 companies[].merchants[] 摊平成店铺清单。"""
    rows = []
    for c in (companies or []):
        cid = c.get("id") or c.get("companyId")
        cname = c.get("name") or c.get("companyName") or ""
        for m in (c.get("merchants") or []):
            rows.append({
                "companyId": m.get("companyId") or cid,
                "companyName": cname,
                "merchantId": m.get("id"),
                "name": m.get("name") or m.get("shortName") or "",
                "serverMode": m.get("serverMode"),
                "isActive": m.get("isActive"),
                "timezoneId": m.get("timezoneId"),
            })
    return rows


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
    # 排序与展示用「本地时间」而非 UTC：UTC 与美西差 8 小时，
    # 直接用 orderCreateAt 会让阅读者把美西晚上的单看成第二天。
    def _ts(x):
        return str(x.get("orderCreateAtLocal") or x.get(L) or "")
    recs = sorted(recs, key=_ts)
    total = 0.0
    for i, r in enumerate(recs, 1):
        amt = r.get("totalAmount") or r.get("netSales") or 0
        try:
            total += float(amt)
        except Exception:
            pass
        if i <= 40:
            print(f"{i:>3}|{_ts(r)[:19]:<19}|{str(r.get('orderSerial')):>18}|"
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


async def cmd_daily(start=None, end=None, ym=None, tool="total"):
    """按营业日输出逐日汇总，表头与 POS 界面「销售汇总」对齐。

    为什么单独做一条：订单明细(query_datax_order_sales_reports)是「一笔一行」，
    含 Progressing 等未完成单；而 POS 界面「销售汇总」只算已完成营业日汇总，
    两者笔数天然不同（实测 127 笔明细 vs 102 笔汇总）。
    要和界面核对必须用 query_datax_total_reports + date(营业日) 字段。

    实测校验（2026-11-06，中环公鸡 Local）：
      订单总数/税后营业额 与 POS 界面逐日完全一致。
    """
    if ym:
        y, m = int(ym[:4]), int(ym[5:7])
        start = f"{y}-{m:02d}-01"
        end = f"{y + (m // 12)}-{(m % 12) + 1:02d}-01"
    if not start or not end:
        raise SystemExit("需要 --start/--end 或 --ym YYYY-MM")

    name = ("query_datax_total_reports" if tool == "total"
            else "query_datax_order_sales_reports")
    params = {"merchantId": MID, "startAt": start, "endAt": end}
    if name.endswith("order_sales_reports"):
        params["limit"] = 1000
    err, txt = await call(name, params)
    try:
        d = json.loads(txt)
    except Exception:
        print(txt[:1500])
        return
    if d.get("code") not in (0, None):
        print(json.dumps(d, ensure_ascii=False)[:800])
        return

    data = d.get("data") or {}
    rp = data.get("requestedPeriod") or {}
    warns = data.get("warnings") or []
    recs = flat(d)

    # 按营业日(date)归并。
    # ⚠ 多段返回(segments)里会夹带「全 0 的占位记录」，若直接累加会让真实数值被
    #   空记录覆盖/干扰（实测单日区间取 flat() 首条曾拿到全 0）。故按天取
    #   **数值最大的一条**，而不是累加：同一天不会被真正拆到两个 segment。
    days = {}
    for x in recs:
        k = str(x.get(DATE_FIELD) or x.get("date") or "")[:10]
        if not k:
            continue
        cur = days.get(k)
        if cur is None:
            days[k] = {c: x.get(c) for _, c in POS_SUMMARY_COLS}
            continue
        for _, c in POS_SUMMARY_COLS:
            try:
                if float(x.get(c) or 0) > float(cur.get(c) or 0):
                    cur[c] = x.get(c)
            except (TypeError, ValueError):
                pass

    print(f"\n系统: {d.get('system', '')} 店铺: {MID}")
    print(f"区间: {rp.get('startAt')} ~ {rp.get('endAt')} "
          f"(endInclusive={rp.get('endInclusive')}, basis={rp.get('basis')})")
    print(f"时区: {data.get('timeZoneId')}  工具: {name}")
    if warns:
        print(f"warnings: {warns}")

    print("\n" + "=" * 84)
    head = f"{'日期':<12}" + "".join(f"{t:>14}" for t, _ in POS_SUMMARY_COLS)
    print(head)
    print("-" * 84)
    def _num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    tot = {c: 0.0 for _, c in POS_SUMMARY_COLS}
    for k in sorted(days):
        row = days[k]
        cells = []
        for _, c in POS_SUMMARY_COLS:
            v = _num(row[c])
            cells.append(f"{int(v):>14}" if c == "orderCount" else f"{v:>14,.2f}")
            tot[c] += v
        print(f"{k:<12}" + "".join(cells))
    print("-" * 84)
    cells = []
    for _, c in POS_SUMMARY_COLS:
        cells.append(f"{int(tot[c]):>14}" if c == "orderCount"
                     else f"{tot[c]:>14,.2f}")
    print(f"{'合计':<12}" + "".join(cells))

    if any(str(w).upper() == "SEGMENTS_MUST_NOT_BE_SUMMED" for w in warns) \
            or len(data.get("segments") or []) > 1:
        print("\n⚠ SEGMENTS_MUST_NOT_BE_SUMMED：本次返回被拆成多个独立段，"
              "上述合计仅作参考，跨段相加可能重复计数；请按段单独看。")


def cmd_login(email, password, save_env=False):
    """登录指定账号并直接输出其名下全部店铺（含 serverMode）。

    这是「某账号在公鸡 POS 有哪些店铺」最快的答案来源：登录返回体自带
    companies[].merchants[]，不需要再调 MCP（省一次连接 + 避开 CompanyId 头坑）。
    """
    print(f"登录: {email}")
    print(f"网关: {LOGIN_URL}")
    try:
        tok, acct, comps = login(email, password)
    except Exception as e:
        print(f"\n[登录失败] {e}")
        return
    print(f"\n账号: {acct.get('firstName', '')} {acct.get('lastName', '')} "
          f"| id={acct.get('id')} | phone={acct.get('phone')}")
    print(f"token payload: {json.dumps(_jwt_payload(tok), ensure_ascii=False)}")

    rows = stores_of(comps)
    print(f"\n=== {len(comps)} 家公司 / {len(rows)} 家店铺 ===")
    w = max([len(str(r['name'])) for r in rows] + [6])
    for r in rows:
        mark = " *" if str(r['merchantId']) == str(MID) else ""
        print(f"  {str(r['name']):<{w}}  merchant={r['merchantId']:<18} "
              f"company={r['companyId']:<18} {r['companyName']}{mark}")
    print("\n用法（--company-id 与 --merchant-id 必须成套切换）:")
    for r in rows:
        print(f"  python scripts/rooster_mcp.py --token {tok[:16]}... "
              f"--company-id {r['companyId']} --merchant-id {r['merchantId']} "
              f"call query_datax_total_reports --json '{{\"relativePeriod\":\"Today\"}}'")

    if save_env:
        _update_env("ROOSTER_MCP_TOKEN", tok)
        if rows:
            _update_env("ROOSTER_MCP_COMPANY_ID", str(rows[0]['companyId']))
            _update_env("ROOSTER_MERCHANT_ID", str(rows[0]['merchantId']))
        print(f"\n已写回 .env（ROOSTER_MCP_TOKEN / COMPANY_ID / MERCHANT_ID）")


def _jwt_payload(tok):
    """解 JWT payload（仅用于展示，不校验签名）。"""
    try:
        import base64
        p = tok.split('.')[1]
        p += '=' * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def _update_env(key, val):
    """就地更新 .env 的某个键（不存在则追加）。"""
    path = os.path.join(_ROOT, ".env")
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        lines = []
    out, hit = [], False
    for ln in lines:
        if ln.strip().startswith(key + "="):
            out.append(f"{key}={val}")
            hit = True
        else:
            out.append(ln)
    if not hit:
        out.append(f"{key}={val}")
    open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")


async def cmd_stores(mode=None, only_default=False):
    """列出全部公司/店铺及 serverMode，给出「店铺→CompanyId」配套关系。

    为什么需要：Local 店与 Cloud 店可能分属不同公司，CompanyId 头传错会报
    40100 越权（请求到不了路由层），看起来像「路由坏了」其实是鉴权没配对。
    本命令直接输出可直接用于 --company-id / --merchant-id 的组合。
    """
    err, txt = await call("query_companies", {})
    try:
        d = json.loads(txt)
    except Exception:
        print(txt[:2000])
        return
    if d.get("code") not in (0, None):
        print(json.dumps(d, ensure_ascii=False)[:1000])
        return
    rows = []
    for c in d.get("data") or []:
        for m in c.get("merchants") or []:
            rows.append({"companyId": c.get("id"), "companyName": c.get("name"),
                         "merchantId": m.get("id"), "merchantName": m.get("name"),
                         "serverMode": m.get("serverMode")})
    if mode:
        rows = [r for r in rows if str(r["serverMode"]).lower() == str(mode).lower()]
    if only_default:
        rows = [r for r in rows if str(r["merchantId"]) == str(MID)]
    from collections import Counter
    cnt = Counter(str(r["serverMode"]) for r in rows)
    print(f"=== {len(rows)} store(s)  serverMode={dict(cnt)} ===")
    print(f"默认上下文: CompanyId={CID}  MerchantId={MID}\n")
    w = max([len(str(r["merchantName"])) for r in rows] + [4])
    for r in sorted(rows, key=lambda x: (str(x["serverMode"]), str(x["companyName"]))):
        mark = " *" if str(r["merchantId"]) == str(MID) else ""
        print(f"{str(r['serverMode']):<6} {str(r['merchantName']):<{w}}  "
              f"merchant={r['merchantId']:<18} company={r['companyId']:<18} "
              f"{r['companyName']}{mark}")
    print("\n用法(成套切换, 缺一不可):")
    for r in rows:
        print(f"  python rooster_mcp.py --company-id {r['companyId']} "
              f"--merchant-id {r['merchantId']} call query_datax_total_reports "
              f"--json '{{\"relativePeriod\":\"Today\"}}'")


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
    """通用调用。params 未给店铺上下文时自动补 MID。

    报表类工具（query_datax_*）必填 merchantId；手工 --json 只写业务参数
    （如 {"relativePeriod":"Today"}）时若不自动补，会稳定报「店铺 ID 不能为空」，
    看起来像工具坏了。补默认值后 --merchant-id 即可整套切换目标店铺。
    """
    p = dict(params or {})
    _mid_keys = ("merchantId", "merchantIds")
    if (not any(k in p for k in _mid_keys)
            and str(tool).startswith("query_datax_") and MID):
        p["merchantId"] = MID
    err, txt = await call(tool, p)
    print(f"[{tool}] params={json.dumps(p, ensure_ascii=False)} isError={err}")
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
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS：子解析器同样继承本组参数，若给普通默认值会把父级
    # 已解析出的值覆盖回 None（实测 --merchant-id 失效）。SUPPRESS 保证
    # 「没传就不写进 namespace」，父级传的值得以保留。
    common.add_argument("--company-id", default=argparse.SUPPRESS,
                        help="CompanyId 请求头（跨公司必须切换，默认读 .env）")
    common.add_argument("--merchant-id", default=argparse.SUPPRESS,
                        help="merchantId（目标店铺，默认读 .env）")
    common.add_argument("--token", default=argparse.SUPPRESS,
                        help="直接指定 MCP token（覆盖 .env 的 ROOSTER_MCP_TOKEN）")

    ap = argparse.ArgumentParser(description="Rooster POS MCP query", parents=[common])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tools", parents=[common])
    # 登录并列出该账号名下全部店铺（最快确认「某账号有哪些店铺」）
    sp = sub.add_parser("login", parents=[common],
                        help="用账号密码登录，列出其名下全部店铺")
    sp.add_argument("--email", required=True)
    sp.add_argument("--password", required=True)
    sp.add_argument("--save-env", action="store_true",
                    help="登录后把 token 与首选店铺写回 .env")
    sp = sub.add_parser("stores", parents=[common])
    sp.add_argument("--mode", choices=["Local", "Cloud"], help="\u6309 serverMode \u8fc7\u6ee4")
    sp.add_argument("--default", action="store_true", help="\u53ea\u770b\u5f53\u524d\u9ed8\u8ba4\u5e97\u94fa")
    sp = sub.add_parser("month", parents=[common])
    sp.add_argument("ym")
    sp.add_argument("--only", choices=["orders", "total"])
    sp.add_argument("--daily", action="store_true", help="\u9644\u52a0\u9010\u65e5\u660e\u7ec6")
    sp.add_argument("--breakdown", action="store_true", help="\u9644\u52a0\u8d39\u9879\u94bb\u53d6")
    # 逐日汇总（表头对齐 POS 界面「销售汇总」）
    sp = sub.add_parser("daily", parents=[common],
                        help="按营业日输出逐日汇总，对齐 POS 界面表头")
    sp.add_argument("--ym", default="", help="YYYY-MM，与 --start/--end 二选一")
    sp.add_argument("--start", default="")
    sp.add_argument("--end", default="")
    sp.add_argument("--tool", choices=["total", "orders"], default="total",
                    help="total=经营汇总(与界面一致)；orders=订单明细(含未完成单)")
    sp = sub.add_parser("orders", parents=[common])
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.add_argument("--tool", default="query_datax_order_sales_reports")
    sp.add_argument("--json", default="{}")
    sp.add_argument("--file", default="", help="params \u4ece JSON \u6587\u4ef6\u8bfb\u53d6")
    sp.add_argument("--merchants", action="store_true", help="list merchants first")
    sp.add_argument("--daily", action="store_true", help="\u9644\u52a0\u9010\u65e5\u660e\u7ec6")
    sp.add_argument("--breakdown", action="store_true", help="\u9644\u52a0\u8d39\u9879\u94bb\u53d6")
    sp = sub.add_parser("call", parents=[common])
    sp.add_argument("tool")
    sp.add_argument("--json", default="{}")
    sp.add_argument("--file", default="", help="params \u4ece JSON \u6587\u4ef6\u8bfb\u53d6")
    sp = sub.add_parser("order", parents=[common])
    sp.add_argument("order_id")
    a = ap.parse_args()

    # 全局上下文：子命令前后都允许传，覆盖 .env 默认值。
    # 跨公司探测时必须 cid+mid 成套切换，否则稳定 40100。
    set_context(getattr(a, "company_id", None), getattr(a, "merchant_id", None),
                getattr(a, "token", None))

    if a.cmd == "tools":
        asyncio.run(cmd_tools())
    elif a.cmd == "login":
        cmd_login(a.email, a.password, a.save_env)
    elif a.cmd == "stores":
        asyncio.run(cmd_stores(a.mode, a.default))
    elif a.cmd == "daily":
        asyncio.run(cmd_daily(a.start, a.end, a.ym, a.tool))
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
