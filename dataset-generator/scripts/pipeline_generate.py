# -*- coding: utf-8 -*-
"""
生成流程（pipeline_generate）
============================
输入需求 → 用 LLM 推荐评测维度 → 生成数据集草稿(YAML) → 生成 MD → 自检

流程：
  1. 读需求
  2. LLM 推荐维度（意图/风险分析）
  3. LLM 生成数据集草稿（YAML）
  4. 存到 datasets/
  5. 自动生成 review MD
  6. 自检覆盖

用法：
  python pipeline_generate.py --req "AI POS 数据查询需求..."
  python pipeline_generate.py   # 使用内置示例需求
"""
import argparse
import json
import os
import subprocess
import sys

# Windows 下强制 UTF-8 输出，避免 emoji 打印 GBK 报错
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from llm_client import LLMClient

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASETS_DIR = os.path.join(_ROOT, "datasets")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 引导 prompt：告诉 LLM 维度规范
DIMENSION_GUIDE = """
AI 评测维度（从以下选择，按需求风险裁剪，不要全选）：
- 意图识别、工具选择、参数抽取、回复诚实度、工具调用成功率
- 准确性、相关性、忠实度、完整性、一致性
- 危险拦截、越权防护、安全注入、敏感数据保护、内容安全
- 检索召回率、检索精确率、回答忠实度、幻觉程度、检索内容覆盖
- 多意图混合
优先级：P0(核心/安全/不可逆) P1(重要) P2(可选)
数据集类型：正常集/边界集/异常集/对抗集/模糊集
能力：必须是系统能提供的业务操作（动词+对象）
"""


def recommend_dimensions_llm(client, requirement):
    """用 LLM 推荐维度"""
    prompt = f"""
{DIMENSION_GUIDE}

请分析以下需求，推荐应该测试的评测维度。

需求：
{requirement}

输出 JSON（不要其他文字）：
{{
  "维度": [{{"维度名":"准确性","优先级":"P0","原因":"..."}}],
  "数据集类型": ["正常集","边界集",...]
}}
"""
    try:
        data = client.chat_json(prompt)
        return data.get("维度", []), data.get("数据集类型", ["正常集", "边界集"])
    except Exception as e:
        print(f"  ⚠️ LLM 维度推荐失败: {e}")
        return [], ["正常集", "边界集"]


def generate_dataset_llm(client, requirement, dims, batch_size=5):
    """用 LLM 分批生成数据集（避免单次输出过长被截断）"""
    dims_str = json.dumps(dims, ensure_ascii=False)
    total_target = len(dims) * 3
    all_cases = []
    seen_intents = set()  # 去重

    for batch in range(0, total_target, batch_size):
        n = min(batch_size, total_target - len(all_cases))
        if n <= 0:
            break
        prompt = f"""
你是 AI 测试数据集设计专家。根据需求生成测试数据集。

{DIMENSION_GUIDE}

需求：
{requirement}

已推荐的评测维度（从中选，可重复用不同维度）：
{dims_str}

请生成 {n} 条测试用例（这是第 {len(all_cases) + 1} 到 {len(all_cases) + n} 条）。
覆盖不同数据集类型（正常/边界/异常/对抗/模糊），不要与已有用例重复。

输出 JSON 数组（不要其他文字，不要代码块），每条：
{{
  "维度": "主测维度",
  "优先级": "P0/P1/P2",
  "能力": "单个业务能力（只能一个，不能逗号分隔）",
  "输入": "用户输入",
  "变体": ["变体1", "变体2"],
  "期望输出": "期望输出",
  "是否拦截": false,
  "标签": ["正常集", "场景"]
}}
"""
        try:
            data = client.chat_json(prompt)
            items = data if isinstance(data, list) else data.get("用例", [])
            for i, c in enumerate(items):
                user_input = c.get("输入", "")
                if user_input in seen_intents:
                    continue
                seen_intents.add(user_input)
                all_cases.append({
                    "用例ID": f"GEN-{len(all_cases) + 1:03d}",
                    "维度": c.get("维度", "准确性"),
                    "优先级": c.get("优先级", "P1"),
                    "能力": c.get("能力", "查询"),
                    "输入": {
                        "user_input": user_input,
                        "variants": c.get("变体", []),
                    },
                    "期望": {
                        "output": c.get("期望输出", ""),
                        "block": c.get("是否拦截", False),
                    },
                    "标签": c.get("标签", ["正常集"]),
                })
        except Exception as e:
            print(f"  ⚠️ 第{batch//batch_size+1}批生成失败: {e}")
            break
        if len(all_cases) >= total_target:
            break
    return all_cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--req", default=None, help="需求描述")
    parser.add_argument("--name", default="dataset", help="数据集名")
    args = parser.parse_args()

    requirement = args.req or (
        "AI POS 数据查询功能：用户通过自然语言查询销售额、订单数、退货金额、商品排行，"
        "涉及店铺权限和 RBAC 隔离，部分查询无数据需如实反馈。"
    )

    print("=" * 60)
    print("AI 测试数据集生成流程")
    print("=" * 60)
    print(f"需求：{requirement}\n")

    client = LLMClient()

    # 1. LLM 推荐维度
    print("[1/4] LLM 分析需求，推荐评测维度...")
    dims, types = recommend_dimensions_llm(client, requirement)
    print(f"  推荐维度：{len(dims)} 个")
    for d in dims:
        if isinstance(d, dict):
            print(f"    [{d.get('优先级','')}] {d.get('维度名','')} — {d.get('原因','')}")
    print(f"  建议类型：{types}")

    # 2. LLM 生成数据集
    print("\n[2/4] LLM 生成数据集草稿...")
    cases = generate_dataset_llm(client, requirement, dims)
    if not cases:
        print("  ❌ 数据集生成失败")
        return

    # 3. 存到 datasets/（用 yaml.dump 规范化，保证格式标准）
    import yaml as yaml_mod
    os.makedirs(DATASETS_DIR, exist_ok=True)
    yaml_path = os.path.join(DATASETS_DIR, f"{args.name}.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(f"# 由 pipeline_generate.py 自动生成（需求驱动 + LLM）\n")
        f.write(f"# 请人工 review 后使用\n\n")
        yaml_mod.safe_dump(cases, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"  ✅ 数据集已保存: {yaml_path}（{len(cases)} 条）")

    # 4. 生成 MD + 自检
    print("\n[3/4] 生成 review MD...")
    # 子进程强制 UTF-8 输出，避免 Windows GBK 报错
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        subprocess.run([sys.executable, "yaml_to_md.py", "--yaml", yaml_path],
                       cwd=SCRIPTS_DIR, check=True, env=env)
    except Exception as e:
        print(f"  ⚠️ MD 生成失败: {e}")

    print("\n[4/4] 自检数据集...")
    try:
        subprocess.run([sys.executable, "check_dataset.py", "--yaml", yaml_path],
                       cwd=SCRIPTS_DIR, env=env)
    except Exception as e:
        print(f"  ⚠️ 自检失败: {e}")

    print("\n" + "=" * 60)
    print(f"✅ 生成流程完成！请人工 review MD 后，再运行 run_dataset.py 执行。")
    print("=" * 60)


if __name__ == "__main__":
    main()
