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
NOT_APPLICABLE = {
    "危险拦截": "只读查询，无写操作",
    "内容安全": "查询报表，不涉违规内容",
    "敏感数据保护": "查询经营数据，不涉个人敏感信息",
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

    print("\n" + "=" * 50)
    print("自检完成")


if __name__ == "__main__":
    main()
