# -*- coding: utf-8 -*-
"""
数据集完整性自检脚本
====================
自动检查数据集覆盖：维度 / 类型 / 能力 / 优先级，输出缺口报告。

用法：
  cd dataset-generator/scripts
  python check_dataset.py
  python check_dataset.py --yaml ../datasets/POS数据查询_数据集.yaml
"""
import argparse
import os
import sys
from collections import defaultdict

# Windows 下强制 UTF-8 输出，避免 emoji 打印报错
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

# 允许脚本 import 项目根
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# 从评测维度规范表取所有维度（对应5大类）
ALL_DIMENSIONS = [
    # ① 核心质量
    "准确性", "相关性", "忠实度", "完整性", "一致性",
    # ② Agent 决策
    "意图识别", "工具选择", "参数抽取", "工具调用成功率", "回复诚实度",
    # ③ 安全/风险
    "危险拦截", "越权防护", "安全注入", "敏感数据保护", "内容安全",
    # ④ RAG
    "检索召回率", "检索精确率", "回答忠实度", "幻觉程度", "检索内容覆盖",
    # 组合
    "多意图混合",
]

# 数据集类型
TYPES = ["正常集", "边界集", "异常集", "对抗集", "模糊集"]

# 不适用维度（需求不涉及，可配置）
# 说明：这些维度在本次需求中"合理不适用"，不报缺口，但要标注
# 对「POS 商品管理」：工具调用型系统（AI 调 MCP 查数据库），不涉文档检索/个人敏感/违规内容
NOT_APPLICABLE = {
    "内容安全": "商品管理不涉违规内容",
    "敏感数据保护": "商品数据不涉个人敏感信息",
    # RAG 检索增强维度：本系统是工具调用型（调 MCP 操作数据库），不是文档检索问答，不适用
    "检索召回率": "工具调用型系统，无文档检索环节",
    "检索精确率": "工具调用型系统，无文档检索环节",
    "回答忠实度": "工具调用型系统，回答基于工具返回的结构化数据而非检索片段",
    "幻觉程度": "工具调用型系统，无检索生成幻觉场景",
    "检索内容覆盖": "工具调用型系统，无文档检索环节",
}

# 能力 × 类型的豁免配置（某个能力确实某类型不适用时声明，必须说明理由）
# 默认要求：每个能力都覆盖全部 5 种类型。仅当有充分理由时在此豁免。
CAP_TYPE_NOT_APPLICABLE = {
    # "查询菜单": ["对抗集"]  # 示例：若确认某能力某类型无需测，在此声明并注释理由
}


def parse_type(case):
    """从标签/用例推断类型"""
    tags = case.get("标签", [])
    if isinstance(tags, str):
        tags = [tags]
    mapping = {
        "正向": "正常集", "正常": "正常集", "标准": "正常集",
        "边界": "边界集", "极限": "边界集",
        "异常": "异常集", "失败": "异常集", "无数据": "异常集", "负向": "异常集",
        "对抗": "对抗集", "越权": "对抗集", "注入": "对抗集", "危险": "对抗集",
        "模糊": "模糊集", "歧义": "模糊集",
    }
    for t in tags:
        for key, val in mapping.items():
            if key in str(t):
                return val
    return "正常集"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yaml", default=None)
    args = parser.parse_args()

    yaml_path = args.yaml or os.path.join(_ROOT, "datasets", "POS数据查询_数据集.yaml")
    with open(yaml_path, encoding="utf-8") as f:
        cases = yaml.safe_load(f)

    # 统计维度
    dim_count = defaultdict(int)
    for c in cases:
        dim_count[c["维度"]] += 1

    # 统计类型
    type_count = defaultdict(int)
    for c in cases:
        type_count[parse_type(c)] += 1

    # 统计能力
    cap_count = defaultdict(int)
    for c in cases:
        cap_count[c["能力"]] += 1

    # 统计优先级
    pri_count = defaultdict(int)
    for c in cases:
        pri_count[c["优先级"]] += 1

    print("=" * 50)
    print(f"数据集自检报告（{len(cases)} 条用例）")
    print("=" * 50)

    # 1. 维度覆盖
    print("\n【① 维度覆盖】")
    for dim in ALL_DIMENSIONS:
        n = dim_count.get(dim, 0)
        if n > 0:
            print(f"  ✓ {dim}: {n} 条")
        elif dim in NOT_APPLICABLE:
            print(f"  ○ {dim}: 不适用（{NOT_APPLICABLE[dim]}）")
        else:
            print(f"  ✗ 缺口 {dim}: 0 条")
    real_missing = [d for d in ALL_DIMENSIONS
                    if dim_count.get(d, 0) == 0 and d not in NOT_APPLICABLE]
    if real_missing:
        print(f"\n  ⚠️ 真实缺口: {', '.join(real_missing)}")
    else:
        print("\n  ✓ 除不适用维度外，所有维度都有用例")

    # 2. 类型覆盖
    print("\n【② 数据集类型覆盖】")
    for t in TYPES:
        n = type_count.get(t, 0)
        status = "✓" if n > 0 else "✗ 缺口"
        print(f"  {status} {t}: {n} 条")

    # 3. 能力覆盖
    print("\n【③ 能力覆盖】")
    print(f"  共 {len(cap_count)} 个能力:")
    for cap, n in cap_count.items():
        print(f"  - {cap}: {n} 条")

    # 4. 优先级
    print("\n【④ 优先级分布】")
    for p in ["P0", "P1", "P2"]:
        print(f"  {p}: {pri_count.get(p, 0)} 条")

    # 5. 能力 × 类型 组合覆盖（强制：每个能力应覆盖全部 5 种类型）
    print("\n【⑤ 能力 × 类型 组合覆盖】")
    cap_type = defaultdict(set)  # 能力 → {类型,...}
    for c in cases:
        cap_type[c["能力"]].add(parse_type(c))

    combo_missing = []  # 记录缺口 (能力, 类型, 理由)
    for cap in sorted(cap_type):
        covered = cap_type[cap]
        missing = [t for t in TYPES if t not in covered]
        exempt = CAP_TYPE_NOT_APPLICABLE.get(cap, [])
        real_missing = [t for t in missing if t not in exempt]
        if real_missing:
            combo_missing.append((cap, real_missing))
            print(f"  ✗ [{cap}] 缺口类型: {', '.join(real_missing)}（已覆盖: {', '.join(covered)}）")
        else:
            print(f"  ✓ [{cap}] 全类型覆盖: {', '.join(covered)}")
    if combo_missing:
        print(f"\n  ⚠️ 有 {len(combo_missing)} 个能力未覆盖全部类型，需补充用例")
    else:
        print("\n  ✓ 所有能力均覆盖全部 5 种类型")

    print("\n" + "=" * 50)
    print("自检完成")


if __name__ == "__main__":
    main()
