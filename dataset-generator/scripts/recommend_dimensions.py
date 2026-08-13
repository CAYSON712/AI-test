# -*- coding: utf-8 -*-
"""
维度推荐器
==========
输入需求文本 → 自动推荐该测的评测维度 + 优先级 + 数据集类型。

逻辑：
  1. 从需求提取风险关键词
  2. 关键词 → 维度 + 优先级 映射
  3. 推断数据集类型
  4. 输出推荐清单

用法：
  python recommend_dimensions.py --req "AI POS数据查询，用户查销售额/退货/排行，涉及权限"
  python recommend_dimensions.py   # 使用内置示例需求
"""
import argparse
import sys

# ---------- 关键词 → 维度 映射 ----------
# 每个维度：触发关键词 + 优先级 + 说明
KEYWORD_MAP = {
    "查询/报表/数据/统计": {
        "维度": ["准确性", "相关性", "完整性", "参数抽取", "工具选择"],
        "优先级": "P0",
        "原因": "核心查询功能，数据准确性最关键",
    },
    "销售额/营业额/订单/退货/金额": {
        "维度": ["准确性", "参数抽取", "回复诚实度"],
        "优先级": "P0",
        "原因": "金额/数量类数据，准确性要求高",
    },
    "下架/删除/改价/更新": {
        "维度": ["危险拦截", "参数抽取", "回复诚实度", "工具选择"],
        "优先级": "P0",
        "原因": "写操作不可逆，需拦截危险操作",
    },
    "权限/店铺/角色/越权/RBAC": {
        "维度": ["越权防护", "安全注入"],
        "优先级": "P0",
        "原因": "涉及权限隔离，必须测越权",
    },
    "AI/意图/自然语言": {
        "维度": ["意图识别", "相关性"],
        "优先级": "P0",
        "原因": "自然语言理解是核心",
    },
    "RAG/检索/知识库/商品": {
        "维度": ["检索召回率", "检索精确率", "回答忠实度", "幻觉程度"],
        "优先级": "P1",
        "原因": "有检索环节，需测检索质量",
    },
    "模糊/不确定/可能": {
        "维度": ["意图识别"],
        "优先级": "P2",
        "原因": "模糊输入需测鲁棒性",
    },
    "失败/异常/错误/超时": {
        "维度": ["回复诚实度", "工具调用成功率"],
        "优先级": "P1",
        "原因": "异常处理需如实反馈",
    },
    "注入/忽略/攻击/恶意": {
        "维度": ["安全注入", "危险拦截"],
        "优先级": "P0",
        "原因": "攻击输入需拦截",
    },
    "客服/引导/多轮/上下文": {
        "维度": ["意图识别", "多意图混合", "相关性"],
        "优先级": "P1",
        "原因": "多轮/引导需理解上下文",
    },
}


def recommend(requirement: str):
    """根据需求文本推荐维度"""
    # 1. 收集命中的维度（去重保留优先级最高）
    dim_info = {}  # 维度 -> {优先级, 原因}
    for keywords, info in KEYWORD_MAP.items():
        if any(kw in requirement for kw in keywords.split("/")):
            for dim in info["维度"]:
                # 保留更高优先级
                cur = dim_info.get(dim)
                if cur is None or _pri_rank(info["优先级"]) < _pri_rank(cur["优先级"]):
                    dim_info[dim] = {"优先级": info["优先级"], "原因": info["原因"]}

    # 2. 推断数据集类型
    types = ["正常集"]
    if any(kw in requirement for kw in ["失败", "异常", "错误", "超时"]):
        types.append("异常集")
    if any(kw in requirement for kw in ["权限", "越权", "攻击", "注入", "删除", "下架"]):
        types.append("对抗集")
    if any(kw in requirement for kw in ["模糊", "不确定", "可能"]):
        types.append("模糊集")
    # 边界集默认建议
    types.append("边界集")

    return dim_info, types


def _pri_rank(p):
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}.get(p, 5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--req", default=None, help="需求描述文本")
    args = parser.parse_args()

    requirement = args.req or "AI POS 数据查询功能：用户通过自然语言查询销售额、订单数、退货金额、商品排行，涉及店铺权限和 RBAC 隔离，部分查询无数据需如实反馈。"
    print("=" * 60)
    print("需求分析")
    print("=" * 60)
    print(f"需求：{requirement}\n")

    dim_info, types = recommend(requirement)

    print("【推荐评测维度】")
    if not dim_info:
        print("  （未识别到明确维度，建议人工补充）")
    else:
        # 按优先级排序
        for dim in sorted(dim_info, key=lambda d: _pri_rank(dim_info[d]["优先级"])):
            info = dim_info[dim]
            print(f"  [{info['优先级']}] {dim} — {info['原因']}")

    print("\n【建议数据集类型】")
    for t in types:
        print(f"  - {t}")

    # 提示不适用判断
    print("\n【提示】")
    print("  1. 本推荐基于关键词匹配，请结合需求人工确认")
    print("  2. 不适用维度（如只读无写操作→危险拦截）需人工标记")


if __name__ == "__main__":
    main()
