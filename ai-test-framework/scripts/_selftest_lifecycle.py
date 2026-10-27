"""数据生命周期模块自测（离线，不连 MCP）。

重点验证：未配置系统必须完全旁路 —— 这是「不破坏既有系统」的底线。
"""
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "executors"))

import data_lifecycle as dl  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


def main():
    print("=" * 66)
    print("数据生命周期自测（离线）")
    print("=" * 66)

    # ---- 1. 未配置：必须完全旁路 ----
    c = dl.LifecycleConfig(None)
    check("未配置 enabled=False", c.enabled is False)
    check("未配置 rule_for 返回 None", c.rule_for("batch_item_price") is None)
    check("未配置 is_irreversible=False", c.is_irreversible("delete_x") is False)
    check("未配置 needs_snapshot=False", c.needs_snapshot("batch_item_price") is False)
    ok, reason = dl.check_irreversible("delete_x", c)
    check("未配置不拦截不可恢复工具", ok is True, f"reason={reason!r}")

    # ---- 2. 已配置：正确解析 ----
    raw = {
        "可还原": [{
            "工具": "batch_item_price",
            "快照工具": "item_detail",
            "主键参数": "merchFoodIds",
            "主键字段": "itemId",
            "快照字段": ["price"],
        }],
        "不可恢复": ["delete_store_user"],
    }
    c2 = dl.LifecycleConfig(raw, root_dir=_ROOT)
    check("已配置 enabled=True", c2.enabled is True)
    r = c2.rule_for("batch_item_price")
    check("可还原规则命中", r is not None and r.get("快照工具") == "item_detail")
    check("未声明的写工具无规则", c2.rule_for("set_tip_type") is None)
    check("needs_snapshot 仅对有规则工具为真",
          c2.needs_snapshot("batch_item_price") and not c2.needs_snapshot("set_tip_type"))
    ok, reason = dl.check_irreversible("delete_store_user", c2)
    check("不可恢复工具被拦截", ok is False and "自建自删" in reason)
    ok2, _ = dl.check_irreversible("item_create", c2)
    check("非不可恢复工具放行", ok2 is True)

    # ---- 3. 快照存储：不覆盖首次原值 ----
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        st = dl.SnapshotStore(td)
        p1 = st.save("batch_item_price", "item-1", {"原值": {"price": "10.00"}})
        p2 = st.save("batch_item_price", "item-1", {"原值": {"price": "99.00"}})
        got = st.load("batch_item_price", "item-1")
        check("快照落盘", p1 is not None and os.path.exists(p1))
        check("重复保存不覆盖首次原值",
              got is not None and got["原值"]["price"] == "10.00",
              f"got={got}")
        check("list_all 能列出", len(st.list_all()) == 1)

    # ---- 4. 实体定位与业务错误判定 ----
    data = {"data": {"items": [{"itemId": "a", "price": "1"},
                               {"itemId": "b", "price": "2"}]}}
    f = dl._find_entity(data, "itemId", "b")
    check("_find_entity 定位正确", f is not None and f.get("price") == "2")
    check("_find_entity 未命中返回 None",
          dl._find_entity(data, "itemId", "zzz") is None)
    check("_is_biz_error 识别失败",
          dl._is_biz_error('{"code":1001,"success":false}') is True)
    check("_is_biz_error 识别成功",
          dl._is_biz_error('{"code":0,"success":true}') is False)

    # ---- 5. 第2批：新建后清理（未配置必须旁路）----
    check("未配置 rule_for_create 返回 None",
          c.rule_for_create("add_extra_fee") is None)
    rec = dl.register_created(None, c, "add_extra_fee", {}, "{}")
    check("未配置时不登记新建", rec is None and len(c.created) == 0)

    # ---- 6. 第2批：已配置的清理登记与参数构造 ----
    raw2 = dict(raw)
    raw2["新建后清理"] = [{
        "工具": "add_extra_fee",
        "删除工具": "delete_extra_fee",
        "删除参数": {"merchId": "{merchId}", "feeId": "{新实体ID}"},
        "返回ID字段": "createdFeeId",
    }, {
        "工具": "add_pickup_time_slot",
        "删除工具": "delete_pickup_time_slot",
        "删除参数": {"merchId": "{merchId}",
                     "day": "{建时参数.days}"},
    }]
    c3 = dl.LifecycleConfig(raw2, root_dir=_ROOT)
    created_text = ('{"code":0,"data":{"createdFeeId":"26057dd8-30f0-449c-91b8-'
                    'df2191637cd5","feeName":"x"}}')
    rec = dl.register_created(None, c3, "add_extra_fee",
                              {"name": "x", "confirm": True}, created_text)
    check("新建登记成功", rec is not None)
    check("能从 createdFeeId 捞到新实体ID",
          rec and rec["新实体ID"] == "26057dd8-30f0-449c-91b8-df2191637cd5",
          f"got={rec['新实体ID'] if rec else None}")
    check("上下文ID不被误认（merchId 不算新建ID）",
          dl._first_created_id({"merchId": "d87a750e-88a9-4f6e-8a33-2f81cf6d38ed"})
          is None)

    # 删除参数构造：{新实体ID} 占位符
    class _Sys:
        merchant_id = "d87a750e-88a9-4f6e-8a33-2f81cf6d38ed"

    class _Ex:
        sys = _Sys()

    p = dl._build_delete_params(_Ex(), rec, c3)
    check("{新实体ID} 与 {merchId} 占位符正确替换",
          p == {"merchId": "d87a750e-88a9-4f6e-8a33-2f81cf6d38ed",
                "feeId": "26057dd8-30f0-449c-91b8-df2191637cd5"}, f"got={p}")

    # 建时参数回填（自提时段用 days）
    rec2 = dl.register_created(None, c3, "add_pickup_time_slot",
                               {"days": [1, 2], "openTime": "10:00"}, "{}")
    p2 = dl._build_delete_params(_Ex(), rec2, c3)
    check("{建时参数.xxx} 正确回填",
          p2 is not None and p2.get("day") == [1, 2], f"got={p2}")

    # 缺 ID 时应拒绝构造（不能乱删）
    rec3 = {"工具": "add_extra_fee", "删除工具": "delete_extra_fee",
            "新实体ID": None, "建时参数": {}}
    check("缺 ID 时不构造删除参数",
          dl._build_delete_params(_Ex(), rec3, c3) is None)

    # ---- 7. 第3批：自建自删（未配置必须旁路）----
    check("未配置 self_delete_rule 返回 None",
          c.self_delete_rule("delete_extra_fee") is None)

    raw3 = dict(raw2)
    raw3["自建自删"] = [{
        "删除工具": "delete_extra_fee",
        "建工具": "add_extra_fee",
        "建参数": {"merchId": "{merchId}", "name": "_AUTOTEST"},
        "删除主键": "feeId",
        "返回ID字段": "createdFeeId",
    }]
    c4 = dl.LifecycleConfig(raw3, root_dir=_ROOT)
    check("已配置 self_delete_rule 命中",
          c4.self_delete_rule("delete_extra_fee") is not None)
    check("未声明的删除工具无规则",
          c4.self_delete_rule("delete_store_user") is None)

    # 模拟：建临时实体成功 → 删除参数被换成临时ID
    class _FakeEx:
        sys = _Sys()

        def __init__(self, create_text):
            self.create_text = create_text

        async def _call_tool(self, session, name, tool_params):
            return False, self.create_text, 0.0

    async def _run_case(create_text, delete_params):
        return await dl.prepare_self_delete(
            _FakeEx(create_text), object(), "delete_extra_fee",
            delete_params, c4)

    tmp_id = "11111111-2222-3333-4444-555555555555"
    ok_txt = '{"code":0,"data":{"createdFeeId":"%s"}}' % tmp_id
    ok, np_, reason = asyncio.run(_run_case(
        ok_txt, {"merchId": "real-merch", "feeId": "REAL_REAL_ID"}))
    check("自建自删准备成功", ok is True, f"reason={reason}")
    check("删除目标已换成临时实体ID",
          np_.get("feeId") == tmp_id, f"got={np_.get('feeId')}")
    check("真实ID未被使用", np_.get("feeId") != "REAL_REAL_ID")

    # 建失败时不得放行（否则会拿真实ID去删）
    ok2, np2, reason2 = asyncio.run(_run_case(
        '{"code":1001,"success":false,"msg":"bad"}',
        {"merchId": "m", "feeId": "REAL_REAL_ID"}))
    check("建失败时拒绝执行自建自删", ok2 is False, f"reason={reason2}")
    check("建失败时不改动原参数", np2.get("feeId") == "REAL_REAL_ID")

    # 建了但拿不到ID → 必须拒绝（不能盲删）
    ok3, _np3, reason3 = asyncio.run(_run_case(
        '{"code":0,"data":{"feeName":"x"}}',
        {"merchId": "m", "feeId": "REAL_REAL_ID"}))
    check("拿不到临时ID时拒绝", ok3 is False, f"reason={reason3}")

    print("-" * 66)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
