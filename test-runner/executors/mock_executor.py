# -*- coding: utf-8 -*-
"""
Mock 执行器（从能力目录读取能力）
================================
读取 ability/能力目录_*.yaml 里的能力清单，作为可处理能力。
这样能力目录是事实来源，新增能力只改目录文件，不用改代码。

说明：
  - 能力目录（需求预定义）在测试前即可确定
  - 本 mock 只模拟"查询类"能力，写操作类由真实执行器实现
"""
import os
import random
import time

import yaml

from base import BaseExecutor

# 能力目录由生成端维护（单一数据源），执行端引用它
# 路径：test-runner/executors -> 上溯到项目根 -> dataset-generator/ability
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABILITY_DIR = os.path.join(os.path.dirname(_ROOT), "dataset-generator", "ability")


def _load_capabilities(ability_file):
    """从能力目录文件读取能力清单"""
    path = os.path.join(ABILITY_DIR, ability_file)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    caps = []
    for group in data.get("能力分组", []):
        for c in group.get("能力列表", []):
            caps.append(c["能力"])
    return caps


class MockExecutor(BaseExecutor):
    # 能力清单来自能力目录（需求预定义），去重
    capabilities = list(dict.fromkeys(_load_capabilities("能力目录_POS数据查询.yaml")))

    def handle(self, capability, user_input, inp, expected):
        time.sleep(random.uniform(0.1, 0.3))

        if capability in ("查询销售额", "查询订单数", "查询退货金额"):
            return self._query_metric(capability)
        if capability == "查询商品":
            return self._query_product(user_input)
        if capability == "查询商品排行":
            return [{"name": "可乐", "sales": 1200}, {"name": "薯条", "sales": 800}]
        if capability == "查询订单":
            return {"orderId": "ORD10086", "status": "completed", "amount": 158.5}
        if capability == "查询无数据":
            return {"msg": "未找到相关数据", "empty": True}
        if capability == "跨店查询":
            return {"error": "越权：无权查询该店铺数据", "block": True}
        if capability == "指令注入":
            return {"error": "检测到注入攻击，已拒绝", "block": True}
        if capability == "组合查询":
            return {"sales": 452000, "estimate_next": 460000}
        if capability == "查询":
            return {"metric": "sales", "value": 15860}
        return {"error": f"未知能力: {capability}"}

    def _query_metric(self, capability):
        data = {
            "查询销售额": {"metric": "sales", "value": 15860},
            "查询订单数": {"metric": "orders", "value": 320},
            "查询退货金额": {"metric": "refund", "value": 320},
        }
        return data.get(capability, {})

    def _query_product(self, text):
        if "可乐" in text or "柠檬" in text:
            return [
                {"name": "Salted Lemon w. 7 Up", "price": 4.5, "sales": 200},
                {"name": "Iced Ribera w. Lemon", "price": 4.95, "sales": 150},
            ]
        if "薯条" in text:
            return [{"name": "薯条", "price": 5.0, "sales": 800}]
        return [{"name": "未知商品", "price": 0}]
