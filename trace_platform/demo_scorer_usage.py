# -*- coding: utf-8 -*-
"""
演示：用 scorer 模块给"下架咸檸檬七喜"打连续分（0.0~1.0，颗粒度0.1）
并上报到平台。对比之前"写死 1.0"，现在按真实情况算分。
"""
import os
import sys
import requests

# scorer 已归位到 test-runner/（执行端），这里补充路径以便 import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test-runner"))
from scorer import make_scorer

BASE = "http://127.0.0.1:8000"

# ---- 1. 用 scorer 计算各维度连续分 ----

# 参数抽取：期望 3 个参数(merchantIds/productName/status)，真实抽对 3 个
param_scorer = make_scorer("ratio")
param_score = param_scorer.score(correct=3, total=3)   # -> 1.0

# 工具选择：候选工具集里有正确工具；假设 AI 选对了
tool_scorer = make_scorer("ratio")
# 11 个工具里，属于"下架链路"的正确工具是 search+update，共 2 个，AI 用对了
tool_score = tool_scorer.score(correct=2, total=2)     # -> 1.0

# 意图识别：假设理解部分正确（识别到"下架"但没识别到具体商品）
intent_scorer = make_scorer("ratio")
intent_score = intent_scorer.score(correct=1, total=2)  # -> 0.5

# 忠实度：下架成功且如实反馈，布尔 true
honest_scorer = make_scorer("boolean")
honest_score = honest_scorer.score(True)                # -> 1.0

# 综合质量：加权（准确性0.5 + 完整0.3 + 一致0.2）
weighted_scorer = make_scorer("weighted", weights={"准确性": 0.5, "完整性": 0.3, "一致性": 0.2})
overall_score = weighted_scorer.score({"准确性": 0.9, "完整性": 1.0, "一致性": 0.8})  # -> 0.9

print("===== 连续分打分结果 =====")
print(f"参数抽取: {param_score} (抽对3/3)")
print(f"工具选择: {tool_score} (用对2/2)")
print(f"意图识别: {intent_score} (部分对，漏商品名)")
print(f"回复诚实: {honest_score}")
print(f"综合质量: {overall_score}")
print()

# ---- 2. 上报到平台（带连续分） ----
payload = {
    "name": "下架咸檸檬七喜（连续分演示）",
    "input": "帮我下架咸檸檬七喜",
    "metadata": {"userId": "cayson", "business": "POS", "scenario": "连续分打分演示"},
    "observations": [
        {"name": "LLM-意图理解", "type": "GENERATION",
         "input": "帮我下架咸檸檬七喜",
         "output": '{"意图":"下架","商品":"(未识别)"}'},   # 意图部分正确，商品没识别出
        {"name": "Skill-参数翻译", "type": "SPAN",
         "input": "帮我下架咸檸檬七喜",
         "output": '{"工具":"update_products_by_ids","参数":{}}'},
        {"name": "MCP-update", "type": "SPAN",
         "input": '{"productIds":["9088145987601453"],"status":"Off"}',
         "output": '{"code":0,"success":true}'},
    ],
    "scores": [
        {"name": "intent_accuracy", "value": intent_score, "data_type": "NUMERIC", "comment": "意图识别部分正确"},
        {"name": "tool_selection", "value": tool_score, "data_type": "NUMERIC", "comment": "工具选对"},
        {"name": "param_extraction", "value": param_score, "data_type": "NUMERIC", "comment": "参数抽对"},
        {"name": "reply_honesty", "value": honest_score, "data_type": "NUMERIC", "comment": "如实反馈"},
        {"name": "overall_quality", "value": overall_score, "data_type": "NUMERIC", "comment": "综合质量加权"},
    ],
}

r = requests.post(f"{BASE}/api/trace", json=payload)
r.raise_for_status()
print("✅ 已上报连续分 trace，trace_id =", r.json()["trace_id"])
print("刷新 http://127.0.0.1:8000 查看")
