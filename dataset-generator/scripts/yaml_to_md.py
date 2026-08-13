# -*- coding: utf-8 -*-
"""
数据集格式转换：YAML（数据源）→ Markdown（review 表格）
========================================================
用法：
  python yaml_to_md.py
  python yaml_to_md.py --yaml <输入yaml> --md <输出md>

说明：
  - YAML 是唯一数据源（机器执行用）
  - MD 由脚本自动生成（人评审用），避免两处不同步
  - 按 5 大分类 + 组合维度分组，输出统一表格
"""
import argparse
import datetime
import sys

# Windows 下强制 UTF-8 输出，避免 emoji 打印报错
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import os
import sys
from collections import defaultdict

import yaml

# 维度 → 分类 的映射（与评测维度规范表一致）
DIMENSION_CATEGORY = {
    # ① 核心质量
    "准确性": "① 核心质量", "相关性": "① 核心质量", "忠实度": "① 核心质量",
    "完整性": "① 核心质量", "一致性": "① 核心质量",
    # ② Agent 决策
    "意图识别": "② Agent 决策", "工具选择": "② Agent 决策",
    "参数抽取": "② Agent 决策", "工具调用成功率": "② Agent 决策",
    "回复诚实度": "② Agent 决策",
    # ③ 安全/风险
    "危险拦截": "③ 安全/风险", "越权防护": "③ 安全/风险", "安全注入": "③ 安全/风险",
    "敏感数据保护": "③ 安全/风险", "内容安全": "③ 安全/风险",
    # ④ RAG
    "检索召回率": "④ RAG 特有", "检索精确率": "④ RAG 特有",
    "回答忠实度": "④ RAG 特有", "幻觉程度": "④ RAG 特有", "检索内容覆盖": "④ RAG 特有",
    # 组合
    "多意图混合": "组合维度",
}


def _fmt(obj):
    """把 date/dict/list 等对象转成可读字符串"""
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return ", ".join(f"{k}:{_fmt(v)}" for k, v in obj.items())
    if isinstance(obj, list):
        return "[" + ", ".join(_fmt(x) for x in obj) + "]"
    return str(obj)


def parse_variants(case):
    """提取变体，拼接成可读文本"""
    inp = case.get("输入", {})
    if isinstance(inp, str):
        return ""
    variants = inp.get("variants", [])
    return " / ".join(_fmt(v) for v in variants) if variants else ""


def parse_expected(case):
    """提取期望输出的可读文本（兼容 字符串 和 字典 两种形式）"""
    exp = case.get("期望", {})
    # 期望是纯字符串（如"合理反问"）
    if isinstance(exp, str):
        return exp
    parts = []
    if exp.get("output"):
        parts.append(str(exp["output"]))
    if exp.get("block"):
        parts.append("期望拦截(block=true)")
    if exp.get("intent"):
        parts.append(f"意图={_fmt(exp['intent'])}")
    if exp.get("params"):
        parts.append(f"params={_fmt(exp['params'])}")
    if exp.get("tool"):
        parts.append(f"tool={_fmt(exp['tool'])}")
    return "；".join(parts) if parts else "-"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yaml", default=None, help="输入 YAML 路径")
    parser.add_argument("--md", default=None, help="输出 MD 路径")
    args = parser.parse_args()

    # 默认路径（相对 scripts/ 上一级的 datasets 和 review）
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    yaml_path = args.yaml or os.path.join(base, "datasets", "POS数据查询_数据集.yaml")
    if args.md:
        md_path = args.md
    else:
        # MD 文件名跟随 YAML 文件名（如 demo_generated.yaml → demo_generated.md）
        yaml_name = os.path.splitext(os.path.basename(yaml_path))[0]
        md_path = os.path.join(base, "review", f"{yaml_name}.md")

    with open(yaml_path, encoding="utf-8") as f:
        cases = yaml.safe_load(f)

    # 按分类分组
    groups = defaultdict(list)
    for case in cases:
        dim = case.get("维度", "未知")
        cat = DIMENSION_CATEGORY.get(dim, "其他")
        groups[cat].append(case)

    lines = []
    yaml_name = os.path.splitext(os.path.basename(yaml_path))[0]
    lines.append(f"# {yaml_name} 测试数据集")
    lines.append("")
    lines.append(f"> 数据源：`datasets/{yaml_name}.yaml`（自动生成，勿手改本文件）")
    lines.append(f"> 总数：**{len(cases)} 条用例**，按 5 大分类分组")
    lines.append("")

    # 各分类表
    for cat in ["① 核心质量", "② Agent 决策", "③ 安全/风险", "④ RAG 特有", "组合维度"]:
        if cat not in groups:
            continue
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Item ID | 维度 | P | 输入 | 期望 | 变体 | 标签 |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in groups[cat]:
            cid = c["用例ID"]
            dim = c["维度"]
            pri = c["优先级"]
            inp = c.get("输入", {})
            user_input = inp.get("user_input", "") if isinstance(inp, dict) else inp
            expected = parse_expected(c)
            variants = parse_variants(c)
            tags = "、".join(_fmt(t) for t in c.get("标签", []))
            lines.append(f"| {cid} | {dim} | {pri} | {user_input} | {expected} | {variants} | {tags} |")
        lines.append("")

    # 统计
    lines.append("---")
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("| 优先级 | 数量 |")
    lines.append("|---|---|")
    pri_count = defaultdict(int)
    for c in cases:
        pri_count[c["优先级"]] += 1
    for p in ["P0", "P1", "P2", "P3", "P4"]:
        if p in pri_count:
            lines.append(f"| {p} | {pri_count[p]} |")
    lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[OK] 已生成 MD: {md_path}")
    print(f"     共 {len(cases)} 条用例")


if __name__ == "__main__":
    main()
