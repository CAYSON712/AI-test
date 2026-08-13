# -*- coding: utf-8 -*-
"""上报真实下架咸檸檬七喜的 trace 到自建平台（带真实延迟和真实数据）"""
import requests

BASE = "http://127.0.0.1:8000"

payload = {
    "name": "下架咸檸檬七喜",
    "input": "帮我下架咸檸檬七喜",
    "metadata": {
        "userId": "11284754494063621",
        "business": "POS",
        "scenario": "商品管理-下架",
        "company": "测试公司(9088125566714885)",
        "merchant": "Test01(9088143804924933)",
    },
    "observations": [
        # ① Agent 意图理解
        {"name": "LLM-意图理解", "type": "GENERATION",
         "input": "帮我下架咸檸檬七喜",
         "output": '{"意图":"下架","商品":"咸檸檬七喜","操作":"status=Off"}',
         "metadata": {"scenario": "商品管理-下架"}},

        # ② Skill 路由 + 参数抽取
        {"name": "Skill-参数翻译", "type": "SPAN",
         "input": '{"意图":"下架","商品":"咸檸檬七喜"}',
         "output": '{"工具":"search_products_by_name → update_products_by_ids","参数":{"merchantIds":["9088143804924933"],"productName":"咸檸檬七喜","status":"Off"}}',
         "metadata": {"skill": "商品管理", "skill_name": "下架商品"}},

        # ③ MCP 搜索（真实调用）
        {"name": "MCP-search_products_by_name", "type": "SPAN",
         "input": '{"toolParams":{"merchantIds":["9088143804924933"],"productName":"咸檸檬七喜"}}',
         "output": '{"code":0,"msg":"查询成功，共找到 2 个商品。","data":{"Salted Lemon w. 7 Up":[{"id":"9088145987601453","price":4.5,"status":"Selling"}],"Iced Ribera w. Lemon":[{"id":"9088145987601451","price":4.95,"status":"Selling"}]},"success":true}',
         "metadata": {"mcp": "POS-mcp", "tool": "search_products_by_name", "latency_ms": 2871}},

        # ③b MCP 下架（真实调用）
        {"name": "MCP-update_products_by_ids", "type": "SPAN",
         "input": '{"toolParams":{"merchantIds":["9088143804924933"],"productIds":["9088145987601453"],"status":"Off"}}',
         "output": '{"code":0,"msg":"已将店铺 Test01 的 1 个商品属性更新","data":{"Salted Lemon w. 7 Up":[{"id":"9088145987601453","price":4.5,"status":"Off"}]},"success":true}',
         "metadata": {"mcp": "POS-mcp", "tool": "update_products_by_ids", "latency_ms": 1048}},

        # ④ Agent 生成回答
        {"name": "LLM-生成回答", "type": "GENERATION",
         "input": '{"搜索耗时":2871,"下架耗时":1048,"商品状态":"Off"}',
         "output": "已为您下架咸檸檬七喜（Salted Lemon w. 7 Up），状态已更新为 Off。",
         "metadata": {"latency_total_ms": 3919}},
    ],
    "scores": [
        {"name": "intent_accuracy", "value": 1.0, "data_type": "NUMERIC", "comment": "意图理解正确"},
        {"name": "tool_selection", "value": 1.0, "data_type": "NUMERIC", "comment": "选对商品和工具"},
        {"name": "param_extraction", "value": 1.0, "data_type": "NUMERIC", "comment": "参数抽取正确"},
        {"name": "accuracy", "value": 1.0, "data_type": "NUMERIC", "comment": "真实下架成功,状态变Off"},
        {"name": "reply_honesty", "value": 1.0, "data_type": "NUMERIC", "comment": "如实反馈下架结果"},
    ],
}


def main():
    r = requests.post(f"{BASE}/api/trace", json=payload)
    r.raise_for_status()
    print("✅ 真实下架 trace 已上报，trace_id =", r.json()["trace_id"])
    print("\n刷新 http://127.0.0.1:8000 查看")


if __name__ == "__main__":
    main()
